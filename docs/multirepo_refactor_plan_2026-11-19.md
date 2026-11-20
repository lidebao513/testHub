# 被测目标多仓改造前期方案（2026-11-19）

> 背景：被测目标目前按**单仓**配置（一个 `repo_url` / `local_path`，对这个仓做扫描 + diff 对比 + 生成用例）。
> 目标形态：**一个项目（project）下关联 3 个仓库**，代码对比与用例生成跨这 3 仓进行。
> 本轮**只出方案，不实施**——按用户要求，等多仓改造之外的全部批次（E1–E5）跑完后再执行改造。
> 所有结论带 `文件:行号` 证据，已抽查核实关键项，非臆测。

## 0. 先说结论

- **没有可复用的多仓数据模型**：`projects` 表是严格单仓（`local_path`/`git_url`/`branch` 各一个标量列，`core/db.py:20-30`），`fp/tp/cases/runs/gen_tasks` 全表只有 `project_id`、无 `repo_id`（`core/db.py:33-162`），全仓无 junction 表。改造是**新建 project↔repo 关联 + 全链路单值改多值**，只有两处碎片可复用：`repo_configs` 多行清单、`file_descriptions` 的 `(repo_url,file_path)` 命名空间。
- **最危险的三处技术债**（不改多仓必然互相覆盖）：
  1. `fp_id_of` 哈希不含 repo 分量（`core/contracts.py:79-87`）——3 仓相同相对路径+函数名 → 同 `fp_id` → 静默合并/唯一键冲突；
  2. 按 `project_id` **全删重建**（`core/store.py:119`、`225`、`1544`）——生成仓 B 会删光仓 A 的功能点/测试点；
  3. 唯一索引 `(project_id, contract_id/tp_id)`（`core/db.py:235-240`）——跨仓同 ID 直接冲突。
- **推荐路线**：不拆库、不推翻 project 概念，采用 **"project 作容器 + repo_bindings 关联表 + repo 分量注入 ID/命名空间"** 的渐进改造；分 5 阶段，每阶段独立可回滚、可验收。

---

## 1. 现状证据（单仓假设清单）

### 1.1 数据模型层（根因）
| 位置 | 单仓假设 | 多仓后果 |
|---|---|---|
| `core/db.py:20-30` `projects` | 单 `local_path`/`git_url`/`branch`，`type='repo'` | project 等价于一个仓，无法容纳 3 仓 |
| `core/contracts.py:79-87` `fp_id_of` | `md5(ftype\|file_path\|name)[:8]`，无 repo | 跨仓同路径同函数 → 同 fp_id → 合并/冲突 |
| `core/contracts.py:90-106` `tp_id_of` | 由 fp_id 派生，同样无 repo | 同上 |
| `core/db.py:235-240` 唯一索引 | `(project_id,contract_id)` / `(project_id,tp_id)` | 跨仓同 ID 写冲突 |
| `core/store.py:118-119,225,1544` | `DELETE ... WHERE project_id=?` 后整批插 | 生成仓 B 删光仓 A 的 FP/TP |
| `core/db.py:168-179` `repo_configs` | 多行清单但**无 project_id 外键** | 无法表达"项目 X 拥有仓 A/B/C" |
| `core/db.py:207-215` `file_descriptions` | 复合主键 `(repo_url,file_path)` ✅ | 唯一有 repo 命名空间的正面案例 |

### 1.2 请求契约层（入口）
`service/app.py` 全部单值：`PipelineRequest:234-238`（local_path+repo_url）、`ExecuteRequest:343-355`、`WebhookRequest:877-888`、`PullRequest:1006-1017`、`StageAnalyze/CompareRequest:1077-1093`、`RefsRequest:1384-1392`、`ScanRequest:1805-1812`；`engine/pipeline.py:109-146` `PipelineOptions` 单 `local_path/repo_url/base/target/changed_files`；`cli/main.py:58-66,115-131` 单值参数。

### 1.3 幂等与缓存（串味风险）
- `_idem_key_for_generate`（`app.py:690-712`）只含单 `local_path/repo_url`；`genexec:` 键（`:840-850`）；`_idem_key_for_execute`（`:669-687`）只含 project_id 无 repo。
- `find_by_idempotency`（`service/tasks.py:205-208`）全局按 key 查、无 project/repo 作用域。
- describe 缓存按 `(repo_url,file_path)` 查（`app.py:1549-1571`），但前端 `lastRepoUrl` 单值提交（`service/web/index.html:1819,1935`）→ 3 仓同名文件描述串味。

### 1.4 diff / 对比链路
仓库无 `diff_analyzer.py`，实际在 `diff_tag.py:282-311`（`build_context(repo,base,target)` 单仓）、`change_targeting.py:609-632`（变更文件扁平列表无 repo 维度）、`repo_facts.py:592-611`（单 `{路径:SourceFile}` 字典，跨仓路径覆盖）、`stage_snapshot.py:36-40`（`_slug` 只取路径末段，同名仓冲突）、`stage_compare.py:63-96`、`pull.py:229-236,402-440`。

### 1.5 分支/提交/Codeup
`/api/v1/branches`（`app.py:1470-1505`）、`/commits`（`:1508-1523`）、`_build_codeup_client`（`:1457-1467`）、`codeup_client.py:70,165,190,210,234,268`（全部单 `repo_id: str`）、`remote_scan.py:269-460`。这些是**只读能力**，改造相对独立、风险低。

### 1.6 前端
`service/web/index.html:115-121` 单选下拉 + 单输入框；`applyRepoPreset:574-578` 单 `matchedRepo`；`buildBody:749-790` 单值请求体。

---

## 2. 目标形态

```
Project(容器, 一个项目)
 ├── repo_bindings  ── repo_A (local_path / repo_url / base / target / ref)
 │                  ── repo_B
 │                  ── repo_C
 ├── 数据行带 repo_binding_id（FP/TP/Case/change_log/run/gen_task）
 └── 一次"对比"= 对绑定的 N 个仓各自算 diff，按 repo 命名空间聚合
```

**核心不变量**（贯穿所有阶段，写在设计最前面）：
1. **ID 全局唯一**：任何跨仓持久化的 `fp_id/tp_id/case contract_id` 必须含 repo 分量或 repo_binding_id。
2. **替换以 repo 为界**：任何"先删后插"的粒度从 `project_id` 收窄到 `(project_id, repo_binding_id)`，绝不因生成一个仓而删另一个仓的数据。
3. **幂等键含仓集合**：一次任务涉及的仓（有序 id 列表）必须进键；同一 project 下不同仓集合 = 不同任务。
4. **默认单仓兼容**：未绑定多仓的旧 project 行为完全不变（repo_binding_id 可空，走单仓分支）。

---

## 3. 分阶段改造方案

### 阶段 0 — 契约与数据模型（地基，必须最先）
- `core/db.py` 新增 `repo_bindings` 表：`id, project_id, repo_key, local_path, repo_url, base, target, ref, created_at`，唯一键 `(project_id, repo_key)`；`repo_key` 为项目内稳定短名（如 `backend`/`frontend`/`shared`）。
- `projects` 表语义调整为容器：`type` 增 `'project'`；保留旧 `local_path/git_url/branch` 列做向后兼容（旧项目=隐式单仓 binding）。
- `fp/tp/cases/change_log/runs/gen_tasks` 增 `repo_binding_id INTEGER`（可空）。`file_descriptions` 已有 repo 维度，保持。
- 唯一索引扩展为含 repo_binding_id：`(project_id, repo_binding_id, contract_id)` 等。
- **ID 生成**：`fp_id_of` 增可选 `repo_key` 参数参与哈希；`tp_id_of` 随 fp_id 自动带 repo。旧调用不传 → 行为不变（兼容冻结契约）。
- 迁移脚本 `scripts/migrate_multirepo.py`：建表 + 为现有 project 建一条隐式 binding + 回填 repo_binding_id。
- 验收：迁移可逆（备份 db）；旧 project 数据零变化；单仓回归全绿。

### 阶段 1 — 请求契约与编排（让多仓能进得来）
- 新增 `repos: list[RepoSpec]` 字段（每项含 repo_key/local_path/repo_url/base/target/ref），与旧单值字段**并存**：`repos` 非空时优先，否则回落单值（兼容旧客户端）。
- `PipelineOptions` 增 `repo_specs`；`stage_pull`/`stage_register` 改为**按 binding 循环**：每个仓独立 `pull` + 独立 `upsert_project_binding`，不再把 3 仓注册成 3 个 project。
- `stage_register` 关键：多仓时一个 project 下建 3 条 binding，而非 3 个 project（`app.py:739` 的单 project_id 语义保持）。
- 幂等键：`_idem_key_for_generate` 增 `repo_signature = sorted([(repo_key,local_path,repo_url,base,target)...])` 进键；`_idem_key_for_execute`/`genexec:` 同理带 repo 集合。
- 验收：一次请求带 3 仓能完成 pull+register；3 仓不同组合生成不同 task（不撞幂等键）；旧单值请求产物不变。

### 阶段 2 — 对比与 diff 链路（核心能力）
- `diff_tag.build_context` / `pull` 改为对每仓算独立 diff（各仓 base/target 不同），结果按 `repo_key` 分组。
- `change_targeting.build_target_plan` 变更文件项带 `repo_key`；`repo_facts.build_repo_facts` 接收 `{repo_key: {path: SourceFile}}`，路径按 repo 命名空间隔离，消除覆盖。
- `stage_snapshot._slug` 改为含 project_id+repo_key（消除同名仓冲突）；`stage_compare` 支持同 project 下跨仓阶段对比 + 逐仓对比两种模式。
- `core/store.save_functional_points/save_test_points` 删除粒度收窄到 `(project_id, repo_binding_id)`。
- 验收：3 仓各造 1 个同路径同函数 → 3 个不同 fp_id、各自独立存在；重新生成仓 B 不删仓 A；跨仓 import/路由不串。

### 阶段 3 — 只读能力多仓化（分支/提交/Codeup/扫描）
- `/branches`、`/commits`、`_build_codeup_client`、`codeup_client` 各方法增 repo 维度（可按 repo_key 或 repo_url 指定，默认取第一个 binding 兼容旧调用）。
- `remote_scan` 支持遍历 project 的所有 binding。
- 验收：一个 project 能一次列出 3 仓的分支/提交；旧单仓端点行为不变。

### 阶段 4 — 前端与体验
- `repo_preset` 由单选改为多选/项目级仓库清单；`buildBody` 组装 `repos[]`。
- describe 的 `lastRepoUrl` 改为按条目自带 repo_url，消除缓存串味。
- 报告按 repo 分组展示，头部注明本次对比涉及哪几个仓。
- 验收：UI 能选 3 仓一次提交；报告标注来源仓；describe 同名文件按仓隔离。

### 阶段 5 — 收尾与治理
- `repo_configs` 加 project_id 归属、`seed_repo_configs` 绑定项目；CLI 增 `--repo-spec` 多仓参数；`docs` 与 `AGENTS.md`/`CODE_STANDARDS.md` 仓库约定更新。
- 全量回归 + 多仓 E2E（3 仓 fixture）+ 八环门禁。
- 验收：单仓全量测试不回退（当前基线 967 passed, 1 skipped）；多仓 E2E 覆盖"ID 不冲突 / 不互相删除 / 幂等不撞 / 报告分组"四点。

---

## 4. 兼容策略（红线）

1. **旧单仓零回归**：所有新增字段/参数可空，旧调用路径完全不变——这是整个方案的硬约束，每阶段验收都必须跑单仓全量。
2. **ID 冻结契约**：`fp_id_of`/`tp_id_of` 现有签名与输出在新参数缺省时逐字节一致（`docs` 有"编号算法冻结，不可改"注释，`contracts.py:77`）。
3. **迁移可回滚**：阶段 0 迁移脚本先备份 db；新增列不删旧列。
4. **不碰写保护**：多仓改造不改变 E1 刚修的限流写保护、`allow_write` 语义。

## 5. 风险与开放问题

- **三个仓的 base/target 如何对齐**：各仓分支/提交节奏不同，"一次对比"是取各仓当前 head，还是指定同一时间窗？**建议先按"各仓各自的 base/target"实现**，时间窗对齐作为阶段 2 的可选增强。
- **跨仓依赖**：仓 A 引用仓 B 的组件时，`repo_facts` 的 import 边是否要跨仓解析？**建议阶段 2 先不跨仓解析**（保持每仓独立事实），跨仓影响面作为后续。
- **project 与 repo 的命名冲突**：旧 project 名与 repo_key 可能重名，需唯一约束设计（`(project_id, repo_key)` 而非全局）。
- **要不要拆库**：本方案**不拆库**，单 SQLite 内加表加列；若后期 3 仓数据量激增再评估分库（本次不在范围）。

## 6. 执行顺序与批次关系

- 本方案 **等 E1–E5 全部批次完成后再执行**（用户要求）。
- 多仓改造自身按阶段 0→5 顺序执行，阶段 0 是地基，必须最先且单独验收。
- 每阶段结束跑：单仓全量回归 + 该阶段多仓新增测试；全绿才进下一阶段。

以上为前期方案，待你确认后我再在全部批次完成后按阶段实施（含代码、迁移脚本与自测）。

---

## 7. 实施记录（2026-11-19，阶段 0–5 已全部落地）

### 7.1 各阶段结果

| 阶段 | 交付 | 验收（单仓全量累计） |
|---|---|---|
| 0 地基 | `repo_bindings` 表 + 6 表 `repo_binding_id` 可空列 + 唯一索引改 `COALESCE(repo_binding_id,0)` + `fp_id_of(repo_key=...)` 冻结兼容 + `scripts/migrate_multirepo.py` | 1048 passed |
| 1 契约与编排 | `RepoSpec`/`PipelineRequest.repos`/`PipelineOptions.repo_specs` + `run_multirepo_pipeline`（逐仓子 opts + 共享 project + 结果聚合）+ 幂等键 repo_signature + stage_register/persist 按仓 | 1055 passed |
| 2 diff 链路 | 逐仓独立跑天然隔离 diff/repo_facts/change_targeting；`stage_snapshot.StageStore(namespace=...)` 让同名仓快照不撞（端点带 project_id/repo_key） | 1059 passed |
| 3 只读能力 | 新增 `POST /api/v1/repos/refs` 批量端点（branches/commits/scan 按 repo_key 分组、单仓失败不阻断） | 1064 passed |
| 4 前端与报告 | `buildBody` 经 `window.MULTI_REPO_SPECS`/`setMultiRepos` 组装 `repos[]`；describe 多仓禁用缓存防串味；报告 `repo_summary` + MD「十、仓库分组」（旧单仓不输出） | 1069 passed |
| 5 治理收尾 | CLI `--repo-spec`（JSON/@文件）→ `run_pipeline_auto`；多仓总验收四点 E2E | **1074 passed**（最终） |

### 7.2 关键设计偏差（相对原方案，已确认更优）

1. **采用「逐仓独立跑完整 pipeline」而非「单 pipeline 内多仓 map」**：
   原方案设想把 `diff_tag`/`change_targeting`/`repo_facts` 改成接收多仓字典。实际实现改为
   `run_multirepo_pipeline` 为每个仓派生一个**单仓子 opts**、复用同一条 18 阶段链逐仓跑，
   再聚合结果。这样 diff hunk / `result.files` / `repo_facts` / 变更清单天然按仓隔离
   （各自独立 result 对象），**无需改这几个模块**，侵入面大幅缩小、隔离更彻底。
   `repo_facts` 的跨仓 import 边按原方案「阶段 2 先不跨仓解析」处理。
2. **`repo_configs` 保持全局清单，不加 project_id**：它是「一次配置、多项目复用」的仓库
   凭证库；项目↔仓的归属由 `repo_bindings` 表达。强行给 `repo_configs` 加 project_id
   会破坏复用语义，故治理阶段不改它。
3. **前端采用渐进注入而非重做多选控件**：`setMultiRepos(specs)` 由项目视图注入仓库清单，
   `buildBody` 有则组装 `repos[]`、无则走原单仓字段。旧 UI 零改动，具备多仓提交能力；
   后续项目视图接入 `repo_bindings` 时调用该注入点即可。

### 7.3 兼容与验收

- 旧单仓全部测试零回归（每阶段全量绿）；`fp_id_of` 缺省输出逐字节冻结；报告旧单仓不输出分组章节。
- 迁移脚本幂等、先备份；`CREATE UNIQUE INDEX IF NOT EXISTS` 对旧库同名索引不更新定义的问题
  由脚本显式 `DROP` + 重建处理。
- 多仓 E2E 四点全绿：跨仓同路径同函数 fp_id 不同 / 重跑一仓不删另一仓 / 不同仓集合幂等键不撞 /
  报告按仓分组。
- **八环门禁全绿**：secret / lint / format / enum / structure / type(mypy) / security(bandit) / test；
  覆盖率 **82.69%**（阈值 60%）；最终 **1074 passed, 1 skipped**（改造前基线 1037）。
- 6 个新测试文件：`tests/test_multirepo_stage0.py` ~ `stage5.py`（共 37 例）。
- ~~**尚未接入**：项目视图 (`index.html` 侧) 需从 `repo_bindings` 读清单并调用 `setMultiRepos`~~
  → **M1 已接入（2026-11-20，见 §8）**：项目视图可从 `repo_bindings` 读清单注入。

### 7.4 门禁修复（既有未提交代码，非多仓逻辑，纯机械修正）

跑 `scripts/gate_all.py` 全绿过程中，managed 工具链（ruff 0.16.6 / mypy / bandit 1.9.4）
暴露了几处**先前批次遗留**的问题，一并修正（均不改运行语义）：

- `tests/test_f1_pull.py`：managed ruff 格式差异 → 用 managed ruff 重排；
- `engine/change_targeting.py`：`_file_kind` 内部标签 `"api"` 与枚举取值碰撞触发 check_enums
  → 改名 `"api_layer"`（handler key 同步，行为不变，11 例测试通过）；
- `engine/fp_extract.py`：`ast.walk` 循环变量与外层 `ast.stmt` 变量类型冲突 → 改名 `walk_node`；
- `engine/repo_facts.py`：`_OBJ_PATH_RE.search()` 可能为 None → 加空值保护；
- `engine/pipeline.py`：Codeup compare 的 `from_ref/to_ref` 为 `str|None` → `or ""` 收窄；
- `service/app.py`：三处 SHA1 幂等键摘要加 `# nosec B324`（指纹用途、非安全，与 contracts.py 约定一致）；
- `RefsRequest(branches)` 显式补 `ref=""` 满足 mypy 构造签名。

---

## 8. M1：项目视图接入多仓（2026-11-20）

背景：阶段 4 只留了 `setMultiRepos(specs)` 注入点与 `buildBody` 的 `b.repos` 组装，
但**没有数据源端点、也没有调用注入点的 UI**，Web 控制台仍只能手填单仓生成。M1 补齐这条链路。

### 8.1 改动
- **后端** `service/app.py`：新增 `GET /api/v1/projects/{pid}/repos`（`require_auth`）——
  从 `store.list_repo_bindings(pid)` 读清单，转成 `RepoSpec` 可消费结构
  （`repo_key/local_path/repo_url/base/target/ref`，空 base/target→`None`），
  并带 `multi`（binding>1）标志；空项目/unknown 返回 `repos:[]` 回落单仓。
- **前端** `service/web/index.html`：
  - 「③ 项目列表」加「项目ID 输入 + 载入多仓 / 回到单仓」控件，项目每行加「载入多仓」链接；
  - `loadMultiRepos(pid)` 调新端点 → 映射成 specs → `setMultiRepos(specs)`；
  - `renderMultiRepoBox()` 显示当前模式与仓清单（由 `setMultiRepos` 自动刷新）；
  - `exitMultiRepos()` 清空注入回到单仓。
  - 旧单仓零回归：未注入时 `window.MULTI_REPO_SPECS` 为空，`buildBody` 走原单仓字段。

### 8.2 验收
- 新增 `tests/test_multirepo_stage6.py` 6 例：端点返回结构/multi 标志/空项目/single binding/
  unknown 项目 + 前端静态守护（端点调用、注入刷新、载入/退出 UI、buildBody repos 保留）。
- 相关测试 10 passed（stage6 + stage4）；全量 **1076 passed, 1 skipped**（M1 前 1074）；
  八环门禁全绿，覆盖率 82%+。
- 本项仅打通「读清单→注入→一次生成」的 UI 链路；**仓库绑定本身仍由 CI/CLI 生成期或
  `scripts/migrate_multirepo.py` 注册**（M2 负责真实库回填），M1 不新增绑定写入入口。

---

## 9. M2：真实库执行迁移（2026-11-20）

### 9.1 迁移前体检
真实库 `services/testgen/data/testgen.db`（99,676,160 B，WAL，`testgen.db-wal` 为 0 B）：
- `schema_version=6`，无 `repo_bindings` 表，六张数据表全缺 `repo_binding_id` 列；
- 42 个 project；六表合计 62,756 行（FP 9,936 / TP 21,725 / Case 30,839 / change_log 58 /
  runs 119 / gen_tasks 79）；三个唯一索引仍是旧定义 `(project_id, xxx_id)`；
- 端口 8910/8100 未监听（sidecar 计划任务 `testhub-sidecar` 为 Ready），迁移期间无写入占用。

### 9.2 备份（两份）
1. **一致性备份**（先做）：用 `sqlite3.Connection.backup()` 生成
   `testgen.db.pre-multirepo-consistent-20261120-094346.bak`，`PRAGMA integrity_check=ok`，
   各表行数与原库逐表相等。backup API 会带上 WAL 已提交内容，比直接复制主文件可靠。
2. **脚本自带备份**：迁移时脚本再 `copy2` 一份
   `testgen.db.pre-multirepo-20261120-094450.bak`。

### 9.3 执行前发现并修复的脚本缺陷
`--dry-run` 在真正改造前的库上崩溃：`sqlite3.OperationalError: no such table: repo_bindings`。
根因是 dry-run 刻意跳过 `init_db`，但后续仍无条件查 `repo_bindings`。
修复：`migrate()` 增 `has_bindings` 表存在性守卫，两处查询在无表时跳过（不写盘）；
补回归 `test_migrate_dry_run_on_legacy_db_without_bindings_table`——构造真正的 v6 形态库
（DROP 六个引用列的新索引 → DROP COLUMN → 恢复旧索引 → DROP TABLE），跑 dry-run 后断言
`bindings_created=1 / indexes_rebuilt=3` 且库形态未被改动。
修复后预演：`DRY-RUN: {'bindings_created': 42, 'rows_backfilled': 0, 'indexes_rebuilt': 3}`。

### 9.4 执行结果
```
[migrate] DONE: {'bindings_created': 42, 'rows_backfilled': 62724, 'indexes_rebuilt': 3}
```
复核（迁移库 vs 一致性备份）：
- `schema_version=7`；`PRAGMA integrity_check=ok`；
- `repo_bindings` 42 行，42 个 project **全部**有 `repo_key='default'` 绑定；
- 六表行数与备份**逐表相等**（零丢失），三个唯一索引均含 `COALESCE(repo_binding_id, 0)`，
  三个普通索引 `idx_{fp,tp,cases}_repo` 存在；
- 回填 62,724 而非 62,756，差额 32 行**全部是 `gen_tasks.project_id IS NULL`** 的历史失败任务
  （如 `gen-ca7da72d5261`，被测目录不存在），无 project 可归属，保持 NULL 按 `COALESCE` 归 0 桶，
  不影响唯一性——属预期，不是漏迁。

### 9.5 旧单仓零回归验证
用迁移后真实库经 `core.store` 读取，与迁移前备份库直接 SQL 计数对比：
- 数据量最大的 project 24：迁移前 `(FP=924, TP=4660, Case=4660)`，
  迁移后 `store.list_*` 返回 `(924, 4660, 4660)`，**逐项一致**；
- `report_snapshot(24)` 正常生成；`list_projects()` 仍返回 42 个项目；
- `store.list_repo_bindings(1)` 正确读到 default binding（`local_path='demo-integ'`）。

### 9.6 验收
- `tests/test_multirepo_stage0.py` 12 passed（含新增 dry-run 旧库守护）；
- 全量 **1077 passed, 1 skipped, 3 deselected**（M2 前 1076，净增 1 例）；
- 八环门禁全绿：`GATE_EXIT=0`，secret / ruff lint / ruff format / check_enums /
  check_structure / mypy / bandit / pytest 全部 rc=0，覆盖率 82%+。
- **未提交**（按仓库约定，M1 + M2 改动合并后由用户决定是否入库）。
