# AI 测试平台能力增强 —— 实施方案与执行清单

> 来源文章：《终于把 AI 测试平台搭建起来了……》（作者 Raina，2026-11-02，从零自建覆盖需求→功能→接口→UI→知识库全流程的 AI 测试平台）。
> 生成日期：2026-11-12（依代码核对与最新进度修订）
> 用途：把文章中可补充 / 增强 `testhub_platform` 的能力，整理为可执行的实施方案。每项含「目标 / 现状缺口 / 关键文件 / 实施步骤 / 预期结果 / 验证」，在 Agent 模式按步骤落地即可。
> 文档状态：**方案 + 执行清单，未改动任何实现代码**。

---

## 0. 背景与项目现状基线

### 0.1 来源文章核心方法（提炼，不含项目判断）

- 以「项目」为聚合载体，把 AI 嵌入需求→功能测试→接口→UI→知识库全流程。
- 需求分析 Agent 三件套：上传 md/txt/pdf/docx → 输出「功能清单 / 界面描述 / 待澄清项」；右侧对话 Agent 可将澄清结果喂回、修改需求（**澄清→修订闭环**）。
- 草稿-确认-写入范式：AI 生成用例先出草稿，用户确认后才写入用例库（人机协同）。
- 知识库 RAG：文档上传→分块→向量化→试检索，供用例生成 Agent 检索增强。
- 接口自动化链路：导入 swagger/openapi → 接口资产清单 → 一键进接口管理；单接口 Agent（单接口用例设计）+ 场景 Agent（多接口串联，执行失败 **AI 归因**）。
- UI 自动化双模式：探索生成可复用脚本 / Agent 直跑（Playwright 无障碍树）；复杂或验证码元素用 **Midscene**（多模态）补齐；**环境变量注入**（localStorage 预置登录态）免重复登录。
- 系统 AI 配置三件套：决策大模型（建议多模态）+ 向量模型（知识库）+ Midscene（UI 多模态识别）分别配置。

### 0.2 项目能力基线（已核对代码，非凭记忆）

| 文章能力 | testhub 现状 | 核对依据 |
|---------|-------------|---------|
| 以项目为聚合载体 | ✅ 已具备（`projects` app） | 目录确认 |
| Browser-use Agent 直跑 | ✅ 深度集成（`ui_automation/ai_base.py`：`_ActionGuard` 域名白名单、`_assert_cost_allowed` 成本闸门、CDP 截图、Windows CDP patch、`role=browser_use_text`） | 已读代码确认 |
| 用例生成 + 草稿闸门 | 🟡 `GeneratedTestCase.status` 已有 `generated→reviewing→reviewed→approved→rejected→adopted→discarded` 状态机；`AnalysisTask.result` 为 JSONField | `apps/requirement_analysis/models.py:120-199` |
| 接口自动化基础 | 🟡 `api_testing` 已有 `ApiProject/ApiCollection/ApiRequest/Environment/TestSuite/TestSuiteRequest/TestExecution/ScheduledTask/AIServiceConfig` | `apps/api_testing/models.py` |
| 知识库（KB） | 🟡 `llm_judge/kb_service.py` 存在，但是**金融评判用途 KB**（导出 `financial_kb.json`，结构化 companies/periods/metrics/values），非面向用例生成的通用 RAG | `apps/llm_judge/kb_service.py` |
| 需求分析三件套结构化输出 | ❌ `requirement_analysis` 无「功能清单 / 界面描述 / 待澄清项」结构化产物 | 全 app 检索无对应模型 / 导出 |
| openapi/swagger 导入 | ❌ `api_testing` 无 swagger/openapi 解析与导入 | 无对应文件 |
| AI 接口用例生成 Agent + 执行归因 | ❌ 无单 / 场景接口 Agent；执行侧未接 AI 归因 | 模型 / 视图检索无对应 |
| 系统 AI 配置三件套分离 | ❌ `AIModelConfig.role` 仅 `writer/reviewer/browser_use_text`，无 embedding/multimodal 语义 | `apps/requirement_analysis/models.py:214-218` |
| Midscene 多模态 UI | ❌ 全项目无 midscene | 全 app 检索无命中 |
| localStorage 注入登录态 | ❌ `test_executor.py:1446-1447` 仅 `localStorage.clear()` / `sessionStorage.clear()`（清空），非注入 | 已读代码确认 |

### 0.3 当前整合进度与本文档定位

> 本文档 7 项均为**净新增产品能力**；与之并行推进的是 `testCodeFast → testhub` 整合收尾（`整合实施_未完成任务追踪.md` 的 G 系列）。两者共享 `requirement_analysis` / `api_testing` / `ui_automation` 三个 app 的模型与 migration，须错峰执行。

- **整合 G 系列最新状态（2026-11-12）**：G1 运行环境已就绪（dev_stack 计划任务持久在线，3306/6379/8910/8900/3900 稳定 LISTEN；MySQL root 口令已轮换并验证；真实库 13 条幽灵迁移记录已清理，`makemigrations --check` 通过）；G2/G7 集成代码已提交 PR #18 待合；G3/G4/G5/R2、G8–G11 待做。
- **本文档定位**：在整合 G 系列闭合后再按序启动，避免并行 migration 分支冲突与集成测试互相踩踏。
- **内部依赖**：P1-2（系统 AI 配置三件套）是 P1-1（KB RAG）与 P2-1（Midscene）的前置配置位，须先于二者完成。

---

## 1. 通用执行约束（红线）

1. **不升级 `starlette==0.50.0`**（channels/Daphne WebSocket 依赖，升了会崩）。
2. **不触碰 `services/testgen/.venv`**（独立 venv，破坏即失能）。
3. **不修改 `.env*` 文件**，不把 `.env*` / `*.bak` / `*.rdb` / `*.db` / `*.sqlite*` 纳入提交。
4. **AI 生成内容必须经过人工确认闸门**：`GeneratedTestCase` 状态机 `generated→…→approved→adopted`，禁止 AI 直接把用例写入正式库；新增写库动作一律默认走「草稿待确认」。
5. **换行策略不变**：`.gitattributes` 固定 `*.sh`=LF、`*.bat/*.cmd/*.ps1`=CRLF，不改写该文件。
6. 改动后跑：八环门禁（`scripts/gate_all.py`）+ `pytest` 对应 app；新增 Django model 须出 migration。
7. 多修改合并一个 commit，信息 `<type>: <简短描述>`；提交前 `git diff --cached --name-only` 复核敏感残留。

---

## 2. 增强项总览与优先级

| 优先级 | 编号 | 能力 | 依赖 |
|---|---|---|---|
| P0 | 1 | 需求分析三件套 + 对话修订闭环 | 无 |
| P0 | 2 | openapi 导入 + 接口 Agent + AI 归因 | 无 |
| P1 | 2 | 系统 AI 配置三件套分离 | 无（但为 P1-1 / P2-1 的前置） |
| P1 | 1 | 用例生成知识库 RAG | 依赖 P1-2 |
| P2 | 1 | Midscene 复杂 UI 补齐 | 依赖 P1-2 |
| P2 | 2 | UI 双模式显式化 | 无 |
| P2 | 3 | localStorage 注入免登录 | 无 |

> 实施批次：**批次一（P0）** ① → ②；**批次二（P1）** ④(P1-2) → ③(P1-1)；**批次三（P2）** ⑤ → ⑥ → ⑦。P1-2 须在 P1-1 之前（修正原文档顺序歧义）。

---

## 3. P0-1 需求分析三件套 + 对话修订闭环

**目标**：需求文档上传后，AI 输出三种结构化产物（而非一段文本），并支持把澄清回答喂回修订原产物的闭环。

**现状缺口**：`requirement_analysis` 当前产出为单段文本，缺少「功能清单 / 界面描述 / 待澄清项」结构化产物与修订闭环。

**关键文件**：`apps/requirement_analysis/models.py`（新增 3 个 model，关联 `RequirementDocument`）、`services.py`（拆分单输出为三产物 + 修订接口）、`views.py` / `urls.py`（三产物 CRUD + 对话修订端点）、`serializers.py`、`migrations`；提示词复用 `PromptConfig`（需扩展 `PROMPT_CHOICES`）。

**实施步骤**：
1. 在 `models.py` 新增三模型，均 `ForeignKey(RequirementDocument)`：
   - `FunctionalInventory(doc, seq, name, summary, source_ref)`
   - `UIElementDescription(doc, page, element, desc, interaction)`
   - `ClarificationItem(doc, question, impact, suggestion, status[open|answered|resolved], answer, revision_log JSONField)`
2. `services.py` 新增分析入口：调用现有 LLM 能力，将需求文档分别生成上述三产物（结构化 JSON）；新增「澄清-修订」方法：接收 `clarification_id` + 用户回答，置 `status=answered`，将修订结果写回对应产物的 `revision_log`（记录谁在何时改了什么）并产出 `revised` 字段。
3. `views.py` + `urls.py` 暴露：三产物按 `doc` 过滤的列表 / 详情；`POST /api/requirement-analysis/<doc_id>/clarify` 提交澄清回答触发修订；保持与现有 `AnalysisTask` 状态机一致。
4. `serializers.py` 补对应序列化器；`PromptConfig.PROMPT_CHOICES` 扩展三产物专用提示词类型并补模板。
5. 生成并校验 migration：`python manage.py makemigrations requirement_analysis && migrate`。

**预期结果**：
- 三张新表 migrate 成功，上传需求文档后能分别以结构化列表返回三类产物。
- 对一条待澄清项提交回答后，原产物出现修订留痕且状态正确流转（`open→answered→resolved`）。
- 现有 `requirement_analysis` API 契约向后兼容，未破坏。

**验证**：
- [ ] `FunctionalInventory` / `UIElementDescription` / `ClarificationItem` 三表已 migrate。
- [ ] 上传文档后三产物均为结构化列表（非纯文本）。
- [ ] 提交一条澄清回答后，对应产物 `revision_log` 被更新、`status=resolved`。
- [ ] 单测通过；八环门禁绿。

---

## 4. P0-2 openapi 导入 + 接口 Agent + AI 归因

**目标**：解析 swagger/openapi 为一键导入的接口资产，提供单接口 / 场景用例生成 Agent，并在执行失败时给出 AI 归因。

**现状缺口**：`api_testing` 无 openapi 解析导入；无单 / 场景接口 Agent；执行侧未接 AI 归因。

**关键文件**：`apps/api_testing/models.py`（新增 `ApiSpecImport`）、`services.py`（openapi 解析 + 单 / 场景 Agent + 归因）、`views.py` / `urls.py`、`serializers.py`、`ssrf_guard.py`（导入时校验外链）；模型来源复用 `AIServiceConfig`（api_testing 已有的 AI 配置，非 `requirement_analysis.AIModelConfig`）。

**实施步骤**：
1. openapi 导入：新增解析服务（支持 JSON 与 YAML），将 `paths` 解析为 `ApiCollection` + 多条 `ApiRequest`（method/path/params/body schema/示例）；新增 `ApiSpecImport` 模型记录原始 spec、来源、导入结果；导入时对 spec 内 `server` URL 走 `ssrf_guard` 校验。
2. 单接口 Agent：`POST /api/api-testing/<api_request_id>/generate-cases`，调 LLM 依据该接口生成用例（正常 / 边界 / 异常），结果以**草稿**形态返回（不直接写正式库，需前端确认后 adopt）。
3. 场景 Agent：`POST /api/api-testing/scenario/generate`，接收多个 `ApiRequest` id，AI 串联业务流、给出请求顺序与数据依赖，生成 `TestSuite` + `TestSuiteRequest` 草稿。
4. AI 归因：`TestExecution` 状态为 `failed` 时，`POST /api/api-testing/executions/<id>/attribution` 调 LLM 基于请求 / 响应 / 断言 / 日志产出失败原因 + 修复建议，存回执行记录（新增 `attribution` 字段或独立的 `ExecutionAttribution` 模型）。
5. 模型来源复用 `AIServiceConfig`；新增对应 serializer 与路由；生成 migration。

**预期结果**：
- 最小 openapi 3.0 JSON 导入后生成正确的 `ApiCollection` 与 `ApiRequest`，且外部 URL 经 SSRF 校验。
- 单接口 Agent 产出草稿用例（正常 / 边界 / 异常三类齐备）；场景 Agent 产出 `TestSuite` 且顺序 / 依赖合理。
- 失败执行调归因后返回可读原因与建议；草稿用例不污染正式库。

**验证**：
- [ ] 最小 openapi JSON 导入后生成正确 `ApiCollection` + `ApiRequest`。
- [ ] 单接口 Agent 产出三类草稿用例；场景 Agent 产出合理 `TestSuite`。
- [ ] 失败执行调归因返回原因文本；SSRF 防护对外链生效。
- [ ] 八环门禁绿。

---

## 5. P1-2 系统 AI 配置三件套分离

**目标**：扩展 `AIModelConfig.role`，为决策大模型 / 向量模型 / Midscene 多模态预留独立配置位，使平台可分别配置并隔离使用。

**现状缺口**：`models.py:214-218` 的 `ROLE_CHOICES` 仅 `writer/reviewer/browser_use_text` 三类文本角色，无 embedding/multimodal 语义；`get_active_config(model_type, role)`（245-251）按 role 取活跃配置，扩展 role 即可复用。

**关键文件**：`apps/requirement_analysis/models.py`（扩展 `ROLE_CHOICES`）、`serializers.py`、`views.py`（`AIModelConfigViewSet` 已支持按 role 过滤，天然兼容）、`apps/ui_automation/views_config.py`（browser_use 侧可对接 multimodal 角色）。

**实施步骤**：
1. 扩展 `AIModelConfig.ROLE_CHOICES`，新增：`('decision','决策大模型')`、`('embedding','向量模型')`、`('multimodal','Midscene 多模态识别')`；保留 `writer/reviewer/browser_use_text` 不变。
2. 新增类方法 `get_active_by_role(role)` 封装取活跃配置（兼容既有 `get_active_config`）。
3. 确认 `serializers` / `views` 对新角色的 list_filter 覆盖；管理端点天然按 role 过滤，无需改调用方。
4. 生成 migration（仅枚举变更也需 `makemigrations` 校验）。

**预期结果**：
- `ROLE_CHOICES` 含 `decision/embedding/multimodal`，且 `writer/reviewer/browser_use_text` 既有取数逻辑与调用方完全不变。
- 新角色配置可被独立存取，互不干扰。

**验证**：
- [ ] `ROLE_CHOICES` 含三个新角色；`writer/reviewer` 行为不变。
- [ ] 通过管理端点创建 `role=embedding` 配置 → `get_active_by_role('embedding')` 能取到。
- [ ] 单测通过；八环门禁绿。

---

## 6. P1-1 用例生成知识库 RAG

**目标**：把历史用例、业务规则、接口契约、需求文档沉淀为知识库，新需求 / 接口生成用例时检索增强，减少重复、提升一致性。

**现状缺口**：`llm_judge/kb_service.py` 是金融评判 KB，**不可直接当用例生成 RAG**；缺少通用 KB 服务。

**关键文件**：新建 `apps/knowledge_base/`（模型 `KnowledgeDocument` / `KnowledgeChunk` 含 embedding 向量字段或外链向量库 id、`source_type` 如 requirement/api/history_case；服务层 `ingest` 上传→分块→向量化→存储 与 `retrieve` top-k 检索）；在 `requirement_analysis` / `api_testing` 的生成入口注入检索结果。

**实施步骤**：
1. 新建 app `apps/knowledge_base`：模型 `KnowledgeDocument` / `KnowledgeChunk`；服务层 `ingest`（上传→分块→向量化→存储）与 `retrieve`（top-k 检索）。向量化使用 P1-2 预留的 `embedding` 角色配置。
2. 提供开关 `ENABLE_KB_RAG`：开启时注入检索片段，关闭时退化为纯提示词生成（不报错）。
3. 在 `requirement_analysis` 用例生成与 `api_testing` 单 / 场景 Agent 入口，将 `retrieve` 命中相关知识作为上下文追加进 prompt（不改既有输出结构）。
4. 暴露基础管理端点：文档上传 / 列表 / 删除、检索试跑（便于验证召回质量）。
5. 生成 migration；写 README 片段说明向量存储选型与降级策略。

**预期结果**：
- 知识库 app 可独立 `ingest` 与 `retrieve`，且不影响 `llm_judge`。
- 生成用例时检索片段真正进入 prompt 上下文；`ENABLE_KB_RAG=false` 时退化为纯生成。

**验证**：
- [ ] KB app 可独立 ingest/retrieve，且不影响 llm_judge。
- [ ] 生成用例时检索片段进入 prompt 上下文。
- [ ] `ENABLE_KB_RAG=false` 退化为纯生成且不报错；单测通过；八环门禁绿。

---

## 7. P2-1 Midscene 复杂 UI 补齐

**目标**：对验证码、图像类、坐标 / 样式强依赖等 browser-use 难以处理的 UI，引入 Midscene（多模态）作兜底通道，与现有 browser-use 直跑分工。

**现状缺口**：全项目无 midscene；多模态模型配置位由 P1-2 预留（`multimodal` 角色）。

**关键文件**：`apps/ui_automation/` 新增 `midscene_driver.py`（封装 Midscene 客户端 / SDK 调用）；在 `ai_base.py` 执行路由增加 `execution_mode`（`browser_use` | `midscene`）；复用现有 `_ActionGuard` 域名护栏（419 行起）与 `_assert_cost_allowed` 成本闸门（760 行起）。

**实施步骤**：
1. 新建 `apps/ui_automation/midscene_driver.py`：封装 Midscene 客户端初始化（指向 P1-2 预留的 `multimodal` 角色模型）、`query` / `action` 接口；所有调用**先过 `_ActionGuard` 域名护栏、再过成本闸门**（与 browser_use 一致）。
2. 在 `ai_base.py` 执行路由增加 `execution_mode` 参数：`browser_use`（默认）| `midscene`；`midscene` 模式走 `midscene_driver`，复用现有截图留证 / 会话隔离逻辑。
3. 在 UI 配置 / 任务入口暴露模式选择（默认 `browser_use`，复杂场景切 `midscene`）。
4. 如需新增第三方包，写入本项目依赖清单（**不碰 `services/testgen/.venv`**），并验证与本机 Playwright/starlette 共存无冲突。

**预期结果**：
- Midscene 通道可调，且强制过 `_ActionGuard` 与成本闸门；执行模式可切换，默认仍是 `browser_use`（向后兼容）。
- 新增依赖可独立卸载，不影响现有 browser_use 链路。

**验证**：
- [ ] Midscene 通道可调用且强制过护栏 / 成本闸门。
- [ ] 执行模式可切换，默认 `browser_use` 行为不变。
- [ ] 新增依赖不破坏本机 Playwright/starlette；八环门禁绿。

---

## 8. P2-2 UI 双模式显式化（探索生成脚本 vs Agent 直跑）

**目标**：把两种 UI 自动化模式在平台里显式区分——`agent_run`（browser-use 直跑，适合临时验证）与 `explore_to_script`（探索→生成可复用脚本，适合回归）。

**现状缺口**：browser-use 直跑已深集成；缺「探索→生成可复用脚本」通道与模式选择的 UI 表达。

**关键文件**：`apps/ui_automation/ai_base.py`（加 `ui_mode` 路由）、`services.py` 或 `tasks.py`（探索后处理为 Playwright 脚本草案）、`views.py` / `views_config.py`（模式选择入口）。

**实施步骤**：
1. 在 `ai_base.py` 增加 `ui_mode` 参数：`agent_run`（沿用现有 browser-use 直跑）/ `explore_to_script`（新增）。
2. `explore_to_script` 流程：启动 browser-use 探索目标页面 → 收集操作轨迹 → 后处理为 Playwright 脚本草案（带步骤注释、选择器、断言占位）→ 以**草稿**形态返回，不直接落库，需用户确认后才保存为可复用脚本。
3. 复用现有 `_ActionGuard` 域名护栏、`_assert_cost_allowed` 成本校验、截图留证与会话隔离。
4. 在 UI 配置 / 任务入口暴露模式选择，默认 `agent_run`（向后兼容）。

**预期结果**：
- 两种模式可独立触发，默认 `agent_run` 行为不变。
- `explore_to_script` 产出可复用脚本草案且走确认闸门。

**验证**：
- [ ] 两种模式可独立触发，默认 `agent_run` 行为不变。
- [ ] `explore_to_script` 产出可阅读脚本草案且过护栏 / 成本闸门。
- [ ] 复用护栏 / 成本 / 截图；八环门禁绿。

---

## 9. P2-3 localStorage 环境变量注入免登录

**目标**：测试执行前把预置的登录态（token / localStorage / cookie）注入浏览器，免去每个用例重复登录。

**现状校正**：`apps/ui_automation/test_executor.py:1446-1447` 仅有 `localStorage.clear()` / `sessionStorage.clear()`（**清空**），**不是注入**——这是缺口，不是已有能力。

**关键文件**：`apps/ui_automation/test_executor.py`（在执行前、清空前增加「注入」分支）、`models.py` 或配置（新增「登录态预设」配置项，关联 Project/Environment）、前端 / 配置入口（让用户录入预设键值）。

**实施步骤**：
1. 新增「登录态预设」配置：支持 key-value（localStorage / cookie / 自定义 JS 注入片段），关联到 Project 或测试环境；敏感值**加密存储**，禁止明文写入日志与 `.env*`。
2. 在 `test_executor.py` 浏览器初始化后、用例执行前，若配置了预设则**先注入**再执行（保留原有 clear 作为「无预设时的干净起点」语义，或改为按开关控制）。
3. 提供开关 `use_login_preset`（默认关，向后兼容现有行为），开启时走注入路径。
4. 配置入口暴露预设的增删改查（脱敏展示）。
5. 生成 migration（如新增模型）。

**预期结果**：
- 开启预设后用例执行前浏览器已含注入的登录态，无需重复登录；敏感值加密存储、脱敏展示、不泄露到日志。
- 默认关闭，现有测试行为不变。

**验证**：
- [ ] 开启预设后用例执行前浏览器已含注入登录态，无需重复登录。
- [ ] 敏感值加密存储、脱敏展示、不进日志明文。
- [ ] 默认关闭，现有行为不变；八环门禁绿。

---

## 10. 实施顺序与内部依赖

1. **批次一（P0）**：① 需求分析三件套 + 对话修订闭环 → ② openapi 导入 + 接口 Agent + AI 归因。投入产出比最高，直接补齐全流程 AI 闭环最关键的「需求结构化」与「接口 AI」。
2. **批次二（P1）**：④ 系统 AI 配置三件套分离（**先于** ③）→ ③ 用例生成知识库 RAG。④ 为 ③ 的向量模型配置位前置。
3. **批次三（P2）**：⑤ Midscene 复杂 UI → ⑥ UI 双模式显式化 → ⑦ localStorage 注入免登录。
4. **与整合 G 系列的排期**：建议先闭合 `整合实施_未完成任务追踪.md` 的 G3/G4/G5/R2/G8–G11（整合收尾），再启动本增强；二者共享 `requirement_analysis` / `api_testing` / `ui_automation` 的模型与 migration，错峰执行避免并行冲突。

---

## 11. 通用验证清单（所有批次适用）

- [ ] 八环门禁 `scripts/gate_all.py` 全绿（secret/lint/format/enum/structure/type/security/test）。
- [ ] `pytest` 对应 app 通过；新增 model 均 `makemigrations` + `migrate` 成功。
- [ ] 所有 AI 写库动作走「草稿待确认」闸门，无直接污染正式库。
- [ ] 不触碰红线：starlette 版本、testgen venv、`.env*`、换行策略、提交前敏感复核。
- [ ] 每个功能有可重复的自测脚本 / 用例并给出结果。

---

## 12. 风险与回滚

- **新增依赖风险**（Midscene、向量库）：均走独立依赖项，可 `pip uninstall` 回退；不写入 `services/testgen/.venv`。
- **模型配置扩展风险**（P1-2）：仅扩展枚举，回滚 = 还原 `ROLE_CHOICES` + 删除对应 migration。
- **行为变更风险**：所有新能力默认开关关闭（`use_login_preset` / `ENABLE_KB_RAG` / `ui_mode` 默认 `agent_run`），未启用即无影响，回滚只需关开关。
- **数据风险**：新增 model 如需回滚，先 `migrate <app> <prev>` 再删代码；正式库数据不受影响（AI 产物均草稿态）。
- **通用红线**：任何批次失败，先 `git stash` / 回退该批改动，不影响主干；绝不升级 starlette。
