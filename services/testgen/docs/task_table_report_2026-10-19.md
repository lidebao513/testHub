# testgen 任务表整体更新报告（2026-10-19）

> 对象：`services/testgen` 的 `gen_tasks` 任务表（共 26 条）。
> 动作：已解决任务迁出并归档至独立文件；未完成任务逐条核对并标注状态/优先级。

## 一、本次更新动作

| 动作 | 说明 |
|------|------|
| 全量导出 | 26 条完整记录（含 request/result/error 大字段）已落盘：`data/archive/gen_tasks_full_2026-10-19.json` |
| 独立归档 | 21 条**已解决**任务迁出到独立文件：`data/archive/gen_tasks_resolved_2026-10-19.json` |
| 活动表迁出 | 从 `gen_tasks` 删除这 21 条已解决任务 → 活动表现剩 **5 条未完成**（均为 failed） |
| 还原保障 | 全量 JSON + DB 备份 `data/testgen.db.bak-20261019`（另有 10-10 增量备份）→ 删除可一键还原 |
| 配套修复 | 控制台 `restoreLastTask` 在任务 404 时清理 localStorage，避免迁出后前端永久重试死任务（详见代码 review §E-1） |

## 二、已归档任务清单（21 条 · 已解决）

> 状态分布：17 条 `success` + 4 条 `cancelled`。完整字段见独立归档文件。

| 任务 ID | 状态 | 项目 | 创建时间 |
|---------|------|------|----------|
| gen-c5cf10e6d5ca | success | 2 | 2026-09-21 |
| gen-dcde84c369c7 | success | 3 | 2026-09-22 |
| gen-53c8db1daac5 | success | 3 | 2026-09-22 |
| gen-ce722092d407 | success | 4 | 2026-09-23 |
| gen-b35b3c014d64 | success | 4 | 2026-09-23 |
| gen-6a6eb0facda5 | success | 4 | 2026-10-08 |
| gen-c2d40ce228c2 | success | research-agent | 2026-10-08 |
| gen-639c9033f93a | success | subset_full | 2026-10-09 |
| gen-592f26436456 | success | subset_probe | 2026-10-09 |
| gen-d30c346f46f4 | success | subset_full2 | 2026-10-09 |
| gen-d857c1632699 | success | subset_one2 | 2026-10-09 |
| gen-eb0bac02704f | success | debug_gen | 2026-10-09 |
| gen-88ac87b6bc64 | success | cmp_full | 2026-10-09 |
| gen-f8662987127d | success | cmp_sub | 2026-10-09 |
| gen-be65967e8499 | success | 4 | 2026-10-13 |
| gen-4d10d2805f6d | success | 4 | 2026-10-14 |
| gen-2d4d5b536793 | success | 4 | 2026-10-14 |
| gen-ff61e3e9c223 | cancelled（原 pending，本会话取消） | — | 2026-09-20 |
| gen-745671baeb60 | cancelled | — | 2026-09-22 |
| gen-eb08274e1389 | cancelled | — | 2026-09-23 |
| gen-22edfe3dc1b0 | cancelled | — | 2026-09-23 |

## 三、未完成任务核对（5 条 · 全部 failed）

逐条对比**当前代码与运行情况**，判断是否需要继续完成，并标注状态与优先级。

| 任务 ID | 失败原因（当时） | 当前代码/运行核对 | 是否需要继续完成 | 状态 | 优先级 |
|---------|----------------|------------------|------------------|------|--------|
| gen-ca7da72d5261 | 被测目录不存在 `/srv/code/demo` | 参数校验**正确拦截**（目录确实不存在）；属测试残留 | 否——校验行为正确，无需改 | 已搁置 | 低 |
| gen-416c28f34a18 | 缺少被测来源（空请求） | 参数校验**正确拦截**；属空请求测试残留 | 否——校验行为正确 | 已搁置 | 低 |
| gen-3d89279c8393 | 取码失败 `refs_unavailable`（无凭证） | 根因=当时无 Codeup PAT；现已在 `.env` 配置 `CODEUP_TOKEN` 且 `pull.inject_credentials` 注入 | 否——缺陷已修复；项目 4 已在 10-14 同参成功生成覆盖 | 已搁置 | 低 |
| gen-74e3d9033f2e | ref 不可解析 `oa-20260909/master` | 根因=短分支名未归一化；已修复 `diff_tag.resolve_ref` 归一化为 `origin/<ref>` | 否——缺陷已修复；10-14 `gen-2d4d5b536793` 同参数（base=oa-20260909,target=master）**成功**已验证 | 已搁置 | 低 |
| gen-d3d5365a8416 | 增量基线不可解析 `oa-20260909` | 同上 ref 修复；10-14 同参数成功运行已覆盖 | 否——缺陷已修复且被成功运行验证 | 已搁置 | 低 |

### 核对结论

- **5 条失败均非活跃阻塞**：
  - 2 条（demo 路径、空请求）是**参数校验正确拦截**的测试残留，系统行为正确，无需任何代码改动；
  - 3 条（2026-10-13 的 research-agent 取码/ref 失败）是**修复前的历史缺陷**，关键证据是：**2026-10-14 的 `gen-2d4d5b536793` 用完全相同的参数（`base=oa-20260909`、`target=master`、project 4）已 `success`**——直接证明取码凭证注入与 ref 归一化两项修复均已生效，这 3 条失败任务已被成功运行"覆盖"，无需重跑。
- **当前无"进行中"任务，无待立即处理项。**
- 活动表保留这 5 条 failed 作为历史失败留痕（便于日后追溯 ref/凭证失效形态）；如需活动表彻底干净，可将其一并归档后删除（已有备份可还原）。

## 四、可选后续

1. **状态/优先级持久化**：当前"状态/优先级"以本报告为权威管理视图。若希望写入任务表本体，可给 `gen_tasks` 增加 `status`/`priority` 列并由控制台展示（需配套迁移与 API/UI 改动）。
2. **活动表清零**：5 条 failed 可一并归档删除；还原路径同上。
3. **控制台任务列表增加筛选/分页**：当前 `limit=20`，迁出后体量已很小，非紧急。
