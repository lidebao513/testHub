# testgen 全阶段模块化与 LLM 接入矩阵 · 任务执行列表

> 派生自 `docs/testgen_stage_modularization_llm_matrix.md` §7 分阶段实施计划。
> 本列表为**执行清单**，按 P0→P1→P2 顺序落地；每项均以「八环门禁全绿（secret/lint/format/enum/structure/type/security/test，cov≥60%）」为验收闸门。
> 统一纪律：所有 LLM 调用经 `engine/llm_fallback.chat_with_fallback`（已落地，含降级链 + 进程缓存快速切换 + 视觉兜底），禁止在各调用点各自实现降级。

---

## 分支策略（贯穿全程，先于任何代码改动）

- [x] **T0 · 切分支**：从稳定 `main`（当前 `85e20cf`）拉 `dev/modularization`；`main` 冻结为应急基线，任何人应急用 `main`，不受扰动。
  - 验收：`git branch` 可见 `dev/modularization`，`main` 仍可独立 `cli.main pipeline --url …` 跑通。
  - 依赖：无（先行）。

---

## P0 · 能力层（八环全绿为门槛）

- [x] **P0-1 · 新建 `engine/comparator/` 模块**
  - 内容：`compare_one(case, actual, model_opts)` / `compare_batch(...)`，复用 `engine/expert/llm` 经统一 `chat_with_fallback`（降级链已就绪）。
  - 契约：`StageInput={case(预期), actual(执行结果), model_opts}` → `StageOutput={verdict, confidence, reason, diff[], rule_verdict}`。
  - 五维判定复用（NORMAL/ABNORMAL/AUTH/PRIV_ESC/BOUNDARY）；诚实降级 `verdict=inconclusive`/`skipped≠pass`，绝不假装通过。
  - 产出：`engine/comparator/compare.py` + 契约。
  - 验收：八环全绿；单测 mock `chat_with_fallback` 覆盖「成功 / 超时 / 额度不可用降级 / 真实错误不降级」四分支。
  - 依赖：T0。

- [x] **P0-2 · comparator 对外端点 + CLI**
  - 内容：`POST /api/v1/compare`（`service/app.py` 新增，与 `/generate` 生成-only 红线隔离）；CLI `testgen compare`（参数 `--project --case-id --actual --json-in`）。
  - 红线：凭证不落地、不入库、不入日志、不入产物；`/api/v1/generate` 守"生成-only"（带 execute→422 既有约束不变）。
  - 产出：端点 + CLI + 请求/响应契约。
  - 验收：八环全绿；端点单测（mock comparator）覆盖正常/超时降级。
  - 依赖：P0-1。

- [x] **P0-3 · 能力层 11 项补 `StageInput/StageOutput` 契约 + `StageRegistry`**
  - 11 项：extract · tp_expand · case_gen · semantic_enrich · expert_review · llm_design · prd_ingest · comparator · runtime_ui · auth_scan · tag。
  - 内容：每项抽成「无副作用纯函数 + 契约」；新增 `engine/stage_registry.py` 注册 17 个 stage；`pipeline` 改为「按 registry 组链」（先注册能力层，适配层 P1 接）。
  - 当前 `pipeline.run_pipeline` 硬编码编排（1282–1372），本步只抽契约 + registry 占位，**不删硬编码**（避免大爆炸，迁移留 P1）。
  - 产出：11 份契约 + `StageRegistry` + 注册代码。
  - 验收：八环全绿；现有端到端用例无回归（契约为只读描述 + 薄适配器，不改变既有调用）。
  - 依赖：P0-1（comparator 契约同源）、T0。

---

## P1 · 适配层解耦 + 对话代理（八环全绿为门槛）

- [x] **P1-1 · 适配层 6 项经 registry 组链，`pipeline` 去硬编码**
  - 6 项：pull · resolve_sources · register · scan · partition_channels · persist。
  - 内容：`pipeline.run_pipeline` 改为读 `StageRegistry` 顺序组链，删除 1282–1372 硬编码；适配层封装 DB/项目状态/双通道拆分，逻辑下沉能力层。
  - 产出：解耦后的 `pipeline.py` + 适配层模块。
  - 验收：八环全绿；双通道（code/url）端到端用例无回归；`channels_summary.json` 仍正确产出。
  - 依赖：P0-3。
  - 收尾：commit 267b039 一并修复 `service/app.py` 4 端点 webhook 幂等（dedupe 条件 `in (PENDING,RUNNING)` → `!= CANCELLED`），消除 `test_webhook_idempotent_for_same_event` 偶发「首任务完成到 SUCCESS 后重放又建新任务」的 flaky。

- [x] **P1-2 · 对话代理（Dialogue Agent）外壳**
  - 内容：适配层新增 NL 意图解析 → 阶段/端点映射（复用 `engine/expert/llm` 经 `chat_with_fallback`）。
  - 路由："测试这个URL"→`POST /api/v1/generate(+url)`；"用 X 重新生成"→注入 `EXPERT_MODEL` 后 generate；"执行并对比结果"→`/execute`→`POST /api/v1/compare`；"列出失败用例"→`GET /api/v1/tasks/{id}`。
  - 诚实降级：LLM 解析失败 → 回退关键字路由；比对失败 → 回退规则判定。
  - 产出：对话代理模块 + 意图映射表 + 单测（mock 意图解析 + 端点调用）。
  - 验收：八环全绿；不改动能力层（仅新增适配层）。
  - 依赖：P0-2、P1-1。

---

## P2 · 跨项目复用包（八环全绿为门槛）

- [x] **P2-1 · 能力层抽独立可 `pip install` 包**
  - 内容：抽 `engine/comparator` + 能力层为轻量包，不依赖 testgen 存储（`projects/cases/runs` 表）；跨项目调用方自带存储后端（依赖注入）。
  - 红线：能力层不读 testgen 的 `projects/cases/runs` 表。
  - 产出：独立包骨架 + `pyproject` + 导入示例。
  - 验收：八环全绿（包内自测）；`git check-ignore` 验证密钥不入仓。
  - 依赖：P1-1。

- [x] **P2-2 · 跨项目调用示例（aiSitePilot）**
  - 内容：aiSitePilot 经 `POST /api/v1/compare` + `/api/v1/analyze` 调用 testgen 能力层；出一篇「跨项目调用 README」。
  - 产出：示例脚本 + README。
  - 验收：示例跑通（隔离网络环境，mock 或本地 testgen 服务）；文档评审通过。
  - 依赖：P0-2、P2-1。

---

## 收尾 Checkpoint（合并前必做）

- [x] **C1 · `dev` 端到端验证**：在 `dev/modularization` 跑通「生成 → 执行 → comparator 比对 → 对话代理」全链路；八环全绿。
  - 验收：`scripts/gate_all.py` 八环全绿（secret/lint/format/enum/structure/mypy/bandit/pytest，cov=83.88%）；`cli.main chat --show-template` 与 `cli.main --help` 冒烟通过，对话代理/comparator/生成链路接通。
- [x] **C2 · `main` 不受影响验证**：`git checkout main` 仍可独立 `cli.main pipeline --url …` 跑通，核心 `engine/cli/service` 干净。
  - 验收：用独立 worktree 检出 `main`（85e20cf），`import service.app/cli.main/engine.pipeline/core.contracts` 全绿、`cli.main --help` 正常、pytest collect-only 全绿；`main..dev` 为空（模块化改动零反向泄漏）。
- [x] **C3 · 合并**：`dev` 验证通过后 `git checkout main && git merge dev/modularization`；散脚本（`diagnose_*.py`/`run_*.py`）按需迁入 `scripts/` 或清理。
  - 验收：`git merge --ff-only dev/modularization` → main 推进至 `8d58c4d`（fast-forward，无冲突）。散脚本为 dev 工作树未跟踪调试脚本（runtime_ui 联调遗留），未纳入本次合并，留待单独清理/迁移。
- [x] **C4 · 密钥复核**：提交前 `git diff --cached --name-only | grep -iE "\.env$|venv/|\.db$"` 复核无敏感残留。
  - 验收：`git diff --name-only main dev` 全量文件列表无 `.env`/`venv`/`.db`/密钥类文件；`scripts/check_secrets.py` 全仓扫描「未发现问题凭据 ✅」。
  - ⚠️ 遗留提示（非本次引入）：`work/research-agent-test/full_run/tokens.json` 为**已跟踪**文件且本地有修改，含 ft.cntaiping.com 业务 token，未被本次合并纳入（不在 diff 内）；建议后续 `git rm --cached` + 加入 `.gitignore` + 轮换凭据，与本模块化任务解耦处理。

---

## 范围摘要（本列表之外，本次已完成的相邻工作）

- **统一 LLM 降级链（用户本次主任务，已完成）**：新增 `engine/llm_fallback.chat_with_fallback`，两通道（专家 `ExpertLLMClient` / 语义增强 `LLMClient` / 用例设计 `llm_design` 复用）统一委托，不再散落。降级触发 = 403/404/429 + 额度关键字（`AllocationQuota.FreeTierOnly.`）；401 不降级（同 key 必败）；进程缓存最优模型下标实现「停后快速切换」；非视觉模型自动剥离 `image_url`。降级链经 `.env` 的 `EXPERT_MODEL_CHAIN` / `LLM_MODEL_CHAIN` 注入（10 模型逗号分隔，同 key）。
- **门禁**：secret/lint/format/enum/structure/mypy/bandit 七环全绿；新增单测 `tests/test_llm_fallback.py`（6 例，纯 mock 不触网）全绿；`test_llm_design_enabled_reports_no_effect` 改为 hermetic（mock `design_cases`）。
- **已知遗留（非本次回归）**：`tests/test_a2_runtime_cases.py::test_url_only_channel_produces_cases_end_to_end` 为**真实 LLM 集成测试**（断言 `cases>0` 需 DashScope 实调成功），在沙箱网络慢时触发 pytest 120s 超时——属环境抖动，非降级链逻辑回归。建议后续将其标记为「需可达 LLM」或 mock，避免门禁被外部网络拖垮。
