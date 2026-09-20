# TestHub 代码规范（CODE_STANDARDS）

> 适用范围：本仓库（TestHub 测试中心）及其整合进来的 `services/testgen` 代码生成 sidecar。
> 本文在 `AGENTS.md` / `CLAUDE.md` 已沉淀的**架构、常用命令、提交规范**基础上，补全**命名、目录、注释、提交、安全、迁移铁律**等可执行细则，作为二次开发的统一约束。
> 凡本文与 `AGENTS.md`/`CLAUDE.md` 冲突，以本文为准；本文未覆盖的架构/命令，仍以 `AGENTS.md` 为准。

---

## 0. 通用原则

1. **可读性优先**：代码是写给人看的，注释用中文（与现有代码库一致），用户可见文案统一走 i18n。
2. **只增不改**：对现有 21 个 Django app 的**业务**逻辑保持只读；新增能力一律以**新 app / 新模块 / 新端点**方式落地（见 §6 迁移铁律）。
3. **单一职责**：业务逻辑下沉到 `services/` 或 `core/`，ViewSet 只做请求编排与序列化，不堆砌业务代码。
4. **显式优于隐式**：跨服务调用、外部依赖、超时、重试必须显式写出，禁止裸 `requests.get` 无 timeout。
5. **安全前置**：任何 `*_PASSWORD` / `*_OTP` / `API_KEY` 绝不进 git 或数据库明文（见 §7）。

---

## 1. 后端（Django 4.2 / DRF / Python）

### 1.1 目录结构（强制）

```
testhub_platform/
├── backend/            # Django 项目包：settings.py / urls.py / asgi.py / celery.py / wsgi.py / test_settings.py
├── apps/               # 业务 app（模块化，每个 app 自包含 models/serializers/views/urls/admin/migrations/tests）
│   ├── <app_name>/     # 蛇形小写，单一职责；app 内再按服务拆 services/ utils/
│   └── testgen_integration/   # 集成 testgen sidecar 的「新增」app（代理 + 资产回流），不得改其他 app
├── services/
│   └── testgen/        # 代码→用例生成 sidecar（FastAPI，独立工程，业务零改，见 §5）
├── frontend/           # Vue3 前端（见 §2）
├── manage.py / requirements.txt / pytest.ini / CODE_STANDARDS.md / AGENTS.md / CLAUDE.md / README.md
```

- 新增 Django app：在 `apps/` 下 `python manage.py startapp <snake_name> apps/<snake_name>`，注册到 `backend/settings.py` 的 `INSTALLED_APPS`（追加，不改现有项）。
- 跨模块共享逻辑放 `apps/core/`（变量解析、管理命令、`locator` 策略等），**禁止**在 app 间循环 import。
- 所有管理命令放 `apps/core/management/commands/`。

### 1.2 命名约定（强制）

| 对象 | 规则 | 示例 |
|---|---|---|
| 文件 / 模块 | `snake_case.py` | `requirement_analysis/views.py`、`services/compare_report.py` |
| 类（Model/ViewSet/Serializer/View/Service） | `CapWords` + 语义后缀 | `TestCase`、`RequirementDocumentViewSet`、`AppProjectSerializer`、`JudgeSingleView` |
| Model 类名 | 优先带 app 前缀避免跨表歧义（遵循现有 `App*` 约定） | `AppProject`、`AppTestCase`；通用域可无前缀如 `TestSuite`、`Version` |
| Serializer | `XxxSerializer`；读写分离用 `XxxCreateSerializer` / `XxxUpdateSerializer` / `XxxSimpleSerializer` | `VersionCreateSerializer` |
| ViewSet | `XxxViewSet`（ModelViewSet / ReadOnlyModelViewSet / 裸 ViewSet） | `GeneratedTestCaseViewSet` |
| APIView / GenericAPIView | `XxxView` / `XxxAPIView` | `DashboardStatsView`、`VersionListCreateView` |
| 函数 / 方法 / 变量 / 字段 | `snake_case` | `run_all_scheduled_tasks`、`is_refreshing`、`login_mode` |
| 常量 | 模块级 `UPPER_SNAKE_CASE` | `DEFAULT_SCOPE`、`ALL_TP_TYPES` |

### 1.3 代码风格（推荐 → 强制）

- **基线**：PEP 8。
- **行宽**：建议 ≤ 119（与 testgen sidecar 的 100 互不强制；新增 Django 代码建议 ≤ 119，testgen 维持 100，见 §5）。
- **导入顺序**：标准库 → 第三方（`django`、`rest_framework`、`requests`…）→ 本地（`apps.*`、`backend.*`）；用 `isort` 风格，本地包 `known-first-party = ["apps","backend","services"]`。
- **类型注解**：新函数建议加类型注解（`def foo(x: int) -> str:`），与 testgen 保持一致。
- **Lint/Format（补齐缺口）**：后端目前**无强制 lint 配置**。新代码推荐引入 `ruff`（复用 testgen `services/testgen/pyproject.toml` 的 select 集作为起点：`E/F/I/W/B/C4/UP/SIM/RUF/PLR/BLE/C90`，行宽按上表），在 CI 中跑 `ruff check` + `ruff format --check`。**引入前先在 `requirements.txt` 加 `ruff`，并在 PR 中单独提交，避免与功能改动混在一起。**

### 1.4 注释与文档

- **docstring**：新模块/公共函数采用 **Google 风格**（已有 `perf_testing`、`data_factory` 新模块先例）：
  ```python
  def verify_token(token: str) -> bool:
      """校验访问令牌是否有效。

      Args:
          token: JWT 访问令牌字符串。
      Returns:
          有效返回 True，否则 False。
      """
  ```
- **行内注释**：用中文，解释「为什么」而非「是什么」。遵循现有 `api.js` / `gate_all.py` 风格（`# 正在刷新的标志`、`# 子进程清空批量删除护栏变量…`）。
- **Model 字段**：`help_text` 用中文说明业务含义；`verbose_name` 用中文。

### 1.5 Django 专项

- Model 层不放跨表复杂查询；聚合/编排放 `services/` 或 ViewSet 的 `get_queryset` 简单过滤。
- 序列化与持久化分离：写操作优先用 `Serializer` + 显式 `save()`，禁止在 ViewSet 里直接拼 SQL。
- 外部 HTTP 调用统一走 `requests`/`httpx` 并带 `timeout`（范本 `apps/assistant/views.py:115` Dify 调用）；配置项从 `settings`/`env` 读取，禁止硬编码 URL/密钥。

---

## 2. 前端（Vue 3 + Vite + Element Plus）

### 2.1 目录结构（强制，沿用现有）

```
frontend/src/
├── views/         # 按功能模块建 kebab-case 目录（api-testing/ app-automation/ defects/ …），页组件 PascalCase
├── api/           # API 服务层（按模块拆文件）
├── stores/        # Pinia 状态（useUserStore / useAppStore …）
├── router/        # Vue Router（index.js）
├── components/    # 共享组件 PascalCase
├── layout/        # 布局组件
├── locales/       # vue-i18n（zh-hans / en / ja / ko）
└── utils/         # 共享工具（api.js 为 axios 实例，baseURL='/api'）
```

### 2.2 命名约定（强制）

| 对象 | 规则 | 示例 |
|---|---|---|
| Vue 组件文件 | `PascalCase.vue`；模块入口用 `index.vue` / `Index.vue` | `AIServiceConfig.vue`、`CaptureElementDialog.vue`、`Login.vue` |
| 功能模块目录 | `kebab-case` | `api-testing/`、`app-automation/`、`scheduled-tasks/` |
| 变量 / 函数 / ref / reactive key | `camelCase` | `isRefreshing`、`loginMode`、`handleLogin` |
| 常量 / 枚举 | `UPPER_SNAKE_CASE` 或导出的 `const XxxKey` | `DEFAULT_TIMEOUT` |
| CSS class | `kebab-case` | `login-container`、`feature-card` |

### 2.3 编码风格（强制）

- **`<script setup>` Composition API**：所有新组件必须用 `<script setup>`，禁止 Options API 新写法。
- 状态：`ref()` / `reactive()`；方法用 `const foo = () => {}` 箭头函数常量。
- 全局 HTTP：`import api from '@/utils/api'`（已含 token 刷新拦截、401 处理、错误兜底），**禁止**在组件里另起 axios 实例。
- 状态管理用 Pinia（`@/stores/*`），**禁止**在组件间用全局事件总线传业务数据。
- **i18n**：所有用户可见文案走 `$t()` / `t()`（vue-i18n），新增文案在 `locales/zh-hans` 补 key；模板中用 `<!-- 中文注释 -->`。
- UI 库统一 Element Plus（`el-form` / `el-input` …），不混用其他组件库。
- **Lint**：前端已有 `npm run lint`（ESLint），提交前必须跑通。

---

## 3. API 与接口规范

- **前缀**：所有后端接口统一 `/api/`；testgen sidecar 用 `/api/v1/`（独立端口 8100，不进 Django 路由）。
- **鉴权**：testhub 用 JWT（SimpleJWT，`/api/auth/token/` 取、`/api/auth/token/refresh/` 刷新）；testgen 用 `X-Auth-Token` 头（仅当 `AUTH_TOKEN` 非空时校验）。
- **幂等**：可重复触发的生成/执行接口必须带幂等键（testgen 用 `idem_key`；testhub 侧以 `external_id` upsert 回流，避免重复用例）。
- **错误格式**：业务错误返回 `{ "detail": "..." }` 或 `{ "error": "..." }`，HTTP 状态语义正确（400/401/403/404/422/429/500）。
- **超时与重试**：跨服务调用显式 `timeout`；testgen `/generate` 为异步（202 + `task_id` + `poll_url`），调用方轮询 `GET /api/v1/tasks/{id}`。
- **自描述**：testhub 已暴露 `/api/schema/`（drf-spectacular）、`/api/docs/`（Swagger）、`/api/redoc/`；新增端点须可被 schema 正确采集（避免 `@action` 缺 `responses` 导致文档缺失）。

---

## 4. 提交规范（强制，对齐 AGENTS.md/CLAUDE.md 并收紧）

> `AGENTS.md` 已规定：默认不自动提交、相关修改合一、`<type>: <简短描述>`、提交前跑 lint+test。本文在此**明确 type 集合与写法**，并统一历史混用现状。

### 4.1 提交信息格式

```
<type>: <中文简短描述>
```

- **type（英文小写，固定集合）**：

  | type | 含义 |
  |---|---|
  | `feat` | 新功能 |
  | `fix` | 缺陷修复 |
  | `docs` | 文档（含 AGENTS/CLAUDE/README/CODE_STANDARDS） |
  | `refactor` | 重构（非功能新增、非修复） |
  | `perf` | 性能优化 |
  | `test` | 测试新增/修正 |
  | `style` | 格式（不影响逻辑，如 ruff format） |
  | `chore` | 构建/依赖/杂项 |
  | `ci` | CI / 部署配置 |
  | `revert` | 回滚 |

- **描述**：中文、祈使、≤ 30 字；说明「做了什么」而非「为什么」（原因进正文或 PR）。
- 示例：`feat: 新增代码分析入口调 testgen 生成`、`fix: 修复 token 刷新队列死循环`、`docs: 补全 CODE_STANDARDS 命名约定`。

### 4.2 提交纪律

- **默认不自动提交**：AI / 开发者完成改动后，由人确认再提交；禁止 `--no-verify` 跳过钩子（除非用户显式要求）。
- **相关修改合一**：同一功能的前后端、多文件改动合并为一个 commit；不要用「修复1/修复2」碎片化提交。
- **提交前必须**：后端跑 `pytest`（`pytest.ini` 已配 `DJANGO_SETTINGS_MODULE=backend.test_settings`）+（启用后）`ruff`；前端跑 `npm run lint` + `npm run build`。
- **历史兼容**：仓库早期提交存在纯中文自由描述，新提交自本文生效日起一律按本规范；不要求回填旧提交。

### 4.3 分支与合并

- 分支流：**`feature/<名>` → `dev` → `release`**，经 PR（GitHub/GitLab Merge Request）合入；`main` 仅作受保护发布分支（建议开启分支保护）。
- PR 描述须说明：改动范围、测试方式、是否触碰现有 app 业务（应填「否」）、回滚方案。

---

## 5. testgen sidecar 子规范（整合后强制保留）

`services/testgen/` 是以 FastAPI 形态迁入的代码生成引擎，**业务零改、独立工程**。其自带规则随代码原样保留，禁止重建或删减：

- **质量门禁（八环）**：`scripts/gate_all.py` —— secret → ruff(lint+format) → check_enums → check_structure → mypy → bandit → pytest（cov≥60）。整合后在 testhub CI 中作为**独立 job** 跑（不并入 testhub 现有 CI，避免两套规则互相拖累）。
- **ruff 配置**：`services/testgen/pyproject.toml` 定义 select 集与 `per-file-ignores`（含 `engine/runtime_ui.py`、`engine/llm_fallback.py` 放行 `BLE001`）。**该文件整体随迁，禁止重写或精简豁免项**，否则门禁会假性失败。
- **运行环境**：`requires-python >=3.11`；复用 testhub 的 Python 解释器与 venv；**不得升级 testhub 现有 starlette**（与 channels/Daphne 链路耦合），fastapi 版本须与之兼容。
- **端口 / 网络**：默认监听 `127.0.0.1:8100`，绝不暴露公网；跨容器走内部网络 + `TESTGEN_BASE_URL`。
- **生成-only 红线**：testgen `/api/v1/generate` 收到 `execute=True` 必须返回 422；执行一律交给 testhub 的 `ui_automation`/`api_testing`。

---

## 6. 迁移专项铁律（整合 testgen 期间强制）

1. **testhub 只增不改**：现有 21 个 app、settings、urls、前端业务组件一律不改动业务；新增能力只以新 app（`testgen_integration`）、新端点、新前端页面方式接入。
2. **testgen 业务零改**：`core/`、`engine/`、`service/app.py` 逐字节不变；迁移仅改「落位 + 部署壳 + 新增接口薄封装（如 `/verdict`）」。
3. **功能完整保留**：所谓「放弃」是集成流分工（执行/报告/缺陷下沉 testhub），testgen 代码全保留、standalone 仍可跑全部能力，**绝不删除既有功能**。
4. **门禁不合并**：testgen 八环与 testhub CI 各自独立 job。
5. **可回滚**：每阶段独立 commit；任一步可 `git revert` 不影响已有功能。
6. **凭证零明文**：`services/testgen/.env` 必须 gitignore；被测系统账号密码仅在调用会话内存转发，回流资产只存脱敏 `runtime:<url>`。

---

## 7. 安全与敏感信息（强制）

- `.env` / `.env.example` 分离：真实值进 `.env`（gitignore），模板进 `.env.example`（可入库，值留空或占位）。
- 代码与提交中**严禁**出现 `*_PASSWORD` / `*_OTP` / `API_KEY` / 真实 token；提交前用 `git grep -iE "password|secret|api_key|token" <改动文件>` 自检（testgen 另有 `scripts/check_secrets.py` 门禁）。
- 内网/生产地址（如 `47.97.154.50:8090`、被测系统 URL）可出现在代码注释/配置键，但**不得**与真实账号密码同时硬编码。

---

## 8. 检查清单

### 提交前（后端）
- [ ] `pytest` 全绿（Django test settings）
- [ ] （启用 ruff 后）`ruff check` + `ruff format --check` 通过
- [ ] 新 Model 已 `makemigrations` 且迁移文件已提交
- [ ] `git grep` 确认无密钥明文泄露
- [ ] 未改动现有 app 业务逻辑（仅新增）

### 提交前（前端）
- [ ] `npm run lint` 通过
- [ ] `npm run build` 成功
- [ ] 新增用户可见文案已加 i18n key
- [ ] 未引入新 axios 实例（统一用 `@/utils/api`）

### PR 前
- [ ] 提交信息符合 §4 格式
- [ ] PR 描述含：改动范围 / 测试方式 / 是否触碰现有业务（应「否」） / 回滚方案
- [ ] testgen sidecar 改动（若有）八环门禁仍绿

### 迁移启动前（整合 testgen）
- [ ] testhub Python ≥ 3.11，复用其 venv
- [ ] 记录 testhub 现有 starlette 版本（fastapi 须兼容，不升级 starlette）
- [ ] 两仓库 git 状态干净（无脏改动带入 subtree）
- [ ] testgen↔testhub 共享服务 token 已确定注入方式
- [ ] testhub 运行实例 base URL / 网络可达性已确认
- [ ] 凭证托管方案已定（避免双份明文）
- [ ] 已阅读 `整合实施_总览与代码分析对比.md` 及 M0–M5 阶段文档（位于 testCodeFast 仓库）
```
