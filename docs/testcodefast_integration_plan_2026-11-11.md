# testCodeFast → testhub 整合方案（G1–G11 规划）

> 状态：**方案文档（待确认）**。本文只做规划与设计，不触碰任何实现文件（不动 .py/.js/.html），待你确认后逐批实施（含代码与自测）。
> 日期：2026-11-11
> 依据：源仓库 `C:\Users\EDY\WorkBuddy\testCodeFast\` 的整合文档集（M0–M4 阶段报告、跨项目重叠分析、独有功能补位规划、P0–P3 执行确认书）+ 本仓库 `apps/testgen_integration/` 现有测试与代码实测。

---

## 1. 目标与现状盘点

**整合目标**：把 testCodeFast 的「代码 → 测试资产生成」能力并入 testhub，testgen 退守为**生成微服务**（scan→fp→tp→case + 安全专项），执行/报告/缺陷/资产/性能/对话下沉或复用 testhub，消除 O1–O10 的重复实现。

**已完成（实证，非推断）**：

| 阶段 | 内容 | 落地位置 | 证据 |
|------|------|----------|------|
| M0 环境/依赖 | testgen-service 迁入 `services/testgen`，独立 venv，阿里云镜像，探活通过 | `services/testgen/` | 八环门禁全绿 |
| M1 迁移适配与映射 | 生成链迁至 testhub 运行，映射维度→test_type、P0–P3→priority、`tg:<tc_no>` 幂等键 | `apps/testgen_integration/mapping.py` | `test_mapping.py` |
| M2 安全判定 verdict | 五维安全判定端点（AUTH/PRIV_ESC/BOUNDARY 等） | `apps/testgen_integration/verdict.py` + `views.py` | `test_security_verdict_endpoint.py`、`test_verdict.py` |
| M3 CI/部署/凭证 | testgen 独立 CI（`testgen-gate.yml`），AUTH_TOKEN 双端一致 | `.github/workflows/testgen-gate.yml` | gate 检查 |
| M4 联调验证 | 端到端联调 | — | `test_e2e_m4.py` |
| **G2** | sidecar 可达性 503 友好报错（`classify_sidecar_error` / `is_sidecar_available` / 视图 503） | `apps/testgen_integration/views.py` + `client.py` | `test_g2_g7.py::TestG2SidecarErrors` |
| **G4 / G5** | 执行后安全裁判钩子 + 缺陷直写 payload 契约 | `apps/testgen_integration/hooks.py` + `defects.py` | `test_g4_hook.py`（全测通过） |
| **G7** | Web 平台操作入口（控制台模板渲染） | 模板 | `test_g2_g7.py::TestG7ConsolePage` |

**待办 / 部分落地**：
- G2/G7 残留 skip：`generate` 的 `project_id` 校验与 taskcenter 落库、`TestgenHealthView`、`TestgenTaskStatusView`/`client.poll` 尚未实现（测试显式 `@unittest.skip`，标注「待 G2/G7 批次落地后移除」）。
- 源仓库 `testCodeFast/work/test-accel/` 仍有未迁移模块：`backend/modules/code_analyzer.py`、`diff_analyzer.py`、`gen_test_points.py`、`ADR/ADR-0001-枚举单源与执行层.md` —— 这些是 testgen 前身 test-accel 的静态分析能力；在 testhub 一侧已由 `services/testgen/engine/` 的 `scan.py`/`fp_extract.py`/`diff_tag.py` 等重新实现覆盖，**无需再搬**（避免双源分叉）。

---

## 2. 10 项功能重叠（O1–O10）与处置建议（R1–R10）状态

来源：`testCodeFast/跨项目重叠分析_testgen_vs_testhub.md`。

| # | 重叠领域 | 建议（R） | 当前状态 |
|---|----------|-----------|----------|
| O1 | 用例生成（testgen 从源码 / testhub 从文档） | 保留两套入口，testgen 只产出、testhub 统存 | 生成侧已在 services/testgen；testhub `requirement_analysis` 主力文档通道 |
| O2 | UI 自动化执行（强重叠） | **R1 执行能力外包**，停第二套 Playwright/HTTP 引擎 | **未做**：testgen `runtime_ui/ui_executor/executor` 仍在，未编排调用 testhub `ui_automation` |
| O3 | API 测试执行（强重叠） | R1 同上 | **未做**：testgen `executor.py` 仍独立 HTTP 执行 |
| O4 | 报告导出（pdf/docx/xlsx 双写） | **R3 去重**，testgen 只出 md/json/html，pdf/docx/xlsx 复用 testhub | **未做** |
| O5 | 缺陷建模与来源 | **R4 统一入 testhub `defects`** | **已落地**（G4/G5 + `defects.py`，缺陷回流） |
| O6 | LLM 测试智能 | **R5 抽公共 LLM client** | **未做**：两侧各写 LLM 封装 |
| O7 | PRD/需求→用例 | **R7 收敛到 testhub `requirement_analysis`** | **未做**：testgen `prd_ingest` 默认关，与 testhub 重复 |
| O8 | 性能测试（testgen SKIPPED） | **R6 复用 testhub `perf_testing`** | **未做**：testgen 性能点诚实 SKIPPED |
| O9 | 项目/用例资产库（SQLite vs MySQL 并存） | **R2 对接而非并存** | **部分落地**：`mapping.py` 已建幂等映射，但「关闭 testgen 自建资产库写入」未做 |
| O10 | 差异对比/复核 | 收敛到 testhub `reviews`/`llm_judge` | 部分（verdict 端点已建） |

**testgen 独占差异化能力（合并时必须保留/放大）**：代码级分析（`scan/fp_extract/diff_tag`）、安全专项（`auth_scan`/`tenant_retest`）、生成链编排（`pipeline`/`fp_merge`）。这些 testhub 不具备，是整合的核心卖点。

---

## 3. G1–G11 映射与剩余工作

> 说明：G2/G4/G5/G7/M2/M4 由现有测试文件名与实现实证的；**G1 由工作记忆明确定义**（Django+MySQL 运行环境/DB root 密码）；G6 未在测试名显式出现，按 R-list 推测为 verdict 安全判定端点（已随 M2 落地）。**G8–G11 为基于 R6/R7/R8/R9/R10 的推断映射**，本仓库无权威 G-list 文档，待你确认/校正。

| G# | 内容（推断/实证） | 状态 | 阻断/依赖 |
|----|------------------|------|-----------|
| **G1** | Django+MySQL 运行环境 / DB root 密码（整合基础环境） | ⛔ **阻断中** | 你本轮选「暂不改 MySQL 密码」→ DB 环境未就绪，依赖 MySQL 改密 + 同步 `.env` + 重启栈 |
| G2 | sidecar 503 友好报错 + 生成入口校验（project_id 校验 + taskcenter 落库） | 🟡 部分（503 已测；校验/落库 skip 待补） | 需 G1 环境；taskcenter 落库依赖 `apps/taskcenter` 完整注册 |
| G3 | 资产库对接（映射层幂等写入 testhub TestCase/TestSuite，关闭 testgen 自建资产库写入） | 🟡 部分（映射层已建，R2 收尾待做） | 需确认「关闭 testgen 本地资产库写入」的边界 |
| G4 | 执行后安全裁判钩子 + G5 直写 payload 契约 | ✅ 已落地（全测） | — |
| G5 | 缺陷/安全判定直写 payload 契约（G4 引用） | ✅ 已落地 | — |
| G6 | 安全判定 verdict 端点（推测，随 M2） | ✅ 已落地 | — |
| G7 | Web 平台操作入口（控制台；health/task-status 视图） | 🟡 部分（模板已测；health/task-status skip 待补） | 需 G1 环境 |
| **G8** | **R1 执行能力外包**：testgen `/api/v1/execute` 改为编排调用 testhub `ui_automation`/`api_testing`，停第二套引擎 | ⛔ 未做 | 大块；需 G1 + testhub 执行服务接口稳定 |
| **G9** | **R3/R4 收尾 + R6/R7 收敛**：报告导出去重、性能复用 `perf_testing`、PRD 收敛 `requirement_analysis` | ⛔ 未做 | 依赖 G8 执行下沉后统一出口 |
| **G10** | **R5 公共 LLM client 抽象**：统一 base_url/model/retry/代理/密钥/限流，两侧共用 | ⛔ 未做 | 跨 services/testgen 与 testhub 的重构 |
| **G11** | **R8/R9/R10 固化**：对话入口统一 testhub `assistant`（Dify）、微服务边界固化（`/api/v1/generate` 生成-only + `/api/v1/execute` 编排）、技术栈/运维收敛 + 全量回归验证 | ⛔ 未做 | 依赖 G8–G10 |

---

## 4. G1 阻断现状（重点）

G1 = Django + MySQL 运行环境 + DB root 密码。本轮你确认「**暂不改 MySQL 密码**」，因此：
- 当前 `DB_PASSWORD` 仍是已泄露的弱口令（还原值），MySQL 环境虽本机可用但**凭据未止血**；
- testhub 整合所需的「真实运行环境联调」无法在干净凭据态下进行；
- 同时 G1 也是 #202 凭据轮换的 DB 项与 testCodeFast 整合的共同前置。

**解除 G1 的最小动作**（待你授权）：用 `.env` 现有密码 `ALTER USER` 改密 → 同步 `.env` 的 `DB_PASSWORD` → 重启服务栈。此动作同时推进 #202 与 G1。

---

## 5. 建议执行顺序（分阶段，待确认后实施）

**阶段 A — 解除 G1（前置，需你授权 MySQL 改密）**
1. 备份当前 `.env` 与 MySQL 库；
2. `ALTER USER 'root'@'localhost' IDENTIFIED BY '<新密>'` → 同步 `.env` → 重启 `dev_stack`；
3. 验证 testhub 业务接口读写正常。

**阶段 B — 收尾已开工的 G2/G3/G7（低风险，纯 testhub 侧）**
4. 补 G2 残留：`generate` 的 `project_id` 校验 + taskcenter 落库（先确认 `apps/taskcenter` 完整注册）；
5. 补 G7 残留：`TestgenHealthView` / `TestgenTaskStatusView` + `client.poll`；
6. 收尾 G3/R2：确认映射层幂等写入，评估「关闭 testgen 本地资产库写入」的开关。

**阶段 C — 执行能力外包 G8（最大块）**
7. testgen `/api/v1/execute` 改为编排调用 testhub `ui_automation`/`api_testing`；保留生成自校验最小 dry-run；
8. 保证 testgen 不再维护独立 Playwright/HTTP 执行引擎。

**阶段 D — 收敛 G9/G10/G11（去重与固化）**
9. G9：报告导出去重（testgen 只出 md/json/html）、性能复用 `perf_testing`、PRD 收敛 `requirement_analysis`；
10. G10：抽象公共 LLM client，两侧共用；
11. G11：对话入口统一 `assistant`、固化微服务边界、技术栈收敛 + 全量回归（含本次 `FULL_REGRESSION_REPORT` 的 99 路由复跑）。

---

## 6. 风险与注意

- **G1 是硬前置**：不解决 MySQL/DB 环境，B/C/D 的联调无法在干净态完成。
- **双源分叉风险**：`testCodeFast/work/test-accel/` 的 `code_analyzer.py`/`diff_analyzer.py` 已被 `services/testgen/engine/` 重新实现覆盖，**不应再搬**，否则出现两套静态分析逻辑。
- **跳过项即技术债**：G2/G7 当前用 `@unittest.skip` 占位，需在 B 阶段补实，否则控制台 health/task-status、生成落库存在功能空缺。
- **凭据红线**：services/testgen 与 testhub 的 `AUTH_TOKEN`/`TESTGEN_AUTH_TOKEN` 必须双端一致（本次已修复代理令牌注入，见 `ui_verify/FULL_REGRESSION_REPORT.md`）；任何迁移脚本禁止把 `.env`/`.db` 纳入 git（`.gitignore` 已拦截）。
- **不动 testCodeFast 源仓库**：本文为 testhub 侧规划，源仓库仅作只读参考。

---

## 7. 需你确认的关键点

1. **G-list 权威性**：本仓库无 G1–G11 的权威文档，G8–G11 为基于 R6–R10 的推断，请校正或提供原始 G-list。
2. **G1 是否解除**：是否授权阶段 A 的 MySQL 改密（解除 #202 DB 项与 G1 共同前置）。
3. **执行下沉范围（G8）**：是否同意 testgen 停第二套执行引擎、改编排调用 testhub（这是最大架构决策）。
4. **test-accel 模块处理**：确认 `work/test-accel` 的静态分析模块不再迁移（已由 engine 覆盖）。

---

*以上为方案，待你确认后我再逐批实施（含代码与自测）。*
