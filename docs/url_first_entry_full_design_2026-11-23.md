# 「地址优先」独立入口 · 完整分析与方案（2026-11-23）

> 文档性质：**方案**，不执行。本次仅做了只读代码勘察，未修改任何 `.py`/`.js`/`.html`/测试文件。
> 覆盖你提出的三组问题：① 输入后查/建项目并读模块；② 模块入库与代码扫描用例是否冲突；③ 重心从"代码扫描为主"改为"地址账号密码为主、变更记录辅助"的改动量与效果。

---

## 0. 结论先行（三条）

1. **查/建项目**：testgen 侧已有"按项目名 get-or-create"，但**没有按被测地址查重**；testhub 侧项目甚至没有地址字段。要做到"输入地址→查到已有项目→读数据；没有就新建并扫描"，需要**新建一条按 URL 归一化的项目查找链**，并把两侧项目的对应关系持久化——这是本次最大的一块新工作。
2. **模块入库会冲突，不建议新建"模块实体表"**：扫描出的"模块"其实是**源文件**，一个文件能产多个功能点、多条用例，粒度对不上；系统里本来就存在"同一页面两套功能点（静态+运行时）"并用专门合并逻辑消解。想长期整理扫描结果，**已有 `file_descriptions` 表（带幂等键和内容指纹）可直接复用**；想做业务模块实体挂接用例则会引入"同一功能两套数据、重复计数、归属不清"三类冲突。
3. **重心改为"地址为主、变更辅助"：改动中等偏小，且核心算法已经现成**。双入口合并、运行时优先、"变更靶向"（`stage_target_runtime`）都在，且靶向逻辑不依赖有代码。真正要改的是三处：**默认重心（mode/靶向开关）**、**纯地址时给靶向补源码文本与 hunk 的桥接**、**主平台参数透传**。效果好坏取决于场景——见第 3 节对比，我的建议是"双通道并存 + 地址优先为默认"，而不是删掉代码扫描。

---

## 1. 问题一：输入后查/建项目、读/扫模块

### 1.1 现状（事实与证据）

| 能力 | 现状 | 证据 |
|---|---|---|
| testgen 项目表 | 有 `projects` 表，字段 `id/name(UNIQUE)/local_path/git_url/branch/type/status/base_url/created_at` | `services/testgen/core/db.py:20-31` |
| 按名查重复用 | 有 `upsert_project(name, local_path, base_url=...)`：按 `name` 查，存在则 UPDATE、否则 INSERT | `core/store.py:43-74` |
| 按地址查重 | **没有**：无 `find_by_url`、无 host 唯一约束；`base_url` 列生产代码从未写入 | `core/store.py` 全局；`engine/pipeline.py:383` 只传 `(name, source)` |
| 缺省项目名 | 纯地址时 = `urlparse(base_url).netloc`（如 `host:8090`）；代码时 = 目录名 | `engine/pipeline.py:364-369 _default_project_name` |
| 每次生成的项目行为 | 同 name 复用、不同 name 新建（是 name 相等的副作用，不是 URL 查重；同 host 不同端口/路径会建多个项目） | `engine/pipeline.py:372-383 stage_register` |
| testhub 侧 Project | 无地址字段；地址只在 `ProjectEnvironment.base_url` | `apps/projects/models.py:5-29, 52-65` |
| 两侧项目对应 | **没有任何关联字段/映射表**；回流时要同时手传 `project_id`（testhub）与 `testgen_project_id`（sidecar） | `apps/testgen_integration/views.py:202-249`；`hooks.py:24-51` 明确"两表无任何关联字段" |

### 1.2 设计（按现有逻辑会做成什么样）

输入（地址 + 账号密码）提交后的流程：

```
输入地址 → 归一化 URL(去协议/尾斜杠/默认端口，取 scheme+host+port+根路径)
        → 按归一 URL 查「项目映射」
            ├─ 命中 → 读出 testgen project_id + testhub project_id → 展示该项目已有模块/功能点/用例
            └─ 未命中 → ① 新建 testhub Project（写入归一 URL/名称）
                        ② 新建 testgen project（upsert_project，把归一 URL 写进此前闲置的 base_url 列）
                        ③ 落一条「地址↔两侧项目 id」映射（新表，唯一键=归一 URL）
                        ④ 对该地址做首次发现 → 得到模块清单 → 展示
```

要点：
- **归一 URL 是新建项目的关键键**，建议独立成 `url_projects` 映射表（字段：normalized_url / testgen_project_id / testhub_project_id / host / created_at，唯一键 normalized_url），这样做的价值是**把两套孤立的项目 id 绑起来**，一次绑定，后续生成和回流都不用手传两个 id。
- **"读项目数据展示模块"**：对已有项目，模块数据来自两块——扫描阶段缓存的 `file_descriptions`（若有代码）与已落库的 `functional_points.module` / `cases.module`（字段都是路径推导的字符串）。要展示"这个地址有哪些模块"，最合适的数据源是**已生成功能点的 module 去重列表**（现成），而不是新建一张模块表。
- **"新建则扫描对应模块"**：地址通道下没有代码可扫，这里的"扫描"就是 `stage_runtime_ui` 的浏览器发现（列页面/接口/元素）。首次生成完成后，把这些页面的 module/area 汇总展示即可。

### 1.3 需要你确认

- 项目唯一粒度：按 **host**（`host:port`）还是按 **完整 URL 含路径**？（`/chat` 与 `/pc` 是一个项目还是两个）
- 映射表要不要同时自动建 testhub Project？（涉及 owner 归属、默认套件，需定规则）

---

## 2. 问题二：模块入库与代码扫描用例是否冲突

### 2.1 "模块"到底是什么（事实）

- **主平台没有 Module 模型**：层级是 `Project → TestCase`（直接 FK）与 `Project → TestSuite → TestCase`（多对多）。`module` 只是 analytics/defects/requirement 里的自由字符串字段。**testgen 回流时 module 被降级为 description 文本，不落字段**（`mapping.py:13-14, 188, 230-231`）。
- **扫描出的"模块"= 源文件**：`/api/v1/scan` 返回扁平 `items`（`path/name/functions/classes/bytes/change_status`），无 children；"模块树"只是前端按顶级目录分组渲染出来的（`web/index.html:2070-2076`）。scan **零写库**。
- **唯一入库的扫描衍生数据**：`file_descriptions` 表，键 `(repo_url, file_path)`，带 `symbols_sig` 内容指纹，用于描述缓存（`core/db.py:220-233`、`core/store.py:1839-1859`）。
- **用例的 module 从路径推导**：`module_of(rel)` 取路径中最有信息量的一段目录名（`fp_extract.py:137-146`），一路传到 `cases.module`。`area` 属于 test_points，表示"测什么对象"（URL/接口/元素），不是归属模块。
- **同一功能本就有两套功能点**：静态 `file_path=源码路径` 与运行时 `file_path=runtime:<url>`，靠 `fp_merge.py:154-174` 按"层类型+名称归一"判等、**运行时优先**消解；三通道 body/impact/runtime 有 `consistent` 自检（`quality.py:213-233`）。

### 2.2 冲突分析（结论：新建"模块实体表"会冲突，复用现有表不会）

| 冲突点 | 说明 | 证据 |
|---|---|---|
| C1 粒度错位 | scan 一项=一个文件；一个文件可产多个 FP、多条 Case。模块表按文件入库无法与用例一一对应 | `remote_scan.py:3`；`tp_expand.py:724-795`；`case_gen.py:476-488` |
| C2 同一功能两套数据 | 静态 FP 与运行时 FP 语义同一页面但 id 不同，已有专门合并；新表会成第三套 | `pipeline.py:1454-1471`；`fp_merge.py:154-174` |
| C3 唯一键两难 | `module_of` 产出的模块名不唯一、跨仓同名；用 path 做键又与 `file_descriptions` 重复 | `fp_extract.py:137-146`；`db.py:224-232` |
| C4 回流断链 | 主平台 TestCase 无 module/source/suite，新模块表无法与主平台用例建立归属 | `mapping.py:13-14, 188` |
| C5 重复计数 | scan 数 ≠ FP 数 ≠ TP 数 ≠ Case 数；模块维度与 body/impact/runtime 三通道交叉，再按模块计数会破坏 consistent 口径 | `app.py:2068`；`quality.py:213-233` |
| C6 语义混用 | scan 侧"模块"=源文件；需求侧"功能模块"=业务概念（纯计算不入库），一张表装两种会归属不清 | `remote_scan.py:3`；`advanced_analyzer.py:172-209` |

### 2.3 建议路径

- **目标若是"长期整理扫描出的文件/页面清单"**：不建新表，扩展 `file_descriptions`（它已经是"扫描元数据入库"且幂等+带指纹），地址通道的页面可另起同类结构但**沿用 `(源, 对象路径)` 复合键 + 内容指纹**口径。
- **目标若是"业务模块实体并挂接用例"**：先解决 C1~C5 才能做，尤其是 module 名不唯一与回流断链两处——否则必然出现同一功能两套数据、统计口径打架、归属说不清。**建议这一版不做，放到二期。**

---

## 3. 问题三：重心改为"地址账号密码为主、变更记录辅助"

### 3.1 改动大不大？——中等偏小，核心已现成

**已存在、可直接复用：**

| 能力 | 证据 |
|---|---|
| 双入口判定 `source_kind`（code / url / code+url）与运行时优先合并 | `pipeline.py:2129-2153`、`1454-1471` |
| 纯地址端到端生成（发现→FP→用例） | `pipeline.py:1395-1451`；`test_a2_runtime_cases.py:167-194` |
| **变更靶向核心 `stage_target_runtime`**：按 changed_files/hunks 只留相关运行时页面/接口/元素，**无 `has_code` 守卫**，纯地址也能跑 | `pipeline.py:1561-1603`；`engine/change_targeting.py:611-661` |
| 变更→页面/接口的映射算法（页面名推路由、接口字面量匹配） | `change_targeting.py:316-337, 404-436, 551-561` |
| `selected_paths` 只扫变更文件以压低代码扫描成本 | `pipeline.py:422-436` |
| 地址通道 0 条诊断（含靶向剔除原因） | `web/index.html:1260-1315` |

**需要新建/改动的四处缺口：**

1. **默认重心**：`mode` 默认 `"full"`、`target_runtime` 默认 `False`（`pipeline.py:143, 171`）。要"地址优先"需改默认或新入口固定参数。
2. **CLI 缺靶向开关**：`_TARGET_FIELDS` 只有 url/login_*，CLI 下 `url + changed_files` 不会靶向（`cli/main.py:378-396`）；HTTP 入口则默认靶向（`app.py:598`）。
3. **最关键——纯地址时靶向缺源码文本与 hunk**：`stage_target_runtime` 的 `sources=result.files`（纯地址下为空）、`_resolve_hunks` 三级来源全需代码/repo。后果：**页面目标还能靠文件名推断保留，接口目标一条都映射不到**。要"代码只用来算变更点"，必须保留一个**轻量取码 + 仅扫变更文件供给文本**的桥接（用 `selected_paths` 收窄，不产出/展开代码 FP）。
4. **主平台参数链断开**：`client.generate()`/`_build_generate_payload` 不传 `test_url/login_*/runtime_scope`（`client.py:112-157`），入口若在主平台必须补。

另外：`stage_target_runtime` 强绑定 `opts.is_incremental`（`:1575`），"地址为主但想 full 模式也按变更靶向"需放宽守卫并重评 `test_full_mode_is_not_targeted`。且**缺少 `test_url + incremental + changed_files` 的端到端测试**（现有 `test_target_runtime.py` 是单元级直调阶段）。

### 3.2 三种做法的效果对比

| 做法 | 改动量 | 用例覆盖 | 精准度 | 成本/耗时 | 适用 |
|---|---|---|---|---|---|
| **① 纯地址全站发现**（现状地址通道） | 无（已支持） | 最全，覆盖整个可达站点 | 低：改一个文件也生成全站用例，噪声大 | 浏览器遍历耗时最长 | 首次建项目、定期全站巡检 |
| **② 代码扫描为主**（现状默认） | 无（已支持） | 只覆盖代码可静态推断的面；**看不到真实渲染元素** | 中：变更定位准，但漏运行时 | 无浏览器，最快 | 快速 CI 门禁、纯后端改动 |
| **③ 地址为主 + 变更辅助**（你想要） | **中等偏小**（第 3.1 四处缺口） | 主产物是真实页面/元素；变更只做靶向收窄 | **最高**：真实可达面 ∩ 本次变更 | 需轻量取码+发现，比①略省（靶向剔除后展开少） | 日常迭代、每次提交后的针对性回归 |

**哪个效果好：**
- 若目标是**"每次变更生成一批能真跑的用例"**——③ 最好，它同时拿到"真实页面/元素"和"只测本次变更"，但要接受上面第 3 点那座"代码供给桥"的改动。
- 若目标是**"先把整个系统摸一遍、建项目基线"**——① 更合适，③ 的靶向在此时反而会漏。
- ② 不建议作为默认主路线（看不到运行时元素，正是你前几轮一直在补的短板）。

**我的建议**：不要删代码扫描，把它**降为"变更探测器 + 靶向证据源"**，默认走 ③；在"首次建项目/定期巡检"场景显式切 ①。这样既不浪费已有能力，又贴合你"地址账号密码为基础、变更记录为辅助"的定位。

### 3.3 分阶段落地（确认后才执行）

- **阶段 A（打地基）**：归一 URL + 项目映射表（问题一）+ 主平台地址参数透传
- **阶段 B（重心切换）**：新入口默认 ③；补轻量取码/变更文件文本供给桥；CLI 加靶向开关
- **阶段 C（模块整理，建议二期）**：先扩展 `file_descriptions` 做清单展示；**不新建模块实体表**，等 C1~C5 结论明确后再议
- **阶段 D（验收）**：补 `test_url + incremental + changed_files` 端到端测试 + 三做法效果实测对比

---

## 4. 需要你拍板的问题清单

1. 项目唯一粒度：host（含端口）还是完整 URL（含路径）？
2. 是否自动创建 testhub 侧 Project（含 owner/默认套件规则）？
3. 模块：只要"清单展示与整理"（复用 `file_descriptions`，推荐），还是要"业务模块实体挂接用例"（二期，冲突多）？
4. 重心：确认走"③ 地址为主 + 变更辅助"为默认、保留 ① 作巡检，还是别的组合？
5. 入口落点：沿用上一份文档的 A/B/C 选择（sidecar 独立页 / 主平台菜单页 / 独立工具）。

---

以上为方案，待你确认后我再逐批实施（含代码与自测）。
