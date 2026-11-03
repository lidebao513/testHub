# 代码 Review：页面预探索（#177）

> 变更范围：`apps/ui_automation/ai_base.py`（**+242 行，0 删除**）+ 新增单测 + 2 个验证脚本
> 日期：2026-11-03　结论：**通过，纯增量、可回滚；4 处缺陷已在 review 中修复**

---

## 一、变更清单

| 文件 | 变更 | 说明 |
|---|---|---|
| `apps/ui_automation/ai_base.py` | +242 / -0 | 纯增量：4 个纯函数 + 1 个方法 + 开关变量 + prompt 注入 + step 钩子 |
| `apps/ui_automation/tests/test_pre_explore.py` | 新增 29 例 | 开关 / prompt / 格式化 / 渲染 / 非阻断语义 |
| `tests/verify_pre_explore_live.py` | 新增 | 活体验证：真实浏览器会话跑同一 DOM 管线 |
| `tests/verify_ai_page_ui.py` | 新增 | 页面级验证：登录→AI 页→填任务→运行 |

**关键指标：`git diff --stat` 显示 0 行删除** —— 所有改动是新增函数与新增调用点，不改动既有逻辑分支，回退只需删掉注入点，风险最低。

---

## 二、实现设计（及为什么不按"另起浏览器预扫描"做）

文档原设计是：Agent 启动前先抓页面快照 → 把元素清单作为上下文前缀注入 task。

**核验代码后发现该设想的性价比有问题**：browser-use 每一步都会重算完整 DOM 并注入模型（这正是单条用例几十万 input token 的来源）。也就是说模型**从来不缺页面信息**，缺的是"先结构化看一遍"的动作。

因此实现改为两段：

| 段 | 做法 | 落点 |
|---|---|---|
| ① prompt 级预探索 | 在 task 文本里要求：动手前先列出可交互元素（index + 类型 + 可见文本），且**只能用当前页真实存在的 index** | `final_task += build_pre_explore_prompt()`（规则区之前） |
| ② 首步快照审计 | 第 1 步结束后抓一次 `get_browser_state_summary()`，转结构化清单写日志 + 回传前端日志面板 | `on_step_end` → `_run_pre_explore()` |

这样：**零额外浏览器、零额外 LLM 调用**（`include_screenshot=False`），只多一次 DOM 序列化。

### 新增函数

| 函数 | 职责 | 可测性 |
|---|---|---|
| `_ui_pre_explore_enabled()` | 读 `UI_PRE_EXPLORE`，只有 `0/false/off/no` 才关 | 纯函数 |
| `build_pre_explore_prompt()` | 生成指令块（含"NEVER invent an index"硬约束） | 纯函数 |
| `_pre_explore_text_of(node)` | 优先 `get_meaningful_text_for_llm()`，回退 node_value / aria-label 等 | 纯函数 |
| `_pre_explore_is_noise(node, text)` | 过滤无 role、无文本的布局容器 | 纯函数 |
| `_pre_explore_element_label(node, text=None)` | 生成 `tag[role] "文本"` | 纯函数 |
| `format_pre_explore_elements(selector_map, limit)` | selector_map → 结构化清单 | 纯函数 |
| `render_pre_explore_block(...)` | 清单 → 可读文本 | 纯函数 |
| `BaseBrowserAgent._run_pre_explore()` | 抓快照 + 聚合 + 日志 + 回调，异常不外抛 | 可 stub 测试 |

---

## 三、Review 发现的 4 处缺陷（已修）

### 3.1 首步未导航 → 抓到 about:blank 后永久放弃（**功能级缺陷**）

- **问题**：原实现"第一次 `on_step_end` 就标记 `captured=True`"。若首步只做思考/未导航，页面仍是 `about:blank`，清单 0 项，**之后再也不会抓**。
- **修复**：改为"占位页不算成功"，最多重试 3 次；会话不支持快照时立刻停止重试（不刷日志）。
- **代码**：`on_step_end` 中 `pre_explore_attempts < 3` + `is_placeholder_url()` 判定（复用 run_task 内既有函数）。

### 3.2 元素文本含换行 → 撑破"一行一元素"格式（**可读性缺陷，实测发现**）

- **证据**：活体跑真实登录页时输出 `[250] span[button] "🇨🇳\n简体中文"`。
- **修复**：`' '.join(text.split())` 折叠所有空白。
- **测试**：`test_label_collapses_newlines`。

### 3.3 清单被布局容器稀释（**收益缺陷，实测发现**）

- **证据**：真实登录页 `selector_map` 23 项，其中 8 项是无 role、无文本的 `div/span/i/svg`，对 Agent 无信息量；而 prompt 又要求模型"最多列 15 行"，噪音会挤掉真控件。
- **修复**：新增 `_PRE_EXPLORE_NOISE_TAGS` 与 `_pre_explore_is_noise()`：标签在噪音集合内、且无 `role`、无可读属性、无文本才过滤。
- **效果**：**23 项 → 7 项**，7 项全是真控件（用户名/密码/登录/语言切换/密码登录/短信登录/立即注册）。

### 3.4 同一节点重复遍历子树（**性能缺陷**）

- **问题**：噪音判断与标签生成各调一次 `_pre_explore_text_of()`，而该方法可能遍历子树（`get_meaningful_text_for_llm`）。60 个节点 = 120 次遍历。
- **修复**：文本在循环内只提取一次，作为参数传给两个函数。

---

## 四、残余风险与未解决项（需后续处理）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | **清单未落库**，只进日志与前端日志面板 | 事后审计要翻日志，无法按执行记录查询 | 给 `AIExecutionRecord` 加 `pre_explore` JSONField（需迁移），与 #178 的成本字段一并做 |
| 2 | **无 run_task 级集成测试** | 无法在单测层证明"prompt 真的拼进 task" | 可用 monkeypatch 替换 `Agent`/`Controller` 断言 `task` 含关键串；或靠真实 AI 任务兜底 |
| 3 | **收益未量化** | 「减少步数/token」是推断，没有实测对照 | 需 1 个可用的 `browser_use_text` 模型配置，跑同任务开/关对照 |
| 4 | **抓取时机是 step_end（动作后）** | 严格意义上不是"动手前的状态"，第 1 步的动作已经生效 | 当前对审计足够；若要严格前置，需用 `initial_actions` + `on_step_start`（改动更大） |
| 5 | **页面文本进日志** | 元素可见文本（可能含用户名等）会写进 `AIExecutionRecord.logs` | 单条已截断 40 字、最多 15 条；如涉敏可在日志层做脱敏 |
| 6 | prompt 要求"先列元素再操作"会多一次思考 | 可能增加 1 步 | 与"减少走偏"对冲，需实测确认净收益 |

---

## 五、验证证据

| 层级 | 手段 | 结果 |
|---|---|---|
| 单元测试 | `pytest apps/ui_automation/tests/` | **75 passed**（新增 29 例） |
| 全量回归 | `pytest tests apps/ui_automation/tests apps/llm_judge/tests` | **292 passed**（263 基线 + 29 新增，零破坏） |
| 活体 DOM 管线 | `tests/verify_pre_explore_live.py`（真实浏览器会话，不走 LLM） | 原始 23 项 → **清单 7 项**，用户名/密码/登录按钮索引正确 |
| 页面级 UI | `tests/e2e_ui_smoke.py`（真实登录 + 19 页遍历） | **19/19 通过** |
| 入口页针对性 | `tests/verify_ai_page_ui.py`（登录→AI 页→填任务→点运行） | 页面正常渲染、未白屏；后端返回 `No API Key found for mode: text`（**环境缺模型配置，非本次改动**） |
| 编译 | `py_compile ai_base.py` | OK |

### 未完成的验证（受环境阻塞）

**真实 AI 任务端到端**：DB 中 `AIModelConfig` 计数为 **0**，无 active 的 `browser_use_text` 配置，`_verify_execution_llm()` 直接 fail-closed。补齐配置后即可跑：

```bash
# 配一条 active 的 browser_use_text 后再执行
venv/Scripts/python.exe tests/verify_ai_page_ui.py
# 预期：页面日志面板出现「[预探索] 预探索元素清单 N 项」
```

---

## 六、结论

- **代码质量**：纯增量 242 行、0 删除；所有新逻辑都有异常兜底，预探索任何失败都不阻断主链路。
- **实测驱动的改进**：3.2 / 3.3 两条缺陷是活体验证后才暴露的，说明"只跑单测"不够——这也是本次坚持跑真实浏览器会话的价值。
- **可以合入**：功能可用，风险受控。建议与 #178（模型分级 + 成本落库）同一分支推进，把"清单落库"（残余风险 1）一并解决。
