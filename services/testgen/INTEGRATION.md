# testgen ↔ testhub 集成运维手册（M4-6）

> 配套代码：`apps/testgen_integration/`（testhub 侧客户端/映射/编排/端点）
> 配套 sidecar：`services/testgen/`（testgen FastAPI 独立服务）
> 整合铁律：testgen 业务零改（仅新增独立工程）；testhub 只增不改（既有 21 app 零改动）。

---

## 1. 架构图

```
┌──────────────────────────── testhub (Django + DRF, :8000) ────────────────────────────┐
│                                                                                        │
│   apps/testgen_integration                                                              │
│   ┌────────────┐  ┌────────────┐  ┌─────────────────────────────────────────────┐     │
│   │ client.py  │  │ mapping.py │  │ flow.py                                      │     │
│   │ HTTP 客户端│─▶│ 用例→TC映射│─▶│ sync_cases / run_security_verdict (编排)      │     │
│   │ (红线守卫) │  │ (枚举对齐) │  │ upsert/create_defect 注入式（写库在 views 层）│     │
│   └─────┬──────┘  └────────────┘  └─────────────────────────────────────────────┘     │
│         │  views.py: /api/testgen/generate · /api/testgen/sync                         │
│         │                                                                              │
└─────────┼──────────────────────────────────────────────────────────────────────────────┘
          │ localhost HTTP (trust_env=False 绕过透明代理)
          ▼
┌──────────────────────────── testgen sidecar (FastAPI, :8100) ─────────────────────────┐
│   /health · /ready · /api/v1/generate · /api/v1/tasks/{id} · /api/v1/pull             │
│   /api/v1/projects/{pid}/cases · /api/v1/verdict · 平台控制台单页                      │
│   仅生成 + 五维安全判定；执行统一由 testhub 的 ui_automation/api_testing 承接          │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

- **testhub → testgen**：经 `client.py` 的 `trust_env=False` Session 直连 `127.0.0.1:8100`（避免本机透明代理劫持 localhost）。
- **轮询不另开端点**：testhub 侧无 `/api/testgen/tasks`，轮询由 `client.poll()` 直连 `testgen /api/v1/tasks/{id}`。
- **执行不在 testgen**：`client.generate(execute=True)` 客户端与服务端（422）双重拒绝；执行交给 testhub。

---

## 2. 接口清单

### 2.1 testhub 侧（新增，挂在 `/api/testgen/`）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/testgen/generate` | 触发 testgen 异步生成，返回 `task_id` + `poll_url`；调用方按 `task_id` 轮询 |
| POST | `/api/testgen/sync` | 拉取 testgen 项目用例 → 映射 → 幂等回流为 `TestCase`（body: `project_id`,`testgen_project_id`,`suite_id?`） |

> 鉴权：`IsAuthenticated`（与 apps/* 其余端点一致）。
> 文档 M4-1 原称 `/api/testgen/import`，代码落地命名为 `/api/testgen/sync`（功能等价）。

### 2.2 testgen 侧（sidecar 既有 `/api/v1/*`，零改动）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针（无鉴权） |
| GET | `/ready` | 就绪探针（仅含 `*_configured` 布尔，无密码明文） |
| POST | `/api/v1/generate` | 异步生成；`execute` 传 `true` 返回 422 |
| GET | `/api/v1/tasks/{id}` | 任务进度/结果（终态含 result/error） |
| POST | `/api/v1/pull` | 取码（凭证仅在请求内，不落明文） |
| GET | `/api/v1/projects/{pid}/cases` | 已生成用例（DB 行） |
| POST | `/api/v1/verdict` | 五维安全判定（同步纯计算） |
| GET | `/` | 平台控制台单页 |

---

## 3. 环境变量

### testgen sidecar（`services/testgen/.env`）
| 变量 | 说明 | 默认 |
|---|---|---|
| `HOST` | 绑定地址 | `127.0.0.1` |
| `PORT` | 绑定端口 | `8100` |
| `AUTH_TOKEN` | `X-Auth-Token` 校验（空=不校验） | 空 |
| `DB_PATH` | 用例元数据库路径 | `data/testgen.db` |
| `LLM_*` / `EXPERT_*` / `RUNTIME_UI_*` | 生成所需 LLM/运行时配置 | 视部署 |

### testhub（`backend/settings.py`，用 `config()` 读取）
| 变量 | 说明 | 默认 |
|---|---|---|
| `TESTGEN_BASE_URL` | testgen sidecar 地址 | `http://127.0.0.1:8100` |
| `TESTGEN_AUTH_TOKEN` | 调 sidecar 的 `X-Auth-Token` | 空 |
| `TESTGEN_TIMEOUT` | 客户端超时（秒） | `120` |
| `TESTHUB_BASE_URL` / `TESTHUB_AUTH_TOKEN` | （可选）自环建缺陷用；推荐直接 `Defect.objects.create` | 空 |

---

## 4. 启动 / 探活

### 启动 testgen sidecar
```powershell
cd C:\Users\EDY\WorkBuddy\testhub_platform\services\testgen
.\.venv\Scripts\python.exe -m uvicorn service.app:app --host 127.0.0.1 --port 8100
```
### 探活
```bash
curl http://127.0.0.1:8100/health   # {"status":"ok",...}
curl http://127.0.0.1:8100/ready    # {"status":"ready",...}（无密码明文）
```
### 启动 testhub（需 Django + MySQL）
```bash
cd C:\Users\EDY\WorkBuddy\testhub_platform
python manage.py migrate            # 含 testgen_integration（仅新增 app）
python manage.py runserver 0.0.0.0:8000
```

---

## 5. 回滚步骤（可回滚是底线）

每阶段独立 commit，任一步可单独撤销而不波及其他既有功能：

```bash
git log --oneline | grep -E "testgen|testgen_integration"   # 找到对应 commit
git revert <commit>        # 撤销该阶段；testhub 21 app 零改动，不受影响
# 或整体回退到集成前：
git revert d2b0897 6968f72 1229f73
```

- testgen 代码独立工程，删除 `services/testgen/` 即完全隔离，不影响 testhub。
- 测试：M4 端到端测试 `apps/testgen_integration/tests/test_e2e_m4.py` 可随时重跑验证回归。

---

## 6. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| testhub 调 sidecar 超时/连接拒绝 | sidecar 未启动 或 本机透明代理劫持 localhost | 确认 sidecar 在跑；`client.py` 已 `trust_env=False` 绕过代理 |
| `/api/v1/generate` 返回 422 | 误传 `execute=true` | 移除 `execute` 字段（客户端默认 `execute=False`，双保险拒绝） |
| 回流用例翻倍 | 幂等键缺失 | 确认 `tags` 含 `tg:<tc_no>`；`sync_cases` 以该标签 upsert |
| 映射后类型/优先级越界 | testgen 枚举与 testhub 不匹配 | `mapping.py` 已对齐（`case_type`→`test_type`、`P0-P3`→`priority`）；勿改模型字段名 |
| `/verdict` 维度识别错 | 传了英文而端点期望中文 | `verdict.py` 已做中英双语归一化；优先传 testgen 原生中文 `case_type/dimension` |
| LLM 相关生成失败 | 未配 `LLM_*` | 仅影响真实生成；本地联调可用 `test_e2e_m4.py` 注入式用例绕过 |

---

## 7. 零回归声明

- testhub 既有 21 个 app（`testcases`/`api_testing`/`ui_automation`/`defects`/…）**代码零改动**。
- 集成仅在 `backend/urls.py` 追加 1 行 `path('api/testgen/', ...)`、`backend/settings.py` 追加 3 个 `TESTGEN_*` 配置、`LOCAL_APPS` 追加 1 项。
- OpenAPI 自描述（`/api/schema/`）除新增 `/api/testgen/*` 外无任何端点变化。
- 详见《整合实施_未完成任务追踪.md》的缺口与优先级。
