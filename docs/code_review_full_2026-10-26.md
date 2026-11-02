# TestHub 全项目代码审查报告（2026-10-26）

> 范围：`testhub_platform` 整个后端（Django `apps/` + `backend/`）与 `services/testgen` sidecar，含前端 `frontend/src` 安全面。
> 方法：**人工精读**（分 4 个模块并行审查）+ **自动化静态扫描**（`ruff 0.17.2` 全量 lint、`bandit 1.9.4` 安全扫描，已排除 `venv/`、`.venv/`、`node_modules/`、`frontend/` 第三方依赖）。
> 代码量：核心 Python 约 **3384 个文件**；前端约 12000 文件（安全面定向 Grep）。
> 说明：本报告为**纯审查产物**，未改动任何代码；所有整改项均为建议，待你确认后分批实施。

---

## 一、审查范围与基线数据

| 维度 | 结果 |
|---|---|
| 后端 Python 文件 | `apps/` + `backend/` + `services/` 共约 3384 个 `.py` |
| 自动化 lint（ruff） | 共 **3339** 条，绝大多数为风格类（import 排序 I001、百分号格式化 UP031 等）；**真实缺陷：F821 未定义名 6 处**（全部在 `playwright_engine.py`） |
| 自动化安全（bandit） | 项目代码 High **58** / Medium **229** / Low **3965**（含 venv 噪声已剔除后，真实 High 集中在弱哈希与 `verify=False`） |
| 人工审查 | 4 模块并行：后端安全/配置、api_testing 外部调用、testgen 服务、ui/app 自动化+前端 |

---

## 二、问题清单（按严重程度）

### 🔴 严重（High）—— 真实可利用风险，建议优先修复

| 编号 | 位置 | 类别 | 问题 | 整改方案 |
|---|---|---|---|---|
| **H1** | `backend/settings.py:298-300` | 安全 | `CORS_ALLOW_ALL_ORIGINS=True` 与 `CORS_ALLOW_CREDENTIALS=True` 共存。django-cors-headers 会把 `Access-Control-Allow-Origin` 反射为请求来源并带 `Allow-Credentials`，**任意网站可以当前登录用户身份发跨域请求（账号接管）**。 | 禁止 allow-all 与 credentials 共存；始终要求显式 `CORS_ALLOWED_ORIGINS` 白名单；无法给白名单时 `CORS_ALLOW_CREDENTIALS=False`。 |
| **H2** | `backend/settings.py:13` | 安全/配置 | `SECRET_KEY` 默认硬编码弱密钥 `'django-insecure-...'`。生产漏配即 JWT 与会话可伪造。 | 移除默认值，`config('SECRET_KEY')` 缺失时启动即报错（fail-fast）。 |
| **H3** | `backend/settings.py:15,19` | 配置 | `DEBUG=config('DEBUG', default=True)` + `ALLOWED_HOSTS=['*']`。生产漏设即开 DEBUG 并放行所有 Host。 | 默认 `False`；仅开发显式开启；`ALLOWED_HOSTS` 从环境变量读取。 |
| **H4** | `apps/api_testing/views.py:434,444,757`；`utils.py:181,342` | 安全 | **SSRF**：`requests.request(method, url)` 的 `url` 直接来自前端/环境变量/定时任务，无协议与主机校验，可打 `http://169.254.169.254/`（云元数据）、内网地址，且默认跟随重定向（302 转向内网）。 | 抽取统一安全请求函数：仅允许 `http/https`；解析主机后拒绝 `127/8`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、IPv6 链路本地/ULA、`0.0.0.0`；禁用自动重定向或限重定向域；`ScheduledTask` 目标 URL 在创建/激活时静态校验。 |
| **H5** | `apps/api_testing/serializers.py:818` | 安全 | `AIServiceConfigSerializer` 的 `api_key` 字段**既可读又可写**，任何列出/获取该配置都会**明文返回 API Key**（对比 `assistant/serializers.py:10`、`requirement_analysis/serializers.py:173` 已用 `write_only`/脱敏）。 | 设 `api_key = write_only=True`；新增 `api_key_masked` 只读脱敏字段；库内建议用 `apps/monitor/utils/crypto.py` 加密存储。 |
| **H6** | `apps/users/views.py:102-105`（UserDetailView） | 安全 | `permission_classes=[IsAuthenticated]` 但**无对象级权限**，任何已登录用户可对 `/api/users/users/<pk>/` 做 GET/PUT/PATCH/**DELETE**，可篡改/删除他人账号。 | 加 `IsOwnerOrAdmin` 权限类；列表接口限定本人或管理员。 |
| **H7** | `backend/middleware.py:3-7` + `settings.py:112` | 安全 | `DisableCSRFMiddleware` 对全部 `/api/` 置 `_dont_enforce_csrf_checks=True`，导致基于会话的 `/api/` 接口 CSRF 防护被整体绕过（登录接口会下发 sessionid）。 | 移除该中间件；JWT 场景只保留 JWT 认证；确需 CSRF 豁免的接口用 `@csrf_exempt` 精确标注。 |
| **H8** | `services/testgen/service/app.py:164-165` | 安全 | `require_auth` 在 `settings.auth_token` 为空（文档默认）时**直接放行**——auth 默认关闭，未设 `AUTH_TOKEN` 即完全无鉴权。 | fail-closed：非 dev 环境强制要求 token，或显式 `AUTH_DISABLED=1` 才允许关闭。 |
| **H9** | `apps/monitor/utils/probes.py:643-648` | 安全/bug | SQL 用 f-string 拼接 `db['table']`、`db['time_field']`、`db.get('where_clause')` 且未参数化；其中 `where_clause` 为原生 SQL 片段，若探针配置可由用户编辑即构成 **SQL 注入**。 | 表名/字段走白名单校验；拒绝接受原始 `where` 片段；值一律参数化。 |
| **H10** | `apps/monitor/utils/probes.py:352,474,547,589`；`apps/api_testing/custom_email_backend.py:14`；`apps/testgen_integration/proxy.py:59` | 安全 | 多处 `requests` 探测 / SMTP / 代理转发使用 `verify=False`，关闭 TLS 证书校验，存在中间人窃听/令牌窃取。 | 默认 `verify=True` 或显式受信 CA；仅对确需自签的受信域放宽。 |

### 🟠 中（Medium）—— 功能缺陷或部署后高风险

| 编号 | 位置 | 类别 | 问题 | 整改方案 |
|---|---|---|---|---|
| **M1** | `apps/testgen_integration/proxy.py:25,73,80` | 安全 | 两代理视图全 `@csrf_exempt` 且转发 state-changing POST（generate/pull/execute）到 sidecar；任意同源 XSS 即可驱动特权 sidecar。 | 不安全方法保留 CSRF；或要求平台会话并显式注入 sidecar token，而非信任客户端头。 |
| **M2** | `apps/testgen_integration/proxy.py:56` | 安全 | `cookies=request.COOKIES` 转发到 sidecar，把平台会话 cookie 泄露给独立服务。 | 丢弃 `cookies=`（或擦除 session cookie），鉴权已走 `X-Auth-Token`。 |
| **M3** | `backend/urls.py:62-72` | 安全 | `django.views.static.serve` 未鉴权暴露 `app-automation-templates/`、`app-automation-reports/`（生产不应使用，信息泄露）。 | 加权限校验；生产改用 Nginx/X-Sendfile 或白名单存储后端。 |
| **M4** | `apps/users/views.py:212,220-228` | bug | `exchange_token_view` 存 `redis_client.setex(..., request.user.id)` 为 int，而 `redis_lib.from_url` 未 `decode_responses`，`get` 返回 `bytes`；`int(user_id)` 对 `b'5'` 抛 `TypeError`（未被 `except User.DoesNotExist` 捕获）→ **授权码兑换必然 500**。 | 存字符串，或初始化 `decode_responses=True` 并 `int(user_id.decode())`；补异常捕获。 |
| **M5** | `apps/users/urls.py:11`（`test-register`） | 安全 | 测试接口直接挂在产品路由，绕过正式序列化器校验，且 `except Exception` 把 `str(e)` 原样回显（异常信息泄露）。 | 移出产品路由或仅 `DEBUG` 启用；禁止回显原始异常。 |
| **M6** | `frontend/.../TestCaseDetail.vue:37,40,43`；`GeneratedTestCaseList.vue:240,244` | 安全 | 用 `v-html` 直接注入 DB 原始字段（preconditions/steps/expected_result）→ **存储型 XSS**（任何能编辑用例的人可在他人浏览器执行 JS）。 | 改为纯文本渲染，或经 `DOMPurify` 清洗后再渲染；禁止对 DB 原始文本 `v-html`。 |
| **M7** | `frontend/.../ExecutionReport.vue:290,423`；`ComparisonReports.vue:102,170`；`DocsCenter.vue:33,66` | 安全 | `v-html="marked.parse(...)"` 无 sanitizer；`ExecutionReport.vue:412` 注释**错误地声称 "marked 默认转义原始 HTML，防 XSS"**（实际不转义），AI/文档输出被原样渲染。 | 一律 `DOMPurify.sanitize(marked.parse(src))`；修正错误注释。 |
| **M8** | `frontend/.../stores/user.js` + `router/index.js:727-730` | 安全 | access 与 refresh JWT 均存 `localStorage`，任意 XSS（见 M6/M7）即可窃取，含 refresh token。 | 改 httpOnly+Secure Cookie 或内存存储；**绝不**在 localStorage 存 refresh token。 |
| **M9** | `apps/app_automation/consumers.py:9-18` | 安全 | `AppExecutionConsumer.connect()` 接受任意 WebSocket 并按 `execution_id` 分组，**无认证/授权**，可猜 id 读取他人 App 执行进度/结果。 | 强制 `self.scope["user"].is_authenticated` 并校验 execution 归属后再 `group_add`。 |
| **M10** | `apps/ui_automation/ai_base.py:1681` | 安全 | AI 智能体（browser_use_text）把**不可信网页 DOM/观测**与系统指令混在同一 prompt，仅用软性"CREDENTIALS RULE"提示，无结构隔离 → 恶意站点可 prompt injection 骗取凭据/越权。 | 将页面文本与系统指令分通道；用域名/动作白名单约束；日志/输出脱敏。 |
| **M11** | `services/testgen/service/app.py:506-518` | bug（正确性） | `_idem_key_for_generate` **漏 `selected_paths`**（而 genexec 键 :646-654 已含）。同仓库换不同文件子集二次提交会命中旧幂等键 → 返回旧任务、丢弃新选择。 | 在键中追加 `",".join(sorted(req.selected_paths))`（1 行改动）。 |
| **M12** | `services/testgen/service/tasks.py:313` | bug | `BackgroundExecutor._run` 无条件置 `RUNNING`；若 `cancel()` 在 `PENDING` 时已置 `CANCELLED`，会被覆盖，任务仍跑到 `SUCCESS` → **未开始任务的取消丢失**。 | 置 `RUNNING` 前先 `is_cancelled()` 判断，已取消则中止。 |
| **M13** | `apps/testgen_integration/client.py:346-350` | 性能/bug | `quality_summary` 用 `ex.submit(...).result()` 逐个阻塞，四调用**串行**而非并行（尽管开了 4 线程池）。 | 先全部 submit，再 `as_completed` 收集。 |
| **M14** | `apps/testgen_integration/client.py:351` | bug | `all_ok = all(s.get("ok", False) ...)` 但 `quality_trend` 等返回原始 sidecar JSON（无 `ok` 键）→ `all_ok` 恒为 `False`，即使成功也判失败。 | 以 `raise_for_status()` 成功（HTTP 200）视为 ok，或给每个结果包 `ok=True`。 |
| **M15** | `apps/api_testing/views.py:476,251,375,392`；`utils.py:251` | 性能/bug | `response.text`/`response.json()` 全量读入内存并写库，**无响应体大小上限**（响应大小 DoS + DB 膨胀）。同时 `models.py:93` 的 `auth` 字段定义了却从未被附加到出站请求 → 认证请求静默失效。 | 限最大读取字节（`stream=True`+截断，标记 `truncated=True`）；补全 `auth` 注入到 header/基础认证。 |
| **M16** | `apps/api_testing/views.py:2175`；`2163-2165` | 安全 | webhook URL 直接 `requests.post`（可发往内网，SSRF）；钉钉签名日志打印了含 `secret` 的 `string_to_sign`/`sign`/`webhook_url`（**密钥与带令牌 URL 泄露进日志**）。 | webhook URL 施加同 H4 的 SSRF 校验；删除敏感字段日志，URL 记录前截断 `access_token/sign`。 |
| **M17** | `apps/ui_automation/playwright_engine.py:611,630,635,636` | bug（死代码/未定义名） | **ruff F821**：`js_result` 未定义。`return True, log, None`（609 行）之后的 611–638 行是**不可达死代码**（JS 分支被早退截断），整个 JS 下拉点击逻辑实际失效/报错。 | 重排函数结构：把 JS 分支逻辑移到 early-return 之前，正确赋值 `js_result`；删除不可达块。属真实逻辑缺陷，应优先修。 |
| **M18** | `services/testgen/service/app.py:464,1352` | 性能 | `/pipeline`、`/describe`（逐条调 LLM）为**同步阻塞**，高负载下占用 worker/事件循环。 | 改为后台任务 / 异步 LLM 调用（docstring 已承认）。 |
| **M19** | `services/testgen/service/tasks.py:240-243` | 性能 | `is_cancelled()` 每次进度回调都全表 `SELECT`。 | 每个任务在内存缓存 cancelled 标志。 |
| **M20** | `services/testgen/service/app.py:395` | 安全（信息泄露） | `/ready` 返回 `settings.public_dict()` 含 `db_path`/`output_dir`/`workspace_root` 等文件系统路径。 | 返回最小状态 payload。 |

### 🟡 低（Low）—— 可维护性 / 风格 / 待确认

| 编号 | 位置 | 类别 | 问题 | 整改方案 |
|---|---|---|---|---|
| **L1** | 全仓（`ruff` 统计） | 可维护性 | 3339 条 lint，主导为 I001 import 排序、UP031 百分号格式化、RUF059 未使用解包等。 | `ruff check --fix` 可自动修 1528 条（含 98 条需 `--unsafe-fixes` 复核）；建议接入 pre-commit + CI 门禁。 |
| **L2** | `apps/api_testing/views.py:47-49` | 可维护性/潜在 bug | 同一模块内重复定义 `ApiProjectSerializer` 等序列化类（F811）。 | 删除重复定义，确认实际引用的是期望版本。 |
| **L3** | `services/testgen/tests/test_store.py:62,71` | 安全（测试） | 硬编码 `/tmp` 路径（B108）。 | 改用 `tempfile` 临时目录。 |
| **L4** | `apps/data_factory/tools/encryption_tools.py:36,50`；`apps/app_automation/...:134,457`；`image_tools.py:104`；`views.py:620` | 安全（待确认） | MD5/SHA1 用于"安全"目的（B324）。`encryption_tools` 用 `from Crypto...`（实为 pycryptodome 维护版，B413 基本是误报）。 | 确认这些哈希是否用于口令/签名等安全场景；若是改 SHA-256；`Crypto`（pycryptodome）可用，无需替换。 |
| **L5** | `services/testgen/engine/pipeline.py` 等 | 可维护性 | 上帝文件 `app.py`（1778 行）、双层 stage 顺序表（A-1/A-4，详见优化批次文档）。 | 专项重构，配测试防回归。 |
| **L6** | `services/testgen/scripts/ui_test_tp_ui.py` | 可维护性 | 半成品 UI e2e 草稿脚本（RUF059 等告警）。 | 收口为正式 Playwright 用例或移出主分支。 |
| **L7** | `services/testgen/tests/`（假密码 `S3cret-PW-xyz` 等） | 合规 | 测试夹具含明显假密码/token（M1-R4/M3-R1），CI secret-scan 会误报。 | 改为 `dummy` 常量或移出默认集；`.secrets-allowlist.json` 入库。 |
| **L8** | `apps/api_testing/views.py:1697-1799` | 性能 | `run_now` 每触发新建守护线程且无并发上限；定时任务可反复打内网（放大 H4 SSRF）。 | 改用 Celery/RQ 等带并发限制的异步队列。 |

---

## 三、整改优先级路线图（建议）

1. **P0（先堵真实可利用漏洞，本地自用也建议修）**
   H2 / H3（SECRET_KEY、DEBUG 默认值）、H8（testgen auth 默认关）、H9（monitor SQL 注入）、M4（token 兑换 500）、M17（playwright 死代码）、M11（幂等键漏 selected_paths）。
2. **P1（上局域网/公网部署前必修）**
   H1 / H7（CORS + CSRF 组合）、H4 / H5 / H10（SSRF + api_key 明文 + verify=False）、H6（用户对象权限）、M1/M2/M3（testgen 代理安全）、M6/M7/M8（前端 XSS + JWT 存储）、M9（WS 无认证）、M10（AI prompt injection）、M16（webhook SSRF + 日志泄密）。
3. **P2（健壮性/性能，按需）**
   M12/M13/M14（任务取消竞态、伪并发、all_ok 恒假）、M15（响应体大小 + auth 未应用）、M18/M19（同步阻塞/SELECT）、M20（/ready 泄露）、L2/L8。
4. **P3（打磨/合规）**
   L1（lint 门禁）、L3/L4/L7（测试假密码/弱哈希）、L5（上帝文件拆分）、L6（草稿脚本）。

> 注：H1–H10、M1–M20 中多数与既有《整合实施_未完成任务追踪》第十一章 T1–T8 及优化批次 B1–B7 重叠，建议**合并排期**，在 G1（Django+MySQL 环境）解决后统一评审，避免一次性大改引入回归。

---

## 四、未完成任务与待办清单（按模块/优先级归纳）

### 模块 A：testgen ↔ testhub 整合（来源：`整合实施_未完成任务追踪.md`）
| 优先级 | 项 | 状态 |
|---|---|---|
| **P0** | G1 提供 Django+MySQL 运行环境（DB root 密码被拒待确认）→ 解锁所有写库/端点/钩子联调 | 阻断 |
| **P1** | G2 sidecar 可达性探活/友好报错；G3 `POST /api/testgen/security-verdict`；G4 执行后安全裁判钩子；G5 缺陷建单 `Defect.objects.create`；G7 实现（G1 后） | 依赖 G1 |
| **P2** | G6 越权双身份复测；G7 设计；G8 门禁双绿 + secret 白名单 | — |
| **P3** | G9 凭证脱敏；G10 git 历史（可选）；G11 verdict 审计日志 | 无阻断 |
| 设计 | WEB-R1 Web 平台操作入口（testgen 菜单+页面） | 待统一设计 |

### 模块 B：本次代码审查发现的整改（来源：本报告 §二）
- **高优（P0/P1）**：H1–H10、M1–M11、M16–M17、M20 → 见 §三路线。
- **中低（P2/P3）**：M12–M15、M18–M19、L1–L8 → 见 §三路线。

### 模块 C：Playwright CLI+Skills 能力引入（来源：`docs/playwright_cli_skills_adoption_...md`）
- T1（拆 9 项）：Skill 规范层 / 断言来源纪律 / 常驻会话 / 快照降 Token / 生成代码评审 / Heal 固化 / 密钥卫生 / attach 调试 / CLI 备选。优先级高→低，建议 B2 批次先做前两项（零新架构、ROI 最高）。

### 模块 D：优化批次 B1–B7（来源：`docs/optimization_batches_2026-10-20.md`）
- B1 零风险纯修复（A-5/B-1 幂等键、A-3 /ready、B-4 并发、E-2 组头语义）
- B2 Playwright 规范层（PW-1/PW-2）
- B3 部署前安全必修（B-2 鉴权/CORS/verify）
- B4 性能必修（A-2/D-2 异步、C-1/D-1 分页）
- B5 体验打磨、B6 写放大/连接、B7 上帝文件拆分。

### 模块 E：分支阶段对比功能（来源：`docs/branch_stage_compare_design_...md`）
- **已完成**：A 方案、B 细化设计、C1 阶段锚定模块（`engine/stage_snapshot.py` + 单测）、C2 两阶段对比编排（`engine/stage_compare.py` + 单测）—— 自动化单测全绿。
- **待做**：C3 CLI/API 入口（`cli/main.py` 加 `stage`/`stage-compare` + `app.py` 加端点）、C4 前端"分支阶段对比"视图、C5 全面 UI 层 Playwright e2e 验证。

### 模块 F：邮件监听 / 生产联调（来源：用户长期任务背景）
- 邮件监听系统接口测试闭环（query_auth/query/cancel_listener/add_listener/sendemail），重点 sendemail 假成功缺陷复验；生产 `ecloud.tppension.com` 与各测试 token 管理（运维类，非代码待办）。

---

## 五、结论

1. **最危险的是"默认即不安全"的配置与缺失的 SSRF/api_key 防护**（H1–H10）：CORS+凭证组合、SECRET_KEY/DEBUG 默认值、SSRF、明文 api_key、testgen auth 默认关闭、monitor SQL 注入。这些在出本机部署前必须修；本地自用也建议至少修 H2/H3/H8/H9。
2. **真实功能缺陷**（M4 token 兑换 500、M11 幂等键、M17 playwright 死代码、M12 取消竞态、M13/M14 伪并发/恒假判断）会影响实际可用性，建议 P0/P1 一并修。
3. **前端 XSS 面（M6/M7/M8）** 构成完整攻击链（存储型 XSS → 窃取 localStorage JWT），必须 `DOMPurify` + 改 token 存储。
4. **自动化扫描补强了人工盲点**：`playwright_engine.py` 的 `js_result` 死代码是 ruff 抓出、人工未发现的真实缺陷。建议把 ruff/bandit 接入 CI（L1）形成长期门禁。
5. 全部整改项与既有整合追踪、优化批次、能力引入方案**高度重叠**，建议合并到统一的"统一解决"批次，按 G1 解锁后的 P0→P3 顺序推进。

> 本报告仅作分析与建议，未修改任何源码。需要我针对其中某一批（如 P0 的 H2/H3/H8/H9/M4/M11/M17）产出**具体实施方案与自动化自测**，请明确范围，待你确认后我再动手。
