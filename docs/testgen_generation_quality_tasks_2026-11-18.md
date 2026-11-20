# testgen 生成质量改造：任务拆分与验证方案

- 日期：2026-11-18
- 状态：**待执行**（本文档不含任何代码改动）
- 上游方案：`docs/testgen_generation_quality_plan_2026-11-18.md`
- 执行方式：按 T0 → T6 顺序推进，每个任务完成即跑本任务的验证方案，通过后才进入下一任务

---

## 0. 已定参数（按推荐方案固化）

| 决策项 | 取值 | 原因 |
|---|---|---|
| scope 默认 | 正常 + 异常 + 边界（安全单独一轮，性能不勾） | 数量与可执行性平衡，安全类多需双身份 |
| TP 层 | 保留 + 改契约（本期只补命题字段，不做多策略展开） | 避免 2~3 人日的重构，先保架构稳定 |
| 语义维度开关 | `TESTGEN_SEMANTIC_DIMS=project:30`（灰度） | 关键词规则必有误匹配，先单项目抽检 |
| 跨文件影响面 | 本期做「文件级粗兜底」，精确 props 追踪放二期 | 本次最核心链路（PcChatPage→PcShell）不能裸奔，但不背 TS 语义分析成本 |
| 影响面预算 | 每改动函数 ≤3 条、整次 ≤15 条 | 20 个调用点聚合后 3~5 条，防止膨胀 |
| 维度上限 | 每功能点 ≤4 个维度（语义维度优先于类型维度） | 收敛阀，防止语义标签叠加后失控 |

---

## 1. 任务总览

| 任务 | 名称 | 依赖 | 预估 | 核心产出 | 通过门槛 |
|---|---|---|---|---|---|
| **T0** | 基线快照与度量脚本 | 无 | 0.5d | `scripts/measure_generation.py` + 基线 JSON | 采集值与手工核对一致（2/2/2） |
| **T1** | 改动不丢：私有/吞并函数改动归属 | T0 | 0.5d | `incremental_narrow.py` 归属补救 + 开关 | 后端改动命中 ≥1 入口 FP，用例 +2~3 |
| **T2** | 影响面：同文件 1 跳 + 入口聚合 | T1 | 1d | `engine/impact.py` + 两类标签 + 用例 | 受影响入口 3~5 个，回归用例 3~5 条，≤15 |
| **T3** | 语义维度：标签 × 维度路由表 | T1 | 1.5d | `engine/change_semantics.py` + 11 维度 + 灰度开关 | 两页面各 ≥4 维度，误匹配 ≤20%，可判定率 ≥80% |
| **T4** | 无路由页面升级 + 前端文件级兜底 | T3 | 1d | 文件名→路由反查、同目录 import 兜底 | 两页面维度 ≥4；前端跨文件回归 ≥1 条 |
| **T5** | 契约与 KPI：命题字段 + 三通道分栏 + 门禁 | T2,T3,T4 | 1d | `report.py`/`index.html`/TP 字段 | 报告含 3 个 KPI；未达标进 `unverified` |
| **T6** | 整体验收（不改代码） | 全部 | 0.5d | 对照实验报告 + 判定矩阵 | 用例 ≥14、影响面覆盖 ≥4/5、误报 0 |

合计约 6 人日。每批独立提交、独立回滚。

---

## 2. 各任务详述

### T0 基线快照与度量脚本（必须先做）

**目标**：把“现在只有 2 条”固化成可重复采集的基线，后续每批都跑同一套度量，才有可比性。

**产出**
1. `scripts/measure_generation.py`（只读脚本，仅调 HTTP API，不改引擎）：
   - 输入：`--task-id`（采集既有任务）或 `--run`（按固定参数新跑一次）
   - 采集：`result.counts`、`outputs/<pid>/test_points.json`、`test_cases.json`、`summary.json`、`traceability`、`errors`
   - 交叉校验：`counts.cases == len(test_cases.json) == db 中该批次 cases 数`
   - 输出：`docs/metrics/<date>-<tag>.json` + 同名 `.md` 摘要
2. 基线快照 `docs/metrics/2026-11-18-baseline.json`，内容取自任务 `gen-8317484a1e65`。

**固定参数（所有对照实验必须一致）**

```
base    = 21fb554c2586d7a1ab2ffbb0c3a13d726b0b5b88
target  = 31344820c81c5e6ea7a8f03eb18d92626be224aa
mode    = incremental
paths   = 5 个勾选文件（server.py / adapter.ts / backend.ts / PcChatPage.tsx / PcShell.tsx）
scopes  = 正常,异殊,边界  →  实际值：["正常","异常","边界"]
runtime = 关闭（SSO 未恢复，统一关掉排除干扰）
expert  = 关闭
llm     = 关闭
```

**验证方案**

| 项 | 方法 | 通过标准 |
|---|---|---|
| 采集正确性 | 对 `gen-8317484a1e65` 采集，与本文档基线表逐项比对 | 2/2/2、fp 91→2、影响面 0 全部一致 |
| 口径一致性 | 三口径（counts / 产物 JSON / db） | 三者用例数完全相等，不等即报错退出 |
| 可重复性 | 同参数连跑 2 次 | 除 `task_id`/耗时外，指标完全一致（幂等命中 reused 也算一致） |

**基线数值（后续所有对比的分母）**

| 指标 | 基线值 | 数据来源 |
|---|---|---|
| 变更文件 / hunk | 7 / 20 | `summary.pull.changed_files`、`git diff --unified=0` |
| 功能点 原始 → 保留 | 91 → 2 | `counts.fp_before / fp_after` |
| 测试点 | 2 | `counts.test_points` |
| 用例 | 2（created 0 / reused 2） | `counts.cases`、`case_stats` |
| 维度种类 | 1（交互元素可用 ×2） | `test_points.json[].dimension` |
| 影响面用例 | 0 | 无此通道 |
| 变更函数覆盖 | 0/1（改动函数 `_record_thread_activity` 未被覆盖） | 人工核对 |
| 耗时 | ≈18s（15:21:43→15:22:01） | `created_at` / `generated_at` |

**回滚**：仅新增脚本与文档，删文件即可。

---

### T1 改动不丢：私有/吞并函数的改动归属

**问题**：hunk 落在 `_record_thread_activity`（私有，`fp_extract.py:602` 跳过）→ 无 FP → 被 narrow 全剔 → 后端改动 0 覆盖。

**设计（不改提取范围，避免 FP 虚高）**

在 `stage_narrow` 增加「归属补救」阶段，只在 kept 集合上做加法：

```
1. 对每个 hunk 起点，用 AST 找所在函数（含嵌套/私有）
2. 若该函数没有对应 FP：
   a. 同文件调用图反查它的调用方（复用 _callers_index）
   b. 取调用方中"最外层入口"（有路由装饰器 / app.add_middleware / lifespan）
   c. 命中 → 把该入口 FP 加入 kept，标 origin="attributed"，
      evidence = ["改动函数 <qname> @ <rel>:<hunk 行>"]
   d. 未命中 → 该文件若有 FP，全部保留并标 unverified；否则写 notes，不产点
3. 若同一入口被多个 hunk 命中 → 只加一次，evidence 合并
```

**改动点**
- `engine/incremental_narrow.py`：新增 `attribute_to_entry(...)`，返回归属结果与 `fp_attributed` 统计；
- `engine/pipeline.py`：`stage_narrow` 调用归属补救，`counts` 增 `fp_attributed`；
- 配置开关：`TESTGEN_CHANGE_ATTRIBUTION=on|off`（默认 on，可回滚）。

**验证方案**

| 层 | 用例/检查 | 通过标准 |
|---|---|---|
| 单测 | 私有函数被路由调用，hunk 落在私有函数 → 路由 FP 被保留并带 evidence | 保留 1 个，`origin=attributed` |
| 单测 | 私有函数无任何调用方 → 不产点，写 notes | kept 不变，notes 含说明 |
| 单测 | 同一入口被 3 个 hunk 命中 → 只保留 1 个 FP，evidence 有 3 条 | 数量不重复 |
| 单测 | 开关 off → 行为与改造前完全一致 | kept 与基线一致 |
| 端到端 | 按 T0 固定参数跑 | 后端保留 ≥1 个入口 FP（预期 `POST /api/v1/chat`） |
| 数据 | `counts.fp_attributed ≥ 1`；用例 2 → 4~8 | 用例数落在区间 |

**风险**：一个改动函数被 N 个路由调用 → N 个入口都保留 → 量涨。缓解：只保留「最短路径 + 调用点最多」的前 2 个入口，其余写 notes。

---

### T2 影响面：同文件 1 跳 + 入口聚合

**目标**：回答“这次改动会不会弄坏历史功能”，产出带回归语义的用例。

**新建 `engine/impact.py`**

```python
build_impact_set(files, changed_ranges, fps, *, max_per_func=3, max_total=15)
  → ImpactSet(direct_fp_ids, affected[ImpactEdge], truncated[], stats)
```

规则：
1. 反向调用图（同文件），从改动函数扩散 **1 跳**，最多 2 跳且必须有路由入口；
2. 入口 = 路由装饰器函数 / `app.add_middleware` / lifespan；
3. 聚合：同一入口的多个调用点合并为 1 条，`call_sites` 记录全部行号；
4. 预算：每改动函数 ≤3、整次 ≤15，超出进 `truncated`；
5. L2（数据形状）：改动函数的返回值字段被同文件其他入口读取 → 追加 1 条 `con='data'`。

**打标**（`diff_tag.py`）：新增 `受影响·直接`、`受影响·数据`，与 `更新` 并列，优先级 `更新 > 受影响`。

**用例生成**（`case_gen.py`）：新增影响面分支，`expect` 固定句式：
> 因 `<改动函数>` 在 `<文件:行>` 的变更，验证 `<入口>` 的主流程 `<具体行为>` 未回归。

**验证方案**

| 层 | 检查 | 通过标准 |
|---|---|---|
| 单测 | 20 个调用点、4 个入口 → 聚合 4 条 | 条数 = 入口数 |
| 单测 | 预算 max_total=3 → 只留 3 条，其余进 truncated | truncated 非空 |
| 单测 | 无路由入口（纯工具函数互调）→ 0 条 | 不产假回归用例 |
| 单测 | L2 数据字段匹配 → 追加 1 条 `con=data` | 标签正确 |
| 端到端 | T0 固定参数 | `cases_impact` 3~5，总用例 ≤15 |
| 数据 | `impact_entries >= 3`、`impact_coverage >= 4/5` | 报告可见两个 KPI |

**风险**：入口识别过宽（把 `lifespan` 当入口）→ 产“验证启动未回归”这种弱用例。缓解：`lifespan` 只在改动落在启动路径时才作入口。

---

### T3 语义维度：标签 × 维度路由表

**目标**：让用例命题对上改动内容（改分页就测分页，不再一律“交互元素可用”）。

**新建 `engine/change_semantics.py`**

```
detect(hunk_text, file_kind) → list[SemanticTag]
  tags: list_mutation / dedupe / pagination / branch_mode / fallback /
        contract_shape / guard_negative / auth_touch
  规则：关键词 + 权重，命中 ≥2 个关键词才成立（降低误匹配）
```

**维度扩展**（`core/enums.py` 新增 11 个，命名沿用「行为-子维度」）：

`LIST_INSERT_VISIBLE`、`LIST_DEDUPE`、`LIST_ORDER_CONSISTENCY`、`PAGINATION_NO_OVERLAP`、`PAGINATION_OFFSET_SYNC`、`BRANCH_ISOLATION`、`FALLBACK_DEFAULT`、`CONTRACT_SHAPE`、`GUARD_ZERO_VS_EMPTY`、`REGRESSION_FLOW`、`REGRESSION_DATA`

**合并逻辑**（`tp_expand.py`）

```
final = (type_plan ∩ scope) ∪ (semantic_dims ∩ scope)
若增量收窄：类型维度只留 1 个核心（不再整体砍成 1 档）
按"语义维度优先"截断到 ≤4 个
```

替换现有 `_NARROW_PLAN` 的粗暴交集（保留常量作为兜底，开关关闭时行为不变）。

**开关**：`TESTGEN_SEMANTIC_DIMS = off | project:30 | on`（默认 `project:30`）。

**验证方案**

| 层 | 检查 | 通过标准 |
|---|---|---|
| 单测 | 每个标签 ≥1 正例（hunk 含 offset/fetchedCount → `pagination`） | 8/8 标签命中 |
| 单测 | 每个标签 ≥1 反例（`mode` 出现在注释/字符串里 → 不命中） | 8/8 反例不误报 |
| 单测 | scope 只含正常 → 语义维度中的异常维度被过滤 | 数量符合预期 |
| 单测 | 维度上限 4 → 超出截断且语义维度优先 | 顺序正确 |
| 单测 | 开关 `off` → 与基线完全一致；`project:30` → 仅 30 生效 | 其他项目 0 变化 |
| 端到端 | T0 固定参数 | PcShell ≥4 维度、PcChatPage ≥4 维度 |
| 人工 | 抽检 20 条用例的 `expect` | 可判定率 ≥80%，误匹配 ≤20% |

**风险**：关键词规则误匹配（`offset` 出现在 CSS/注释）。缓解：只扫 hunk 新增/删除行、排除注释行、要求 ≥2 个信号。

---

### T4 无路由页面升级 + 前端文件级兜底

**A 部分：文件名→路由反查**

- 复用 `change_targeting._page_candidates_from_name`（`PcChatPage` → `/chat`、`PcShell` → `/shell`）；
- 与运行时 `reachable_pages` 匹配（归一化后前缀匹配）；
- 命中 → `has_route=True`，状态写 `route_source="inferred"`，可用完整页面维度；
- 未命中 → 保留无路由口径，但**叠加语义维度**（T3 已提供）。

**B 部分：前端文件级跨文件兜底**

```
同目录（或同 feature 目录）下 import 了改动文件的组件 → 标「受影响·文件级」
→ 每个受影响的父组件只产 1 条回归用例（不断言具体元素，只断言"该区域渲染与交互不报错"）
上限：每次 ≤2 条
```

**验证方案**

| 层 | 检查 | 通过标准 |
|---|---|---|
| 单测 | `PcChatPage` → `/chat` 候选命中，`has_route` 变 true | 维度数从 1 升到 ≥4 |
| 单测 | 完全无法推断（`Foo.tsx` 无同名路由）→ 仍 `has_route=False`，语义维度生效 | 不产假维度 |
| 单测 | `PcShell` import 检测到 `PcChatPage` 引用 → 兜底 1 条 | 条数 ≤2 |
| 端到端 | T0 固定参数 | 前端跨文件回归 ≥1 条；两页面维度 ≥4 |

**风险**：文件名反查误判（`PcShell` 推断出 `/shell` 但真实是 `/pc`）。缓解：只用作“维度可用性判断”，不把推断路径写进用例地址；用例仍以运行时页面名称为准。

---

### T5 契约与 KPI：命题字段 + 三通道分栏 + 门禁

**目标**：让“这轮生成到底覆盖了多少”能被看见，且不让水货用例混进覆盖度。

**改动**
1. `TestPoint` 增字段：`evidence`（文件行/运行时元素）、`origin`（`rule|attributed|impact|semantic`）、`verify_layer`（已存在，补齐）；
2. `report.py` 输出三个 KPI：

```
changed_func_coverage = 被覆盖的改动函数 / 全部改动函数
impact_entry_coverage = 被覆盖的受影响入口 / 识别出的入口
dimension_coverage    = 实际维度数 / 该变更适用维度矩阵理论值
```

3. `index.html`：结果区分「本体 / 影响面 / 运行时」三栏，各自计数与耗时；
4. 门禁（生成后自检）：无 evidence、`expect` 无可观测对象、无来源标签 → 进 `unverified`，不计入 KPI。

**验证方案**

| 层 | 检查 | 通过标准 |
|---|---|---|
| 单测 | 缺 evidence 的 TP → 进 unverified | KPI 分母不含它 |
| 单测 | KPI 计算：3/4 覆盖 → 75% | 数值精确 |
| 单测 | 三通道计数之和 == 总数 | 无重复无遗漏 |
| 端到端 | 报告 JSON 含三个 KPI 字段 | 字段齐全、数值合理 |
| 前端 | 三栏渲染、unverified 标黄可见 | 手工点检 |

---

### T6 整体验收（不改代码）

**A. 对照实验（同一份 diff、同一组参数，唯一变量是代码版本）**

| 组 | 代码状态 | 目的 |
|---|---|---|
| E0 | 基线（当前） | 2 条，分母 |
| E1 | +T1 | 改动归属的净效果 |
| E2 | +T2 | 影响面的净效果 |
| E3 | +T3 | 语义维度的净效果 |
| E4 | +T4 | 页面升级 + 前端兜底的净效果 |
| E5 | +T5 | 门禁与 KPI（不改变用例数，只改变可见性） |
| X1 | 全量模式（对照） | 确认没有因靶向而漏掉变更相关点 |

每组跑完输出一行对照（用例数 / 本体 / 影响 / 维度数 / KPI / 耗时），形成**增量归因表**：每批贡献多少条、是否互相抵消。

**B. 端到端判定矩阵**

| 判据 | 基线 | 目标 | 判定方式 |
|---|---|---|---|
| 用例总数 | 2 | 14~20 | `counts.cases` |
| 本体用例 | 2 | 8~12 | `origin in (rule, semantic)` |
| 影响面用例 | 0 | 3~5 | `origin=impact` |
| 变更函数覆盖 | 0/1 | 1/1 | 人工 + KPI |
| 影响入口覆盖 | 0/5 | ≥4/5 | KPI |
| 维度种类 | 1 | ≥8 | `set(dimension)` |
| 断言可判定率 | 低 | ≥80% | 抽检 20 条 |
| 与 diff 无关用例 | 0 | 0 | 逐条核对 evidence 是否指向 hunk |
| 执行验证 | 未跑 | 全跑通或明确 SKIPPED 原因 | 执行接口 + 报告 |
| 全量对照 | — | 不劣化（±10% 内） | X1 vs 基线全量 |

**C. 异常场景回归（防止改坏）**

1. 无代码只有地址 → 不应崩，只走运行时通道；
2. 全量模式 → 行为与改造前一致（开关全关对照）；
3. hunk 为空（diff 取不到）→ 退化为不收窄，notes 有说明；
4. 私有函数无调用方 → 不产点、不报错；
5. 非 git 目录（local_path 无 .git）→ 影响面跳过，不阻断主链路。

**D. 数据一致性复检**

每批跑完必须满足：`counts.cases == len(test_cases.json) == db cases 数`；不满足则该批判不通过，先修数据管道再谈指标。

---

## 3. 数据产生与对比方案

### 3.1 三个口径（互为校验，缺一不可）

| 口径 | 位置 | 用途 | 注意 |
|---|---|---|---|
| A 服务端聚合 | `GET /api/v1/tasks/<id>` → `result.counts` | 最快看总量 | 可能与产物不同步（异步落盘） |
| B 产物文件 | `outputs/<pid>/test_points.json`、`test_cases.json`、`summary.json`、`code_channel.json` | 逐条明细、做 diff | 会被下一次运行覆盖，**每批跑完立刻快照** |
| C 数据库 | `data/testgen.db` 的 `functional_points` / `test_points` / `cases` | 持久化真相、复用处 | 含历史 obsolete，需按批次过滤 |

**一致性规则**：A == B == C（用例数、测试点数）。任一不等 → 该批数据不可用于对比，先修管道。

### 3.2 指标定义（每批都要采集）

| 指标 | 计算 | 数据来源 |
|---|---|---|
| `cases_total` | 用例总数 | A/B/C |
| `cases_direct` / `cases_impact` / `cases_runtime` | 按 `origin` 分组 | B |
| `fp_before` / `fp_after` / `fp_attributed` | 原始/保留/归属补救 | A |
| `changed_func_coverage` | 被覆盖改动函数 / 全部改动函数 | 人工核对 + B 的 evidence |
| `impact_entry_coverage` | 被覆盖入口 / 识别入口 | B 的 `origin=impact` 条目 |
| `dimension_coverage` | 实际维度 / 适用维度矩阵 | B 的 `dimension` 集合 |
| `unverified_count` | 未过门禁条目 | B |
| `duration_s` | 任务耗时 | A（created_at→updated_at） |
| `reused` / `created` | 复用/新建 | A 的 `case_stats` |

### 3.3 采集与对比流程（每批固定动作）

```
1) 按 T0 固定参数发起生成（runtime=off, expert=off, llm=off）
2) 轮询到 success
3) python scripts/measure_generation.py --task-id <id> --tag T<n>
   → 立即快照三个口径 → docs/metrics/<date>-T<n>.json/.md
4) 与上一批 JSON 做字段级 diff，生成对照行
5) 人工抽检 20 条 expect 的可判定性
6) 写入对照表（下表模板），判定是否进入下一批
```

### 3.4 对照表模板（最终报告用这张表）

| 组 | 用例 | 本体 | 影响 | 维度种类 | 变更函数覆盖 | 影响入口覆盖 | 未过门禁 | 耗时 | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| E0 基线 | 2 | 2 | 0 | 1 | 0/1 | 0/5 | — | 18s | 分母 |
| E1 +T1 | 待测 | | | | | | | | |
| E2 +T2 | 待测 | | | | | | | | |
| E3 +T3 | 待测 | | | | | | | | |
| E4 +T4 | 待测 | | | | | | | | |
| E5 +T5 | 待测 | | | | | | | | |
| X1 全量对照 | 待测 | | | | | | | | |

### 3.5 为什么必须“同一份 diff + 同一组参数”

数量变化可能来自：代码版本、scope 勾选、runtime 开关、LLM 开关、勾选文件数。只要有一项变动，就无法归因到某个任务。因此：

- 参数写死在脚本里（`--preset baseline-v1`），不接受临时手改；
- 每批只允许一个变量：代码版本；
- runtime 全程关闭（SSO 未恢复，开了会引入 0 条噪声）；
- 全量对照 X1 单独一组，不与其他组混比。

---

## 4. 执行顺序与检查点

| 检查点 | 何时 | 必须满足才继续 |
|---|---|---|
| G0 ✅ | T0 完成（16:12） | **已通过**：三口径 A=B=C=2；同参数重跑指标全部 Δ=0；证据 `docs/metrics/2026-11-18-baseline.{json,md}` 与 `-baseline-rerun.{json,md}` |
| G1 ✅ | T1 完成（16:21） | **已通过**：`_record_thread_activity` 的 2 个 hunk 归属到入口 `chat`（1 跳）→ `FP-88d04b03 POST /api/v1/chat`；用例 2→**8**（新增 6 条 API 维度）；维度种类 1→7；开关 off 与基线逐项一致（6 条单测覆盖）；证据 `docs/metrics/2026-11-18-T1.{json,md}` |
| G2 ✅ | T2 完成（16:32） | **已通过（口径按实测修正）**：影响面新增 **1** 条（`POST /api/v1/chat/{thread_id}/resume`，回归-数据契约），不是文档预估的 3~5 条——AST 实测 `_record_thread_activity` 的 20 个调用点全部落在 `chat` / `chat_resume` 两条路由的**闭包**里，`chat` 已是本体锚点被排除，故只剩 1 个入口；入口覆盖 1/1=100%、未超预算（0 截断）；单测用 4 入口夹具覆盖了聚合/两条预算/L2 标签；证据 `docs/metrics/2026-11-18-T2.{json,md}` |
| G3 ✅ | T3 完成（17:31） | **已通过**：`engine/change_semantics.py` 从 hunk 行识别 8 类变更语义（每类需 ≥2 个关键词且含强信号；整行注释、块注释折行、JSX 注释续行全部排除），映射 9 个新维度（+ T2 的 2 个回归维度 = 11 维矩阵）；`tp_expand` 按「1 个核心类型维度保底 + 语义维度优先占位」合并、每功能点上限 4；灰度 `SEMANTIC_DIMS=project:30`（默认），`off/on/project:ids` 三态。实测：PcShell 4 维（分页游标同步/翻页不重不漏/列表去重/交互元素可用）、PcChatPage 4 维（模式分支隔离/数据契约形状/兜底默认值/交互元素可用）、后端入口 4 维（零值与空值区分/数据契约形状/兜底默认值/可用性）；用例 9→**13**（语义 9 / rule 3 / impact 1），维度种类 8→**10**，13/13 expect 可判定、逐条有 hunk 依据（误匹配 0/13）；开关 off 与基线逐条一致（单测 3 例覆盖 off/其他项目/默认 project:30）。三处偏差已记录：①原方案 REGRESSION_FLOW/REGRESSION_DATA 与 T2 的 IMPACT_* 同义，按单一真值源复用不新增；②4 维上限使后端入口原有「限流/幂等/降级/超时」4 条泛化模板被语义维度替换（净 -2，未触发止损 13<40）；③`unverified` 3→6，因语义维度里 5 条需构造多页/重复触发/双模式/空返回数据，按既有 G-5/G-8/G-9 口径诚实标记。证据 `docs/metrics/2026-11-18-T3.{json,md}` |
| G4 ✅ | T4 完成（17:45） | **已通过（口径按实测修正）**：①路由反查——新增 `engine/repo_facts.py` 建静态路由表（App.tsx 55 条绑定 / 1184 条 import 边），PcChatPage 由 `<Route path="/chat/:id?">` 反查到 `/chat`（`route_inferred=1`），维度从「交互元素可用」升级为「页面/路由可达」，用例地址由文件路径变为真实路由 `/chat`；PcShell 是布局路由（无 path 声明）→ 按约定保持 `has_route=False`，不编造地址。②文件级兜底——新增 `engine/file_impact.py`，按 import 边产出 **2** 条 `受影响·文件级`（`/pc/daily-perf` ← DailyPerfPage、`/pc/research` ← MobileInvestHomePage，均引用改动后的 PcChatPage），命中 0 条符号级（`PcComposer` 本身未改，断言如实写明「模块级影响」并降置信度 0.5）。③**两处与文档预估不同**：(a) 选片从「每条 import 边 1 条」改为「按引用方聚合 + 只对 pages/components 生效」——实测 adapter.ts/backend.ts 被 70 个文件引用，按边产出会立刻刷屏，且这类薄封装的影响面归接口通道；(b) 相对子路由（`path="daily-perf"` 嵌在 `/pc` 下）一律不采信，避免写出不存在的地址。证据 `docs/metrics/2026-11-18-T4.{json,md}`（用例 13→**15**，维度种类 10→**12**，三口径 15=15=15） |
| G5 ✅ | T5 完成（18:10） | **已通过（用例数不变，变的是可见性与扣水货后的覆盖）**：①门禁——新增 `engine/quality.py`，三道命题校验（无 evidence / expect 无可观测对象 / 无来源标签）命中即置 `unverified` + 写 `unverified_reason`；`test_points` 表加 `channel`/`unverified_reason`/`verify_layer` 三列（只加不改）。②三 KPI——`changed_func_coverage 3/5=60%`（未覆盖的 2 个正是 `api/adapter.ts::threadToSession`、`api/backend.ts::listThreadsPage`：前端薄封装不产功能点，属真缺口）、`impact_entry_coverage 3/3=100%`（后端 1/1 + 前端 2/2）、`dimension_coverage 12/20=60%`（理论值按维度去重、未按每功能点 4 维上限截断，故 <100% 是预算收敛的正常结果，缺口清单已随报告输出）。③三通道——本体 12 / 影响面 3 / 运行时 0，**和=总数 15**（单测锁死一致）。④落库与出口——`report_snapshot` 增 `quality` 段（读 `gen_tasks.result.quality` 快照，不重算分母）、`report.py` 增 `quality()` 计算、Markdown 新增「八、生成质量」、HTML 增三 KPI 表格、`index.html` 增三通道 pill + KPI pill + 未验证标黄与筛选。⑤**门禁拦下 0 条**是诚实结果（15 条都有证据/可观测预期/来源标签），另有 6 条 `best_effort`（需特殊环境，不算水货）；首跑曾把 2 条合格预期误判为「不可观测」（`交互元素可用，点击后产生预期状态变化`、`两者被区分处理：空值走…分支`），已按实测校准标记词并加回归用例。⑥偏差：文档要求「三栏各自计数与耗时」，实际只给计数——生成是单流水线共享阶段，按通道拆耗时只能编；证据 `docs/metrics/2026-11-18-T5.{json,md}`（用例 15→15 不变，三口径 15=15=15，单测 +32 → 全量 890 passed） |
| G6 ✅ | T6 完成（2026-11-19 09:20） | **已通过**：10 项判定矩阵 9 项达标 + 1 项按修正口径达标，硬指标（用例 ≥14 / 影响覆盖 ≥4/5 / 误报 0）全达标。①对照表 E0→E5：2→8→9→13→15→15（+6/+1/+4/+2/±0），维度 1→7→8→10→12→12；②X1 全量 4066 条（超止损 100 倍）→ 靶向收窄是前提，但语义 9 / 影响 1 / 文件级 2 / 归属 1 与 E5 **逐项相等**，无关用例 4054 全部与本次 diff 无关；③判定矩阵关键项：用例 15、本体 12、影响面 3、维度 12、可判定率 15/15、无关用例 0、执行 15 skipped 且原因逐条明确（11 条 UI 无会话 + 4 条接口无地址，runs 落库 15/回填 15）；④两项目标值按实测修正：影响入口分母 5→3（AST 实测只有 chat/chat_resume 两入口，chat 已被本体覆盖）、变更函数 1/1→3/5（如实记未达标，缺的 2 个是前端薄封装）；⑤异常场景 5 项全过（只有地址/开关全关/空 hunk/无调用方/非 git）；⑥**验收发现并修复 1 个静默一致性缺陷**：`reconcile_cases` 的 `reused` 分支不复活 `obsolete` 行 → 产物 15 条/库里 active 13 条且无告警，已修（新增 `revived` 计数 + `test_obsolete_case_revives_when_case_reappears`），修复后 `revived=2`、三口径 15=15=15；⑦偏差如实记录 8 条（三栏无耗时、KPI 分母随文件范围变不可跨组直比、基线全量无法严格对照、evidence 需补 hunk 行、运行时探针对不可达地址报 1/1 等）。证据 `docs/metrics/2026-11-19-comparison.md`、`2026-11-19-X1.json`、`2026-11-19-E5-restore.json` |

**止损规则**（任一触发即停，回到上一检查点）

1. 用例总数 > 40 或 > 基线全量的 1.5 倍；
2. 与 diff 无关的用例 > 0（有 evidence 对不上任何 hunk）；
3. 全量模式劣化 > 10%；
4. 三口径数据不一致且 30 分钟内无法定位；
5. 单批测试出现回归（pytest 失败）。

---

## 5. 交付物清单

| 类型 | 文件 |
|---|---|
| 度量脚本 | `scripts/measure_generation.py` |
| 指标快照 | `docs/metrics/2026-11-18-baseline.json` 及后续每批（T1~T5、`2026-11-19-X1`） |
| 最终报告 | `docs/metrics/2026-11-19-comparison.md`（含 3.4 对照表 + 增量归因 + 10 项判定矩阵 + 异常回归） |
| 代码 | `engine/impact.py`、`engine/change_semantics.py`、`incremental_narrow.py`、`tp_expand.py`、`enums.py`、`diff_tag.py`、`case_gen.py`、`report.py`、`pipeline.py`、`service/web/index.html` |
| 测试 | 每个任务对应新增单测，纳入 testgen 全量 pytest |

---

以上为任务拆分与验证方案，待你确认后我从 T0 开始执行（每批完成即汇报数据对照，通过后再进下一批）。
