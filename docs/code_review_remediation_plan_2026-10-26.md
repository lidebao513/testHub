# TestHub 代码审查整改方案（P1 / P2 / P3）— "统一解决"批次

> 来源：`docs/code_review_full_2026-10-26.md`（2026-10-26 全项目审查）。
> 目标：把 H1–H10 / M1–M20 / L1–L8 与既有整合追踪、优化批次、能力引入方案**合并排期**，统一到"统一解决"批次。
> 推进顺序：**待 G1（Django+MySQL 运行环境）解锁后，按 P0 → P1 → P2 → P3 推进**。
> 本文档原以方案层编写；**P0 与 1a 子批（H1/H7/H10）已先行落地并自测 + 活体 e2e（见 §0、§0.1）**，其余 P1 子批 / P2 / P3 仍为方案。

---

## §0 P0 已落地（本次执行，已通过自动化自测）

| 编号 | 位置 | 整改内容 | 自测 |
|---|---|---|---|
| H2 | `backend/settings.py` | `SECRET_KEY` 缺失时 fail-fast：生产（`DEBUG=False`）未配置即拒绝启动；开发（`DEBUG=True`）给一次性占位值并告警 | 模块加载冒烟通过（本地 `.env` 已配，行为不变） |
| H3 | `backend/settings.py` | `DEBUG` 默认值 `True` → `False`；`ALLOWED_HOSTS` 始终从环境变量读取 | 同上，本地 `DEBUG=True` 行为不变 |
| H8 | `services/testgen/core/config.py` + `service/app.py` + `services/testgen/.env` | `require_auth` 改 fail-closed：未配 `AUTH_TOKEN` 且未显式 `AUTH_DISABLED=1` 一律拒绝；本机 `.env` 加 `AUTH_DISABLED=1` 保留联调链路 | 3 条用例（fail-closed / 逃生口 / 配令牌校验） |
| H9 | `apps/monitor/utils/sql_safe.py`（新增）+ `probes.py` | 新增纯函数 `is_safe_sql_identifier` / `is_safe_where_clause`，`_sl651_db_check` 对表名/字段/WHERE 做白名单校验，阻断 `; -- /* */ UNION DROP` 等注入 | 注入用例全拦截、合法中文谓词放行；**自测中发现守卫原先置于 `pymysql.connect()` 之后（无效/恶意输入仍会先建连），已调整为建连前校验** |
| M4 | `apps/users/token_exchange.py`（新增）+ `views.py` | 抽纯函数统一"写字符串/读兼容 bytes+str"，`exchange_token_view` 不再因 `int(b'5')` 抛 `TypeError` → 500 | 编码/解码/None/非法 4 条用例 |
| M11 | `services/testgen/service/app.py` | `_idem_key_for_generate` 补 `selected_paths`，同仓不同勾选子集不再命中旧幂等任务 | 子集差异键不同 + 顺序无关 |
| M17 | `apps/ui_automation/playwright_engine.py` | 删除 611–638 行不可达死代码（ruff F821 未定义 `js_result`）与 511–548 行未使用 `js_selector_code` | `ruff --select F821` 全绿 + 11 条 P0 自测全过 |

> P0 自测文件：`services/testgen/tests/test_p0_security.py`（**20 条**，testgen venv pytest 全绿）。
> 自测覆盖：H2/H3 用 `exec` 真实源码片段 + stub `config`（复刻 decouple 的 `cast=bool` 解析）验证 fail-fast / DEBUG 默认 False / ALLOWED_HOSTS 分支；H8 用 `load_settings(env=...)` 隔离验证 fail-closed 三态；M11 验证幂等键含 `selected_paths` 且顺序无关；M4 验证 bytes/str 编解码 + 非法转 `ValueError`；H9 既测纯函数，又**注入假 `pymysql` 验证守卫已真正接入 `probes._sl651_db_check`**（此过程发现并修复了 H9 守卫顺序 bug：校验须在建连前）；M17 以"源码不含 `js_result`/`js_selector_code`"做回归守卫。`ruff --select F821` 全绿，`py_compile` 全过。

## §0.1 阶段 1 · 1a 子批已落地（H1/H7/H10，已自测 + 活体 e2e）

| 编号 | 位置 | 整改内容 | 自测 / e2e |
|---|---|---|---|
| H1 | `backend/settings.py` | 生产分支删除 `CORS_ALLOW_ALL_ORIGINS=True`；未配白名单 → `CORS_ALLOWED_ORIGINS=[]` 且 `CORS_ALLOW_CREDENTIALS=False`；有白名单才 `CORS_ALLOW_CREDENTIALS=True` | 单元：prod 无来源 / 有来源两态；**活体**：`evil.example.com` 预检**无任何 `Access-Control-Allow-Origin` 反射**（证明无 allow-all） |
| H7 | `backend/settings.py:129` + 删除 `backend/middleware.py` | 移除 `DisableCSRFMiddleware` 注册与死文件，`CsrfViewMiddleware` 恢复默认强校验 | 单元：MIDDLEWARE 不再含该中间件 + 文件已删；**活体** `manage.py check` 0 issues |
| H10 | `apps/testgen_integration/proxy.py:59` / `apps/monitor/utils/probes.py` / `apps/api_testing/custom_email_backend.py` | proxy 删 `verify=False`（localhost HTTP 默认 True）；probes 改 env 驱动默认安全的 `_tls_verify()`；邮件后端改 `ssl.create_default_context()`（`EMAIL_TLS_VERIFY=0` 才放开自签） | 单元：proxy 不传 `verify=False`、probes `_tls_verify()` 默认 True、邮件后端默认受信 CA 校验；**源码无硬编码 `verify=False`** |

> 1a 自测文件：`tests/test_p1_1a_security.py`（**13 条**，backend venv pytest 全绿）。
> 活体 e2e：Django `:8900` 启动成功（G1 部分就绪：MySQL/Redis 已起，sidecar 未起）；CORS 预检证明无 allow-all；proxy 转发在 sidecar 未起时返回干净 502（无 TLS/verify 崩溃）；P0 自测（20 条）复跑不回归。
> **风险备注**：H10 邮件后端默认开启证书校验，若现有 SMTP 为自签证书，需在其环境设 `EMAIL_TLS_VERIFY=0`（或把 CA 加入信任库）；probes 同理需对自签 HTTPS 端点设 `PROBE_TLS_VERIFY=0`。

## §0.2 阶段 1 · 1b 子批已落地（H6/M1/M2/M9，已自测 + 活体 e2e）

| 编号 | 位置 | 整改内容 | 自测 / e2e |
|---|---|---|---|
| H6 | `apps/users/permissions.py`（新增） + `apps/users/views.py` | 新增 `IsOwnerOrAdmin`（本人或 `is_staff`）；`UserDetailView` 应用 → 阻断查看/改/删他人；`UserListView.get_queryset` 非管理员仅列自己 | 单元：`has_object_permission` 三态（owner/admin/other）；非 staff 列表 `filter(id=self)`、staff 不过滤；**活体**：`/api/auth/users/` 未认证 401 |
| M2 | `apps/testgen_integration/proxy.py:51-59` | 删除 `cookies=request.COOKIES`，不再把平台会话 Cookie 透传给 sidecar（仅走 `X-Auth-Token`） | 单元：`_do_proxy` 调 `requests.request` 不含 `cookies` kwarg；**活体**：proxy 转发路径仍在，sidecar 未起时返回干净 502（无会话泄漏） |
| M1 | `apps/testgen_integration/proxy.py` | `_do_proxy` 要求 `request.user.is_authenticated`（阻断匿名 CSRF / 未授权访问）；注入服务端 `TESTGEN_AUTH_TOKEN` 作为 `X-Auth-Token`（sidecar H8 鉴权，不再依赖 `AUTH_DISABLED`）；`testgen_web_proxy` 去掉 `@csrf_exempt`（页面只读 GET 不需），`testgen_api_proxy` 因内嵌 testgen 控制台异源 JS 无法带 CSRF 令牌而保留（见下方残留说明） | 单元：匿名→403、已登录注入 `X-Auth-Token`、伪造请求头被服务端令牌覆盖；**活体**：匿名 `POST /api/v1/gen` 与 `GET /testgen/` 均 **403** |
| M9 | `apps/app_automation/consumers.py` | `AppExecutionConsumer.connect()` 先校验 `is_authenticated`（匿名→`close(4401)`），再按 `execution_id` 查 `AppTestExecution` 归属（本人或 `is_staff`→放行，否则→`close(4403)`），校验通过才 `group_add` | 单元：`_owns_execution` owner/admin/other 三态；connect 匿名拒绝、owner/admin 放行、非 owner 拒绝（不进 group） |

> 1b 自测文件：`tests/test_p1_1b_security.py`（**16 条**，backend venv pytest 全绿）。
> 全量回归：**P0(20) + 1a(13) + 1b(16) = 49 条全绿，零回归**（testgen venv 跑 P0；backend venv 跑 1a/1b）。
> 活体 e2e：Django `:8900` 启动（G1 部分就绪：MySQL/Redis 已起，sidecar 未起）；匿名代理请求 403（M1 鉴权门生效）；`manage.py check` 0 issues。
> **M1 残留说明（已知局限）**：`testgen_api_proxy` 因内嵌 testgen 控制台为"异源 JS"、其 `/api/v1/` 调用不会带 Django CSRF 令牌，若强制 CSRF 校验会使控制台不可用，故保留 `@csrf_exempt`。补偿控制：① 已要求代理访问必须登录（阻断匿名 CSRF）；② 已注入服务端 `X-Auth-Token`（sidecar H8 鉴权）；③ 已去除会话 Cookie 透传。后续建议：让控制台改用带 CSRF 令牌或 scoped token 的调用，方可彻底移除 `@csrf_exempt`。
> **配置要求**：生产环境须在 Django 进程环境中设置 `TESTGEN_AUTH_TOKEN`，并与 sidecar 的 `AUTH_TOKEN` 保持一致；同时**取消** sidecar 的 `AUTH_DISABLED=1`（本地开发逃逸开关），使 H8 的 fail-closed 在生产真正生效。

---

## §0.3 阶段 1 · 1c 子批已落地（H4 + M16 SSRF 防护，已自测 + 活体 e2e）

| 编号 | 位置 | 整改内容 | 自测 / e2e |
|---|---|---|---|
| H4 | `apps/api_testing/ssrf_guard.py`（新增）+ `views.py` / `utils.py` | 新增纯函数守卫：`validate_url`（协议白名单 + DNS 解析后校验**全部** A/AAAA 记录）、`safe_request`（**逐跳重校验**的重定向跟随）、`SsrfPolicy`（strict / allow_hosts / max_redirects，env 可配）。`api_testing` 全部 9 处出站调用（单请求执行、套件执行、套件并发执行、AI 连通性测试、4 处 AI 补全）统一改为 `safe_request`，模块内已无直连 `requests.*` | 单元 50 条；**活体**：真实 socket + 真实 302 → 元数据被拦、服务端仅 1 次命中（第二跳从未发出） |
| M16 | `apps/api_testing/views.py`（webhook 发送 / 钉钉签名日志 / NotificationLog）+ `serializers.py` | webhook 发送改走 `safe_request` 并单独捕获 `SsrfBlocked`；删除 `string_to_sign` / `sign` / 完整 URL 日志，改为 `mask_webhook_url`；`NotificationLog.webhook_bot_info.webhook_url` 落库同时脱敏；`TaskNotificationSettingDetailSerializer.validate_custom_webhook_bots` 在**写入口**做静态预检（不做 DNS） | 单元：元数据/file 协议 webhook 被拒、正常企业微信/钉钉地址放行；脱敏断言不含 key/uuid/token |

**分层策略（关键设计取舍，务必知悉）**

本平台的**正当用途就是测用户自己的内网接口**（192.168/10/172.16/localhost），所以不能像公网 SaaS 那样一律封私网段，否则直接废掉核心功能。故按三层处理：

1. **恒拦（任何配置）**：非 `http/https` 协议、云元数据（`169.254.169.254`、阿里云 `100.100.100.200`、Azure `168.63.129.16`、`fd00:ec2::254`）、链路本地、`0.0.0.0`、组播、保留段。这些无任何合法测试用途，是凭据外泄主通道。
2. **可配置**：私网/回环默认放行以保留内网测试能力；**多租户或公网暴露部署必须设 `API_TESTING_SSRF_STRICT=1`**（此时私网+回环+`.internal/.local` 一律拒绝）。
   另可用 `API_TESTING_SSRF_ALLOW_HOSTS=host1,10.0.0.0/8` 开白名单，`API_TESTING_SSRF_MAX_REDIRECTS=3` 收紧跳数。
3. **重定向逐跳校验**：默认最多 5 跳，每跳目标重新校验——堵住 `公网URL → 302 → 169.254.169.254` 这类经典绕过。

> 1c 自测文件：`tests/test_p1_1c_ssrf.py`（**50 条**，backend venv pytest 全绿）+ 活体脚本 `tests/e2e_ssrf_live.py`（真实 socket，7 项全 PASS）。
> 全量回归：**P0(20) + 1a(13) + 1b(16) + 1c(50) = 99 条全绿，零回归**；`manage.py check` 0 issues；ruff `F/E9` 对改动文件**未新增**任何问题（存量 30 条均为既有）。
> **残余风险（已登记）**：① DNS 校验与实际连接之间存在 rebinding 时间窗，彻底消除需在连接层 pin IP（当前以「校验全部 A 记录」缓解）；② 默认非 strict 部署仍允许访问私网——需运维按部署形态显式开启 `API_TESTING_SSRF_STRICT=1`。

---

## §0.4 阶段 1 · 1d 子批已落地（H5 + M3 + M5，已自测 + 活体 e2e）

| 编号 | 位置 | 整改内容 | 自测 / e2e |
|---|---|---|---|
| H5 | `apps/api_testing/models.py`、`serializers.py`、`views.py`（5 处出站）、`migrations/0004_alter_aiserviceconfig_api_key.py` | 模型 `save()` 幂等加密落库（对齐 `monitor` 的 `encrypt(decrypt(...))` 做法）、新增 `get_api_key()` 解密取值与 `masked_api_key()`；字段长度 500→1024（Fernet 令牌约为明文 1.4 倍 + 57，原长度会截断，**需迁移**，已生成并应用）；序列化器 `api_key` 改 `write_only`、新增只读 `api_key_masked`（前 8 位 + `****`）、新建必填校验、局部更新留空不清空；5 处出站改为 `config.get_api_key()`（已无 `config.api_key` 直读） | 单元 8 条：密文落库 / 重复保存不套娃 / 明文历史数据兼容 / 序列化响应无明文 / 新建必填 / 局部更新保 Key / 出站改用解密值；**活体**：创建→响应无明文、列表无明文、库内 `gAAAAA…`、外呼 `Bearer <明文>` |
| M3 | `apps/app_automation/static_views.py`（新增）+ `backend/urls.py` | 原裸 `django.views.static.serve` 替换为 `protected_serve`：**必须已登录**（401）、路径穿越/反斜杠/空字节/隐藏文件拒绝、**扩展名白名单**（报告所需 html/js/css/json/字体/图片）、目录请求 404、`document_root` 规范化二次校验、统一补 `X-Content-Type-Options: nosniff` | 单元 9 组：匿名 401 / 登录 200+nosniff / 8 种非法路径 404 / 目录 404 / urlconf 已换；**活体**：匿名 401、登录 200、`%2e%2e` 穿越 404、非白名单 404 |
| M5 | `apps/users/test_views.py` | 删除 `except Exception → str(e)` 回显，改「通用提示 + `logger.exception`」；字段校验**委托**正式 `UserCreateSerializer`（密码长度、两次一致、模型约束），不再自建账号绕过校验；响应结构保持 `{success,user,access,refresh}` 兼容注册页 | 单元 8 条：无回显（`SECRET_DB_ERROR_xyz` 不外泄）/ 弱密码被拒且不建号 / 两次密码不一致被拒 / 成功仍返回 token / 非法 JSON / 手机号格式 / 源码无 `str(e)`；**活体**：405 / 非法 JSON 400 无 Traceback / 缺验证码 400 |

**1c 的 e2e 补强（本轮同做）**：新增 HTTP 级 e2e `tests/e2e_ssrf_http.py`——真实 Django 路由 + 真实 DB + 真实本地 trap server，验证「内网目标放行且真实外呼 / 元数据 400 且**零外呼** / 302 跳板只发出首跳 / 非法协议 400」，9 项全 PASS。同时把 `test_connection` 的 `SsrfBlocked` 从「500 未知错误」改为**400 可读原因**。

> 1d 自测文件：`tests/test_p1_1d_security.py`（**29 条**，backend venv pytest 全绿）+ 活体脚本 `tests/e2e_1d_live.py`（真实路由 + 真实 DB，17 项全 PASS）。
> 全量回归：**P0(20) + 1a(13) + 1b(16) + 1c(50) + 1d(29) = 128 条 pytest 全绿**，外加 **3 个活体脚本共 33 项**（1c socket 7 + 1c HTTP 9 + 1d HTTP 17）全 PASS；`manage.py check` 0 issues；ruff `F/E9` **零新增**（存量 31 条均为既有，已用 `git show HEAD` 逐条比对确认）。

**偏差与发现（需你知晓）**

1. **M5 路由未移出产品 URL（有意偏差）**：前端注册页 `frontend/src/stores/user.js` 的 `register()` 目前唯一实现在打 `/api/auth/test-register/`，直接按「仅 DEBUG 挂载」会让**生产注册页 404**。因此本轮改为「消除绕过 + 去除异常回显」——安全问题已闭环，路由暂留。彻底移出需前端改走 `/api/auth/register/` 并适配返回体（`{user,token}` vs `{user,access,refresh}`），已列入下一轮任务。
2. **发现一处存量迁移漂移**：`makemigrations` 检出 `apirequest.tags`（`0003` 建了列，但 `models.py` 无该字段）。删除列会丢数据且与本次整改无关，故 `0004` **故意只含 `AlterField`**，漂移单独登记待确认。
3. **H5 存量数据**：开发库现有 `AIServiceConfig` **0 条**，无需数据迁移；`crypto.decrypt()` 对明文幂等，若历史环境存在明文行仍可用，下次保存自动转密文。
4. **M3 依赖会话 Cookie**：报告/模板访问走 Django 会话（`login_view` 已 `login()` 建会话）。若后续改为「纯 JWT、不建会话」，这些静态目录会 401——届时需换成签名 URL 或 `X-Accel-Redirect`。生产建议仍由 Nginx `X-Accel-Redirect` / 对象存储签名 URL 接管。

## §0.5 阶段 1 · 1e 子批已落地（前端 XSS 攻击链 M6/M7/M8 + 注册路由收敛 + tags 漂移回填）

| 编号 | 位置 | 整改内容 | 自测 / e2e |
|---|---|---|---|
| M6 | `TestCaseDetail.vue`、`GeneratedTestCaseList.vue`（DB 原始字段）；另纳入 `AssistantView`、`TaskDetail`、`RequirementAnalysisView`、`InterfaceManagement` 的 `v-html` | 所有 `v-html` 经 `DOMPurify.sanitize` 净化（`utils/sanitize.js` 统一入口），阻断存储型 XSS | 前端 `vite build` 全量编译通过；M8 视图级测试覆盖 token 链路 |
| M7 | `ExecutionReport.vue`（含修正"marked 默认转义"错误注释）、`ComparisonReports.vue`、`DocsCenter.vue` | `marked.parse` 结果必过 `DOMPurify.sanitize`；统一 `renderMarkdown` 工具，修正误导注释 | 同上 |
| M8 | `stores/user.js`、`router/index.js`、`utils/api.js`、`apps/users/views.py` | access token 仅内存态（不落 localStorage）；refresh 改后端下发 **HttpOnly + SameSite=Lax** Cookie，前端绝不持有；新增 `cookie_token_refresh`（免 body refresh、禁用会话认证避 CSRF）；`logout` 从 Cookie 读 refresh 并清 Cookie | 后端视图级 3 项：注册下发 HttpOnly cookie / cookie 续期 / logout 清除；`vite build` 通过；`makemigrations --check` 0 漂移 |
| 收尾① | `frontend/src/stores/user.js`、`frontend/src/router/index.js` | 前端注册改走正式 `/api/auth/register/`，移除 `test-register` 路由；返回体适配 `{success,user,access,refresh}` | 前端编译通过 + 后端 `RegisterView` 视图级测试 |
| 收尾② | `apps/api_testing/models.py` | `apirequest.tags` 迁移漂移回填（0003 已建列，`models.py` 漏字段）→ 补 `JSONField` 而非删列；`makemigrations --check` 确认 0 漂移 | `manage.py makemigrations --check --dry-run` 无新增迁移 |

> 1e 自测：`tests/test_p1_1d_security.py`（29 条，覆盖 M5 注册/异常回显）+ `tests/test_m8_cookie_flow.py`（3 条，M8 Cookie 链路）+ 前端 `vite build`（全部含 `v-html`/`marked` 的文件与新 `sanitize` 工具编译通过）。
> 偏差与发现：
> 1. **M5 路由已彻底移除**：`apps/users/urls.py` 删除 `test-register`，前端 `register()` 改打 `/api/auth/register/`（§0.4 偏差项 ① 已闭环）。
> 2. **`apirequest.tags` 漂移已回填**：确认是保留字段，补回 `models.py` 而非删列（§0.4 偏差项 ② 已闭环）。
> 3. **XSS 攻击面比原 M6/M7 清单更广**：原方案只列 2+3 个文件，实际全仓 grep 出 **9 个** `v-html`/`marked` 使用点（含方案外的 `AssistantView`/`TaskDetail`/`RequirementAnalysisView`/`InterfaceManagement`），本轮全部纳入净化，统一走 `utils/sanitize.js`。
> 4. **真实浏览器 UI e2e 已跑通（`tests/ui_e2e_1e.py`）**：启动真实 MySQL + Memurai(Redis) + Django:8900 + Vite:3900，用 Playwright(Chromium) 从 UI 层真实操作登录页/主页/AI 评测师，**8 项全 PASS**（登录成功 / refresh_token HttpOnly / httpOnly=true / secure=False / 刷新后 cookie 续期保持登录 / XSS onerror 未执行 / 渲染 DOM 无 onerror / 登出后 cookie 清除）。此前"仅视图级/编译级验证"的缺口已闭合。
> 5. **真实链路暴露并修复缺陷①（M8 续期失效）**：`initAuth` 原先只在 `localStorage.user` 存在时才用 refresh cookie 续期，但登录流程未写 `localStorage.user` → 刷新页面后既不续期也不报错、直接退回登录页。修复：`else` 分支无条件尝试 cookie 续期（refresh cookie 是唯一可信凭证来源）。
> 6. **真实链路暴露并修复缺陷②（无限 401 循环）**：无条件续期后，未登录访问登录页时 refresh 401 会触发 `api.js` 拦截器 `userStore.logout()` → `window.location.href='/login'` → 页面重载 → 再续期 → 401 死循环。修复：拦截器对 `/auth/token/refresh/` 的 401 只清内存态、**不跳转**，交由调用方决定后续。
> 7. **真实链路暴露并修复缺陷③（logout CSRF 403）**：`logout_view` 仅有 `@csrf_exempt`，但请求携带 `sessionid` 时 DRF `SessionAuthentication` 仍强制 CSRF → 登出返回 403、refresh cookie 无法清除。修复：补 `@authentication_classes([])` 禁用会话认证（与 `cookie_token_refresh` 一致）。
> 8. **UI 交互细节**：`/home` 为独立布局，登出带 `ElMessageBox` 二次确认（须再点"确定"才真正登出）；`/home` 的功能卡片/用户菜单是可点击 `div` 而非 `<button>`，自动化需按文本 / `.el-dropdown-link` / `role=menuitem` 定位。

### §0.6 二开功能 UI 层测试补全（前置缺口闭合）

**范围界定**：`api-testing` / `ui-automation` / `performance-testing` / `monitor` / `defects` / `data-factory` / 项目-用例-评审-版本-套件-执行-报告 / 用户权限 属 testhub **基础功能**；围绕 AI 与 testgen 的模块属**二开新增功能**，即本轮补测对象：

| 二开模块 | 路由 | 补测方式 |
|---|---|---|
| 需求分析（智能用例生成） | `/ai-generation/requirement-analysis` | 页面渲染 + **真实提交生成任务**（断言 201 + task_id） |
| testgen 控制台（内嵌 sidecar） | `/ai-generation/testgen-console` | iframe 指向 Django 反代 `/testgen/?embed=1`、iframe 内 34 个按钮渲染、iframe 内切「代码仓库」视图 |
| AI 生成用例记录 | `/ai-generation/generated-testcases` | 统计卡与列表渲染 |
| AI 测试报告 | `/ai-generation/reports` | ECharts 5 张图渲染（canvas + `_echarts_instance_`） |
| AI 评测师 | `/ai-generation/assistant` | 页面渲染（XSS 净化已在 §0.5 UI e2e 覆盖） |
| 配置中心三件套 | `/configuration/ai-model`、`prompt-config`、`generation-config` | 列表渲染 + AI 模型「新增」弹窗可打开 |
| 文档中心 | `/docs-center` | 10 篇文档列表 + 点击渲染 Markdown（36KB HTML） + 无 `script/onerror` + 搜索过滤 |
| 智能评分器 | `/llm-judge/dashboard`、`rubrics` | 看板统计卡 + 评分标准页 |
| MCP 控制台 | `/mcp/console` | 17 个工具行 + 3 tab + 切「调用日志」无接口错误 |

**新增脚本**：`tests/ui_e2e_dev2.py`（Playwright + Chromium，真实服务链路：MySQL + Memurai + Django:8900 + Vite:3900 + testgen sidecar:8910），**30 项全 PASS**。每页统一断言「未跳登录 + 无 pageerror + 页面内 `/api/` 无 4xx-5xx + 关键元素就位」，再叠加上述交互断言。

**本轮真实链路暴露并修复的缺陷**：

1. **创建生成任务必 500（表结构漂移）**：`testcase_generation_task.source_type` 在库中为 `NOT NULL` 且无默认值，但模型/迁移中缺失 → `POST /api/requirement-analysis/testcase-generation/generate/` 报
   `(1364, "Field 'source_type' doesn't have a default value")`。修复：模型补 `source_type`（choices，default=`manual`）与 `source_ref`；视图按请求写入来源（`manual`/`document`/`testgen`）；前端手动输入与文档上传分别传 `manual`/`document`。
2. **同类漂移一次性清零**：新增常备工具 `tests/scan_schema_drift.py`（模型字段 ⇄ 库列双向比对），扫出并修复 3 表 4 列：`testcase_generation_task.source_type/source_ref`、`ui_test_cases.tags`（迁移 0003 已建列、模型漏字段）、`ui_ai_execution_records.source_generation_task`。修复后扫描结果 **DB_ONLY=0 / MODEL_ONLY=0**。
3. **迁移必须跨库**：`0003/0004` 用 `SeparateDatabaseAndState`（state 加字段 + DB 幂等建列），且列存在性判断改走 `connection.introspection.get_table_description()`——**初版用 MySQL 专有的 `information_schema`，在 SQLite 测试库上 `no such table` 直接崩**，已修正。
4. **`execute_security_verdict` 编排缺失（G3/G4/G5）**：`test_security_verdict_endpoint.py` 与 `test_g4_hook.py` 早已写好规格但函数不存在。已实现：observations 校验（400）→ sidecar 探活（503）→ project_id 回填 → `flow.run_security_verdict(verdict_fn=verdict, create_defect=create_security_defect_orm, reporter_id=user_id)`。
5. **缺陷建单未区分「诚实跳过」**：`flow.run_security_verdict` 把「缺 project_id」也标成 `defect=failed`。改为 `skipped_no_project`（引擎用例无规范项目关联属预期情形，不猜映射、不误建、不崩）。
6. **sidecar 错误分类补齐**：新增 `client.classify_sidecar_error()`（连接失败/超时→503，其它→502）与 `client.is_sidecar_available()`；`views._sidecar_error_response` 统一按分类返回，文案含「不可达」，替代原先一律 502。
7. **`apps/core/variable_resolver.py` 导入期篡改全局 stdout**：模块顶部在 win32 下执行
   `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`——导入即替换全局 stdout，旧 wrapper 回收后底层 buffer 关闭，任何后续接管 stdout 的框架（pytest capture / 日志重定向 / WSGI 服务器）都会 `ValueError: I/O operation on closed file`，且重复导入层层包装。**这是本仓库长期「pytest capture 假失败」的真正根因**。修复：改用 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`（原地改编码，不新建对象），异常时静默保持原样。

**仍挂起的规格先行用例**（10 skipped，已在代码内注明原因与追踪位置）：
- `TestGenerateProjectValidation`（7 项）：`generate` 的 `project_id` / `code_source` AK·SK 校验与 taskcenter 落库未实现；
- `TestgenHealthView` / `TestgenTaskStatusView`（3 项）：平台侧 health/task-status 端点与 `client.poll` 未实现。

> 验证：`pytest tests/ apps/testgen_integration/tests/` → **220 passed, 10 skipped**；`tests/ui_e2e_dev2.py` → **30/30 PASS**；`makemigrations --check` → 0 变化；`tests/scan_schema_drift.py` → 0 漂移；前端 `vite build` 通过。

---


## §1 P1（部署前必修：出本机/局域网/公网前必做）

> 这组是"默认即不安全"的配置、SSRF、明文密钥、前端 XSS 攻击链、WS/代理越权——**离开本机前必须修**。

### §1.1 总览

| 编号 | 位置 | 类别 | 一句话问题 | 整改提示（内容） |
|---|---|---|---|---|
| H1 | `backend/settings.py:298-300` | 安全 | `CORS_ALLOW_ALL_ORIGINS=True` + `CORS_ALLOW_CREDENTIALS=True` 共存 → 任意站点以当前登录用户身份发跨域请求（账号接管） | 禁止 allow-all 与 credentials 共存；强制 `CORS_ALLOWED_ORIGINS` 白名单；给不了白名单就 `CORS_ALLOW_CREDENTIALS=False` |
| H4 | `apps/api_testing/views.py` `utils.py`（原 5 处 `requests.request`） | 安全(SSRF) | `requests.request(method, url)` 的 `url` 直连，无协议/主机校验，可打云元数据 `169.254.169.254`、内网，`verify` 默认跟重定向 | ✅ **已完成（见 §0.3）**：`ssrf_guard.safe_request` 统一收口，逐跳校验重定向，恒拦元数据/链路本地 |
| H5 | `apps/api_testing/serializers.py:818` | 安全 | `AIServiceConfigSerializer.api_key` 可读可写，列出/获取配置即明文返回 API Key | ✅ **已完成（见 §0.4）**：`api_key` write_only + `api_key_masked` 脱敏 + `crypto` 密文落库（含 500→1024 迁移） |
| H6 | `apps/users/views.py:102-105` | 安全 | `UserDetailView` 仅 `IsAuthenticated`，无对象级权限 → 任意登录用户可 GET/PUT/PATCH/**DELETE** 他人账号 | 加 `IsOwnerOrAdmin`；列表限本人或管理员 |
| H7 | `backend/middleware.py:3-7` + `settings.py:112` | 安全 | `DisableCSRFMiddleware` 对全部 `/api/` 关 CSRF → 基于会话的接口整体绕过 | 移除该中间件；JWT 场景只留 JWT 认证；确需豁免的接口用 `@csrf_exempt` 精确标注 |
| H10 | `probes.py:352,474,547,589`、`custom_email_backend.py:14`、`proxy.py:59` | 安全 | 多处 `verify=False` 关 TLS 校验 → 中间人窃听/令牌窃取 | 默认 `verify=True` 或显式受信 CA；仅对确需自签的受信域放宽 |
| M1 | `apps/testgen_integration/proxy.py:25,73,80` | 安全 | 两代理视图全 `@csrf_exempt` 且转发 generate/pull/execute 等 state-changing POST 到 sidecar → 同源 XSS 即可驱动特权 sidecar | 不安全方法保留 CSRF；或要求平台会话并显式注入 sidecar token，而非信任客户端头 |
| M2 | `apps/testgen_integration/proxy.py:56` | 安全 | `cookies=request.COOKIES` 转发到 sidecar，泄露平台会话 cookie 给独立服务 | 丢弃 `cookies=`（或擦除 session cookie），鉴权已走 `X-Auth-Token` |
| M3 | `backend/urls.py:62-72` | 安全 | `static.serve` 未鉴权暴露 `app-automation-templates/`、`app-automation-reports/` | ✅ **已完成（见 §0.4）**：`protected_serve`（登录 + 穿越/白名单/nosniff）；生产建议仍由 Nginx X-Accel-Redirect 接管 |
| M4 | `apps/users/views.py:212,220-228` | bug | token 兑换 500（已归入 P0 先行修复，见 §0） | ✅ 已完成 |
| M5 | `apps/users/urls.py`(`test-register` 已删除) | 安全 | 测试接口挂产品路由，绕过正式校验，且 `except Exception` 回显 `str(e)`（异常泄露） | ✅ **已完成（见 §0.4 + §0.5）**：异常回显已消除 + 校验委托正式序列化器；`test-register` 路由已删除，前端改走 `/api/auth/register/` |
| M6 | `TestCaseDetail.vue`、`GeneratedTestCaseList.vue`、`AssistantView.vue`、`TaskDetail.vue`、`RequirementAnalysisView.vue`、`InterfaceManagement.vue` | 安全(XSS) | `v-html` 直注 DB 原始字段 / JSON 高亮 → 存储型 XSS | ✅ **已完成（见 §0.5）**：全部 `v-html` 经 `DOMPurify.sanitize`（`utils/sanitize.js` 统一入口） |
| M7 | `ExecutionReport.vue`、`ComparisonReports.vue`、`DocsCenter.vue` | 安全(XSS) | `v-html="marked.parse(...)"` 无 sanitizer；注释误称"marked 默认转义"（实际不转义） | ✅ **已完成（见 §0.5）**：统一 `renderMarkdown`（`DOMPurify.sanitize(marked.parse(src))`）；修正错误注释 |
| M8 | `stores/user.js`、`router/index.js`、`utils/api.py`、`apps/users/views.py` | 安全 | access/refresh JWT 均存 `localStorage` → 任意 XSS 即窃取（含 refresh） | ✅ **已完成（见 §0.5）**：access 仅内存态；refresh 改后端 HttpOnly+SameSite=Lax Cookie；新增 `cookie_token_refresh`；`logout` 从 Cookie 读 refresh |
| M9 | `apps/app_automation/consumers.py:9-18` | 安全 | WebSocket `connect()` 接受任意连接并按 `execution_id` 分组，无认证/授权 → 猜 id 读他人执行结果 | 强制 `self.scope["user"].is_authenticated` 并校验 execution 归属后再 `group_add` |
| M10 | `apps/ui_automation/ai_base.py:1681` | 安全 | AI 智能体把不可信网页 DOM 与系统指令混同 prompt，仅软性提示 → prompt injection 骗凭据 | 页面文本与系统指令分通道；域名/动作白名单约束；日志/输出脱敏 |
| M16 | `apps/api_testing/views.py:2175,2163-2165` | 安全 | webhook URL 直连 `requests.post`（SSRF）；钉钉签名日志打印含 `secret` 的 `string_to_sign/sign/webhook_url`（密钥+带令牌 URL 泄露） | ✅ **已完成（见 §0.3）**：webhook 走 `safe_request`；删敏感日志；`mask_webhook_url` 脱敏；写入口静态预检 |

### §1.2 P1 逐条整改提示与内容（实施时照此落地）

**H1（CORS + 凭证组合）**
- 内容：`django-cors-headers` 在 `CORS_ALLOW_ALL_ORIGINS=True` 时会把 `Access-Control-Allow-Origin` 反射成请求源并带 `Allow-Credentials`，任意网站可借当前登录用户身份发请求。
- 提示：删掉 `CORS_ALLOW_ALL_ORIGINS`；改 `CORS_ALLOWED_ORIGINS`（或 `CORS_ALLOWED_ORIGIN_REGEX`）白名单；确要跨域带凭证时名单必须显式且无通配；名单给不出就 `CORS_ALLOW_CREDENTIALS=False`。

**H4（SSRF）**
- 内容：`api_testing` 的 `requests.request` 与定时任务目标 URL 直连，缺协议/主机校验，且默认跟 302 转向内网。
- 提示：建 `apps/api_testing/ssrf_guard.py`（纯函数，可单测）：仅放行 `http/https`；`urlparse` 后解析主机（含重定向后主机），拒绝私有/链路本地/元数据段；`allow_redirects=False` 或限制重定向目标同域；`ScheduledTask.url` 在 `save()`/`activate` 时静态校验。

**H5（api_key 明文）**
- 内容：`AIServiceConfigSerializer` 的 `api_key` 字段可读写，列出即返回明文。
- 提示：`api_key = serializers.CharField(write_only=True)`；新增 `api_key_masked = serializers.SerializerMethodField()` 只返回 `***`；入库走 `crypto.py` 加密（注意 `assistant/`、`requirement_analysis/` 已有 `write_only`/脱敏先例，对齐）。

**H6（用户对象权限）**
- 内容：`UserDetailView` 仅 `IsAuthenticated`，无对象级鉴权。
- 提示：实现 `IsOwnerOrAdmin` 权限类（比对 `request.user == obj` 或 `is_staff`）；列表接口加 `filter(user=request.user)` 或管理员分支。

**H7（CSRF 整体绕过）**
- 内容：`DisableCSRFMiddleware` 对全部 `/api/` 置 `_dont_enforce_csrf_checks=True`。
- 提示：删除该中间件与 `settings` 注册；JWT 认证路径天然无 CSRF 需求；个别确需豁免的视图（如 `exchange_token_view` 的一次性码兑换属无会话接口）用 `@csrf_exempt` 精确标注。

**H10（verify=False）**
- 内容：探针/SMTP/代理转发关闭 TLS 校验。
- 提示：默认 `verify=True`；确需自签的受信域用 `verify=<CA路径>` 或 `REQUESTS_CA_BUNDLE`；严禁全局 `verify=False`。

**M1（testgen 代理 CSRF）**
- 内容：代理视图全 `@csrf_exempt` 且转发 state-changing POST。
- 提示：移除 blanket `@csrf_exempt`；POST/PUT/PATCH/DELETE 走 CSRF；或要求平台会话 + 代理层显式注入 `X-Auth-Token`（而非信任客户端头）。

**M2（cookie 泄露）**
- 内容：代理把 `request.COOKIES` 转发给 sidecar。
- 提示：删除 `cookies=request.COOKIES`；sidecar 鉴权已走 `X-Auth-Token`，无需会话 cookie；若必须，至少擦除 `sessionid`。

**M3（static.serve 暴露）**
- 内容：未鉴权 `static.serve` 暴露模板/报告目录。
- 提示：加 `login_required`/权限类；生产用 Nginx `internal`/X-Sendfile 或对象存储带签名 URL。

**M5（测试接口挂产品路由）**
- 内容：`test-register` 绕过正式校验 + 回显原始异常。
- 提示：移出产品 `urls.py`（或 `if settings.DEBUG` 才挂）；异常只返回通用错误，不回显 `str(e)`。

**M6 / M7 / M8（前端 XSS 攻击链）**
- 内容：`v-html` 直注 DB 文本（M6）、`marked.parse` 无 sanitizer（M7）、JWT 存 `localStorage`（M8）→ 存储型 XSS 偷 refresh token。
- 提示：所有 `v-html` 经 `DOMPurify.sanitize(...)`；禁对 DB 原始字段直渲染；JWT 改 httpOnly+Secure Cookie 或内存态，绝不存 refresh 于 `localStorage`；修正 M7 中"marked 默认转义"的错误注释。

**M9（WS 无认证）**
- 内容：`AppExecutionConsumer.connect()` 无认证/授权。
- 提示：connect 时校验 `self.scope["user"].is_authenticated`，并按 `execution_id` 查归属；非本人/管理员拒绝 `group_add`。

**M10（AI prompt injection）** ✅ 已落地（2026-10-30，见 `docs/m10_prompt_injection_2026-10-30.md`）
- 内容：不可信网页 DOM 与系统指令混同 prompt。
- 提示：页面观测文本与系统指令分通道拼接（结构化分隔 + 明确"以下为不可信外部内容"）；用域名/动作白名单约束 agent 能执行的敏感操作；日志/产物脱敏。
- 落地：①`_security_boundary_prompt()` 最高优先级边界段（不可信数据 / HOSTILE TEXT / 凭据不得跨域重输）；
  ②在 `ToolRegistry.execute_action`（全动作唯一出口）默认拒绝 `evaluate`/`upload_file`/`read_file`/`write_file`/`replace_file`；
  ③导航类 URL 的 host 白名单（任务内 URL ∪ `UI_ALLOWED_DOMAINS`，`UI_DOMAIN_GUARD=warn|strict|off`，默认 warn）。
  局限：①仍为软约束，③默认不硬拦，IP 直连/302 绕过未处理。单测 29 项，主仓 275 passed。

**M16（webhook SSRF + 日志泄密）**
- 内容：webhook URL 直连（SSRF）；钉钉签名日志打印含 `secret` 串。
- 提示：webhook URL 复用 H4 的 SSRF 校验；日志删除 `string_to_sign/sign/webhook_url` 原始值，`webhook_url` 记录前截断 `access_token/sign` 参数。

---

## §2 P2（健壮性 / 性能，按需）

| 编号 | 位置 | 类别 | 问题 | 整改提示（内容） |
|---|---|---|---|---|
| M12 | `services/testgen/service/tasks.py:313` | bug | `BackgroundExecutor._run` 无条件置 `RUNNING`，覆盖 `PENDING` 时已 `CANCELLED` → 取消丢失 | 置 `RUNNING` 前先 `is_cancelled()`，已取消则中止 |
| M13 | `apps/testgen_integration/client.py:346-350` | 性能/bug | `quality_summary` 用 `.result()` 逐个阻塞，4 调用串行（虽开 4 线程池） | 先全部 `submit`，再 `as_completed` 收集 |
| M14 | `apps/testgen_integration/client.py:351` | bug | `all_ok = all(s.get("ok", False)...)` 但部分返回无 `ok` 键 → 恒 `False` | 以 `raise_for_status()`（HTTP 200）为 ok，或给每个结果包 `ok=True` |
| M15 | `apps/api_testing/views.py:476,251,375,392`、`utils.py:251`、`models.py:93` | 性能/bug | 响应体全量读入内存写库无上限（DoS+膨胀）；`auth` 字段定义却从未附加 → 认证请求静默失效 | `stream=True`+截断并标 `truncated=True`；补全 `auth` 注入到 header/基础认证 |
| M18 | `services/testgen/service/app.py:464,1352` | 性能 | `/pipeline`、`/describe` 同步阻塞逐条调 LLM | 改后台任务/异步 LLM 调用 |
| M19 | `services/testgen/service/tasks.py:240-243` | 性能 | `is_cancelled()` 每次进度回调全表 `SELECT` | 每任务内存缓存 cancelled 标志 |
| M20 | `services/testgen/service/app.py:395` | 安全(信息泄露) | `/ready` 返回 `public_dict()` 含 `db_path/output_dir/workspace_root` 等路径 | 返回最小状态 payload |
| L2 | `apps/api_testing/views.py:47-49` | 可维护性/潜在 bug | 同模块重复定义 `ApiProjectSerializer` 等（F811） | 删重复定义，确认实际引用版本 |
| L8 | `apps/api_testing/views.py:1697-1799` | 性能 | `run_now` 每触发新建守护线程无并发上限（放大 H4 SSRF） | 改用 Celery/RQ 等带并发限制的异步队列 |

---

## §3 P3（打磨 / 合规）

| 编号 | 位置 | 类别 | 问题 | 整改提示（内容） |
|---|---|---|---|---|
| L1 | 全仓（ruff 统计） | 可维护性 | 3339 条 lint（I001/UP031/RUF059 主导） | `ruff check --fix` 自动修 1528 条（98 条需 `--unsafe-fixes` 复核）；接 pre-commit + CI 门禁 |
| L3 | `services/testgen/tests/test_store.py:62,71` | 安全(测试) | 硬编码 `/tmp` 路径（B108） | 改用 `tempfile` 临时目录 |
| L4 | `encryption_tools.py:36,50`、`app_automation/...:134,457`、`image_tools.py:104`、`views.py:620` | 安全(待确认) | MD5/SHA1 用于"安全"目的（B324）；`Crypto` 实为 pycryptodome（B413 误报） | 确认是否用于口令/签名；若是改 SHA-256；`Crypto` 可用无需替换 |
| L5 | `services/testgen/engine/pipeline.py` 等 | 可维护性 | 上帝文件 `app.py`(1778 行)、双层 stage 顺序表 | 专项重构，配测试防回归 |
| L6 | `services/testgen/scripts/ui_test_tp_ui.py` | 可维护性 | 半成品 UI e2e 草稿脚本 | 收口为正式 Playwright 用例或移出主分支 |
| L7 | `services/testgen/tests/`（假密码 `S3cret-PW-xyz` 等） | 合规 | 测试夹具含明显假密码/token，CI secret-scan 误报 | 改 `dummy` 常量或移出默认集；`.secrets-allowlist.json` 入库 |

---

## §4 推进原则与结语

1. **合并批次**：以上 P1/P2/P3 与《整合实施_未完成任务追踪》T1–T8、优化批次 B1–B7、Playwright 能力引入 **高度重叠**，统一归入"统一解决"批次，避免一次性大改引入回归。
2. **解锁条件**：等 **G1（Django+MySQL 运行环境）** 就绪后，按 **P0 → P1 → P2 → P3** 顺序推进；P0 已先行落地并自测全绿。
3. **回归防护**：每批落地后跑对应自动化自测（参照 `test_p0_security.py` 的纯函数 + 集成分层写法），并接入 `ruff`/`bandit` CI 门禁（L1）。
4. **P1 优先级最高**：H1/H4/H5/H6/H7/H10 + M1–M10/M16 是"出本机即高危"项，务必在局域网/公网部署前清零。

> 以上为方案，待你确认后我再逐批实施（含代码与自测）。

---

## §5 统一解决批次 · 推进路线图（待 G1 解锁后按 P0→P3）

> 本节是"待 G1 解锁后按 P0→P3 推进"的**执行序列与每批提示/内容**。
> **进度现状**：P0（§0）、1a（§0.1）、1b（§0.2）、1c（§0.3）、1d（§0.4）、1e（§0.5）**已落地**；1e 另完成**真实浏览器 UI e2e（`tests/ui_e2e_1e.py` 8 项全 PASS）**，P1 阶段清零。**二开功能 UI 层测试已补全**（§0.6，`tests/ui_e2e_dev2.py` 30 项全 PASS，并修复 7 个真实缺陷）。**阶段 2（P2）已完成清零**：L2 / M12 / M13 / M14 / M15 / M18 / M19 / M20 / L8 / G3G4G5 全部落地（收尾批次 M12/M18/M19/L8 的实施与验证记录见 `docs/p2_batch3_plan_2026-10-30.md` §8）。**P1 唯一缺口 M10（AI prompt injection）已于 2026-10-30 落地**（prompt 边界段 + 动作黑名单 + 域名白名单三层，见 `docs/m10_prompt_injection_2026-10-30.md`），P1 至此实质清零（M1 的 `@csrf_exempt` 属已登记的已知局限）。

### §5.0 G1 是什么 / 为何阻塞

**G1 = Django + MySQL + Redis 运行环境就绪**（本机：`Start-Service MySQL80` → 确认 3306 监听 → Django `:8900` 起；Redis 起；testgen sidecar `:8910`/`8100` 可达；前端 `:3900` dev server）。

- **阻塞点 A（P0 真实端到端）**：M4 的真实 Redis 读写、H9 的真实 MySQL 探针，目前用"纯函数 + 注入假依赖"做了单元/集成级验证，**真实链路需 G1 复跑确认**。
- **阻塞点 B（P1 全栈验证）**：H1/H4/H5/H6/H7/H10 + M1–M10/M16 全是 Django 侧（含 WS、代理、前端 XSS），需全栈 + 浏览器才能验证。
- **阻塞点 C（P2 集成）**：M12/M13/M14/M18/M19 需 sidecar/Redis/MySQL 跑集成；L8 需异步队列。
- **不阻塞项**：L1（ruff 门禁）、L3/L7（测试项）可**不依赖 G1 先并行**做。

### §5.1 阶段 0 — P0（已完成，自测全绿）

- 内容：H2/H3/H8/H9/M4/M11/M17 共 7 项，见 §0 与 `test_p0_security.py`（20 条）。
- 提示：G1 解锁后，第一件事是**用真实环境复跑 M4（Redis 兑换码）/ H9（真实 MySQL 探针）端到端**，确认纯函数层发现的问题在真实栈不复现。

### §5.2 阶段 1 — P1 部署前必修（G1 解锁后第一批，拆 5 子批）

| 子批 | 项 | 内容（做什么） | 提示（G1 依赖 / 验证手段） |
|---|---|---|---|
| **1a 后端安全配置** | H1(CORS) / H7(CSRF 中间件移除) / H10(verify=False) | ~~删 `CORS_ALLOW_ALL_ORIGINS` 改白名单；删 `DisableCSRFMiddleware`；`verify` 默认 `True`~~ **✅ 已完成（见 §0.1）** | Django 起即可验证；H10 多数在 `probes.py`/`proxy.py`/`custom_email_backend.py`，可用纯函数/单测先锁 |
| **1b 鉴权与对象权限** | H6(用户权限) / M1(代理 CSRF) / M2(cookie 泄露) / M9(WS 认证) | ~~`IsOwnerOrAdmin`；代理注入 `X-Auth-Token` + 去掉会话 cookie 转发 + 鉴权门；WS `connect` 校验归属~~ **✅ 已完成（见 §0.2）** | 需 Django+Redis（会话/M9 走 channels）；M1/M2 关联 H8 的 sidecar 鉴权，需 sidecar 起 |
| **1c SSRF 防护** | H4 + M16(webhook) | ~~抽 `ssrf_guard.py` 纯函数：仅 http/https、拒绝链路本地/元数据段、重定向逐跳校验；webhook 复用；日志脱敏~~ **✅ 已完成（见 §0.3）** | **纯函数 + 活体 socket e2e 均已验证，未依赖 G1**；`API_TESTING_SSRF_STRICT=1` 为生产收紧开关 |
| **1d 密钥与数据暴露** | H5(api_key 明文) / M3(static 暴露) / M5(测试路由) | ~~`api_key=write_only` + `api_key_masked` 脱敏 + `crypto.py` 加密；`static.serve` 加权限或改 X-Sendfile；`test-register` 移出产品路由或仅 DEBUG~~ **✅ 已完成（见 §0.4）** | 已用真实路由 + 真实 DB 活体验证；M5 路由未移出（前端依赖），改为消除绕过 + 去异常回显，见 §0.4 偏差说明 |
| **1e 前端 XSS 攻击链** | M6(v-html) / M7(marked 无 sanitizer) / M8(JWT 存 localStorage) + 注册路由收敛 + tags 漂移回填 | 所有 `v-html` 经 `DOMPurify.sanitize`；`marked.parse` 结果必过 sanitizer；JWT 改 httpOnly+SameSite=Lax Cookie / 内存态，绝不存 refresh | ✅ **已完成（见 §0.5）**：9 个 `v-html`/`marked` 文件全净化 + 前端 `vite build` 通过；后端 `cookie_token_refresh` + 视图级 3 项测试 PASS；`test-register` 路由删除；`apirequest.tags` 补回模型 |

- **阶段 1 收尾验证**：出本机/局域网前，跑"登录态 + 跨域 + 越权 + XSS"四维手测 + 自动化回归。
- **阶段 1 进度**：1a ✅ / 1b ✅ / 1c ✅ / 1d ✅ / 1e ✅（阶段 0~1d 累计 **128 条 pytest 全绿** + 3 个活体 e2e 脚本 33 项全 PASS；1e 新增 `test_m8_cookie_flow.py` 3 项视图级测试 + 前端 `vite build` 全量编译通过）；**下一批 = 阶段 2 P2（§5.3）**。
- **1e 收尾项（本轮已闭环）**：① 前端注册改走 `/api/auth/register/` 并删除 `test-register` 路由 ✅；② `apirequest.tags` 迁移漂移确认保留字段并已补回 `models.py` ✅。

### §5.3 阶段 2 — P2 健壮性 / 性能（P1 清零后）

| 项 | 内容（做什么） | 提示（G1 依赖 / 验证手段） |
|---|---|---|
| M12 取消竞态 | 置 `RUNNING` 前先 `is_cancelled()` | ✅ **已完成**：改为 `TaskStore.try_start()` 条件更新 `WHERE state='pending'`（单语句原子，根治 TOCTOU，不依赖 Redis）。真实链路验证：`GEN_MAX_WORKERS=1` 下排队期取消，`started_at` 恒空、状态恒 cancelled；运行中取消连跑 5 轮无 success。详见 `docs/p2_batch3_plan_2026-10-30.md` §8 |
| M13/M14 伪并发 / all_ok 恒假 | `submit` 后 `as_completed` 收集；以 `raise_for_status` 定 ok | ✅ **已完成**：`apps/testgen_integration/client.py::quality_summary` 改 `as_completed` 真正并发 + `_safe` 返回 `(ok, data)`，顶层 `ok` 以「是否抛异常」为准；新增 2 项单测（`test_client.py` 9 项全通过） |
| M15 响应体上限 + auth 注入 | `stream=True`+截断标 `truncated`；补全 `auth` 到 header/基础认证 | ✅ **已完成**：`utils.read_response_body()`（默认上限 2MB，超限截断 + 主动 `close()` 释放连接 + 回填 `_content` 使断言仍可读）、`read_response_json()`、`apply_auth()`（bearer/basic/api_key-in-header/query，显式 Authorization 优先）；3 处落库路径（单请求执行 / 套件执行 / collection 执行）统一改为 `stream=True` + 受控读取 + `response_data.truncated`；AI 连通性测试的 `details` 输出截断 2000 字。验证：`tests/test_m15_response_limits.py` 21 项（截断/边界/编码/JSON/auth 各分支）+ `tests/test_m15_live_execute.py` 3 项真实 HTTP（服务端收到 `Bearer` 头、3MB→截断且 `truncated=True`、小响应正常）全通过 |
| M18/M19 同步阻塞 LLM / 全表 SELECT | 后台任务/异步 LLM；每任务缓存 cancelled 标志 | ✅ **已完成**：M19 加「已取消内存快路径 + 未取消回查 DB + 终态 forget + 1000 条上限」；M18-b `/describe` 组间 4 路并发 + 总预算 `describe_budget_seconds`(90s) 超时降级不挂死（LLM 并发、DB 写串行）；M18-a `/pipeline` 增 `async_mode`（默认 false 零回归，202 + 复用 `/generate` 任务模型）。详见 `docs/p2_batch3_plan_2026-10-30.md` §8 |
| M20 `/ready` 信息泄露 | 返回最小状态 payload | ✅ **已完成**：`/ready` 不再返回 `settings.public_dict()`（含 db_path/output_dir/workspace_root），只返回 `{"status":"ready"}`；testgen `test_api.py` 9 项通过 |
| L2 重复序列化器定义(F811) | 删重复定义 | ✅ **已完成**：F811 全仓 23 处清零；`TestCaseGenerationRequestSerializer` 拆分出 `GenerateTaskRequestSerializer`（修复 `generate_test_cases` 因类被覆盖导致 KeyError）；删残缺 `get_queryset`、删被覆盖的旧 `analyze_text`；主仓 111 项 pytest 全绿 |
| L8 无并发上限守护线程 | 改 Celery/RQ 带并发限制队列 | ✅ **已完成**：新建 `apps/api_testing/tasks.py`（`@shared_task`），`run_now` 改派发器——主路径 Celery，broker 不可用退 **BoundedSemaphore(4) 有界池**，槽位满返回 **429** 且执行日志落 FAILED；管理命令共用同一入口。真实链路 `run_now → celery → worker → COMPLETED` 已打通 |
| G3/G4/G5 安全裁决编排缺失 | 实现 `views.execute_security_verdict` + 缺陷建单「诚实跳过」 | ✅ **已完成**（§0.6）：observations 校验→sidecar 探活 503→project_id 回填→`flow.run_security_verdict`；`defect=skipped_no_project` 区别于 `failed`；`client.classify_sidecar_error` / `is_sidecar_available` 补齐 |

### §5.4 阶段 3 — P3 打磨 / 合规（可并行，不完全依赖 G1）

| 项 | 内容（做什么） | 提示（G1 依赖 / 验证手段） |
|---|---|---|
| L1 lint 门禁（3339 条） | `ruff check --fix`（98 条需 `--unsafe-fixes` 复核）+ pre-commit + CI | **不需 G1，可立即并行**；先修 P0 涉及的 `F821`/安全类 |
| L3 测试硬编码 `/tmp`(B108) | 改 `tempfile` 临时目录 | 测试项，不需 G1 |
| L4 MD5/SHA1 用途确认(B324) | 确认是否用于口令/签名，是则改 SHA-256；`Crypto` 实为 pycryptodome 可用 | 代码审查即可 |
| L5 上帝文件重构 | `app.py`(1778 行) 专项拆分，配测试防回归 | 不需 G1，但需配套测试 |
| L6 半成品草稿脚本 | 收口为正式用例或移出主分支 | 不需 G1 |
| L7 测试假密码/token | 改 `dummy` 常量或移出默认集；`.secrets-allowlist.json` 入库 | 测试项，不需 G1 |

### §5.5 推进纪律

1. **每批独立 commit**（`<type>: <简短描述>`），不自动提交整仓。
2. **每批附自测**：纯逻辑抽纯函数 + 单测（参照 P0 写法）；集成项待 G1 起全栈复跑。
3. **回归门禁**：P0 的 `test_p0_security.py` 必须常驻绿；新增项按同样分层补测试。
4. **顺序硬约束**：P0 → P1（1a→1e）→ P2 → P3；P1 未清零不得出本机/局域网。

> 以上为方案，待你确认后我再逐批实施（含代码与自测）。
