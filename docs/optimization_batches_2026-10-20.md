# testgen / testhub 优化分批实施方案（总纲）

> 文档日期：2026-10-20
> 输入：① `services/testgen/docs/code_review_2026-10-19.md`（A–E 共 25 项，含 2 项已修）；② `docs/playwright_cli_skills_adoption_and_code_tradeoff_2026-10-20.md`（9 项引入方案 + 权衡）；③ 运行态观察。
> 目标：把"该改什么、分到哪批、每批怎么验证"一次性说清，**每批内置自动化自测 + e2e 验证**，由助手逐步交付。
> 原则：① 用户手工测试时间少 → **自测以自动化为主**（pytest 单测/集成 + node 单测 + Playwright e2e）；② 每批质量须高于以往（含回归防护）；③ 端到端（e2e）全面覆盖关键路径。

---

## 一、类别体系（定义与边界）

优化项从两个**正交维度**分类：**价值/风险分层**（决定"做不做、何时做"）与**技术维度**（决定"改哪类问题"）。两者交叉，见 1.3 映射表。

### 1.1 价值/风险分层（第一维）

| 类别 | 定义 | 边界（什么算 / 什么不算） | 触发条件 |
|---|---|---|---|
| **① 零风险纯修复** | 错误/瑕疵修复或行为澄清，**零或极低回归风险**、**不改外部 API 契约**、无需前后端协同迁移。 | ✅ 内部键拼接补字段、去除探针副作用、并发实现修正、UI 交互语义本地化。❌ 任何改动外部契约（端点签名/响应结构）或需前端后端一起改的，不算此类。 | 任何时候都该收，与部署无关。 |
| **② 部署前必修** | 安全/性能问题，**本地单机无感、一旦暴露到局域网/公网即高危或卡顿**。 | ✅ 鉴权形同虚设、CORS 通配、代理 `verify=False`、同步端点阻塞探针、大列表无分页。❌ 纯本地演示且永不上网的不在此列。 | 只要"出本机"就必须先做；仅本地则用不上。 |
| **③ 看场景可不做** | 大型重构，**ROI 取决于是否真出现规模/多人维护/部署痛点**。 | ✅ 上帝文件拆分、stage 双表去抽象、连接池/写锁。❌ 这些在单人本地、短期不扩展时是"为未来买单"，不应主动做。 | 踩到痛点（卡顿/要部署/要加端点）时再做。 |
| **④ Playwright 能力引入** | 受文章启发**新增能力**，非修现有 bug，属"能力补齐"。 | ✅ Skill 规范层、断言来源纪律、常驻会话、快照降 Token、评审门禁、三段式、密钥卫生、attach、CLI/MCP。❌ 与 A–E 修复区分开，单独排期。 | 按杠杆高低逐步引入，不与修复混做。 |

### 1.2 技术维度（第二维）

| 维度 | 定义 | 边界 |
|---|---|---|
| **A 设计不合理** | 结构/架构层面的可维护、可扩展性问题（上帝文件、同步阻塞、双表、探针语义）。 | 关注"形状"，不是单点 bug。 |
| **B 实现不当** | 代码"怎么做"的选择问题（鉴权默认放行、透传头、伪并行、缓存命名空间）。 | 关注"实现选择正确性"。 |
| **C 潜在缺陷** | 尚未被观测触发、但逻辑上会出错的 bug（幂等漏字段、无分页、截断计数、取消延迟）。 | 区别于已发生的失败。 |
| **D 性能瓶颈** | 吞吐/延迟/资源问题（阻塞 worker、大字段轮询、连接开闭、写放大）。 | 关注"快不快/撑不撑"。 |
| **E 交互体验** | 控制台（index.html）UI/UX 问题（死任务、半选语义、错误截断、空选反馈、loading）。 | 仅前端，且只限控制台。 |

### 1.3 两轴交叉映射表

| 编号 | 标题 | 价值分层 | 技术维度 | 文件:行 |
|---|---|---|---|---|
| A-1 | 拆 `app.py` 上帝文件 | ③ | A | app.py(整) |
| A-2 | 同步端点阻塞 worker | ② | A/D | app.py:463/1353 |
| A-3 | `/ready` 有写副作用 | ① | A | app.py:386-395 |
| A-4 | 双层 stage 顺序表 | ③ | A | pipeline.py:1385/1423 |
| A-5 | 幂等键漏 `selected_paths` | ① | A/C | app.py:506-518 |
| B-1 | 同 A-5（实现视角） | ① | B/C | app.py:506-518 |
| B-2 | 鉴权/CORS/`verify=False` | ② | B | app.py:106-112/161-167 + proxy.py |
| B-3 | 透传全部 header（含 Cookie） | ② | B | proxy.py:43-46 |
| B-4 | `quality_summary` 伪并行 | ① | B | client.py:346-350 |
| B-5 | describe 缓存命名空间串味 | ③ | B | app.py:1263 |
| C-1 | 列表/cases 无分页 | ② | C/D | app.py:669/890 |
| C-2 | `stage_persist` 全量 replace | ③ | C/D | pipeline.py:1333 |
| C-3 | analyze 截断但 count 全量 | ③ | C | app.py:876 |
| C-4 | cancel 依赖阶段边界 | ③ | C | tasks.py:316 |
| C-5 | 死任务永久重试（已修） | — | C/E | index.html:863（已修） |
| D-1 | 任务列表轮询大字段 | ②/③ | D | app.py:669 + index.html:876 |
| D-2 | describe 同步 LLM 阻塞 | ② | D | app.py:1353 |
| D-3 | TaskStore 每操作开闭连接 | ③ | D | tasks.py |
| D-4 | replace 写放大 | ③ | D | pipeline.py:1333 |
| E-1 | 死任务 localStorage（已修） | — | E | index.html（已修） |
| E-2 | 半选组头点击语义歧义 | ① | E | index.html:1416-1420 |
| E-3 | 失败错误截断 40 字 | ③ | E | index.html:887 |
| E-4 | 空选无反馈 | ③ | E | index.html:1431 |
| E-5 | 大仓库无 loading/超时 | ③ | E | index.html |
| E-6 | 任务列表无分页/筛选 | ③ | E | index.html |
| PW-1 | Skill 规范层 | ④ | 能力 | ui_automation/aiSitePilot |
| PW-2 | 断言来源纪律 | ④ | 能力 | aiSitePilot/testgen |
| PW-3 | 常驻+命名会话隔离 | ④ | 能力 | ui_automation 驱动层 |
| PW-4 | 快照式上下文降 Token | ④ | 能力 | ui_automation AI 上下文 |
| PW-5 | AI 生成代码评审门禁 | ④ | 能力 | 接入八门禁 |
| PW-6 | Plan→Generate→Heal 三段 | ④ | 能力 | aiSitePilot |
| PW-7 | 会话/数据隔离与密钥卫生 | ④/② | 能力/安全 | secret 扫描扩展 |
| PW-8 | attach 到调试会话 | ④ | 能力 | ui_automation 调试 |
| PW-9 | CLI 作为 MCP 备选 | ④ | 能力 | 选型评估 |

> 跨列说明：A-5/B-1 同一缺陷双视角（设计遗漏 + 实现缺陷）；A-2/D-2 同一问题双视角（设计同步 + 性能阻塞）；C-5/E-1 已修复，仅留痕。

### 1.4 边界与排除原则

1. **不改外部契约优先于改契约**：① 类全部满足"不改契约"；②/③ 类中涉及契约变化（如分页新增 `offset/limit`、任务列表轻量字段）必须配前端协同与回归测试，故不入 ①。
2. **已修复项不入批**：C-5/E-1 已从活动清单移除，仅在文档留痕，避免重复劳动。
3. **重构不与功能开发混做**：A-1/A-4 属 ③，单独"重构专项"批次，配测试防回归，不与修复同批。
4. **引入与修复分离**：PW-1~PW-9（④）是净新增能力，单独批次，不因"文章启发"而顺手改现有代码契约。
5. **本地 vs 部署分流**：② 类"部署前必修"的真实紧迫性取决于是否出本机；当前单机演示阶段，它们可排到"部署触发"时，但方案必须提前备好。

---

## 二、分批路线图（7 批）

| 批 | 主题 | 包含项 | 价值层 | 依赖 | 交付顺序 |
|---|---|---|---|---|---|
| **Batch 1** | 零风险纯修复（正确性+健壮性+交互小修） | A-5/B-1、A-3、B-4、E-2 | ① | 无 | 1（现在做） |
| **Batch 2** | Playwright 高杠杆规范层 | PW-1、PW-2 | ④ | 无 | 2 |
| **Batch 3** | 部署前安全必修 | B-2、B-3、PW-7 | ② | 无（部署触发前必做） | 3 |
| **Batch 4** | 部署前性能必修 | A-2/D-2、C-1/D-1 | ② | 无 | 4 |
| **Batch 5** | 健壮性/交互打磨 | A-4、B-5、C-3、C-4、E-3~E-6 | ③ | 无 | 5 |
| **Batch 6** | 性能写放大/连接 | C-2/D-3/D-4 | ③ | Batch 4 分页落地后 | 6 |
| **Batch 7** | 可维护性重构 | A-1 | ③ | 门禁全绿后 | 7 |

> 排序逻辑：① 零风险修复与部署解耦、随时可做，且 A-5 是真实正确性缺陷 → 最先；④ 高杠杆规范层零新架构、ROI 最高 → 次先；② 依赖"是否出本机" → 排到部署前；③ 看场景 → 最后；⑦ 大重构需门禁护体 → 末位。

---

## 三、各批次详细方案

每批统一结构：**(a) 类别说明 / (b) 实施方案 / (c) 自测方案 / (d) e2e 验证要点**。

### Batch 1 — 零风险纯修复

**(a) 类别说明**
- A-5/B-1（①/A/C）：幂等键漏 `selected_paths` → 同仓库不同子集返回旧任务（真实正确性缺陷，最高优先）。
- A-3（①/A）：`/ready` 探针内调 `init_db()` 建表，只读探针不应改库；权限收紧场景会失败。
- B-4（①/B）：`quality_summary` 名为并行实则串行（submit 后立即 `.result()` 阻塞），注释误导。
- E-2（①/E）：半选组头点击被浏览器当作 `checked=true` → 全选，可能误生成过大子集。

**(b) 实施方案**
- `app.py:506-518` `_idem_key_for_generate`：在 join 列表追加 `",".join(sorted(req.selected_paths))`；同步核对 `genexec` 键（648-658）已含，无需改。
- `app.py:385-395` `/ready`：删除 `init_db()` 调用，仅保留 `connect()+SELECT 1`（建表已在 `lifespan` 做）。
- `client.py:346-350` `quality_summary`：先 `submit` 四个 future 收集，再 `as_completed` 取结果，真正并发；保留 `_safe` 异常隔离。
- `index.html:1416-1420` 组头点击：新增 `toggleGroupHeader(headerEl)`——`indeterminate` 时执行**折叠为全不选**（防误扩），非 `indeterminate` 时按 `checked` 切换；抽取纯函数 `groupHeaderClickTarget(indeterminate, checked)` 到 `service/web/console_logic.js`（经 `/static` 挂载），index.html 通过 `<script src="/static/console_logic.js">` 引入，便于 node 单测。

**(c) 自测方案（自动化为主）**
- `tests/test_batch1_idem.py`（pytest）：
  - 单测 `_idem_key_for_generate`：相同请求不同 `selected_paths` → 键不同；完全相同 → 键同。
  - 集成（TestClient + `fresh_db` + LLM 桩）：monkeypatch `executor.submit` 为 no-op（任务停留 pending），POST `/api/v1/generate` 子集 A 两次 → 同 id；再 POST 子集 B → **不同 id**（验证修复）；断言响应含 `poll_url`。
- `tests/test_batch1_ready.py`（pytest）：monkeypatch `service.app.init_db`，TestClient GET `/ready` → 断言 `init_db` **未被调用**且 `status=="ready"`；反向：DB 不可用时返回 `not_ready`。
- `tests/test_batch1_quality_summary.py`（pytest）：patch `quality_trend/flaky/filters/changes` 各 `sleep(0.2)` 并记录开始时间；断言总耗时 `< 0.6s`（证明真正并发），且四项结构完整、单项异常被隔离为 `{"ok":False}`。
- `tests/test_console_logic.mjs`（node）：import `console_logic.js` 的 `groupHeaderClickTarget`，断言 `indeterminate→false`（折叠）、`indeterminate=false & checked=true→true`、`checked=false→false`。

**(d) e2e 验证要点**
- 后端：启动 sidecar → `GET /ready` 返回 `ready` 且不重建表（对比日志无 `init_db` 调用）；`POST /api/v1/generate`（小仓库 sample_repo，subset A 与 subset B 各一次）→ 两次返回不同 `task_id`，轮询两任务均 `success` 且 `selected_paths` 与请求一致。
- 前端：Playwright 加载控制台 → 注入混合勾选的扫描结果（部分勾选组头呈 `indeterminate`）→ 点击该组头 → 断言**全部取消勾选**（非全选）；再次点击 → 全选。脚本：`tests/e2e/test_console_group_header.spec.js`。

---

### Batch 2 — Playwright 高杠杆规范层（PW-1、PW-2）

**(a) 类别说明**：④ 能力引入。给现有 AI 流程"立规矩"，低成本高杠杆，不引入新架构。
**(b) 实施方案**
- PW-1 Skill 规范层：在 `ui_automation`/`aiSitePilot` 增加 `SKILL.md` 式提示词模板 + 约束清单（探页面→生成→自愈；会话管理；Mock/Trace 调试），Agent 每次加载。
- PW-2 断言来源纪律：修复/生成闭环增加"断言来源校验"——关键业务（支付/订单）**禁止 Agent 改断言**；非关键业务改断言须记录来源与 diff。
**(c) 自测方案**：单元测 Skill 加载与约束注入；单元测"断言来源校验"拦截逻辑（mock 一次违规修复 → 断言被拒绝）。
**(d) e2e 验证要点**：AI 跑一个已知 UI 用例生成+自愈，断言：① 流程符合 Skill 三段；② 修复未擅自删/放宽关键断言（git diff 断言行无删减）。

### Batch 3 — 部署前安全必修（B-2、B-3、PW-7）

**(a) 类别说明**：② 安全。出本机即高危。
**(b) 实施方案**
- B-2：写操作（POST/PUT/DELETE 仓库、LLM、webhook）强制 `X-Auth-Token`；CORS 收窄为已知前端源并移除 `allow_credentials` 或配对具体 origin；`proxy.py` `verify=False` 仅 localhost 成立；CSRF 豁免收敛。
- B-3：`proxy.py` 默认剥离 `Cookie`，仅白名单头透传（先评估有无 Cookie 依赖）。
- PW-7：扩展 secret 扫描覆盖 Cookie/截图路径；命名会话隔离。
**(c) 自测方案**：pytest 验证未带 token 的写操作返回 401；CORS 预检仅放行白名单源；proxy 不转发 Cookie（抓包断言）。
**(d) e2e 验证要点**：部署形态下，外部 IP 无 token 访问写接口被拒；带 token 正常； secret 扫描在 CI 覆盖新增敏感面。

### Batch 4 — 部署前性能必修（A-2/D-2、C-1/D-1）

**(a) 类别说明**：② 性能。阻塞探针 + 大列表卡顿。
**(b) 实施方案**
- A-2/D-2：`/pipeline`、`/describe`、`/analyze` 改 `async def` + 丢 `BackgroundExecutor`（同 `/generate`），describe 改"提交→轮询"。
- C-1/D-1：`cases` 加分页（`offset/limit`，默认不含 obsolete）；`tasks` 列表只返轻量字段，`result` 仅详情端点返回。
**(c) 自测方案**：集成测长耗时请求不再阻塞 `/health`（并发探测）；分页参数生效、列表响应体显著缩小。
**(d) e2e 验证要点**：大仓库（4359 条用例）cases 分页拉取流畅；describe 大仓库不阻塞探针。

### Batch 5 — 健壮性/交互打磨（A-4、B-5、C-3、C-4、E-3~E-6）

**(a) 类别说明**：③ 中低优先。
**(b) 实施方案**：A-4 去双表；B-5 本地缓存命名空间前缀；C-3 响应带 `truncated/total`；C-4 重计算循环内周期检查；E-3 错误展开、E-4 空选禁用提示、E-5 loading/超时、E-6 任务筛选。
**(c) 自测方案**：逐项单测 + 前端组件测。
**(d) e2e 验证要点**：analyze 大结果前端不误判；取消大型任务及时生效；控制台交互反馈完整。

### Batch 6 — 性能写放大/连接（C-2/D-3/D-4）

**(a) 类别说明**：③ 仅并发重提触发。
**(b) 实施方案**：项目级写锁/乐观版本号；增量 upsert；连接节流。
**(c) 自测方案**：并发重提同一项目不互相覆盖；连接数受控。
**(d) e2e 验证要点**：快速重提两次生成，产物一致无交错。

### Batch 7 — 可维护性重构（A-1）

**(a) 类别说明**：③ 大重构。
**(b) 实施方案**：`app.py` 拆 `APIRouter`（tasks/scan/projects/repos/llm/quality），`app.py` 仅 lifespan+中间件+include_router。
**(c) 自测方案**：拆分后全量回归（八门禁 + 既有 pytest）+ 路由前缀一致性断言。
**(d) e2e 验证要点**：所有端点行为不变（契约对照测试）。

---

## 四、统一自测 / e2e 基线约定

1. **后端自测**：pytest 跑于 testgen venv（已装 8.4.2），复用 `conftest.py` 的 `fresh_db`/`sample_repo`/`git_repo`/LLM 桩；新增用例放 `services/testgen/tests/test_batchN_*.py`。
2. **前端自测**：纯逻辑抽 `console_logic.js` 用 `node` 单测；交互用 Playwright（项目已有）写 `tests/e2e/*.spec.js`。
3. **e2e 基线**：每批至少 1 条端到端路径（启动 sidecar 或 TestClient 真实跑通关键链路），不靠手工点。
4. **回归防护**：每批交付前跑 `scripts/gate_all.py`（八门禁）与受影响既有 pytest，确保不引入回归。
5. **门禁约束**：敏感文件（`.env` 等）不入库；提交前 `git diff --cached --name-only | grep -iE "\.env$|venv/|\.db$"` 复核。
