# 调研与立项方案：Playwright MCP 脚本生成（#185）

> 本文档为「调研 / 立项」性质，只给出结论、架构与分批计划，**不改动任何实现文件**。
> 任务 #185 维持 pending，待你拍板立项后再逐批实施。

---

## 0. 结论先行

- **可行性：已实锤可立项。** 本机 Node v22.22.2 + npm 10.9.7 就绪；`@playwright/mcp@0.0.84` 已全局安装（9s，`npm install -g @playwright/mcp@latest` 成功）；CLI 参数齐全（`--codegen` / `--port` / `--caps` / `--browser` / `--allowed-hosts` / `--blocked-origins`）。官方 MCP server 通过 accessibility snapshot（非截图）驱动浏览器，**每个工具响应都附带对应的 Playwright 代码片段**，天然契合"脚本生成"——agent 实操一遍，MCP 把每步转成代码。
- **建议路径：分阶段。** 阶段一先做端到端 **spike**（后端 MCP client 连通 + 对 demo 页面产出脚本 + 落库 `TestScript`），验证可行性；阶段二再补前端入口 + 安全护栏 + 自测。**以阶段一通过作为 full build 门槛**。
- **不重复造轮子。** 平台已有 `TestScript` 模型、`playwright_engine`、`ScriptEditorEnhanced`、内网白名单 guard、`_scrub_secret` 凭据脱敏、preview/confirm 两段式、LLM 编排能力。#185 是"**新增一个浏览器控制 MCP 通道**"，与现有 browser-use 探索式执行互补，而非替换。

---

## 1. #185 真实定义（来自既有设计文档）

- `docs/unified_priority_2026-11-03.md:173`：「新-7 Playwright MCP｜先调研后立项」。
- `docs/browser_use_enhancement_prompts_2026-11-03.md:261-292`（P2-1 Playwright MCP 互补）：
  - 引入 Playwright MCP server 作为「脚本生成」能力，与现有 browser-use 探索式执行互补。
  - 在 `ui_automation` 新增"脚本生成模式"：调用 MCP 产出 Playwright 代码，入库到脚本表，供后续脚本化回归。
  - 验收：给一个页面，能产出可运行的 Playwright 脚本；不改动 browser-use 现有逻辑；文档说明两种模式适用边界。

一句话定位：**把"探索式执行"沉淀为"确定性回归脚本"**——LLM 用 MCP 驱动真实浏览器走一遍流程，MCP 自动累积出可重复的 Playwright 脚本，省去人工在编排器里逐步骤拖。

---

## 2. 现状盘点（已建能力，直接复用，不要重造）

| 资产 | 位置 | #185 复用方式 |
|---|---|---|
| `TestScript` 模型 | `apps/ui_automation/models.py:177` | 脚本落库目标（language=python/javascript、framework=playwright、content、steps、element_usages、`generate_code()`） |
| 执行引擎 | `apps/ui_automation/playwright_engine.py`、`selenium_engine.py` | 生成的脚本可直接复用现有执行链路 |
| 低代码编排 UI | `frontend/.../scripts/ScriptEditorEnhanced.vue` | 在其上扩展"AI 生成"入口，而非新建页面；AI 产物落入草稿后人工微调 |
| 平台自有 MCP server | `apps/mcp/server.py`（FastMCP，Python `mcp` SDK，17 工具） | 方向相反（Agent 操控平台）；证明其 MCP 协议栈已通，Python 侧做 MCP client 有先例 |
| 内网/云元数据硬拦截 | `apps/ui_automation/ai_base.py:304-354` | **安全基座**：169.254.0.0/16 无条件硬拦 + 内网私有段默认拦 + `UI_ALLOWED_DOMAINS` 白名单 |
| 凭据脱敏 | 现有 `_scrub_secret`（PR #13 引入） | 生成脚本入库前剔除明文凭据 |
| 两段式人工复核 | 平台 MCP `preview_*` / `confirm_*` 工具 | 生成脚本落库前复用 confirm 机制做人工复核 |
| LLM 编排 | `run_adhoc` / ClosedLoop / Orchestrator | agent loop 思路复用（LLM 决策 → 调工具 → 收结果） |

> 注：前端已有 `scriptGeneration: '脚本生成'` / `智能脚本生成` 文案与 `ScriptList`/`ScriptEditorEnhanced` 页面，说明"脚本生成"方向早已预留 UI 位。

---

## 3. Playwright MCP 能力边界（官方实证）

来源：`playwright.dev/mcp/tools/assertions` 官方文档 + 本机实装 CLI。

- **协议**：MCP；默认 **stdio**，亦支持 `--port <port>` 的 **SSE/HTTP** 传输。
- **驱动方式**：accessibility snapshots（结构化可访问性树，非截图），无需视觉模型；target 用稳定的 ref（如 `e3`），比 CSS/XPath 韧性高。
- **代码生成（#185 核心）**：每个工具响应都附带 `// Generated:` 代码块；`--codegen <lang>` 设定语言，支持 **typescript（默认）/ python / java / csharp / none**。
- **测试能力**：`--caps=testing` 开启 `browser_verify_*`、`browser_generate_locator` 等断言/定位工具。
- **配置**：`--config <path>` 读配置文件；`--allowed-hosts` / `--blocked-origins` 做 DNS rebinding / 源拦截。

⚠️ **致命风险点**：`browser_run_code_unsafe` 等价于 RCE（在 server 进程内执行任意 Playwright 代码），**必须永远关闭，绝不在任何启动命令里出现**。

---

## 4. 可行性探针结果（本次调研已执行）

| 检查项 | 结果 |
|---|---|
| Node / npm | ✅ v22.22.2 / v10.9.7 |
| 安装 `@playwright/mcp` | ✅ `npm install -g @playwright/mcp@latest` → added 3 packages in 9s |
| CLI 参数 | ✅ `--codegen` / `--port` / `--caps` / `--browser` / `--allowed-hosts` / `--blocked-origins` / `--config` 均存在 |
| 结论 | ✅ 本机可拉起 Playwright MCP server，接入可行性成立 |

**待立项后实测（阶段一 spike 内验证）**：
- JS 版 chromium 内核安装：`npx playwright install chromium`（约 150MB；底层二进制与 Python 侧 `~/.cache/ms-playwright` 共享，可能免重复下载，需实测）。
- Python `mcp` SDK client 经 stdio 拉起 npx 子进程并 `ClientSession.call_tool` 连通。
- Windows 下 Python 拉起 `npx @playwright/mcp` 子进程的 PATH / 命令解析兼容性。

---

## 5. 接入架构对比

| 选项 | 形态 | 优劣 | 结论 |
|---|---|---|---|
| **A（推荐）** | 平台 Python 后端做 **MCP client**，stdio 拉起 `@playwright/mcp` 子进程；自有 agent loop（LLM 决策 → 调 MCP 工具 → 累积 generated code → 组装 `TestScript`） | 复用最多（TestScript/引擎/guard/confirm/`mcp` SDK）；脚本生成与平台账号/项目/白名单一体；可控性强 | ✅ 采用 |
| B | 前端/IDE 直接接 VS Code + Playwright MCP，平台只提供"粘贴导入" | 实施快但脱离平台 agent 编排，ROI 低，与"TestHub 平台能力"定位不符 | ❌ |
| C | 独立进程产出 `.spec.ts`/`.py` 落 `tests/`，平台扫描入库 | 脱离平台 LLM 编排，重复造轮子 | ❌ |

---

## 6. 推荐方案（分阶段 + 批次）

### 阶段一 · 端到端 Spike（立项门槛，验证即止）
- **A1** 新增 `apps/ui_automation/pw_mcp_client.py`：用 Python `mcp` SDK 的 `stdio_client` 拉起 `npx @playwright/mcp --codegen python`，封装 `connect / list_tools / call_tool / close`。
- **A2** 新增精简 agent loop（可放在 `apps/ui_automation/` 内）：给定自然语言流程 + 目标白名单 URL → LLM 循环调 MCP 工具 → 收集每次响应的 `// Generated:` 片段 → 组装为 `TestScript`（language=python, framework=playwright, script_type=CODE）草稿并落库。
- **A3** spike 验证报告：对 demo 页面（如 `https://demo.playwright.dev/todomvc`）产出一份可 `pytest`/可直接被 `playwright_engine` 执行的脚本，证明链路通。

> 阶段一**只验证可行性**，不含前端入口与完整护栏；产物落库前仍走最小白名单校验（复用 `ai_base` 的硬拦截）。

### 阶段二 · Full Build（spike 通过后）
- **B1 安全护栏**：目标 URL 必须过 `ai_base` 内网/元数据硬拦截（`UI_DOMAIN_GUARD=strict`）；启动命令**绝不**带 `browser_run_code_unsafe`；生成脚本入库前过 `_scrub_secret` 剔除凭据；复用平台 `preview_*`/`confirm_*` 两段式做人工复核落库。
- **B2 前端入口**：在 `ScriptEditorEnhanced.vue` 增加"AI 生成脚本"面板（自然语言描述 + 目标 URL + 白名单校验 → 调后端 → 结果预览落入 `TestScript` 草稿 → 可人工微调后保存）。
- **B3 自测**：MCP client 连通性测试（轻量/mock）、agent loop 代码组装测试、白名单拦截测试、入库往返测试。

---

## 7. 安全红线（立项即生效，写入实施）

1. `browser_run_code_unsafe` **永不在任何启动命令出现**（RCE 等价）。
2. 目标 URL 必须过内网/云元数据硬拦截（`ai_base.py:304-354` 既有 guard），白名单外一律拒。
3. 生成脚本入库前经 `_scrub_secret` 脱敏，明文凭据不入脚本、不入日志、不入响应体。
4. 落库必须走 preview/confirm 两段式人工复核（复用平台 MCP confirm 机制），AI 产物默认 `pending`/草稿态。
5. 复用现有"绝不伪装通过"原则：MCP 连接失败/内核缺失时如实报错并给安装指引，不降级谎报成功。

---

## 8. 风险与遗留

| 风险 | 影响 | 缓解 |
|---|---|---|
| JS 版 chromium 内核需安装（~150MB） | 阶段一可能被沙箱/网络卡住 | 先验证 `~/.cache/ms-playwright` 共享；必要时 `npx playwright install chromium` |
| Windows 下 Python 拉起 `npx` 子进程 | PATH / 命令解析兼容问题 | spike A1 优先验证；必要时用绝对 `node` 路径 + `npx` 解析结果 |
| 生成代码质量 | getByRole 韧性高但仍有 LLM 误判 | 强制人工 confirm 复核；产物落草稿态 |
| 与 Python playwright 内核缓存共享 | 未证实 | spike 内实测 `--codegen python` 产出能否被现有 `playwright_engine` 直接执行 |

---

## 9. 立项结论

**建议立项**，以「阶段一 spike 通过」为 full build 门槛。理由：
- 可行性已探针实锤（包可装、CLI 可用、Node 就绪）。
- 复用面极大（TestScript / 引擎 / guard / confirm / `mcp` SDK / ScriptEditorEnhanced），新代码量可控。
- 与现有 browser-use 探索式执行形成"临时快验 vs 长期稳归"互补，补齐 UI 自动化工具链闭环。

待你确认立项后，我按 A1→A2→A3（spike）→ B1→B2→B3 逐批实施并自测。

---

## 阶段一 spike 实施记录（2026-11-04，已完成 ✅）

立项后已实施并验证通过，**full build 门槛已跨过**。

### 新增文件
- `apps/ui_automation/pw_mcp_client.py`（A1+A2）：Python `mcp` SDK 经 stdio 拉起 `@playwright/mcp` 子进程；封装 connect/list_tools/call_tool/close；`is_safe_target_url` 最小安全硬拦截（云元数据/内网私有段，纯函数，复用 `ai_base` 安全语义）；`extract_codegen`/`assemble_script` 收集 MCP 响应里的 Playwright 代码片段并组装为可运行 sync 脚本；`record_flow` 确定性最小流程（navigate→snapshot→找输入框填字→回车）作为 LLM agent loop 占位（阶段二替换为 LLM 决策）。
- `tests/test_pw_mcp_client.py`：7 个纯函数单测，锁定安全红线（云元数据/内网硬拦、启动参数不含 `browser_run_code_unsafe`）+ 组装逻辑合法 Python（无 JS 残留）+ codegen 提取 + input ref 解析。均通过（0.48s，不依赖 node/chromium）。

### A3 端到端验证证据（真实浏览器执行）
- 通道：Python `mcp` client 经 stdio 拉起 `node cli.js`，列出 25 个工具 ✅
- 真实操作：对 `https://demo.playwright.dev/todomvc` 跑 navigate→snapshot→type→press，MCP 自动产出 codegen ✅
- 组装脚本（节选，已验证合法且 sync）：
  ```python
  page.goto("https://demo.playwright.dev/todomvc")
  page.get_by_role("textbox", name="What needs to be done?").fill("spike demo task")
  # Press Enter
  page.keyboard.press('Enter')
  ```
- **真实执行验证**：把生成的脚本落盘后用本仓 Python Playwright（chromium-1200）执行，`rc=0` 零报错 ✅ —— 证明「探索式执行沉淀为确定性回归脚本」链路完全打通。

### 环境钉死（spike 期实测）
- Node：`C:/Users/EDY/.workbuddy/binaries/node/versions/22.22.2-6/node`（v22.22.2）
- `@playwright/mcp@0.0.84` 全局装；CLI 入口 `node_modules/@playwright/mcp/cli.js`
- 浏览器内核：JS 侧装 `chromium-1252`（匹配 MCP 的 playwright 1.65.0-alpha）；Python 侧 `chromium-1200`（本仓 root venv playwright 期望，已存在）。两者并存于 `%LOCALAPPDATA%/ms-playwright`，互不冲突。
- 启动命令**严禁** `browser_run_code_unsafe`（RCE 等价）；`record_flow` 只调 navigate/snapshot/type/press_key。

### 阶段二（full build）待办
- B1 安全护栏：正式接入 `ai_base._ActionGuard` 抽取共享 guard；凭据 `_scrub_secret` 脱敏；preview/confirm 两段式人工复核落库。
- B2 前端入口：`ScriptEditorEnhanced.vue` 加「AI 生成脚本」面板。
- B3 自测：MCP client 轻量连通性测试（mock）+ 白名单拦截 + 入库往返。
- LLM agent loop 替换 `record_flow` 的确定性步骤（用 DeepSeek 决策调用 MCP 工具）。

---

## 阶段二 full build 实施记录（2026-11-04，已完成 ✅）

### 交付清单

| 批次 | 文件 | 内容 |
|---|---|---|
| B1 后端 | `apps/ui_automation/pw_mcp_client.py` | `record_flow` 改为返回结构化 `{content, steps, language}`；新增同步入口 `generate_script(target_url, instruction, language)`，**自行 connect/close**（修复漏 connect 导致 `not connected` 的缺陷） |
| B1 后端 | `apps/ui_automation/models.py` | `TestScript.status` 新增（`draft`/`confirmed`，默认 `confirmed`）→ preview/confirm 两段式草稿态 |
| B1 后端 | `apps/ui_automation/migrations/0009_testscript_status.py` | 对应迁移（默认值 `confirmed`，存量脚本不受影响） |
| B1 后端 | `apps/ui_automation/serializers.py` | `TestScriptCreateSerializer` 纳入 `status`，并回传 `id` |
| B1 后端 | `apps/ui_automation/views_script_generate.py` | `POST /ui-automation/scripts/generate/`：鉴权 + 参数校验 + `is_safe_target_url` 硬拦截 + 调 `GENERATE_FN`（可 patch）+ **instruction 凭据兜底打码**，产物为预览**不落库** |
| B2 前端 | `frontend/src/api/ui_automation.js` | 新增 `generateScript` |
| B2 前端 | `frontend/src/views/ui-automation/scripts/ScriptEditorEnhanced.vue` | 工具栏「AI 生成脚本」按钮 + 对话框（目标 URL / 指令 / 语言）→ 生成后填入编辑器并标记 `isAiGenerated` → 保存时以 `status='draft'` 落库；手动清空/切换项目自动复位 |
| B2 前端 | `locales/lang/{zh-cn,en}/ui-automation.js` | `scriptEditor.aiGenerate*` 与 `messages.*` 文案 |
| B3 自测 | `apps/ui_automation/tests/test_script_generate.py` | 10 例：鉴权、缺参、云元数据/内网拦截、公网放行、预览结构、凭据不回显明文、草稿态落库往返 |
| B3 自测 | `tests/test_pw_mcp_client.py` | 追加 2 例：`generate_script` 必须 init→connect→record_flow→close（锁死本次修复）、unsafe URL 抛错 |

### 阶段二验证证据

- 后端单测：`pytest apps/ui_automation/tests/ tests/test_pw_mcp_client.py -q` → **338 passed**（含本批 12 例新增）。
- **活体验证（真实 MCP + 真实浏览器）**：直接调 `generate_script("https://demo.playwright.dev/todomvc", "phase2 generate probe")`
  - 返回 `{content, language: python, steps: [navigate, snapshot, type, press_key]}`
  - 产出脚本语法合法，落盘后真实执行 **`rc=0`**
- 前端：ESLint 0 error、`npm run build` 通过（18.54s）。
- 迁移一致性：`makemigrations --check --dry-run` 无待生成迁移。

### 与方案的差异（务实取舍）

- **B1 安全 guard 未抽公共模块**：`ai_base.py` 顶层依赖 `langchain_openai`/`browser_use` 等重型库，纯 Python 调用链引入会拖慢/污染；沿用 `pw_mcp_client` 内的最小硬拦截（语义与 `ai_base` 一致，注释指向权威源）。抽公共模块留作后续债务。
- **preview/confirm 用字段而非独立接口**：不加 `TestScript.status` 之外的状态机，前端生成→编辑器复核→保存即「确认」，`draft` 表示 AI 产物未复核。避免一次性引入过多模型改动。
- **LLM agent loop 未落地**：`record_flow` 仍是确定性最小流程（navigate→snapshot→type→press），LLM 决策替换仍是后续增强项（需先有 LLM 调用预算与提示词工程）。
