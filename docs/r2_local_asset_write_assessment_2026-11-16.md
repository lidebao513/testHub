# R2 映射幂等 / 关闭 testgen 本地资产写 —— 核验与决策记录（2026-11-16）

> 任务 #239。**决策已定：方案 C**（保留本地表作回流中转，仅文档固化边界）。不含任何实现改动。

## 一、结论速览

| 子项 | 状态 | 结论 |
|---|---|---|
| ① 映射层回流幂等（`tg:<tc_no>`） | ✅ **已完整落地并验证** | 无需改动 |
| ② 关闭 testgen sidecar 本地资产库写 | ✅ **决策已定 = 方案 C** | 保留本地表作中转，不改代码 |

## 二、子项①：映射幂等 —— 已确认完成

实证链路（全部已存在，测试覆盖）：

- `mapping.py`：`TESTGEN_ID_TAG_PREFIX = "tg:"`（:52），`build_tags` 把 `tg:<tc_no>` 写入 tags（:186-196）；`testgen_id_of` 以 `tc_no`/`tp_id` 作稳定指纹（:199）。
- `flow.sync_cases`：无指纹则跳过，有则调注入的 `upsert(tg_id, payload)`（:26-64）。
- 真实落库 `views.py::_upsert`（:225-240）：`TestCase.objects.filter(project_id, tags__contains=[tag]).first()` → 存在则逐字段更新 + `UPDATED`，否则 `create` + `CREATED`。语义即幂等 upsert（用 JSON 标签列代替独立 external_id 列，因为 testhub TestCase 无该字段）。
- 测试：`test_integration.py` 断言二次回流 `r2["updated"] == 1` 且 `tg:TP-A1` 在 tags；`test_e2e_m4.py` 断言 `r2["updated"] == 5`。**本地实跑 `test_integration.py` + `test_mapping.py` = 24 passed。**

**结论**：子项①无需任何代码改动，已标记完成。

## 三、子项②：关闭本地资产写 —— 边界评估与决策

### 3.1 现状数据流（关键约束）

```
testhub 生成请求 ──client._build_generate_payload(无 persist 字段)──▶ sidecar /api/v1/generate
                                                                        │ PipelineRequest.persist 默认 True (app.py:228)
                                                                        ▼ opts.persist = req.persist (app.py:484)
                                                            stage_persist (pipeline.py:1329)
                                                            ├─ persist=True  → store.reconcile_cases 写本地 cases 表 (:1351)
                                                            └─ persist=False → _persist_without_db 仅算计数，不写 (:1338-1340)
                                                                        │
testhub 回流 TestgenSyncView ──client.get_cases(project_id)──▶ sidecar /api/v1/projects/{pid}/cases
                                                            └─ store.list_cases(pid) ← 读的就是本地 cases 表 (app.py:1045-1049)
```

### 3.2 为什么不能简单关

- testhub 回流**唯一数据来源**是 sidecar 本地 cases 表：`views.py:219 cases = get_cases(int(testgen_project_id))`，而 `get_cases` → `GET /projects/{pid}/cases` → `store.list_cases(pid)` 读本地 SQLite 表。
- sidecar 任务结果 `PipelineResult.to_dict()`（pipeline.py:192-212）只含 `counts`/`case_stats`/`traceability` 等**汇总计数，不含 cases 明细行**；`/tasks/{id}/cases`（app.py:1651）同样读本地 cases 表。
- 因此：**若仅给 generate 请求加 `persist=False`，本地 cases 表为空 → testhub 回流拿不到用例 → 生成-回流闭环断裂**。这是破坏性改动。
- 设计文档原文亦将本项定义为"**确认边界 / 评估开关**"（`docs/testcodefast_integration_plan_2026-11-11.md` O9/G3/阶段B-6），并未要求无条件关闭。

### 3.3 方案对比（决策依据）

| 方案 | 内容 | 风险 | 是否真正"不落本地" |
|---|---|---|---|
| A 加可选开关 | `client` 透传 `persist` 参数，默认 True | 低 | ❌ 开关在生成→回流主链路里用不了（一用回流就读不到用例），只在"只生成不导入"的罕见场景可用 |
| B 结果回流改造 | 任务结果携带 cases 明细 + testhub 改从结果回流 + 再关本地写 | 中高（跨两端） | ✅ |
| **C 文档固化边界（已选）** | 不改代码，正式确认"本地表作中转"为既定架构 | 零 | ❌ 明确接受本地表继续存在 |

**弃 A 的关键理由**：A 提供的 `persist=False` 开关，在 testhub 唯一调用场景（生成 → 回流）中一用就断链——因为回流 `get_cases()` 读的正是被关掉的本地表。它是"看起来有用、实际用不上、用了还断链"的半成品参数，只增加维护负担与误用风险。若真实诉求是彻底不落本地，A 也无能为力，应直接上 B。

### 3.4 决策记录（方案 C）

**已确认的架构边界**：

1. R2 采用"**对接而非删除**"：sidecar 本地 SQLite `cases` 表作为「生成 → 回流」链路的**临时中转**，不是第二套权威资产库。
2. **testhub `apps.testcases.TestCase`（MySQL）是唯一权威用例资产库**；本地 cases 表仅服务回流读取与 sidecar 自身报告/批次（F12 runs 回填、`/tasks/{id}/cases` 前端详情）。
3. 因此**不关闭本地写**，避免破坏已验证的生成-回流闭环与 sidecar 报告链路。
4. 映射层幂等（子项①）已保证：即便重复回流，也不会在权威库产生重复用例。

**后续触发条件**（何时重新评估关闭本地写）：

- 若明确要求**删除 sidecar 自建资产表**以彻底消除数据副本 → 启动方案 B，作为独立批次规划（改 `PipelineResult` 携带 cases 明细 + `client`/`views` 回流改造 + 前端详情面板调整 + 关 `persist`）。
- 在此之前，本决策（C）为最终状态，#239 收尾。

---

本决策已确认，#239 无需代码实施。若未来触发上述条件，再按"先方案后执行"重新出实施计划。
