# stage 双表合并方案（A-4 / #439 子项）

> 基线：`main` @ `0703248`（#439 app.py 拆分已合并）。本文件只做方案，不含实现改动。
> 问题出处：`services/testgen/docs/code_review_2026-10-19.md` A-4、`docs/playwright_cli_skills_adoption_and_code_tradeoff_2026-10-20.md` A-4、`docs/optimization_batches_2026-10-20.md` Batch 5。

## 一、结论先行

把 `engine/pipeline.py` 里的两张硬编码表（`_P1_STAGE_WIRING` + `PIPELINE_STAGE_ORDER`）合并为**一张有序事实源**，并让 `run_pipeline` 直接按这张表调用阶段函数；`REGISTRY` 保留为「契约/元数据镜像」，对外 API 与 19 项注册不变。

这样「顺序」与「实现」物理上无法再各自漂移，消除 A-4 的静默错序风险，且对现有测试、API、多仓编排零行为变更。

## 二、现状事实（已逐项核对）

| 位置 | 内容 | 作用 |
|---|---|---|
| `engine/pipeline.py:2178-2199` | `_P1_STAGE_WIRING`：18 项 `(name, kind, fn)` | 模块加载时把 `stage_*` 编排包装回注 `REGISTRY`（改 `fn/kind/planned`） |
| `engine/pipeline.py:2202-2214` | `_wire_stages_to_registry()` | 执行回注；若 name 不在 REGISTRY 则 `register(Stage(...))`（`execute` 即由此进入） |
| `engine/pipeline.py:2219-2238` | `PIPELINE_STAGE_ORDER`：18 个 name | `run_pipeline` 的执行顺序 |
| `engine/pipeline.py:2254-2255` | `for _name in PIPELINE_STAGE_ORDER: REGISTRY.get_fn(_name)(...)` | 生产代码中**唯一**消费 REGISTRY 的执行点 |
| `engine/stage_registry.py:302-469` | `_build_registry()` / `REGISTRY` | 19 项 = 12 能力层（含 `comparator`、`quality`）+ 7 适配层（含 `execute` 由 pipeline 回注） |
| `engine/pipeline.py:2377-2392` | `run_multirepo_pipeline` | 多仓逐仓复用 `run_pipeline`，**不另写阶段链**，无需单独改 |

补充事实：

- 两表当前 18 项顺序**逐项一致**（`pull → resolve_sources → register → scan → auth_scan → extract → runtime_ui → tag → tp_expand → semantic_enrich → expert_review → prd_ingest → case_gen → llm_design → quality → execute → partition_channels → persist`），风险是「未来改一张忘另一张」。
- `REGISTRY` 比流水线多 1 项 `comparator`（执行后语义比对，不是生成流水线阶段），必须保留，不能因合并被删。
- `service/routers/analysis.py:197` 的 `list_stages` 是**阶段快照业务表**的 API，与 `stage_registry.Stage` 无关，不受影响。
- 现有测试没有在运行期替换 `pipeline.stage_*` 函数（只 `monkeypatch` 了 `get_settings`），因此把 `fn` 固化进表不会破坏测试注入。
- 现有守护：`tests/test_pipeline_t5.py:176-181` 只断言 `quality` 的相对位置；`tests/test_stage_registry.py` 断言 19 项、全 wired、无 planned。

## 三、目标 / 非目标

**目标**

1. 顺序、类别、实现函数只有一处定义；删除 `_P1_STAGE_WIRING` 与独立的 `PIPELINE_STAGE_ORDER` 定义。
2. 把「静默错序」变成**导入期可检测错误**（重名、缺项、顺序约束、不可调用 fn）。
3. 对外契约不变：REGISTRY 19 项、`get_stage/get_fn/list_stages` 等 API、`/api/v1/stages`、多仓编排均不动。

**非目标**

- 不改任何阶段的执行顺序与语义（18 项全序保持上表）。
- 不动 `comparator`、不动 `Stage` 契约 dataclass、不引入阶段动态扩展机制。
- 不夹带 B-5（describe 缓存命名空间）等其它优化项。

## 四、推荐方案 A：单表驱动 + REGISTRY 镜像

### 4.1 数据结构

在 `pipeline.py` 原两表位置新增一个有序结构（用 `NamedTuple` 或 `@dataclass(frozen=True)`，由实施时按仓库风格定）：

```
StageSpec(name: str, kind: StageKind, fn: Callable[..., Any])

PIPELINE_STAGES: tuple[StageSpec, ...] = (
    StageSpec("pull",                StageKind.ADAPTATION, stage_pull),
    ...  # 18 项，顺序与现状完全一致
    StageSpec("persist",             StageKind.ADAPTATION, stage_persist),
)
```

### 4.2 三个替换点

1. `_wire_stages_to_registry()` 改为遍历 `PIPELINE_STAGES`，逻辑不变（已注册则覆盖 `fn/kind/planned`，未注册则 `register`）；模块加载时照旧调用一次。
2. `run_pipeline` 由表直接驱动：

   ```
   for _spec in PIPELINE_STAGES:
       _spec.fn(opts, result, progress)
   ```

   不再走 `REGISTRY.get_fn(name)` 间接层。

3. `PIPELINE_STAGE_ORDER` **保留为派生常量**：

   ```
   PIPELINE_STAGE_ORDER = tuple(s.name for s in PIPELINE_STAGES)
   ```

   兼容 `tests/test_pipeline_t5.py` 与外部潜在引用；因为它由单表派生，永不与表漂移。

### 4.3 导入期自检（关键防回归）

在 `_wire_stages_to_registry()` 之后做一次轻量校验，失败即导入失败（fail-fast，不在运行到一半才炸）：

- `name` 无重复、数量为 18；
- 每项 `fn` 可调用；
- `quality` 的位置晚于 `case_gen` / `llm_design`、早于 `persist`（把现有 T5 守护提升为硬约束）；
- 回注后 `REGISTRY` 仍为 19 项、无 `planned` 残留（复用 `test_stage_registry` 的口径）。

### 4.4 收益与代价

- 收益：两表合一（正确性↑）、`run_pipeline` 退化为一层直接映射（可维护性↑）、错序改由导入期发现（可观测性↑）。
- 代价：`pipeline.py` 核心段约 60 行重写；需全量核对顺序。改动面集中、无数据迁移，风险中低。

## 五、备选方案 B：REGISTRY 单源 + order 字段（本批不采用）

给 `Stage` 增加 `order: int | None`，在 `_build_registry()` 中按流水线顺序声明，`REGISTRY.pipeline_stages()` 返回按 `order` 排序的 18 项；`pipeline` 只消费该方法，`comparator` 标 `order=None`。

| 维度 | 方案 A（推荐） | 方案 B（备选） |
|---|---|---|
| 事实源 | `pipeline.py` 一张有序表 | `stage_registry.py` 的 order 字段 |
| 改动面 | 集中 `pipeline.py`，REGISTRY 不动 | 19 处注册 + Stage 契约 + registry API + 测试 |
| 回注循环依赖 | 保留现有手法（已验证可行） | 仍需回注 fn，无法真正消除 |
| 风险 | 中低 | 中高（顺序/契约同时改） |
| 适用 | 当前 18 阶段静态链 | 未来若阶段需动态插拔再考虑 |

原 review 建议「移除 REGISTRY 间接层」在方案 A 中落实为：**执行路径**不再经 REGISTRY；REGISTRY 仅保留契约镜像与查询 API。

## 六、分批实施（确认后执行）

| 批次 | 内容 | 交付/自测 |
|---|---|---|
| 1 | 先写守护测试：锁定 18 项全序、name 唯一、fn 可调用、REGISTRY 19 项不变、`comparator` 不进流水线；再落 `StageSpec` / `PIPELINE_STAGES` 与导入期自检（旧路径仍在） | 新增 `tests/test_pipeline_stage_order.py`；守护测试绿 |
| 2 | 切换 `run_pipeline` 到单表调用；删除 `_P1_STAGE_WIRING`；`PIPELINE_STAGE_ORDER` 改为派生常量；更新 `stage_registry.py` 头部过时注释（"不改写 pipeline 硬编码编排"已不成立） | 现有 `test_pipeline_t5` / `test_stage_registry` 全绿；无 API 变化 |
| 3 | 全量验证 + 提交收尾：八环门禁 → feature 分支 → PR → CI 5/5 → 合并 → 同步 main | 记录顺序比对结果到工作日志 |

## 七、风险与缓解

| 风险 | 缓解 |
|---|---|
| 顺序改坏（最危险） | 批次 1 先锁定**全序**（不只是 `quality` 三点）；导入期自检；批次 2 前后打印 18 项清单逐项比对 |
| `execute` 由回注注册，漏注册会 KeyError | 保留 `_wire_stages_to_registry`，自检断言 19 项且无 `planned` |
| 测试运行期替换 stage 函数失效 | 已核实现有测试无此用法；批次 2 实施前再 grep 一次 `setattr/patch.object` 复核 |
| 多仓链路回归 | 多仓复用 `run_pipeline`，跑多仓相关测试组（quality 聚合、逐仓结果）确认 |
| 与 `B-5` 等优化混做 | 本批只碰 `pipeline.py` 单表段与其守护测试，不动其它文件 |

## 八、验收口径

- **单测**：新增全序守护 + 现有 `test_pipeline_t5.py`、`test_stage_registry.py` 全绿；`PIPELINE_STAGE_ORDER` 派生值与合并前 18 项清单逐项相等。
- **全量**：testgen 八环门禁 `EXIT=0`（后台跑，约 6~7 分钟）；CI 5/5 全绿。
- **行为等价**：18 阶段执行顺序、`REGISTRY` 19 项注册结果、`/api/v1/stages` 返回均无变化；多仓逐仓生成仍复用同一链。
- **真机**：本批无 UI/API 契约变化，不做真机点击验收；如需冒烟，重启 sidecar 后跑一次最小生成任务确认阶段进度播报顺序即可。
- **回滚**：纯内存结构改动、无迁移；单 commit revert 即可。

以上为方案，待你确认后我再逐批实施（含代码与自测）。
