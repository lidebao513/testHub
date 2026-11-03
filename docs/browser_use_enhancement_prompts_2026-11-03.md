# TestHub × browser-use 增强功能实施提示词集

> **用途**：本文档把《30 分钟搭一个能点浏览器的测试 Agent：browser-use CLI 保姆级上手》（霍格沃兹软件测试开发，2026-11-03）里可落地的能力，拆解成 9 个可独立实施的功能项。每个功能项含三部分：
> 1. **设计（Design）**——改什么、加什么、落哪个文件；
> 2. **执行提示词（Execution Prompt）**——复制粘贴到 WorkBuddy 即可落地；
> 3. **验证（Verification）**——怎么确认做对了。
>
> **用法**：后续执行时，进入 Agent 模式，把对应功能的「执行提示词」整段复制进去即可。每条提示词都已 self-contained（含路径、约束、设计要点、验收标准）。

---

## 0. 背景与项目现状

### 0.1 文章核心结论
- 工具：**browser-use CLI**——让 AI Agent 直接操作浏览器"完成任务"，而不是"写测试代码"。
- 方向：从「AI 写脚本」转向「**AI 直接操作浏览器执行测试**」（Browser Agent / Computer Use）。
- 关键做法：先 `open/state` 理解页面 → `run "自然语言任务"` 让 Agent 执行 → 会话隔离 `-s` → 域名白名单 → 三层断言 → 模型选型控成本。
- 推荐模型：浏览器任务用 **DeepSeek V4.1 Flash**（性价比最高，文章基准得分 75.0 / 22 美分每任务）。

### 0.2 项目已具备的能力（已核对代码，非猜测）
`apps/ui_automation/` 已深度集成 browser-use，**主干能力已有，本次是补缺与场景化**：

| 已有机制 | 位置 | 说明 |
|---------|------|------|
| `ActionGuard` 域名白名单 | `apps/ui_automation/ai_base.py` | `allowed_domains` / `UI_ALLOWED_DOMAINS` / `blocked_actions` / `domain_mode='warn'` / `semantic_guard` |
| 成本校验 | `apps/ui_automation/ai_base.py` | `TokenCost` + `_assert_cost_allowed(model_name)` |
| 快照/截图留证 | `apps/ui_automation/ai_base.py` | CDP session + `ScreenshotWatchdog` + `DOMWatchdog` |
| Windows CDP 适配 | `apps/ui_automation/ai_base.py` | `_patched_connect` 修复本机 `BrowserSession.connect` |
| AI 角色对齐 | `apps/ui_automation/ai_base.py` | `role_name='browser_use_text'`（与 `requirement_analysis` 角色体系一致） |
| 断言框架 | `apps/ui_automation/models.py` | `ASSERT_TYPE_CHOICES` / `assert_type` / `assert_value` |
| 多模型 Provider | `apps/requirement_analysis` | DeepSeek / Qwen / SiliconFlow / Zhipu / 小米 / OpenAI-compatible |

### 0.3 通用执行约束（每条提示词都适用）
1. 工作区：`C:\Users\EDY\WorkBuddy\testhub_platform`。
2. **先读相关文件再动手**，理解现状，不要凭空写；同文件多次 Edit 须分开执行并读回验证。
3. 遵循 `CODE_STANDARDS.md` / `AGENTS.md`：提交前跑 lint + 测试；默认不自动提交，多修改合并一个 commit，`<type>: <简短描述>`。
4. **红线（不可违反）**：
   - 绝不升级根 venv 的 `starlette==0.50.0`（channels/Daphne WebSocket 依赖）。
   - `services/testgen` 用独立 venv（`services/testgen/.venv`），不要动它、不要无锁 `pip install openai`。
   - 不触碰 `.env*` / `*.bak` / `data/*.db` / `*.sqlite*` 等敏感文件；不入库清单见 `.gitignore`。
5. 改动集中、单项功能、可回滚；不擅自扩大改动范围。
6. 完成后给出自测结果与下一步建议。

---

## 1. P0-1 页面预探索（先看页面再动手）

**一句话**：AI 正式点页面前，先把页面上的按钮/输入框/链接列成清单注入上下文，让 Agent 带着"地图"干活，少走偏。

### 设计（Design）
- 在 `apps/ui_automation/ai_base.py` 的 AI 模式入口（Agent 编排处）新增「页面快照预探索」步骤。
- 复用已有 `DOMWatchdog` / DOM 快照能力，抽取**可交互元素清单**（索引 + 类型 + 可见文本，类似文章 `state` 输出）。
- 将该清单作为前缀注入 Agent 的 task 上下文；新增开关 `UI_PRE_EXPLORE`（默认 true）。
- 不改变现有 `ActionGuard`、成本、截图逻辑。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区实施「AI 模式页面预探索」功能。

【前置约束】见文档 0.3 通用执行约束。先 Read 以下文件再动手：
- apps/ui_automation/ai_base.py（重点看 DOMWatchdog、Agent 编排、BrowserSession 使用）
- apps/ui_automation/ai_agent.py（AI 模式入口）

【任务】
1. 新增页面预探索步骤：在 Agent 启动任务前，调用 browser-use 的 DOM 快照，抽取当前页面可交互元素（索引 + 类型 + 可见文本），生成结构化清单。
2. 将该清单作为上下文前缀注入 Agent 的 task（例如："{元素清单}\n\n任务：<原任务>"）。
3. 新增配置项 UI_PRE_EXPLORE（bool，默认 true），关闭时不预探索。
4. 在日志中打印预探索元素数量，便于排查。

【验收标准】
- 不破坏现有 ActionGuard / 成本 / 截图逻辑。
- 单元测试覆盖 pre_explore 函数（输入 HTML/快照，输出元素清单）。
- 改动后跑通 lint + 现有 ui_automation 测试。
```

### 验证（Verification）
- 跑一个 todomvc 任务，确认日志出现「预探索元素清单 N 项」，且 Agent 点击使用的是清单里的真实索引而非猜测。
- 对照实验：同一任务分别开启/关闭 `UI_PRE_EXPLORE`，统计走偏次数，确认开启后下降。
- 单测：`pytest apps/ui_automation/tests/ -k pre_explore`。

---

## 2. P0-2 三层断言固化

**一句话**：AI 跑完不算完，自动验三件事——有没有点错地方、业务结果对不对、多个任务有没有串台；结果可信，人不用再点一遍复核。

### 设计（Design）
- 在 `apps/ui_automation/models.py` 的 `ASSERT_TYPE_CHOICES` 基础上，新增「AI 模式自动断言生成器」。
- 三层断言：
  1. **走偏检测**：对比任务期望动作序列（从 task 文本解析）与 Agent 实际执行轨迹（已有 action history），标记偏差。
  2. **业务状态校验**：基于任务终态读取关键字段（复用 `browser-use get` / 页面状态），与期望业务结果比对。
  3. **会话隔离校验**：多任务并发时确认各自状态无交叉（配合功能 5）。
- 断言结果写入 `ExecutionResult` + 自动附截图证据（已有 ScreenshotWatchdog）。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区实施「AI 模式三层断言」功能。

【前置约束】见文档 0.3。先 Read：
- apps/ui_automation/models.py（ASSERT_TYPE_CHOICES / assert_type / assert_value）
- apps/ui_automation/ai_base.py（Agent 执行轨迹、ScreenshotWatchdog、ExecutionResult 写入处）

【任务】
1. 新增自动断言生成器：Agent 跑完任务后，基于 task 文本 + 页面终态 + 执行轨迹，自动生成三层断言（走偏检测 / 业务状态校验 / 会话隔离校验）。
2. 断言结果落到 ExecutionResult，失败项标红；自动关联截图证据。
3. 提供配置 UI_AUTO_ASSERT（默认 true）。
4. 对"任务成功但结果错误"的用例，断言必须能识别为失败，而非显示通过。

【验收标准】
- 构造一个"会走偏或结果错"的用例，确认系统标红断言失败。
- 不动现有人工断言框架，仅做增强。
- 跑通 lint + 测试。
```

### 验证（Verification）
- 构造反例：给任务"添加 3 条后剩 2 条"，但让 Agent 实际只加 1 条——确认业务状态校验标红。
- 构造走偏例：任务要求点"详情"但 Agent 点了"编辑"——确认走偏检测标红。
- 截图证据随报告可查。

---

## 3. P0-3 模型分级策略

**一句话**：简单任务用便宜快的模型，复杂流程才用贵但强的模型；省钱又稳，每次跑成本可预测。

### 设计（Design）
- 在 `apps/ui_automation/ai_base.py` 的 `_assert_cost_allowed(model_name)` 基础上扩展「分级路由」。
- 解析任务复杂度（步骤数 / 涉及页面数 / 是否含校验），简单 → DeepSeek Flash 级（`deepseek-chat` 或文章 DeepSeek V4.1 Flash），复杂 → 强模型。
- 复用 `requirement_analysis` 的 `AIModelConfig` 多 Provider 能力；新增 `UI_MODEL_ROUTING`（默认 true）+ 复杂度阈值配置。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区实施「UI AI 模式模型分级路由」功能。

【前置约束】见文档 0.3。先 Read：
- apps/ui_automation/ai_base.py（_assert_cost_allowed、模型选择处）
- apps/requirement_analysis/models.py（AIModelConfig 多 Provider）

【任务】
1. 新增复杂度估算：从 task 文本解析步骤数/页面数/是否含校验，输出 simple/complex。
2. 简单任务路由到 DeepSeek Flash 级模型，复杂任务路由到强模型（可配置）。
3. 新增 UI_MODEL_ROUTING 开关 + 阈值；路由决策写入日志与成本统计。
4. 不改动 requirement_analysis 的 Provider 配置，只做 UI 侧路由。

【验收标准】
- 构造 simple/complex 两个任务，确认分别路由到不同模型，TokenCost 统计正确。
- 不升级 starlette；不动 testgen venv。
- 跑通 lint + 测试。
```

### 验证（Verification）
- simple 任务日志显示 `routing=flash`；complex 显示 `routing=strong`。
- 成本统计表能区分两类任务花费。
- `pytest apps/ui_automation/tests/ -k model_routing`。

---

## 4. P1-1 每周冒烟巡检任务型

**一句话**：把文章里"周一让 Agent 登录→看列表→点详情→校验"做成平台定时任务，到点自动跑、出截图+结论，人只看一眼。

### 设计（Design）
- 在 `apps/core/management/commands/run_all_scheduled_tasks.py` 基础上，新增「AI 冒烟巡检」任务类型。
- 读取配置的巡检脚本（自然语言任务列表，存 `data/` 或配置表），逐个用 browser-use 跑。
- 每个巡检项产出执行记录 + 截图 + 结论，汇总进 `reports`。
- 复用功能 2 的三层断言做校验。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区实施「AI 冒烟巡检定时任务」功能。

【前置约束】见文档 0.3。先 Read：
- apps/core/management/commands/run_all_scheduled_tasks.py
- apps/ui_automation/ai_base.py（Agent 调用方式）
- apps/reports（报告产出）

【任务】
1. 在 run_all_scheduled_tasks 中新增 AI 冒烟巡检分支，读取配置的巡检任务列表（自然语言任务，可存 data/smoke_tasks.json）。
2. 逐个调用现有 browser-use Agent 执行，复用 UI_PRE_EXPLORE / 三层断言。
3. 汇总每个巡检项的执行记录 + 截图 + 通过/失败结论，生成一份巡检报告。
4. 支持手动触发（命令参数 --smoke）与定时触发。

【验收标准】
- 配置一个 demo 巡检（如 todomvc 添加+校验），手动触发能产出报告+截图。
- 不破坏现有 API/UI 定时任务逻辑。
- 跑通 lint + 测试。
```

### 验证（Verification）
- `python manage.py run_all_scheduled_tasks --smoke` 产出报告文件 + 截图。
- 故意让一个巡检项失败，确认报告标红且含截图证据。

---

## 5. P1-2 会话隔离强化

**一句话**：同时跑多个测试时，给每个任务独立的浏览器环境，互不干扰，结果干净可靠。

### 设计（Design）
- `ai_base.py` 已有 tab 级 `agent_focus_target_id`，扩展为 **session 级隔离**：每个并发任务用独立 `BrowserProfile` / 独立 `user-data-dir`（参考 Playwright 临时 profile 思路）。
- 新增 `UI_ISOLATE_SESSIONS`（默认 true）；并发任务调度时分配独立 profile 目录，结束后清理。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区实施「UI AI 模式会话隔离」功能。

【前置约束】见文档 0.3。先 Read：
- apps/ui_automation/ai_base.py（BrowserProfile、agent_focus_target_id、任务调度入口）

【任务】
1. 新增 session 级隔离：每个并发 UI AI 任务使用独立 BrowserProfile / user-data-dir（临时目录，任务结束清理）。
2. 新增 UI_ISOLATE_SESSIONS 开关（默认 true）。
3. 确保并发任务间 Cookie / 登录态 / 页面状态不交叉。

【验收标准】
- 并发跑两个不同账号登录的任务，确认各自状态独立、无串台。
- 任务结束后临时目录被清理。
- 跑通 lint + 测试。
```

### 验证（Verification）
- 并发执行"账号A登录页"与"账号B登录页"，确认各自看到的用户不同。
- 检查 `Temp/` 下 profile 目录在任务后消失。

---

## 6. P1-3 内网/IP 默认拦截

**一句话**：AI 默认只能访问白名单网址，内网地址、云后台地址一律拦死，防止 Agent 乱点把生产/内网搞出事故。

### 设计（Design）
- `ActionGuard` 的 `domain_mode` 默认从 `'warn'` 增强为：默认拦截内网段（`10.*` / `172.16-31.*` / `192.168.*` / `127.*` / `169.254.*` 云元数据）+ 显式白名单 `UI_ALLOWED_DOMAINS`，未匹配一律 block。
- 拦截动作记录日志（含目标 host），便于审计。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区实施「UI AI 模式内网默认拦截」功能。

【前置约束】见文档 0.3。先 Read：
- apps/ui_automation/ai_base.py（ActionGuard、_host_allowed、UI_ALLOWED_DOMAINS）

【任务】
1. 将 ActionGuard 默认策略改为：未匹配 UI_ALLOWED_DOMAINS 的请求，默认 block（原 warn 仅告警）。
2. 内置默认拦截内网段：10.*、172.16-31.*、192.168.*、127.*、169.254.*（云元数据）。
3. 拦截时记录日志（目标 host + 触发规则），不静默失败。
4. 保留 domain_mode 可配置（warn/block），默认 block。

【验收标准】
- 给 Agent 一个会跳内网的指令，确认被拦截且日志可追溯。
- 白名单内地址正常放行。
- 跑通 lint + 测试 + 新增单测（host 匹配逻辑）。
```

### 验证（Verification）
- 任务含"访问 http://169.254.169.254/"——确认被 block 并记录。
- 任务含白名单内地址——确认放行。
- `pytest apps/ui_automation/tests/ -k guard`。

---

## 7. P2-1 Playwright MCP 互补

**一句话**：除了让 AI 直接点，还能让 AI 帮你生成/维护传统 Playwright 脚本——临时验证用"直接点"快，长期回归用"脚本"稳，两种模式互补。

### 设计（Design）
- 引入 Playwright MCP server 作为「脚本生成」能力，与现有 browser-use 探索式执行互补。
- 在 `ui_automation` 新增"脚本生成模式"：调用 MCP 产出 Playwright 代码，入库到 `tests/` 或用例脚本表，供后续脚本化回归。
- 不替换 browser-use，二者并列。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区设计并实施「Playwright MCP 脚本生成」能力。

【前置约束】见文档 0.3。先 Read：
- apps/ui_automation（现有 Playwright 使用）
- 确认本机/环境是否已可接入 Playwright MCP server

【任务】
1. 调研并接入 Playwright MCP server，作为"AI 辅助生成/维护 Playwright 脚本"的能力。
2. 新增 ui_automation 的"脚本生成模式"：给定页面/流程，产出可运行的 Playwright 脚本并入库。
3. 与现有 browser-use 探索式执行并列，互不影响。
4. 输出一份设计说明（落 docs/）+ 最小可用 demo。

【验收标准】
- 给一个页面，确认能产出可运行的 Playwright 脚本。
- 不改动 browser-use 现有逻辑。
- 文档说明两种模式适用边界。
```

### 验证（Verification）
- 对 demo 页面生成脚本并 `pytest` 跑通。
- 文档明确 browser-use（探索）vs Playwright MCP（脚本）的取舍。

---

## 8. P2-2 生成-执行-回流闭环

**一句话**：需求文档/代码 → AI 生成用例 → AI 去执行 → 结果自动回流成报告，一条流水线，省掉手工衔接。

### 设计（Design）
- 串联 `requirement_analysis`（文档→用例）+ `services/testgen`（代码→用例）+ `ui_automation` AI（执行）+ `reports`（回流）。
- 新增编排层：把生成的用例自动派发为 AI 执行任务，执行结果回流到用例/报告，形成闭环。
- 复用现有 `testgen_integration` 客户端与 `executions` 调度。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区设计并实施「生成-执行-回流」编排闭环。

【前置约束】见文档 0.3。先 Read：
- apps/requirement_analysis（用例生成）
- apps/testgen_integration（testgen 客户端）
- apps/ui_automation/ai_base.py（执行）
- apps/executions、apps/reports（回流）

【任务】
1. 设计编排层：需求/代码 → 生成用例 → 派发为 UI AI 执行任务 → 结果回流报告。
2. 复用 testgen_integration 客户端，不重复造轮子。
3. 输出设计文档（落 docs/）+ 至少一条端到端 demo 链路。

【验收标准】
- 端到端跑通：一个需求 → 用例 → 执行 → 报告。
- 不破坏各模块独立能力。
- 文档画出数据流与失败重试策略。
```

### 验证（Verification）
- 端到端 demo：输入需求文档，确认产出报告且含执行结果。
- 失败用例能回流标记。

---

## 9. P2-3 统一智能输入框

**一句话**：像文章那样"打开哪个页面、做什么、验证什么"一句话扔进去就能跑，不用切好几个配置页，门槛低谁都能用。

### 设计（Design）
- 复用 aiSitePilot 的统一输入思路，在 `ui_automation` 前端加一个自然语言入口。
- 解析"打开哪页 / 做什么 / 验证什么"为结构化任务，落到现有 AI 模式（预探索 + 三层断言 + 模型分级 + 会话隔离）。
- 前端 `frontend/src/views/ui-automation` 增加统一输入框组件；后端新增解析接口。

### 执行提示词（Execution Prompt）
```
你是 TestHub 平台的开发助手。请在 C:\Users\EDY\WorkBuddy\testhub_platform 工作区设计并实施「UI 自动化统一智能输入框」。

【前置约束】见文档 0.3。先 Read：
- frontend/src/views/ui-automation（现有 UI）
- apps/ui_automation（AI 模式入口、ai_agent.py）
- 参考 aiSitePilot 的统一输入实现思路

【任务】
1. 前端新增统一智能输入框：用户输入"打开 X 页，做 Y，验证 Z"。
2. 后端新增解析接口，把自然语言拆成 {url, actions, asserts} 结构化任务。
3. 落到现有 AI 模式（预探索 + 三层断言 + 模型分级 + 会话隔离）。
4. 输出设计文档（落 docs/）+ 前端组件 + 接口最小实现。

【验收标准】
- 一个框输入一句话，确认自动解析并执行，结果含截图+断言。
- 不改动现有结构化配置入口，仅新增统一入口。
- 跑通 lint + 前端 build + 测试。
```

### 验证（Verification）
- 输入框输入"打开 demo.todomvc，添加三条任务，验证剩 0 条 active"，确认解析正确并执行。
- 前端 `npm run build` 通过。

---

## 附录 A：通用验证清单（每条功能收尾都用）
- [ ] 已 Read 相关文件，理解现状后改动。
- [ ] lint 通过：`ruff` / 项目既定 linter。
- [ ] 测试通过：对应 `apps/<module>/tests/` 或 `pytest`。
- [ ] 未升级 `starlette==0.50.0`，未动 `services/testgen/.venv`。
- [ ] 未触碰 `.env*` / `*.bak` / `data/*.db` 等敏感文件。
- [ ] 改动可回滚（单功能、集中、有 commit 说明）。
- [ ] 自测结果已记录并反馈。

## 附录 B：建议实施顺序
1. **第一批（P0）**：功能 1 + 2 + 3 —— 直接提升现有 AI 模式成功率/可信度/成本，改动集中、风险低。
2. **第二批（P1）**：功能 4 + 5 + 6 —— 场景化落地 + 安全加固。
3. **第三批（P2）**：功能 7 + 8 + 9 —— 闭环与体验增强，可并行调研。

## 附录 C：风险与回滚
- 所有功能均新增开关（默认可按需关闭），出问题改配置即可回退，不依赖代码回滚。
- 涉及 AI 行为的功能（预探索、断言、模型路由）务必保留日志，便于对比前后走偏率/成本。
- 任何改动遵循「先方案后执行」：本文件即方案，执行时复制对应提示词，逐批落地并在每批后自测。
