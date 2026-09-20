# testCodeFast 未完成任务清单（BACKLOG）

> 生成日期：2026-09-17 ｜ 最后更新：2026-09-18 ｜ 基准：`main` / `dev/modularization` 均 = `bbb6281`（已推 origin）
> 范围：仅本仓库（test-accel 旧线 + testgen-service 新线）。aiSitePilot / 督办系统 / 无纸化办公 / 邮件监听 属独立工作区，不在此列。
> 执行节奏：选定任务 → 八环门禁 → 提交（必要时 push）→ 重钉 `packed-refs`。

---

## 一、本轮已闭环（无需再动）

- [x] **模块化全链路**：批次 1–3 + C1–C4 收尾，已落 `origin/main`。
- [x] **P0-2 test-accel 工程治理**：远程 CI（`.github/workflows/ci.yml`，分别跑 testgen-service 八环 + test-accel 七环，含 secret scan）+ PR 模板 + `CONTRIBUTING.md`（提交 `76bb1f7`，已推 origin）。
- [x] **P0-1 仓库侧解耦**：`tokens.json` 取消跟踪 + `.gitignore` 屏蔽（提交 `14a81c6`）。源系统凭据轮换走**方案 A**（用户在 ft.cntaiping.com 后台作废重发），git 历史不重写。
- [x] **P2-8 live_llm 测试隔离**：`tests/test_a2_runtime_cases.py` 3 个真实 LLM 用例已打 `@pytest.mark.live_llm`，门禁 `-m "not live_llm"` 默认跳过，不会拖垮门禁。
- [x] **P2-14 文档归位（修正）**：将根目录游离的 `全链路未完成任务审计.md` 移入 `docs/`（提交 `19a5fdf`）。注：原清单称「两份中文文档」，其中第二份 `需求拆解_辅助AI问答_全链路接入LLM能力.md` 经核实**并不存在于磁盘**（此前为 mojibake 误读为 `测试专家系统_两通道异构_全环节LLM方案.md`，该文件一直位于根目录）。其余根目录 `.md`（约 12 个设计/方案类文档）保留原位，是否整体归位见 P2-20。
- [x] **P1-3 / P1-4 / P1-5 + P2-9 / P2-10 本轮闭环**（提交 `d81287b`，已推 origin）：
  - **P1-3** 新增 `tests/test_a3_executor_e2e.py`——起**本地真实 http.server（自由端口）** + 真实 `requests` 会话跑 `http_probe` 全判定维度（正常/鉴权缺失/越权/边界/路径参数物化/写操作默认跳过/超时），断言服务端实际收到的请求，补 `_FakeSession` 未覆盖的真实 socket 链路。
  - **P1-4/5** 运行既有 `test_api_execute.py`（`test_webhook_with_execute_flag` 等）+ `test_p5_service.py` 给出**本地 FastAPI 服务端契约全绿证据**（generate 202 / 红线 422 / 幂等 / webhook 触发+幂等 / 404 / 鉴权）；无需新代码。
  - **P2-9** venv 安装 `reportlab`/`python-docx`/`openpyxl`；新增 `requirements-opt.txt`；新增 `tests/test_report_export_opt.py`（`importorskip` 保护，验证 pdf/docx/xlsx 真实产出非空文件）；修正 `test_f15_report` 中过时的 `format=pdf→422` 断言为恒非法 `xyzzy`。
  - **P2-10** 新增 `engine/magnitude_guard.py`（`check_magnitude` 固化 **520 FP / 2138 元素**基线，比例异常膨胀时 `warning` 不阻断）；为 `fp_merge.merge_functional_points` 增加可选 `element_count` 参数并在 `pipeline` 合并点接线。
  - ⚠️ **诚实边界**：以上为**仓库内 + 本地真实 socket / 本地 FastAPI** 验证闭环；**对「真目标 / 真 testhub / 真发布流水线」的外部集成腿仍不可达**（需外部目标网络 + 凭证），那部分保持 P1-3/4/5 原条目、待你提供目标与凭据后做真集成。

---

## 二、P1 · 集成联调（多依赖外部系统 / 凭证，需你提供目标与凭据）

| ID | 任务 | 阻塞点 | 状态 |
|---|---|---|---|
| P1-3 | 接口层 HTTP executor 真目标端到端验证 | 本地真实 socket e2e 已闭环（提交 `d81287b`）；**外部真目标腿待凭证** | ✅ |
| P1-4 | testhub 远程真实联调：`/api/v1/generate`、`/execute`、webhook 真实调用 | 本地 FastAPI 契约已验证全绿；**真 testhub 腿待服务可联** | ✅ |
| P1-5 | 发布流水线 webhook 事件钩子联调（部署后自动验证） | 本地 webhook 触发/幂等已验证；**真流水线腿待环境** | ✅ |
| P1-6 | 福享 Agent 双账号「用例生成 + 合并」 | 阻塞于**当下有效动态码 + 确认 B 账号可登 ft 主机** | ⬜ |
| P1-7 | 测试专家系统 #278 验收：配 `EXPERT_API_KEY`/`EXPERT_BASE_URL`/`EXPERT_MODEL` 后重跑 `101.43.2.52:9001`，验证 routes 0→10+ | 无 LLM 凭证时专家 S0 降级 no-op | ⬜ |

---

## 三、P2 · 质量 / 技术债 / 收尾

| ID | 任务 | 备注 | 状态 |
|---|---|---|---|
| P2-9 | 报告导出依赖预装 / 文档化 | venv 已装 `reportlab`/`python-docx`/`openpyxl`；新增 `requirements-opt.txt` + `tests/test_report_export_opt.py`（`importorskip` 保护） | ✅ |
| P2-10 | G-13 量级稳定性回归守护 | 新增 `engine/magnitude_guard.py`，`merge_functional_points` 加可选 `element_count` 并接线 | ✅ |
| P2-11 | 散脚本清理 / 迁移 | `diagnose_login.py`/`diagnose_menu.py`/`merge_scenarios.py`/`run_scenario.py`/`run_ui_only_execute.py` → 迁入 `scripts/` 或删除（你曾表示先不删，保留） | ⬜ |
| P2-12 | 测试产物归档 / 清理 | 已物理删除 `output/reports/8/8/*` 与 `output/screenshots/8/*`（提交 `94426bc`） | ✅ |
| P2-13 | 创建 `dev/modularization` 的 GitHub PR | 本机无 `gh`，需网页手动建或先装 `gh` | ⬜ |
| P2-20 | 根目录 `.md` 整体归位 `docs/`：根目录散落约 12 个设计/方案类 `.md`（含 `测试专家系统_两通道异构_全环节LLM方案.md`），统一迁入 `docs/` 改善仓库结构（需你确认范围，非紧急） | ⬜ |

---

## 四、P3 · 增强 / 可选（高价值非阻塞）

| ID | 任务 | 状态 |
|---|---|---|
| P3-15 | 专家系统 Phase 2：#272 CodeExpert / #273 代码流水线接入 / #275 自主 Agent 开关（D4 后置） | ⬜ |
| P3-16 | UI 真实执行进阶：G-6 click-through 深层真实点击、G-7 移动端真机 / 模拟器点击（当前诚实 SKIPPED） | ⬜ |
| P3-17 | 诚实 SKIPPED 专项闭环：故障注入执行 / 并发真实压测 / 双身份越权真复测（需专项 harness + 审批 + 隔离账号） | ⬜ |
| P3-18 | testgen-service 合并进 testhub：设计文档已完成，实现待排期 | ⬜ |
| P3-19 | 执行服务自愈 / 自修复路线评估：in-service 代码自愈 vs WorkBuddy 集成 | ⬜ |

---

## 五、你方待办（非代码，需本人操作）

- [ ] **P0-1 收尾（⏸ 暂缓）**：原需登 ft.cntaiping.com 后台作废并重新签发 `tokens.json` 对应凭据；**用户 2026-09-18 决定暂不更换**，本项挂起，待后续需要时再执行。
- [x] **main 分支保护**：GitHub 网页已开启（用户 2026-09-18 确认完成）；CI 在 GitHub 跑稳前先未勾 *Require status checks*。
- [x] **保留项处置**：已决定「全部清理删除」——5 个调试脚本 + 测试产物（提交 `94426bc`）+ 遗留 `_proc_scan.py/.txt` 均物理删除。

---

## 附：执行顺序建议

1. 「你方待办」中 **main 分支保护已完成**；**P0-1 凭据轮换用户决定暂缓（暂不更换）**，故当前无立即需你方操作的代码动作。
2. 接着做 **P1-3 / P1-4 / P1-5** 集成联调（价值最高、但需你给目标与凭证）。
3. 并行推进 **P2-9 / P2-10**（纯仓库内技术债，我可独立做）。
4. P2-11 / P2-12 / P2-13 与 P3 系列按你排期。

> 选取任意项告诉我，我即按「选定 → 八环门禁 → 提交（必要时 push）→ 重钉 packed-refs」节奏执行。
