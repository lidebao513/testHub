# 业务模块实体挂接用例 · 设计方案（任务 #436 / 阶段 C 二期）

> 文档性质：**方案，不执行**。本轮只做只读代码调研 + 设计，未修改任何 `.py`/`.js`/`.vue`/测试文件。
> 决策背景：2026-11-24 用户拍板「选 A 维持现状，B 方案延后到合适时机启动」。本文即 B 方案的设计稿，先备着，启动实现前需再确认。
> 依据：`docs/url_first_entry_full_design_2026-11-23.md` §2.2 的 C1~C6；代码调研证据见文末。

---

## 0. 一句话结论

**做成"正统实体"是可行的，但必须先解决两个死结：唯一键不能裸用 module 名（会跨仓撞名），用例归属不能靠 description 文本（现在就是断的）。** 解开后，真正要动的是 1 张新表 + TestCase 1 个 FK + 回流链路补 `module_id` + 模块 CRUD 与前端，工作量中等；`file_descriptions` 和 `TestSuite` 都不必新建或改造。

不建议用"给 tags 加 `mod:xxx`"的凑合写法当最终方案——它不是实体、无约束、反查低效，只适合当临时过渡。

---

## 1. 先看清现状（为什么不能简单做）

调研确认了 6 个冲突的真实形态，比原文档更具体：

| 冲突 | 代码事实 | 影响 |
|---|---|---|
| C1 粒度错位 | scan 一项 = 一个源文件；`module_of()` 取路径段名。一个 module 值对应 0~N 个 FP、0~N 条 Case | 模块表不能用"文件"当粒度，也不能假定与用例 1:1 |
| C2 两套数据 | 静态 FP `file_path=源码路径`、运行时 FP `file_path=runtime:<url>`；`fp_merge.py:154-174 source_rank()` 定义运行时优先，已有归一 | 挂模块前必须先走归一，否则新表成"第三套" |
| C3 唯一键两难 | **两个生成器都不带命名空间**：`module_of()`（`fp_extract.py:137-146`）与 `_module_of_path()`（`runtime_ui.py:2393-2396`）只看路径。真实值 `root`(23684 次)、`api`、`pc`、`chat` 在各项目大量重复 | 裸 module 名做唯一键必然跨项目/跨仓撞名 |
| C4 回流断链 | `TestCase` 无 `module/source/suite` 字段（`testcases/models.py:7-54`）；回流 `case_spec_to_testcase()`（`mapping.py:204-255`）把 module 拼进 description 追溯文本（`:230-231`），从不落字段 | 模块实体建了也挂不上用例，除非改 TestCase |
| C5 重复计数 | scan 数 ≠ FP 数 ≠ TP 数 ≠ Case 数；三通道 body/impact/runtime 有 `consistent` 自检（`quality.py:213-233`） | 按模块计数会破坏既有口径，不能覆盖原统计 |
| C6 语义混用 | scan 侧 module = 路径段（root/api/pc）；需求侧 `BusinessRequirement.module`（`requirement_analysis/models.py:177`）= 中文业务概念（`advanced_analyzer.py:172-209` 纯计算、不入库）。两者零关联 | 一张表装两种语义会归属不清 |

补充发现：**`apps/testsuites.TestSuite` 是死代码**（views 只有 import、`urls.py` 空、前端是占位页），虽然 M2M through 机制上能当"模块容器"，但语义是"执行分组且带 order"，且没有 API/前端，复用它等于从零建设 + 语义污染，不建议。

---

## 2. 核心设计：唯一键与归属链

### 2.1 模块唯一键（解 C3/C6）

新表 `biz_modules`（主平台侧），唯一键 **不是裸 module 名**，而是：

```
(project_id, namespace, name) 唯一
```

| 字段 | 含义 | 取值来源 |
|---|---|---|
| `project_id` | 归属项目（FK） | 回流时的 testhub project_id |
| `namespace` | 来源命名空间 | `code:<repo_binding_id>` / `runtime:<归一URL>` / `requirement:<document_id>` |
| `name` | 业务模块名 | 保留原 module 字符串（root/api/pc/用户管理…），仅在同一 namespace 内唯一 |
| `source` | 语义标记 | `code` / `runtime` / `requirement`，显式区分 C6 的两种语义 |

要点：
- **namespace 是解撞名的关键**。同一 `pc` 模块在 project A 的地址通道与 project B 的代码扫描下是两条不同记录，不再互相覆盖。
- **source 显式化**让 C6 不再是"混用"——三类模块并列存在，前端可按来源分组展示，不做强行统一。
- `file_descriptions` 继续做"扫描清单"（键 `(repo_url, file_path)`，指纹 `symbols_sig`，见 `core/db.py:220-233`），** BizModule 不替代它**。前者是"扫到了什么"，后者是"业务上怎么归类"，两者正交。

### 2.2 用例归属链（解 C4）

`TestCase` 增加：

```python
module = ForeignKey(
    "BizModule", null=True, blank=True, on_delete=SET_NULL,
    related_name="cases"
)
```

- 用 FK 而非 M2M：一个用例只归一个业务模块（M2M 的"一个用例多个模块"会让归属计数失真，正是 C5 要避免的）。
- `null=True` + `SET_NULL`：模块删除不删用例，历史用例留空归属，安全降级。
- **不动 tags 里的 `tg:<tc_no>` 幂等键**——归属与幂等是两件事，互不干扰。

回流链路补齐（这是现在真正断的地方）：

```
sidecar GET cases 已返回 module（app.py:1330）
   ↓
mapping.case_spec_to_testcase() 的 payload 增加 module_id（新增参数，由调用方传入）
   ↓
flow.sync_cases(tg_id, payload, module_resolver) 解析 (project, namespace, name, source) → BizModule
   ↓
两处 upsert（views.py:227 / url_sync_views.py:58-82）写 module_id
```

`module_resolver` 是新增的纯函数：输入 `project_id + module 字符串 + 来源上下文`，做 get-or-create 模块行。地址通道的 namespace 用归一 URL（复用 `core/url_identity.py`），代码通道用 repo_binding_id，需求通道用 document_id。

### 2.3 归一与计数不破坏（解 C1/C2/C5）

- **C2**：BizModule 只在 `flow.sync_cases` 这一层挂接，此时 FP 已经过 `fp_merge.py` 归一、三通道已生成完毕。模块是"事后归类"，不是新的 FP 来源，因此不会产生第三套数据。
- **C1**：模块与用例是 1:N，列表页按模块筛选用例即可，不需要"一个模块一个文件"的对应。
- **C5**：既有 KPI/三通道 `consistent` 自检完全不改。模块维度的统计**另起一个报表字段**（如 `by_module`），与 body/impact/runtime 三通道平行、互不覆盖。禁止用模块计数替代任何现有口径。

---

## 3. 改动面清单（真要启动时按此执行）

| 层 | 文件 | 改动 |
|---|---|---|
| 模型/迁移 | `apps/testcases/models.py`（或新建 `apps/biz_modules/models.py`） | 新建 `BizModule`；`TestCase` 加 `module` FK；1 个 migration |
| 序列化器 | `apps/testcases/serializers.py:43-126` | 三个 serializer 的 `fields` 增 `module`（`__all__` 的会自动带） |
| API | `apps/testcases/views.py:103-134` | `filterset_fields` 增 `module`；新增模块 CRUD ViewSet + URL |
| 回流 | `apps/testgen_integration/mapping.py:204-255` | payload 增 `module_id`；`flow.py:26-67` 增 `module_resolver`；`views.py:227-241`、`url_sync_views.py:58-82` 两处 upsert 写 `module_id` |
| sidecar | `services/testgen/service/app.py:1330` | **无需动**，已返回 module |
| 前端 | `TestCaseList.vue`、`TestCaseForm/Edit.vue`、`TestgenConsole.vue`、`UrlFirstConsole.vue`、`locales/.../testcase.js` | 模块展示/筛选/选择器、i18n 词条 |
| 测试 | 新增模块模型/CRUD/回流挂接单测 | 覆盖撞名隔离、删除降级、三通道口径不破坏 |

**不做**：不新建/改造 `file_descriptions`（它继续做扫描清单）；不复用 `TestSuite`（死代码 + 语义污染）；不动 sidecar 生成引擎（module 已透传）。

---

## 4. 分期建议（若启动）

1. **批次 1 · 地基**：`BizModule` 模型 + TestCase FK + migration + 模块 CRUD API（先能在库里建/查模块）。
2. **批次 2 · 回流挂接**：`module_resolver` + mapping/flow/两处 upsert 补 `module_id`，让生成用例自动归到模块；补撞名隔离与 SET_NULL 单测。
3. **批次 3 · 前端**：用例列表按模块筛选、表单模块选择器、地址/代码两个入口的模块展示。
4. **批次 4 · 验收**：真实项目跑一次生成→回流→按模块筛选，核对三通道 `consistent` 未被破坏、`tg:` 幂等仍成立。

每批跑八环门禁 + 集成测试，沿用仓库提交规范（feature 分支 → PR → CI → 合并）。

---

## 5. 证据索引（代码调研）

- `apps/testcases/models.py:7-54` TestCase 字段（无 module/source/suite）
- `apps/testcases/serializers.py:38-41,51-55,81-84,110-113` 三个 serializer 字段表
- `apps/testgen_integration/mapping.py:186-201,204-255` build_tags / case_spec_to_testcase（module 降级为 description 文本）
- `apps/testgen_integration/flow.py:26-67` sync_cases（`tg:` 幂等）
- `apps/testgen_integration/views.py:227-241`、`url_sync_views.py:58-82` 两处 upsert
- `services/testgen/engine/fp_extract.py:137-146` module_of（无命名空间）
- `services/testgen/engine/runtime_ui.py:2393-2396,2693-2694` _module_of_path / runtime: 前缀
- `services/testgen/engine/fp_merge.py:154-174` 静态/运行时归一与 source_rank
- `services/testgen/core/db.py:220-233` file_descriptions schema（复合主键 + symbols_sig）
- `services/testgen/core/url_identity.py` 归一 URL（namespace 来源）
- `services/testgen/service/app.py:1330` GET cases 已返回 module
- `services/testgen/engine/quality.py:213-233` 三通道 consistent 自检
- `apps/requirement_analysis/models.py:76-96,153-194` FunctionalInventory / BusinessRequirement.module
- `apps/requirement_analysis/advanced_analyzer.py:172-209` 需求模块纯计算不入库
- `apps/testsuites/models.py:7-34` TestSuite M2M through（死代码）
- 真实 module 值统计：`root`(23684)、`research-agent-source`(20006)、`ui_automation`(10585)、`pc`(3307)、`chat`(2890) — 各项目重复，佐证 C3

---

以上为方案，待你确认后我再逐批实施（含代码与自测）。
