# testgen 代码深层次 Review（2026-10-19）

> 范围：`services/testgen`（sidecar 引擎 + HTTP 壳 `service/app.py` + 任务存储 `service/tasks.py` + 编排 `engine/pipeline.py`）+ `apps/testgen_integration`（Django 代理/客户端）+ 控制台 `service/web/index.html`。
> 方法：基于真实源码静态走查，每条结论标注文件/行号与优先级（高/中/低）。

## 0. 总体结论

代码整体质量**高于同类自研工具**：错误不静默、红线清晰（生成-only）、幂等/追溯/掩码都有意识地处理。主要问题是**随功能膨胀出现的"上帝文件 + 同步阻塞 + 鉴权形同虚设 + 幂等键漏字段"**，以及控制台若干交互细节。以下按五类列出，均附可执行改进方案。

---

## A. 设计不合理

### A-1 【中】上帝文件 `service/app.py`（1778 行单模块）
所有路由、鉴权、错误处理、控制台静态服务全堆在一个文件，违反单一职责；改动任意端点都需触碰巨文件，回归风险高、单测需起全 app。
**改进**：用 FastAPI `APIRouter` 拆分——`routers/tasks.py`（生成/任务/取消）、`routers/scan.py`（scan/branches/commits/describe）、`routers/projects.py`（用例/报告/覆盖率/追溯）、`routers/repos.py`（仓库 CRUD/测试）、`routers/llm.py`（厂商 CRUD/测试）、`routers/quality.py`（trend/flaky/change_logs）。`app.py` 仅做 `lifespan` + 中间件 + `include_router` 装配。

### A-2 【中】同步端点阻塞 worker
- `/api/v1/pipeline`（app.py:463）**同步**跑完整生成链路（扫描→…→落库），耗时分钟级，占满线程池；
- `/api/v1/describe`（app.py:1353）在**请求线程内同步调 LLM**，按 5 条/组串行，大仓库会阻塞数十秒到数分钟；
- `/api/v1/analyze` 同理为同步重计算。
FastAPI `def` 端点在线程池执行，但长耗时调用会把线程池占满，拖累其他请求（含 `/health`）。
**改进**：重计算/外部调用端点统一改为 `async def` 并在内部 `await` 或丢进 `BackgroundExecutor`（与 `/api/v1/generate` 同一套机制）；`describe` 改为"提交→轮询"或后台任务 + 进度。

### A-3 【低-中】`/ready` 探针有写副作用
`/ready`（app.py:386-395）调用 `init_db()`，即在只读探针里**建表**。只读副本/权限收紧场景会直接失败；且语义上"就绪检查"不应改库。
**改进**：`/ready` 只 `SELECT 1` 验证连接；建表仅在 `lifespan` 启动时做（已有 `init_db()`）。

### A-4 【低】双层 stage 顺序表过度抽象
`engine/pipeline.py` 同时存在 `_P1_STAGE_WIRING`（:1385）与 `PIPELINE_STAGE_ORDER`（:1423）两张硬编码顺序表，外加 `REGISTRY` 间接层。文档称"去硬编码"，实则两处硬编码，两表不同步即**静默错序**（最危险的一类"看起来对其实错"）。
**改进**：保留单一 `PIPELINE_STAGE_ORDER` 直接映射函数，移除 `REGISTRY` 间接层；或让 `REGISTRY` 成为唯一来源、顺序由其声明顺序推导。

### A-5 【高】幂等键漏 `selected_paths` → 选不同子集返回旧任务
`_idem_key_for_generate`（app.py:506-518）拼接了 `local_path/repo_url/mode/base/target/scopes/changed_files/project_name`，但**没有 `selected_paths`**。后果：同一仓库、勾选**不同文件子集**两次提交 → 幂等键相同 → 第二次返回**第一次的旧任务结果**（旧子集产物），用户看到"生成没生效"。
**改进**：在键中追加 `",".join(sorted(req.selected_paths))`；并同步检查 `execute`/`dialogue` 等其它 idem 路径是否也漏子集字段。

---

## B. 实现不当

### B-1 【高】同 A-5 幂等键缺 `selected_paths`
见 A-5。属真实功能缺陷，影响"按子集生成"正确性，优先级最高。

### B-2 【中-高】`require_auth` 默认形同虚设 + CORS 通配 + 代理全豁免
- `require_auth`（app.py:161-167）：未配置 `auth_token` 时**所有端点免鉴权**；
- `app.py:106-112`：`CORSMiddleware(allow_origins=["*"], allow_credentials=True)`——既违反 CORS 规范（带凭证的通配浏览器会拒，且无意义地放宽），又在本机之外暴露全 API；
- `apps/testgen_integration/proxy.py:73-84`：全部路由 `@csrf_exempt`+`@xframe_options_exempt`，且 `requests` 用 `verify=False`。
三者叠加：任何能访问 8900/8910 的人可 CRUD **仓库凭证（PAT）与 LLM API Key** 并触发生成/执行。本地演示可接受，**一旦部署到局域网/公网即高危**。
**改进**：① 至少对写操作（POST/PUT/DELETE 仓库、LLM、webhook）强制要求 `X-Auth-Token`；② CORS 收窄为已知前端源，去掉 `allow_credentials` 或配对具体 origin；③ `proxy.py` 的 `verify=False` 仅在 `SIDECAR_BASE` 为 localhost 时成立，远程时启用校验或显式证书；④ CSRF 豁免收敛到必要端点。

### B-3 【中】`proxy.py` 透传全部 header（含 Cookie）
`_do_proxy`（proxy.py:43-46）透传除逐跳头外的所有请求头，含 `Cookie`/`Authorization`。iframe 嵌入主平台时，主平台登录态 Cookie 会被转发到 sidecar，存在会话混淆/越权面。
**改进**：默认剥离 `Cookie`，仅透传显式白名单头（如 `X-Auth-Token`、`Content-Type`）。

### B-4 【低】`quality_summary` 伪并行
`apps/testgen_integration/client.py:346-350` 用 `ex.submit(fn).result()` 立即阻塞取结果，等于**串行**还多一层线程开销，注释却写"并行避免最坏 8 分钟阻塞"。
**改进**：用 `future = ex.submit(...)` 收集后 `concurrent.futures.as_completed` 真正并发；或如实改注释为串行。

### B-5 【低】`describe` 本地目录缓存命名空间串味
`describe` 缓存按 `(repo_url, file_path)`（app.py:1263）。本地目录模式 `repo_url=""`，所有本地项目共用空命名空间 → 不同项目的同名文件会命中彼此缓存（描述错位）。
**改进**：本地模式以目录绝对路径作命名空间前缀，区分不同本地仓库。

---

## C. 潜在缺陷

### C-1 【中】任务列表/cases 接口无分页，大项目序列化风险
- `GET /api/v1/projects/{pid}/cases`（app.py:890）`include_obsolete` 默认 false 但**无分页**，项目用例可达数千（实测 4359 条 obsolete）；
- `GET /api/v1/tasks`（app.py:669）`SELECT *` 含大 `result`/`request` blob，前端每 3s 拉 `limit=20`。
大项目下存在响应体过大、序列化耗时、内存峰值风险。
**改进**：cases 接口加分页（`?offset=&limit=`，默认不含 obsolete）；tasks 列表端点只返回轻量字段（task_id/state/stage/progress/error 截断/created_at），`result` 仅详情端点返回。

### C-2 【低-中】`stage_persist` 每次全量 `replace` 功能点/测试点
`pipeline.py:1333-1334` 每次生成都 `replace_functional_points`/`replace_test_points`（整表删插）。虽幂等键防 pending/running 重复提交，但 failed 后可**立即重提**，两次 success 写入可能交错覆盖同一项目。
**改进**：项目级写入锁（或乐观版本号），避免并发/快速重提互相覆盖；增量场景可改为 upsert 而非全删。

### C-3 【低】`analyze` 截断但 `count` 为全量
`app.py:876` 返回 `functional_points[:500]` 但 `counts` 仍是全量；若前端用数组长度判断会误判。小问题，建议响应里显式带 `truncated` 与 `total`。

### C-4 【低】`cancel` 依赖阶段边界检查
`BackgroundExecutor` 的协作取消靠 `pipeline` 每阶段调用 progress 回调里的 `is_cancelled`（tasks.py:316）。若某条执行路径长时间不回调，取消会延迟生效。当前每个 stage 都 emit，风险低；建议在重计算循环内也周期性检查。

### C-5 【已修·本会话】`restoreLastTask` 死任务永久重试
原实现任务被删/取消后 localStorage 不清理，每次进入控制台都 404 重试（index.html:863）。**本会话已修复**：404 时 `localStorage.removeItem(LS_LAST_TASK)`。

---

## D. 性能瓶颈

### D-1 【中】任务列表轮询传输大字段
`GET /api/v1/tasks?limit=20` 单任务 `result` 可达 151 用例 JSON，前端 `loadTasks` 每 3s 拉一次全量大 payload（index.html:876）。
**改进**：列表端点只返回轻量摘要（见 C-1）；或加 `?summary=1` 开关。

### D-2 【中】`describe` 同步 LLM 阻塞（同 A-2）
请求线程内串行调 LLM，大仓库阻塞分钟级。改后台任务 + 轮询（同 A-2）。

### D-3 【低】每次 `TaskStore` 操作开闭 SQLite 连接
`tasks.py` 每操作 `connect()/close()`（为线程安全）。单机可跑；高并发写（进度每阶段一次）≈ 多次 connect/commit。
**改进**：短生命周期连接池（如 `sqlite3` + 连接缓存），或写操作合并（进度按时间/阶段节流落库，而非每回调必写）。

### D-4 【低】`replace_*` 写放大（同 C-2）
大项目每次生成全表删插功能点/测试点，I/O 放大。改 upsert 或按需增量。

---

## E. 交互体验问题（控制台 `service/web/index.html`）

### E-1 【已修·本会话】死任务 localStorage 不清理
见 C-5。已修复。

### E-2 【中】半选组头点击语义歧义
`refreshGroupChecks`（index.html:1403-1410）把部分勾选组头置 `indeterminate`；点击该组头时 `change` 监听（:1412-1420）读 `t.checked`（indeterminate 时浏览器一般返回 `true`）→ `setGroupChecked(group, true)` → **变为全选**。用户直觉"点半选=取消其余"会变成"全选"，与 Playwright E2E 发现的"半选点击=全选"一致，**可能导致误生成过大子集**。
**改进**：组头点击时若 `indeterminate` 为真，执行"仅保留当前已选项 / 或全不选"的明确语义（而非全选），并在 UI 文案说明；或点击 indeterminate 头先清为全不选、再点全选，两步 toggle。

### E-3 【低】失败错误截断 40 字
`renderTasks`（index.html:887）列表只显示 `error.slice(0,40)`，完整错误要翻日志。
**改进**：失败行可点击展开看完整 error，或 `title` tooltip 悬停显示全文。

### E-4 【低】`selectOnlyChanged` 本地无基线时静默空选
`selectOnlyChanged`（index.html:1431-1434）依赖扫描返回的 `change_status`；`local` 模式无 `base_ref` 时所有文件 `unchanged` → 点"只看变更"全空、无反馈。
**改进**：无变更信息/未选基线时禁用该按钮并提示"需先选基线 ref"。

### E-5 【低】大仓库扫描/describe 无超时与 loading 反馈
扫描、describe 是同步 POST，超大仓库按钮可能卡住数分钟无提示。
**改进**：前端对这类重操作加 loading 态 + 超时提示（与 generate 的生成中态对齐）。

### E-6 【低】任务列表无分页/筛选
`limit=20` 之后旧任务不可见（迁出 21 条后体量已很小，非紧急）。后续如需可加"仅失败/仅运行"筛选与分页。

---

## 附：优先级建议落地顺序

1. **高（先修）**：A-5/B-1 幂等键补 `selected_paths`（一行改动，修复子集生成正确性）。
2. **中（部署前必做）**：B-2 鉴权/CORS/代理 `verify` 收窄；A-2/`D-2` describe 与同步端点异步化；C-1 分页。
3. **中-低（可排期）**：A-1 拆分 app.py；A-3 `/ready` 去写副作用；C-2 写锁；E-2 组头点击语义。
4. **低（锦上添花）**：B-4 注释修正；B-5 本地缓存命名空间；C-3 截断标记；D-3 连接池；E-3/E-4/E-5/E-6 交互打磨。
