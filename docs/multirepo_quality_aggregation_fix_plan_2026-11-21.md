# 多仓 quality 聚合缺陷修复方案（2026-11-21）

> 状态：**方案 A 已实施并验证**（2026-11-21）。用户确认「方案 A」后按本方案落地：合并事实字段 + 在聚合后全量产物上重算 quality。守护测试 `tests/test_multirepo_quality_aggregation.py`（6 例），全量 pytest 1085 passed / 覆盖率 82.72%。
> 证据来源：真实业务三仓端到端验收（2026-11-20，`docs/../data/multirepo_real_e2e_20261120-172936.json`）。

## 1. 缺陷事实（已实测复现）

真实三仓（research-agent-backend / -pc-frontend / -h5-frontend）一次多仓生成，`project 62` 的产物总数是 **FP 20 / TP 80 / Case 80**，`repo_summary` 按仓分组也正确（backend 12/48/48、pc 4/16/16、h5 4/16/16）。但质量快照错在两处：

| 表现 | 实测值 | 应有值 |
|---|---|---|
| 异步任务 `result.quality.kpis`（报告 KPI 数据源） | 只反映**最后一个仓**：dimension 4/4、body 16 TP | 三仓聚合：dimension 12/12、body 80 TP |
| `result.counts.kpi_changed_func_pct` / `kpi_impact_entry_pct` / `kpi_dimension_pct` | **300%**（3×100% 求和） | 单值百分比（如 100%）或按仓分列 |

影响：报告 `/api/v1/projects/{pid}/report` 的 KPI 卡片显示末仓数据而非全项目，且 counts 百分比失真。三仓编排本身（ID 隔离 / 幂等 / 报告分组）**不受影响**，已全部验收通过。

## 2. 根因（逐行可证）

**文件**：`services/testgen/engine/pipeline.py` 的 `_aggregate_multirepo_results()`（约 2290–2312 行）。

1. **quality 被字典覆盖**：
   ```python
   parent_result.quality = {k: v for r in results for k, v in (r.quality or {}).items()}
   ```
   各仓 quality 的顶层键（`enabled/gate/kpis/channels/summary`）完全相同，字典推导后面的仓**整体覆盖**前面的仓，最终只剩最后一个仓的快照。

2. **counts 百分比被求和**：
   ```python
   parent_result.counts = dict(sum((Counter(r.counts) for r in results), Counter()))
   ```
   counts 里既有**可加计数**（`functional_points`、`gate_blocked`…），也有**百分比**（`kpi_*_pct`，由 `stage_quality` 用 `_pct(value)` 写入，见约 2006–2008 行）。Counter 求和把三个 100% 加成 300%。

3. **重算所需事实字段未合并**：聚合只拼接了 `functional_points / test_points / cases`，没有合并 `files`、`semantic_dims`、`attributed_evidence`、`hunks`、`incidental_dimensions`。所以"在聚合结果上重算 quality"目前缺输入。

**报告侧无需改**：`engine/report.py:quality()`（约 431–457 行）只透传 `gen_tasks.result.quality.kpis`，不重算、不聚合。engine 修对即可。

## 3. 修复口径（关键决策，先定再做）

KPI 的三个覆盖率**不是简单可加量**，聚合口径必须逐个定：

| 字段 | 口径 | 理由 |
|---|---|---|
| `gate`（门禁计数） | 各仓**求和**（`quality_gated/gate_blocked/各 reason/evidence_*`） | 纯计数，求和即总量 |
| `channels`（三通道 tp/gate_ok/total） | 各仓**求和**；`consistent` 用聚合后的 `sum(三栏)==total` **重算** | 可加；自检标志必须重算而非沿用末仓 |
| `changed_func_coverage` | `covered`、`total` 分别求和；`uncovered` 拼接去重；`value = covered/total` **重算** | 分子分母都是计数，先加再除才对；直接平均三个 value 是错的（分母可能不同） |
| `impact_entry_coverage` | 同上（`covered/total` 求和重算），`backend`/`frontend` 子字典各自聚合 | 同 changed_func |
| `dimension_coverage` | `actual` 按维度**去重后计数**、`theory` 求和；`value = actual/theory`；`missing` 按维度去重 | 维度是"种类"，跨仓要去重；不能用 actual 简单相加（同一维度在 3 仓被算 3 次） |
| `summary` | `total/gate_ok/blocked` 求和；`gate_pass_rate` 用聚合值**重算** | 同 KPI 口径 |
| `enabled` | 取 `all(r.quality.enabled)`（全开才开） | 布尔，AND 聚合 |
| `note` | 取首仓（各仓文案相同） | 静态说明 |
| `counts.kpi_*_pct` | 从聚合后的 quality KPI `value` **回写**（`_pct(value)`），不再让 Counter 求和 | 与 quality 同源，杜绝两个真值 |

> 为什么不用"直接平均三仓百分比"：三个仓的分母不同（backend 12 FP、pc/h5 各 4 FP），平均 value 在数学上不等于总覆盖。

## 4. 实施方案（两条路径，推荐 A）

### 路径 A（推荐）：合并事实字段 + 在聚合结果上重算 quality

在 `_aggregate_multirepo_results()` 中：

1. 拼接事实字段（与 `functional_points/test_points/cases` 同样处理）：
   `files`（dict merge）、`semantic_dims`（按文件拼接）、`attributed_evidence`（list 拼接）、`incidental_dimensions`（若存在）。
2. 从父 `opts` 取回 KPI 重算入参：`scopes`（调用方原始输入）、`mode`（判断 is_incremental）。`hunks` 各仓独立且键为文件路径——合并成 `{path: spans}` 字典即可（冲突时并集）。
3. 调用一次 `stage_quality(opts, parent_result)` 或直接调 `quality.apply_gate / compute_kpis / channel_summary / summarize`，**在已拼接的全量 TP/FP 上重算**。
   - 注意：子仓 stage_quality 已把 `unverified` 标志写进 TP 对象，重算 `apply_gate` 是幂等的（见 quality.py docstring），不会二次篡改。
4. 从重算后的 quality KPI `value` 回写 `parent_result.counts["kpi_*_pct"]`（覆盖 Counter 求和值）。
5. 删除原来的 `parent_result.quality = {k:v ...}` 字典覆盖。

**优点**：口径单一（与单仓完全同一套 compute_kpis），无需手写易错的字段合并逻辑，未来 KPI 增减自动生效。
**风险**：需确认 `scopes`/`mode` 在父 opts 可用（`_derive_repo_opts` 是 deepcopy，父 opts 未被改写，可用）；需确认 `semantic_dims` 合并形态与 compute_kpis 入参一致。

### 路径 B（备选）：按字段语义合并 quality 字典

不重算，写一个 `merge_quality(qualities)` 按第 3 节口径逐字段合并（求和 / 重算比例 / 去重）。改动集中在聚合函数，不动 stage_quality。

**优点**：改动面最小，不动 KPI 计算链。
**缺点**：KPI 结构增减时要同步改合并逻辑，存在"两处真值"漂移风险；`dimension_coverage` 的去重口径需与 compute_kpis 保持一致，容易漏。

## 5. 测试方案

新增 `tests/test_multirepo_quality_aggregation.py`（或并入现有 stage 测试）：

1. **构造三仓子 quality**（不连 codeup）：三个 `PipelineResult`，各带若干 TP（部分 unverified）、FP、counts、`quality` 字典，分母刻意设不同（如 12/4/4）。
2. **断言聚合后**：
   - `gate` 各计数 = 三仓之和；
   - `channels.body.tp` = 三仓之和，`consistent=True`；
   - `changed_func_coverage.covered/total` = 三仓之和、`value` = 总和之比（非平均）；
   - `dimension_coverage.actual` 按维度去重（同维度在 3 仓只算 1 次）；
   - `counts.kpi_*_pct` 与聚合 quality KPI `value` 一致（不再 300%）；
   - 末仓不再"独占"快照（至少断言一个只有首仓才有的计数被保留）。
3. **回归**：单仓 `quality` 行为逐字不变（已有测试不动）；多仓 E2E（真实三仓）报告 KPI 显示聚合值 12/12·80TP。
4. **门禁**：`ruff check` / `format` / `mypy` / 全量 pytest（当前基线 1082 passed）不回退。

## 6. 检查点（DoD）

- [ ] 真实三仓 project 报告 KPI = 三仓聚合（80 TP、dimension 12/12），不再是末仓 16 TP/4/4。
- [ ] `result.counts.kpi_*_pct` 为单值百分比，与 quality KPI `value` 一致。
- [ ] 三通道 `consistent=True`，`gate` 计数为三仓之和。
- [ ] 单仓生成 KPI 逐字不变；全量测试不回退、ruff/mypy 绿。

## 7. 附：连带待决项（非本缺陷）

- 同步 `/api/v1/pipeline` 路径下报告 `quality.available=false`（快照只在异步任务落）——是已知设计，不在本方案范围。
- 验收 project 61–64 的清理与本缺陷独立，另行处理。

---

## 8. 实施记录（2026-11-21，方案 A）

- `engine/pipeline.py`：`_aggregate_multirepo_results` 增 `opts` 入参；新增 `_merge_quality_facts` 合并 `files/diff_hunks/semantic_dims/attributed_evidence/has_code`；删除末仓 quality 字典覆盖，改为在聚合后全量 TP/FP 上调用一次 `stage_quality` 重算；分仓 quality 说明 note 过滤掉，只留聚合版。
- `tests/test_multirepo_quality_aggregation.py`：6 例守护（gate/channels 求和、changed_func 分子分母求和且非平均、kpi_*_pct 不再 300%、维度去重、单仓零回归、事实字段合并）。
- 验证：多仓 + quality 相关回归 82 passed；全量 1085 passed / 1 skipped / 覆盖率 82.72%；ruff check/format、mypy 对改动文件全绿。
- **口径说明**：为了实现「口径单一」，`stage_quality` 只在 `opts.is_incremental` 时由 hunks 反推 changed_func 分母——多仓聚合沿用父 opts 的 mode，与原各仓独立运行时一致。
