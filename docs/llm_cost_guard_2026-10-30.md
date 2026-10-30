# LLM 成本护栏（2026-10-30）

> 起因：项目一直"以为在用阿里云免费额度"，实际产生欠费。本文记录根因、护栏设计与配置方法。

## 1. 为什么"免费额度"会欠费

阿里云百炼的免费额度**不是用完即停**的。官方文档原话：

> 免费额度总量有限，旗舰模型单位消耗更高，很容易快速耗尽，强烈建议开启"免费额度用完即停"开关，
> 避免额度耗尽**自动转为按量计费产生意外账单**。

两类模型行为完全相反，这是认知落差的来源：

| 模型类型 | 额度耗尽后的行为 | 是否产生费用 |
|---|---|---|
| `qwen3.7-flash` 等 `FreeTierOnly` 模型 | 直接返回 403 `AllocationQuota.FreeTierOnly` | **不产生**（这就是"免费额度用完了"的体感来源） |
| `qwen3.8-max` / `qwen3.8-max-0902` 等旗舰 | **静默转按量计费**，请求照常成功 | **产生**，12 元/百万输入、36 元/百万输出 |

本项目实际配置是后者：

```
services/testgen/.env
LLM_MODEL=qwen3.8-max-0902
LLM_MODEL_CHAIN=qwen3.8-max-0902,qwen3.7-flash,...
```

## 2. 三个放大器

按影响从大到小：

1. **前端扫描后自动 describe**（`service/web/index.html`）——每次扫描仓库，对每个未缓存的函数/类
   自动发一次 LLM 请求。换仓库或改代码后缓存失效即重烧。**这是最隐蔽的一条：用户只是点了"扫描"。**
2. **AI 用例（browser-use）**——每一步都把完整 DOM 重新塞进输入，一条 20 步的用例轻松几十万
   input token。3 次真机验证约十几元量级。
3. **模型链首选付费档**——`.env` 里 `LLM_MODEL_CHAIN` 第一项就是 max，任何 LLM 调用天然走最贵的路。

## 3. 护栏设计（三闸 + 一个开关）

代码：`services/testgen/core/llm_budget.py`（sidecar）与
`apps/requirement_analysis/llm_budget.py`（Django），两侧同构（venv 隔离无法共享）。

全部 **fail-closed**：判定不确定时拒绝，不做"拿不准就放行"。

| # | 闸 | 行为 | 拒绝范围 |
|---|---|---|---|
| 1 | 模型门禁 | 配了 `LLM_MODEL_ALLOWLIST` 就只放行清单内模型；未配则按 `max/plus/pro/2.4t` 关键字拦付费档 | `scope=model`，跳过该模型继续试链上下一个 |
| 2 | 单次输入预算 | 估算输入 token 超 `LLM_CALL_MAX_INPUT_TOKENS` 直接拒绝（**不截断**——截断会悄悄改变语义） | `scope=global`，整链停止 |
| 3 | 日累计熔断 | 当日累计超 `LLM_DAILY_TOKEN_BUDGET` 后拒绝一切调用 | `scope=global`，整链停止 |
| + | 输出上限 | 护栏开启时把 `max_tokens` 压到 `LLM_CALL_MAX_OUTPUT_TOKENS`（输出单价是输入的 3 倍） | — |

`scope` 的意义：**换模型是否有用**。付费模型不可用换免费档有用，所以只跳过；预算到顶换模型
一样超，必须整链停——否则就是花钱买"一定会失败"的调用。

### 记账与持久化

- 优先取上游 `usage.prompt_tokens / completion_tokens`（真实计费口径）；端点不返回时用本地估算。
- 落 `data/llm_usage.json`，按日分桶、保留 14 天。
- **进程重启不清零**——否则重启即可绕过预算。
- 记账失败只告警，不阻塞 LLM 调用本身。

### 插桩位置（关键：只改一处出口）

| 侧 | 唯一出口 | 覆盖面 |
|---|---|---|
| sidecar | `engine/llm_fallback.chat_with_fallback` | describe / comparator / expert / dialogue / semantic_enrich 共 5 个调用方 |
| Django | `AIModelService.call_openai_compatible_api`（models.py） | 需求分析 writer/reviewer、性能测试 AI 分析、任务生成 |
| Django | `ai_base._assert_cost_allowed`（执行前） | AI 用例（browser-use 直连 langchain，只能在创建 agent 前拦） |

> ⚠️ `apps/requirement_analysis/ai_models.py` 是**废弃副本**（与 models.py 同名模型冲突，
> 导入即 `RuntimeError: Conflicting 'aimodelconfig' models`）。真正生效的是 `models.py`。

## 4. 前端：自动 describe 改为手动

`service/web/index.html`：

- 扫描完成后**不再自动**调 `/describe`，改为提示"中文描述需手动生成"；
- 新增「✨ 生成中文描述」按钮 + 「扫描后自动生成（消耗模型额度）」复选框，状态存
  `localStorage.tg_auto_describe`，**默认关闭**；
- 清空缓存后也不自动重跑（那等于立刻把整批文件重烧一遍）。

## 5. 模型顺序调用（等用户给清单后填这里）

`model_name` 支持逗号分隔，**按书写顺序**降级；前一个额度耗尽（403/429/`quota` 关键字）
自动切下一个。这是既有能力的延用，不是新造的降级机制。

```
# services/testgen/.env
LLM_MODEL_CHAIN=模型1,模型2,模型3

# Django：AIModelConfig.model_name 填同样的逗号列表
```

判定"该切下一个"的条件：`403 / 404 / 429` 或文本含 `quota / FreeTierOnly / rate limit`。
**401 不切换**——同一 key 对所有模型都会失败，换模型没有意义。

## 6. 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `LLM_BUDGET_ENFORCE` | `on` | 总开关。`off` 只告警不拦（仅排障用） |
| `LLM_DAILY_TOKEN_BUDGET` | `300000` | 当日累计上限，次日自动归零 |
| `LLM_CALL_MAX_INPUT_TOKENS` | `24000` | 单次输入上限，超则整链拒绝 |
| `LLM_CALL_MAX_OUTPUT_TOKENS` | `4096` | 单次输出上限，透传为 `max_tokens` |
| `LLM_MODEL_ALLOWLIST` | 空 | 白名单模式：填「已确认开了用完即停」的模型名 |
| `LLM_ALLOW_PAID_MODEL` | `off` | 显式放行付费档（仅确实要花这笔钱时临时开） |
| `LLM_USAGE_PATH` | `data/llm_usage.json` | 用量文件位置（测试会指向临时目录） |

**白名单与关键字的关系**：填了白名单就**只认白名单**（`allowlist` 优先，付费关键字失效）；
留空才走关键字拦截。所以用户确认开了"用完即停"的模型，填进白名单即可正常使用。

## 7. 验证

| 项 | 结果 |
|---|---|
| sidecar 护栏单测 | `tests/test_llm_budget.py` **19 项**（含 3 条接线断言：跳过付费模型、预算到顶整链停、关闭护栏行为不变） |
| sidecar 降级链回归 | `tests/test_llm_fallback.py` 8 项全绿（原有用例在 conftest 里默认关闭护栏，避免误拦 `qwen-vl-max`） |
| Django 护栏单测 | `tests/test_llm_budget_cost_guard.py` **12 项**（含顺序降级、付费模型零请求、`max_tokens` 压缩、记账落盘） |
| AI 用例护栏 | `apps/ui_automation/tests/test_cost_guard.py` **6 项** |
| 主仓回归 | **300 passed / 10 skipped** |
| sidecar 全量 | 全绿（EXIT=0） |
| lint | 新增文件 0 问题；`llm_fallback.py` 4 条、`ai_base.py` 73 条均为**既存**，本次零新增 |

## 8. 仍需人工确认的事

1. **阿里云控制台开"免费额度用完即停"**（百炼 → 费用与成本 → 免费额度）。护栏是代码层第二道防线，
   平台开关是第一道，两者都要有。
2. 给出「已确认开了用完即停」的模型清单（含快照日期）→ 填 `LLM_MODEL_ALLOWLIST` 与 `LLM_MODEL_CHAIN`。
3. 建议同时设**账单告警**（如单日 > 5 元短信通知）——护栏只能限制本项目，管不了控制台上其他产品。
