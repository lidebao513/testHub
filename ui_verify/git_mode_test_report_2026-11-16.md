# 远程 Git 模式 UI 测试报告

> 测试专家视角 · 2026-11-16 16:42 · 范围：testgen 控制台远程 Git 操作页
> 实测环境：真实 Edge + Playwright，直连 Django `/testgen/?embed=1&zone=generate`（带 session）
> 被测仓库：`git@github.com:lidebao513/testHub.git`（本机 SSH key 可用，4 分支）

## 一、合理性检查（先于用例设计）

用户提出的 5 类场景中，**3 类因系统只读设计不适用**，据理排除，不为其编造用例：

| 场景 | 判定 | 依据 |
|---|---|---|
| 克隆 clone | ❌ 排除 | 系统红线「远程只读取码、不克隆」；无 clone 端点 |
| 推送 push | ❌ 排除 | 纯只读系统，无任何写远端端点 |
| 冲突处理 | ❌ 排除 | 无本地 working tree 与 merge，冲突无从产生 |
| 拉取 pull（=只读获取） | ✅ 适用 | 映射为获取分支 / 只读扫描 / 指定 ref 生成 |
| 分支切换 | ✅ 适用 | 目标 ref / 基线 ref 下拉 + 获取分支 + 阶段对比 |
| 操作组合 + 异常 | ✅ 适用 | 配置→测试→回填→取分支闭环；空/无效地址 |

证据：后端仅 `scan/branches/commits/stage-*` 等只读端点；`list_git_branches` 用 `git ls-remote --heads` 不落盘；`/api/v1/repos` CRUD 仅管理配置。

## 二、测试用例与结果（每个合理场景少量代表性用例，共 8 条）

| 用例 | 场景 | 结果 | 实测证据 |
|---|---|---|---|
| A1 新建仓库配置 | 配置管理-正常 | ✅ PASS | 保存后列表含测试仓库 |
| A2 测试连接（有效 SSH） | 配置管理-正常 | ✅ PASS | `✅ [git] 连接成功（读到 1 个 ref）（4994ms）` |
| D1 空地址拦截 | 异常 | ✅ PASS | `alert=请先填仓库地址`，结果区空，不误报 |
| D2 无效地址报错 | 异常 | ✅ PASS | `❌ [git] 连接失败：and the repository exists.（3804ms）` |
| B1 获取远程分支 | 拉取-正常 | ✅ PASS | 5 个选项：main / feat/p1-closure / fix/... / chore/... |
| C1 切换目标分支 | 分支切换-正常 | ✅ PASS | 成功选中 feat/p1-closure |
| B2 远程只读扫描 | 拉取-正常 | ✅ PASS | 扫描区出现模块/勾选提示，不克隆 |
| E1 配置→控制台回填 | 操作组合 | ✅ PASS | 填到控制台后 URL+源类型(github)正确回填 |

**结果：8/8 通过，失败 0。**

## 三、测试中发现的问题

### 真实缺陷 / 体验问题
1. **（低优先级 UX，已修复）测试连接结果区状态残留**：D1 场景中，先测有效地址得到绿色"连接成功"后，清空地址再点测试连接，虽然正确弹出 `alert("请先填仓库地址")` 且不发请求，但**结果区仍保留上一次的绿色"连接成功"文本**。用户若忽略弹窗，可能被旧的成功状态误导。
   - **修复（2026-11-16 17:03）**：`services/testgen/service/web/index.html` 的 `probeFormRepo()` 与 `probeFormLLM()` 在校验失败分支先清空对应结果区再 `alert` + `return`；两个表单同病同治，各 1 行改动。
   - **回归验证（6/6 PASS）**：`ui_verify/verify_probe_result_reset_2026-11-16.py`（日志 `ui_verify/probe_reset_run.log`）。Git：真实 SSH 有效连接读到 26 个 ref（绿色成功）→ 清空地址再点测试连接 → 结果区为空、alert=`请先填仓库地址`；LLM：模拟旧成功状态 → 清空 Base URL 再点测试连接 → 结果区为空、alert=`请先填 Base URL`。截图 `ui_verify/shots/probe_reset_2026-11-16/`。

### 测试脚本层问题（已修复，非系统缺陷）
- A2/D2 初判 FAIL 系固定等待短于实际探测耗时（3.8~5s），改为轮询后均 PASS。
- 通过 SPA iframe 访问偶发 403（refresh token 401 清登录态），改为直连 `/testgen/`（带 session）稳定复现。
- E1 初版选择器 `#repo_preset` 在 console 视图隐藏，改用列表「填到控制台」链接。

## 四、清理
- 测试仓库 `AUTO-GitMode-Test-20261116` 已在 UI 中删除，未污染配置库。
- 仅改动 `services/testgen/service/web/index.html` 两行（结果区清空），未触碰 key 相关任务。

## 五、产物
- 测试计划：`ui_verify/git_mode_test_plan_2026-11-16.md`
- 执行脚本：`ui_verify/git_mode_ui_test_2026-11-16.py`
- 最终日志：`ui_verify/gitmode_run_final.log`
- 截图：`ui_verify/shots/gitmode_2026-11-16/`（10 张）
- 修复回归：`ui_verify/verify_probe_result_reset_2026-11-16.py`、`ui_verify/probe_reset_run.log`、`ui_verify/shots/probe_reset_2026-11-16/`

> 全部产物未入库（ui_verify/* 按惯例不提交）；实现代码的 2 行修复尚未提交。
