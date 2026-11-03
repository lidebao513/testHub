# #178 模型分级路由 + 成本落库

> 落地日期：2026-11-03 ｜ 范围：`apps/ui_automation`（不涉及 testgen sidecar）

## 一、为什么做这件事

AI 模式（browser-use）的单价结构和别的 LLM 用法完全不同：**每一步都把完整 DOM 重发一遍**。
本次实测一条「打开登录页、确认有输入框和按钮」的极简任务，跑 5 步就消耗 **46,317 token**。
如果每条用例都用顶配模型，成本既高又不可预期，而且此前**完全无法归因**：
`AIExecutionRecord` 只有 `status`，没有任何 token/cost/模型字段，成本只存在于日志里。

一句话：**不落库，就没法证明"分级路由省了钱"，也没法给业务方看账。**

## 二、模型分级路由

### 2.1 判定口径（复用既有打分，不另造标准）

| 项 | 取值 |
|---|---|
| 复杂度打分 | 复用 `_step_complexity_score`（已提到模块级 `step_complexity_score`，方法保留为薄封装） |
| 有规划步骤 | 按步骤判：`max(step_score) >= 2` / `步骤数 >= 6` / `总分 >= 4` |
| 无规划步骤 | 按描述判：`总分 >= 4` / `字数 >= 160` / **动作词 >= 4** |
| 长度分 | **剔除 URL 后再算**（见 2.3） |
| 结论 | 任一信号命中 → `complex`；否则 `simple` |

### 2.2 路由动作

- `simple` → 换到轻档模型；`complex` → 保持主模型（默认）。
- **只换模型名，不换 key/base_url**：跨厂降级会直接撞计费与鉴权，不在本期范围。
- 轻档模型解析顺序：`UI_MODEL_LIGHT` 显式指定 → 同厂降级映射 → 无（留在主模型）。
  内置映射：`deepseek-v4-pro→deepseek-v4.1-flash`、`deepseek-reasoner/v3→deepseek-chat`、
  `qwen-max/plus→qwen-turbo`、`gpt-4o→gpt-4o-mini`、`glm-4-plus/4→glm-4-flash`。
- 路由决策写进执行日志：`[模型路由] simple 任务 → deepseek-v4.1-flash（复杂度分=0，主模型=…）`

### 2.3 fail-safe（宁可多花钱，不可跑歪）

任何不确定都留在主模型，**绝不因为"想省钱"把复杂用例降级**：

| 情况 | 行为 |
|---|---|
| `UI_MODEL_ROUTING=0` | 不路由 |
| 解析不到轻档模型 / 轻档 == 主模型 | 不路由 |
| 轻档模型被成本护栏拒绝 | **回退主模型**并告警（不会因为轻档不在白名单就去跑贵的） |
| 同一 agent 重复执行 | 每次先复位到 `_primary_llm` |

### 2.4 踩到的坑：URL 长度虚增复杂度

首版上线前实测发现，「打开 `http://127.0.0.1:3900/login`，确认有输入框和按钮，然后结束」这条
**极简任务被判成 complex**——仅 URL 就顶满长度分（≥48 字 → 2 分）+ 连接词 1 分 + 双动作 1 分 = 4 分。
后果是分级路由**永远不会生效**。修法：算长度分前把 URL 压成 `<url>`。
（`_should_redecompose_explicit_steps` 仍走原口径，不动既有二次拆解行为。）

## 三、成本落库

### 3.1 四个字段（一次迁移）

`apps/ui_automation/migrations/0005_aiexecutionrecord_cost_...py`

| 字段 | 类型 | 语义 |
|---|---|---|
| `total_tokens` | IntegerField(null) | 本次执行的 token 总量 |
| `cost` | FloatField(null) | 成本（USD）。**未知记 NULL，绝不写 0** |
| `routed_model` | CharField(100) | 实际使用的模型（不是配置里的主模型） |
| `pre_explore` | JSONField | `{url, title, count, elements}`；空表示未启用/未抓到 |

同时暴露到 `AIExecutionRecordSerializer`（只读）。

### 3.2 成本口径：为什么默认是 NULL

- **provider 实报优先**：`history.usage.total_cost > 0` 直接用。
- 其次 `UI_MODEL_PRICE_JSON` 价目表估算（USD / 1M token）。
- 都没有 → `None` + 日志 `cost_source=unavailable`。

**不内置任何价格**：编造单价会让「成本下降」这个结论失去意义。未知必须能被看出来，
所以不用 0 冒充免费。填入价目表后即可得到确定成本：

```bash
UI_MODEL_PRICE_JSON={"deepseek-v4.1-flash":{"in":0.07,"out":0.28},"deepseek-v4-pro-0813":{"in":0.28,"out":1.10}}
```

### 3.3 踩到的坑：browser-use 的 token 记账对我们记 0

`TokenCost.register_llm` 只在 `result.usage` 有值时记账，而 langchain 的 `ChatOpenAI` 把用量放在
`usage_metadata` 上——实测整条用例跑完 `history.usage.total_tokens` 仍是 **0**，成本字段会永远为空。

修法：加 `TokenUsageCounter`（langchain 回调）自己累计 `on_llm_end` 的
`usage_metadata.input_tokens/output_tokens`，两个来源**取较大者**。回调口径不依赖上层实现。

顺带修了 `steps` 恒为 0 的老问题：`AgentHistoryList` 把步骤放在 `history` 字段里（**不是** `steps`），
原代码 `hasattr(history,'steps')` 永远为假。

## 四、归因写回链路

```
页面点「开始执行」
  → views.run_task 线程
    → run_full_process_sync(..., agent_holder=holder)   # 用 holder 交回 agent，不改返回值
      → analyze_task（主模型）→ run_task
        → _apply_model_routing（决定模型）
        → agent.run()
        → finally: _capture_usage(agent)                # 成功/失败/停止都抓
    → apply_usage_snapshot(record, holder['agent'])      # 写 4 个字段
    → safe_save(record)
```

### 4.1 踩到的坑：`refresh_from_db()` 把归因冲掉

第一版把 `apply_usage_snapshot` 放在 `run_full_process_sync` 之后，结果 DB 里四个字段仍是空——
而日志明明打印了 `pre_explore=7项`。根因：紧接着的
`if should_stop():` → `should_stop_sync()` → **`execution_record.refresh_from_db()`**，
把刚赋但未保存的字段从库里重新加载回来，覆盖成空。

修法：归因调用必须放在 `should_stop()` **之后**、最终 `safe_save()` **之前**。
并在代码里写了注释，避免后续维护者再往前挪。

### 4.2 失败路径也记

例外分支同样调用 `apply_usage_snapshot`：只统计成功用例会**低估成本**
（失败用例往往已经烧掉大半 token）。

## 五、端到端验证（真实浏览器 + 真实模型）

`tests/verify_routing_e2e.py` —— 从页面登录、进 AI 智能模式页、填任务、点执行，再回查数据库：

```
id              : 32
status          : passed
routed_model    : 'deepseek-v4.1-flash'
total_tokens    : 46317
cost            : None                # 未配价目表 → 未知（预期）
pre_explore     : count=7 url=http://127.0.0.1:3900/login
duration        : 54.82s

log: [模型路由] simple 任务 → deepseek-v4.1-flash（复杂度分=0，主模型=deepseek-v4-pro-0813）
log: 预探索元素清单 7 项（TestHub - AI一体化测试平台 | http://127.0.0.1:3900/login）
✅ 四项断言全部通过
```

| 验证层 | 结果 |
|---|---|
| 单测 | `apps/ui_automation/tests/` **123 passed**（新增 48 例） |
| 全量回归 | `tests + ui_automation + llm_judge` **340 passed**（292 基线，零破坏） |
| 页面级冒烟 | `tests/e2e_ui_smoke.py` 19/19 |
| 真实 AI 执行 | 上述四项断言通过 |

## 六、模型配置（本次写入 DB，不入库）

| 项 | 值 |
|---|---|
| role | `browser_use_text` |
| name | Browser Use 文本模式（DashScope） |
| model_type | `qwen`（DashScope 兼容模式） |
| base_url | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| model_name | `deepseek-v4-pro-0813`（主/重档）；轻档自动映射到 `deepseek-v4.1-flash` |
| api_key | 取自 `services/testgen/.env` 的 `LLM_API_KEY`（**未写入任何代码/文档**） |

成本护栏对两个模型均放行（登记册在册、未过期、在免费额度内）。

## 七、已知边界

1. **`cost` 需要人工填价目表**：不填恒为 NULL。这是刻意选择，不是缺陷。
2. **复杂度判定是启发式**：中文动作词/连接词计数，不做语义理解。判错方向是
   「简单任务被当成复杂」——多花钱但不会跑歪，属可接受偏差。
3. **只做同厂降级**：跨 provider 路由需要独立的 key/base_url 解析，未在范围内。
4. ~~**前端未展示新字段**：接口已返回，报表页展示是后续项。~~ **已补齐**，见第八节。
5. **`AIExecutionRecord.steps_completed`** 的 steps 修正属顺带修复，历史记录不会回填。

## 八、展示层补齐与 migrations 入库（同日追加）

### 8.1 报告接口

`AIExecutionReportGenerator._generate_ai_usage()` 新增 `ai_usage` 段，
`generate_summary_report` / `generate_detailed_report` / `generate_performance_report`
三类报告都带：

```json
{
  "routed_model": "deepseek-v4.1-flash",
  "total_tokens": 46317,
  "cost": null,
  "cost_known": false,
  "pre_explore": {
    "captured": true, "count": 7,
    "url": "...", "title": "...",
    "elements": [{"index": 4, "tag": "input", "label": "input[text] \"请输入用户名\""}]
  }
}
```

`cost_known` 是显式布尔值：前端不得用 `cost === 0` 判断未知——provider 真报 0
（例如命中缓存）与「我们不知道」是两件事。`pre_explore.elements` 裁到 20 项。

### 8.2 页面

| 位置 | 展示内容 |
|---|---|
| 列表页 | 新增「模型」「Token 数」两列（Token 千分位） |
| 详情弹窗 | 四个归因卡（模型 / Token 数 / 成本(USD) / 预探索）+ 元素清单（最多 20 项） |
| 报告 · 执行摘要 | 新增「AI 用量归因」区块：四卡 + 「动手前页面元素清单」表格 |
| 报告 · 性能分析 | 指标区补 Token / 成本两卡（慢和贵常出自同一处） |

缺数据统一显示 `-` 或「未知」，绝不显示 `0`。

### 8.3 migrations 入库决策

原 `.gitignore` 只有 `apps/*/migrations/*` + `!apps/*/migrations/__init__.py`，
等于只跟踪包占位文件，迁移实体从未进版本库——各环境靠 `makemigrations` 各生成一份，
schema 漂移无法发现。已改为**全量入库**（68 个文件），仅排除 `__pycache__/`；
`manage.py makemigrations --check --dry-run` 输出 `No changes detected`。

### 8.4 页面级验证

`tests/verify_report_usage_ui.py`（真实浏览器从登录页进入）：

```
✅ 列表页表头含「模型」「Token 数」      首行: deepseek-v4.1-flash | 46,317
✅ 详情弹窗含四项归因标签
✅ 预探索元素清单已渲染（7 项）
✅ 报告摘要页含「AI 用量归因」四项
✅ 成本未知显示为「未知」（未被渲染成 0）
✅ 性能分析页含 Token / 成本卡
```
