# TestHub / testgen 控制台交付就绪评估（补充测试 + 退出标准）

> 日期：2026-11-16 · 角色：测试专家
> 背景：针对 2026-11-16 评估中「哪些测试在范围内但未执行」的补齐结果。
> 证据：真实 Edge + Playwright，直连 Django `/testgen/`（带 session）；测试产物在 `ui_verify/`。

## 0. 结论

| 判定 | 结论 |
|---|---|
| 补充测试范围 | ✅ 全部执行完毕（P0 16 条、P1 8 条、P2 8 条，合计 32 条） |
| 实质性产品缺陷 | ✅ 发现的 1 个真实缺陷（远程 SSH 扫描强制 PAT）**已修复并回归通过**，见 §2 |
| 可交付性 | 内部演示/试点：可交付；正式验收：原 P1 阻塞项已消除，待 §4 退出标准与产品签收 |

## 1. 补充测试执行结果

### 1.1 P0 — 远程 Git 模式未执行用例（16 条，14 PASS / 2 FAIL）

| 用例 | 结果 | 实测证据 |
|---|---|---|
| B3-1 指定目标 ref 可选 | ✅ | 分支下拉含 `feat/p1-closure` 等 5 项 |
| B3-2 指定 ref 生成任务成功 | ✅ | task=success, project_id=23 |
| B3-3 生成后出现报告链接 | ✅ | `#report_link` 可见 |
| C2-1 增量模式可切换 + 基线可设 | ✅ | mode=incremental, base=历史 commit |
| C2-2 增量扫描识别变更 | ✅ | 新增 0 / 修改 3 / 未变更 397（本地 git 仓库真实 diff） |
| C2-3 增量自动只勾选变更文件 | ✅ | 选中 3，其中变更 3 |
| C2-0 远程 SSH 扫描凭证缺陷探测 | ❌ → ✅ 已复测 | 修复前为缺陷证据；修复后同仓库扫描成功，见 §2 回归 |
| D3-0 无存凭证时 stored 选项禁用 | ✅ | 正确设计：无凭证不允许选"使用配置凭证" |
| D3-1 manual 空 key 给出提示 | ✅ | `已选「手动输入 PAT」，请填写只读凭证 access_key` |
| E1-1~4 配置/测试连接/回填/取分支 | ✅ | 连接成功（1 ref）、回填 URL 正确、5 分支 |
| E1-5 远程只读扫描 | ❌ → ✅ 已复测 | 修复后 UI 扫描→勾选 400 文件，见 §2 回归 |
| E1-6/7 完整串联生成 + 报告 | ✅ | task=success，报告链接可用 |

> 说明：C2 增量识别原计划用远程 GitHub 仓库验证，因 §2 缺陷改用本地 git 仓库（research-agent，含 3 个真实 diff），验证的是同一套 UI + 引擎增量逻辑。

### 1.2 P1 — 主流程下游（8 条，全部实质通过）

| 用例 | 结果 | 实测证据 |
|---|---|---|
| P1-1 测试点列表加载 | ✅ | 9134 测试点，渲染 500 行上限 |
| P1-2 范围筛选（安全） | ✅ | 精检确认：筛选后 500 行全为"安全"；未筛选时四类各 125 均匀分布 |
| P1-3 关键词搜索 | ✅ | `get_day_bounds` 命中 5 条 |
| P1-4 用例导出数据完整 | ✅ | API 返回 9134 条，UI 导出所需 9 个字段全部有值（缺失数 0） |
| P1-5 报告页可打开 | ✅ | HTTP 200，标题 `测试报告 · AUTO-P0-B3-GitRef（#23）` |
| P1-6 任务列表 | ✅ | 21 行，状态/阶段/项目列正常 |
| P1-7 运行中任务可取消 | ✅ | 取消后 state=cancelled |
| P1-8 重复提交（幂等） | ✅ | 任务数 25→26，重复提交被接受，无重复项目 |

> 脚本层两处断言初判 FAIL（导出用 headless download 事件超时、筛选用了错误的选择器），已用 API 直验 + 独立精检脚本澄清为**产品能力正常**。

### 1.3 P2 — 非功能测试（8 条，8 PASS）

| 用例 | 结果 | 实测证据 |
|---|---|---|
| SEC-1 未登录访问被拒 | ✅ | HTTP 403（fail-closed） |
| PERF-1 首屏加载 | ✅ | 526ms（< 8s） |
| COMPAT 桌面 1440×900 / 窄屏 900×700 | ✅ | 关键控件（源类型/仓库/生成/导航）均可见 |
| SEC-2 XSS 转义 | ✅ | `<img src=x onerror=...>` 被转义为文本，无注入元素 |
| SEC-3 Codeup key 掩码 | ✅ | 4 条仓库配置无明文 key 回显 |
| EXC-1 API 500 容错 | ✅ | UI 未崩溃，控件仍可用（提示文案未精确断言） |
| EXC-2 超大输入 | ✅ | 5000 字符填入 0.05s，无假死 |

> 兼容性限制：本机仅装 Edge，未覆盖 Chrome/Firefox 引擎差异；以上为 Edge 内核双分辨率验证。

## 2. ✅ 真实缺陷 DEF-20261116-01（已修复）

### DEF-20261116-01｜远程 GitHub/Git SSH 模式无法「先扫描再勾选范围」

- **严重级别**：中（P1）——不阻断"直接生成"，但使"扫描→勾选范围→生成"这条精细化流程在 SSH 模式下不可用。
- **现象**：远程 Git SSH 仓库（如 `git@github.com:...`）点「🔍 扫描」报 `HTTP 422 远程扫描：凭证（access_key）为空`，扫描区无文件；但同一仓库直接点「生成用例」能成功（B3 project_id=23，9134 条用例）。
- **根因**：两条路径取码方式不一致——生成走 `pipeline.stage_pull → pull.pull()`（`git clone` 复用本机 SSH key，无需 PAT）；快速扫描走 `app._dispatch_scan → scan_repository()`（只实现 Codeup HTTP 客户端，强制 `access_key`）。
- **影响**：用户用 SSH 仓库时无法享受"扫描后按文件勾选生成范围"和"增量基线对比"的 GUI 流程，只能全量生成。

### 修复实现（采用方向 A）

| 文件 | 改动 |
|---|---|
| `services/testgen/engine/remote_scan.py` | 新增 `scan_git_repo()`：通用 git 仓库浅克隆（`--depth 1`）到系统临时目录 → 复用 `scan_local()` → 临时目录自动清理；支持分支/标签/完整 40 位 SHA 的 `ref`/`base_ref`，补取基线后做增量标注；git 子进程禁用交互凭据提示，复用本机 SSH key / credential helper；失败抛 `RuntimeError` 由端点统一回报。 |
| `services/testgen/service/app.py` | `_dispatch_scan()` 改为三类分派：本地目录 → `scan_local`；Codeup 地址（含域名或显式 PAT）→ 原只读 HTTP API（不克隆、仍需 PAT）；通用 git 地址（`git@`/`ssh://`/`https://*.git`）→ `scan_git_repo`，**不再强制 PAT**。`source` 字段保持 `"remote"` 不变，前端无感。 |
| `services/testgen/tests/test_scan_git_repo.py` | 新增 7 条回归单测：浅克隆扫描与符号解析、分支/SHA 基线增量标注、临时目录清理、失败抛错、三类分派。 |

- **安全/边界**：临时目录用完即清理，不在工作区留克隆；`.git` 本就在扫描排除目录；Codeup 路径及无 PAT 时的 422 提示保持原样，未降低鉴权要求；未改动任何 key 相关配置。

### 回归验证

| 层级 | 结果 |
|---|---|
| 单测 | 7/7 PASS（`services/testgen/.venv` 的 pytest） |
| API E2E（真实 SSH） | `POST /api/v1/scan`（`git@github.com:lidebao513/testHub.git`，ref=`feat/p1-closure`）→ HTTP 200、`ok=true`、400 个文件（过滤后 611，截断如实上报）、32 个文件解析出符号，**不再 422** |
| API E2E（增量基线） | 加 `base_ref=main` → HTTP 200；两分支实际差异仅 3 个文件且都在 `tests/`/`docs/` 噪声目录且均为删除，故 0 变更是正确结果；增量标注能力由单测用真实 git diff 覆盖（added/modified 正确） |
| UI 回归 | `verify_git_scan_fix_2026-11-16.py`：登录 → 填 SSH 地址 → 扫描 → 400 文件 → 勾选范围，**4/4 PASS**；截图 `ui_verify/shots/git_scan_fix_2026-11-16/ssh_scan_files.png` |
| 静态检查 | ruff lint + format --check 对 3 个改动文件均通过 |

> 服务重启：sidecar（:8910）是长进程且 `reload=False`，已通过 `testhub-dev-stack` 计划任务重启后验证，确保跑的是新代码。

### 门禁说明

**八环门禁全部通过（8/8，含 pytest）**——`gate_all.py` EXIT=0，日志 `ui_verify/gate_all_after_auth_fix.log`。

此前 pytest 环 32 条失败（`assert 401 == 200` / `KeyError`）根因与修复：
- **根因**：本地 `tests/conftest.py` 用 `os.environ.pop("AUTH_TOKEN")` 制造"无令牌"测试环境，但 `get_settings()` 里的 `load_dotenv(override=False)` 会把本地 `.env` 的真实 `AUTH_TOKEN` 回填到这个"缺失"的键，使 `settings.auth_token` 非空 → 所有不带令牌的用例被 401 拒绝 → 响应体缺字段引发 `KeyError`。CI 无 `.env`，且 workflow 显式设 `AUTH_DISABLED=1`，故从未暴露。
- **修复**：`tests/conftest.py` 改为 `os.environ["AUTH_TOKEN"] = ""` + `os.environ["AUTH_DISABLED"] = "1"`——"存在但为空"阻止 dotenv 回填（同 `test_llm_budget.py::usage_tmp` 注释的既有模式），逃生口与 CI 对齐。仅改测试隔离配置，**未改动任何 key / `.env` / 生产代码**。
- **验证**：全量 pytest EXIT=0（约 720 条，0 失败）、八环门禁 EXIT=0。日志：`ui_verify/pytest_after_auth_fix.log`、`ui_verify/gate_all_after_auth_fix.log`。

## 3. 数据清理

- 本轮生成的项目 **#22 / #23** 及关联 12,858 测试点 + 12,858 用例 + 功能点等已清理；清理前 DB 已备份至 `ui_verify/testgen_db_backup_2026-11-16.db`（92MB）。
- 任务表无 pending/running 僵尸任务；本轮产生任务均处于 cancelled/failed/success 终态。
- 测试仓库配置 `AUTO-P0-GitMode-20261116`、`AUTO-GitMode-Test-20261116`、XSS 测试仓库均已删除。

## 4. 交付退出标准（草稿，待产品确认）

一项功能进入"可交付"需同时满足：

1. **功能闭环**：主流程（配置→取码→生成→报告→下游查看/导出）端到端通过，核心分支有真实数据验证。
2. **缺陷门槛**：无 P0/P1 未修复缺陷；P2 缺陷须有明确 workaround 并写入用户文档。（DEF-01 已修复并回归通过，当前无 P0/P1 未修复缺陷）
3. **测试覆盖**：测试计划中列出的用例 100% 执行（不允许"计划 13 条跑 8 条即宣布通过"）。
4. **非功能基线**：首屏 < 8s；XSS/越权/凭据回显安全项通过；至少两种视口可用。
5. **回归资产**：关键 UI 脚本纳入版本控制并可重复执行（见 §5）。
6. **数据整洁**：验收环境无测试残留项目/任务/配置。
7. **签收**：产品/业务方对以上 exit criteria 与结果签字确认。

## 5. 关键回归脚本清单（建议入库）

| 脚本 | 用途 |
|---|---|
| `ui_verify/verify_probe_result_reset_2026-11-16.py` | 测试连接结果区残留回归（6/6） |
| `ui_verify/git_mode_ui_test_2026-11-16.py` | 远程 Git 模式 8 场景 |
| `ui_verify/p0_git_mode_full_2026-11-16.py` | B3/C2/D3/E1 完整用例 |
| `ui_verify/p1_downstream_2026-11-16.py` | 下游 + 取消/幂等 |
| `ui_verify/p2_nonfunctional_2026-11-16.py` | 性能/兼容性/安全/异常 |
| `ui_verify/verify_git_scan_fix_2026-11-16.py` | DEF-01 修复回归：SSH 扫描→勾选范围（4/4） |
| `services/testgen/tests/test_scan_git_repo.py` | DEF-01 引擎层回归单测（7/7，新增文件，尚未提交） |

> 按项目惯例 `ui_verify/*` 不入库；如需纳入 CI 回归，需单独确认后移入受控目录并调整断言的稳健性（部分断言依赖固定等待/下载事件）。

## 6. 产物索引

- P0 日志/截图：`ui_verify/p0_gitmode_run.log`、`ui_verify/shots/p0_gitmode_2026-11-16/`
- P1 日志/截图：`ui_verify/p1_downstream_run.log`、`ui_verify/shots/p1_downstream_2026-11-16/`
- P2 日志/截图：`ui_verify/p2_nonfunc_run.log`、`ui_verify/shots/p2_nonfunc_2026-11-16/`
- 精检脚本：`ui_verify/p1_filter_check.py`
- 原始 API 证据：`ui_verify/scan_probe.json`、`p23_tp.json`、`p23_cases.json`、`p23_report.html`、`tasks_snapshot.json`
- DEF-01 修复证据：`ui_verify/test_scan_git_repo.log`（7/7）、`scan_after_fix.json`、`scan_incremental_after_fix.json`、`verify_git_scan_fix_run.log`、`shots/git_scan_fix_2026-11-16/`、`gate_all_after_fix.log`
