# testgen 凭证托管规范（M3-4）

> 红线：任何 `*_PASSWORD` / `*_OTP` / `API_KEY` **绝不**进入 git 或 testhub 数据库明文。
> `.env` 必须 gitignore；如必须入库，改用 secret manager + 环境注入。

## 1. 凭证清单与托管方式

| 凭证 | 存放位置 | 注入方式 | 备注 |
|---|---|---|---|
| testgen `AUTH_TOKEN` ↔ testhub `TESTGEN_AUTH_TOKEN` | testgen `.env`（gitignore） | 由配置中心 / 环境变量**统一注入**，禁止两处各写死 | 两值必须一致；webhook / API 调用带 `X-Auth-Token` 头 |
| LLM / EXPERT API Key | testgen `.env` 仅此一处 | 环境注入 | 若 testhub 也用 LLM，各自独立，不交叉 |
| 被测系统登录账号密码（如福享 Agent） | **不持久化** | 用户经 testhub 智能输入框提交，后端仅在生成/执行会话**内存中转发**给 testgen | testgen 掩码；回流资产只存脱敏 `runtime:<url>` |
| OTP | testgen `RUNTIME_LOGIN_OTP` + `_fetch_otp` | 与 testhub 凭证管理对齐 | 不双份明文；若 testhub 有凭证保险箱，testgen 改从同一来源取 |
| 发布流水线 webhook token | CI secret / 环境变量 | `TESTGEN_TOKEN` 注入到发布流水线调用方 | 与 `AUTH_TOKEN` 同源 |

## 2. 统一注入示例（避免双份明文）

```bash
# 发布/部署侧（一次性注入，不写进任何文件）
export TESTGEN_TOKEN="<从 secret manager 取>"
export TESTGEN_BASE_URL="http://127.0.0.1:8100"   # 同机 localhost；跨容器用 http://testgen:8100

# testgen sidecar 启动（systemd EnvironmentFile 或 compose env_file 指向 .env）
# .env 内容（gitignore，绝不入库）：
#   AUTH_TOKEN=<与 TESTGEN_TOKEN 同源>
#   LLM_API_KEY=<...>
#   EXPERT_API_KEY=<...>
```

> testhub 侧 `TESTGEN_AUTH_TOKEN` 同样应从 secret manager 取，**不要**在 `settings.py` 里写死明文——当前 `backend/settings.py` 已用 `config("TESTGEN_AUTH_TOKEN", default="")`，从环境变量/`.env` 读取，符合本规范。

## 3. webhook 发布后自动验证（部署后调用）

```bash
# 发布流水线「部署完成」最后一步（execute=false：仅生成用例，不真实执行）
curl -X POST "${TESTGEN_BASE_URL}/api/v1/verify/webhook" \
  -H "X-Auth-Token: ${TESTGEN_TOKEN}" -H "Content-Type: application/json" \
  -d '{"event":"deployment.success","target":"<被测目标>","commit":"<刚发布commit>",
       "repo_url":"<被测仓库>","scopes":["正常","安全","边界","异常"],"execute":false}'
# 返回 202 Accepted；幂等键 wh:<target|commit|mode|scopes|gen>，重复触发不重复生成。
```

## 4. 零明文核验（CI / 提交前）

```bash
# 全仓扫描（scripts/check_secrets.py，发现真实凭据退出码 1 拦截）
python services/testgen/scripts/check_secrets.py
# 或 git grep 粗查（应无任何 *_PASSWORD/*_OTP/API_KEY 真实赋值，仅测试假值）
git grep -nE '(_PASSWORD|_OTP|API_KEY)\s*[:=]\s*["'\''][^"'\'']{4,}'
```

## 5. 网络隔离

- systemd：监听 `127.0.0.1`，仅本机 Django 经 localhost 调用。
- compose：用 `expose`（非 `ports`），8100 仅 `testhub-net` 内部可达，绝不映射宿主机/公网。
- 跨容器时确认 testhub Django 与 testgen 在同一内部网络且 `TESTGEN_BASE_URL` 正确。
