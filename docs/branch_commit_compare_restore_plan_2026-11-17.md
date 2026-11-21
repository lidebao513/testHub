# 恢复「分支历史提交对比」功能 · 检查结论与实施方案

> 日期：2026-11-17　状态：**仅方案，待确认后实施**
> 诉求（用户原话）：获取分支后，选择一个分支还能选该分支下不同历史时间点的提交做对比；并且「目标 ref」和「基线 ref」两个选择处都要能选历史提交。先检查、给方案、确认后再执行。

---

## 1. 一句话结论

功能**没有丢，而是"后端全在、前端入口断了"**：历史提交列表接口、阶段快照/对比引擎、以及带完整 UI 的 `console.html` 都还在代码里，但当前用户访问的 `/testgen/` 页面被换成了 sidecar 的 `index.html`，里面的两个分支下拉只列"分支名 + 最近一次提交"，**既没有"选历史提交"的控件，也没有"阶段对比"入口**；而带这些功能的 `console.html` 现在没有任何路由/视图引用，成了孤儿页面。

所以不是"功能被删"，是"页面被替换导致功能不可达"。恢复它不需要重造轮子。

---

## 2. 现状盘点（已核实，含文件与行号）

### 2.1 用户当前看到的页面：`/testgen/` → sidecar 的 index.html
- `backend/urls.py:52-53`：`/testgen/...` 全部反代到 sidecar 根（`testgen_web_proxy`）。
- sidecar 首页 `services/testgen/service/app.py:2013-2023` 直接读 `service/web/index.html` 返回。
- 该页面 B 区「代码生成」有两个下拉：
  - `index.html:154` `scan_ref`（分支／增量目标 ref）
  - `index.html:158` `scan_base_ref`（基线 ref）
- `loadBranches()`（`index.html:1491-1525`）把接口返回的**分支列表**填进两个下拉，每项显示"分支名 · 最近提交时间 · 提交标题"——**只有分支的最新提交，没有该分支的历史提交可选**。
- `onBranchChange()`（`index.html:1529-1532`）仅把分支名同步到 `target_ref`，**没有联动加载该分支的提交历史**。
- 全文件搜索：`index.html` 里没有任何 `stage-analyze`/`stage-compare`/阶段对比入口（只有生成进度里的 "stage" 字样，无关）。

### 2.2 后端能力：全部还在，且可用
| 能力 | 位置 | 状态 |
|---|---|---|
| 列某 ref 的历史提交 | `app.py:1367-1382` `POST /api/v1/commits` → `codeup_client.py:268-292` `list_commits()` | ✅ 在 |
| 分支列表（含每个分支最新提交） | `app.py:1329-1364` `POST /api/v1/branches` → `list_branches()` | ✅ 在（今天刚修复 401） |
| 阶段快照引擎 | `engine/stage_snapshot.py`（11 KB） | ✅ 在 |
| 两阶段对比引擎 | `engine/stage_compare.py`（3.7 KB） | ✅ 在 |
| 阶段 API（3 个端点） | `app.py:932-1050`：`stage-analyze` / `stage-compare` / `stages` | ✅ 在 |
| Django 代理层 | `apps/testgen_integration/views.py:471/489/515`、`client.py:450-492`、`urls.py:50-52` | ✅ 在 |
| 真·版本 diff | `engine/diff_tag.py` `build_context(base..target)` | ✅ 在（设计文档确认） |

### 2.3 带完整功能的页面：console.html 成了孤儿
- `apps/testgen_integration/templates/testgen/console.html` 里**已经有你要的全部 UI**：
  - `console.html:787-851`：分支下拉 + **提交预览** `previewBranchCommits()`，调用 `API.commits`（`:327`）展示某分支最近 15 条提交（带日期、作者、标题）。
  - `console.html:280-316`：「分支阶段对比」区块——登记阶段快照、选 from/to 两阶段、对比（新增/失效/保留测试点 + diff 统计）。
  - `console.html:109`：导航锚点 `#sec-stage-compare`。
  - `console.html:87-91`：`?embed=1&zone=...` 嵌入模式，支持按功能区（含 `stage-compare`）切片渲染。
- 但该页面的**视图类与路由已被移除**：`views.py` 中无 console 渲染视图；`backend/urls.py` 只有 `/testgen/`→sidecar 与 `/api/testgen/*`，没有指向 console.html 的路由。
- Git 史：`3a2a338` 曾引入 console.html 路由，`cf3b737/be03d16` 落地了阶段对比后端+视图，后续改成 sidecar index.html 代理时（约 `docs/testgen_api_prefix_plan_2026-10-30.md`）入口被切断。

---

## 3. 为什么"现在不存在了"（根因）

一次 UI 架构迁移：从"Django 自渲染 console.html"切到"反向代理 sidecar 的 index.html"（同源嵌入，见 `backend/urls.py:47-57` 注释）。迁移时**只迁了主流程（代码生成/扫描/回流/判定），没迁走 console.html 上独有的三块能力**：
1. 分支下历史提交预览（选分支 → 看/选该分支历史提交）；
2. 阶段快照登记与两阶段对比；
3. 对应的 UI 入口（导航锚点 + zone 切片）。

后端接口和引擎一行没删，所以"功能还在，只是点不到了"。

---

## 4. 实施方案（三选一，建议 C）

### 方案 A：恢复 console.html 独立入口（最小改动，UI 现成）
- 在 `views.py` 加一个 `TemplateView`（渲染 `testgen/console.html`），在 `backend/urls.py` 加路由，如 `/testgen-console/`（保留 `/testgen/` 不动，避免影响现有嵌入）。
- 优点：console.html 的提交预览、阶段对比 UI 全部现成，几乎零前端开发；后端接口都在。
- 缺点：出现两个 testgen 页面，用户要在两个入口间切换；console.html 的 API 走 `/api/testgen/*`（Django 代理层），与 index.html 走 `/testgen-api/v1/*` 双轨，长期维护分叉。

### 方案 B：把能力直接补进当前 index.html（统一入口，推荐未来形态）
在 `services/testgen/service/web/index.html` 里增补：
1. **两个分支下拉各加一个"历史提交"二级选择**：新增 `scan_ref_commit` / `scan_base_ref_commit` 两个 select（可放在对应下拉下方，默认"该分支最新提交"）。
   - 触发：用户选定分支后（`onBranchChange` / `scan_base_ref` 的 onchange）调用 `POST /api/v1/commits`（body: `{repo_url, code_source, ref:<分支>}`），把返回提交填进二级下拉（显示 时间 + 短 sha + 标题，取前 N 条 + 可滚动/搜索）。
   - 传值：把选中的 commit sha 作为实际 `ref/target_ref/base_ref` 传给 scan（commit sha 本身就是合法 ref，`diff_tag.build_context` 支持），即"选择某分支下某历史时间点做对比"。
2. **阶段对比入口**：把 console.html 的"分支阶段对比"区块（登记快照 / from-to 对比）迁进 index.html，接口改走 sidecar 自己的 `/api/v1/stage-analyze` 等（sidecar 已实现，无需绕 Django）。
3. 复用现有 `api()` 封装（已修好错误展示），沿用今天的凭证链路。
- 优点：单一入口、单一 API 前缀，用户体验连贯，符合当前架构方向。
- 缺点：前端工作量最大（要把 console.html 的两块 UI 翻译到 index.html 的风格与函数中）。

### 方案 C（建议）：B 的"提交选择"全量 + A 的"阶段对比"最小接入，分两批
- **批次 1（核心，直接解决你截图里的诉求）**：按方案 B-1 给两个分支下拉各补历史提交二级选择，选 commit 即作对比 ref。改动集中在 `index.html`（+ 单测/UI 自测），不动后端。
- **批次 2（阶段对比）**：先恢复 console.html 入口（方案 A，几行路由+视图，UI 现成）作为"阶段快照对比"页；若确认要统一到 index.html，再把该区块迁过去并下线 console.html。
- 优点：先最快交付你最关心的"两处都能选历史提交做对比"，阶段对比用现成页面兜底，风险最低。
- 缺点：短期仍两个入口，批次 2 需再确认是否合并。

---

## 5. 关键实现要点（不论选哪个方案）
1. **commit 作为 ref 的合法性**：`/api/v1/commits` 返回的 commit id 可直接作为 `ref`/`base_ref` 传入 scan/generate；`diff_tag`、`remote_fetch` 均按 ref 取码（设计文档 `branch_stage_compare_design_2026-10-20.md` 已确认底层支持）。
2. **凭证复用**：历史提交接口同样走"仓库配置凭证"链路（今天刚修复的 PAT），无需额外配置。
3. **远端优先完整 commit id**：Codeup 的 commit id 用返回值原样传递，不做缩写（设计文档 B §3 明确拒绝 4-39 位缩写 SHA）。
4. **force push/rebase 兜底**：历史 commit 可能失效，接口报错时前端提示"该提交不可达，请重新加载分支"，不让整个扫描崩掉。
5. **UI 细节**：二级提交下拉默认"最新提交"（等价当前行为，向后兼容）；每个选项显示 `MM/DD HH:mm · 短sha · 标题`；两个下拉独立加载、互不影响。
6. **测试**：补 `POST /api/v1/commits` 的前端联调自测（本地 git 仓库 + Codeup 只读各一条），覆盖"选分支→出历史提交→选两个不同时间点→扫描按 diff 生成"主链路；阶段对比入口仅做可达性冒烟。

---

## 6. 涉及文件预估
- 批次 1：`services/testgen/service/web/index.html`（唯一改动文件）。
- 批次 2：`apps/testgen_integration/views.py`、`backend/urls.py`（恢复 console 视图/路由）；可选再把 UI 迁进 index.html。
- 不改：所有后端接口与引擎（已具备能力）。

---

以上为方案，待你确认后我再逐批实施（含代码与自测）。
