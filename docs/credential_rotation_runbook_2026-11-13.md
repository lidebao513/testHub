# 凭据轮换执行清单（#202，2026-11-13）

> 配套文档：`docs/security_env_bak_remediation_2026-10-30.md`（事件事实与根因）。
> **本文档只写键名与命令，绝不回填任何真实凭据值。** 新值只存在于 `.env`（已 gitignore）与部署侧 secret。
> 本清单为「执行版」：每条都可直接照抄，末尾有勾选框。本地可执行的（SECRET_KEY 切换、历史重写）在你授权后再做；短信/邮箱 AK 必须由你在各自控制台操作。

---

## 0. 现状盘点（2026-11-13 实测）

| # | 凭据 | 当前 `.env` 是否还在用 | 轮换状态 | 执行方 |
|---|---|---|---|---|
| 1 | `SMS_ACCESS_KEY_ID` / `SMS_ACCESS_KEY_SECRET` | ❌ 不在当前 `.env` | 旧值仍有效，**必须吊销** | 你（阿里云 RAM） |
| 2 | `EMAIL_HOST_PASSWORD` | ❌ 不在当前 `.env` | 旧授权码仍有效，**必须重置** | 你（邮箱后台） |
| 3 | `DB_PASSWORD` | ✅ 在用 | 已在 #234 改密（MySQL 在线，应为新密） | 已完成，可复核 |
| 4 | `SECRET_KEY` | ✅ 在用 | **未轮换**（可能仍是泄露值） | 你授权后我本地做 |
| 5 | 历史重写（移除 `.env.bak`） | — | 预演就绪：重写 **101** 个提交 | 你授权后我本地做 |

> ⚠️ SMS/EMAIL 虽不在当前 `.env`，但其旧值仍躺在公开仓历史（提交 `19a0f7d`）且远端/缓存/可能的 fork 仍可取。只要不吊销，就有被盗刷风险——这两项的吊销**不能省**。

**建议顺序**（与修复文档一致，先断盗刷再动本地）：  
① 短信 AK/SK → ② 邮箱授权码 → ③ DB 复核（已做）→ ④ SECRET_KEY 切换 → ⑤ 历史重写（最后做，因为它会重写含前面已改 `.env` 的历史）。

---

## 1. 短信 AK/SK 轮换（阿里云 RAM）— 你做，优先级最高

1. 打开 RAM 控制台：https://ram.console.aliyun.com → **访问控制 → 用户 → 对应用户 → 认证管理 / AccessKey**。
2. 找到旧 AccessKey（`SMS_ACCESS_KEY_ID` 对应的那一个）→ **禁用**，观察业务无异常后再**删除**。
3. **创建新 AccessKey**，仅授予短信发送最小权限（如 `AliyunDysmsFullAccess`，或更细的自定义策略只含 `dysms:SendSms`）。
4. 把新 `AccessKeyId` / `AccessKeySecret` 写入本地 `.env`（对应 `SMS_ACCESS_KEY_ID` / `SMS_ACCESS_KEY_SECRET`，如后续启用短信功能时）。
5. **验证**：
   - 旧 AK 调短信 API 应返回 `InvalidAccessKeyId`；
   - 用新 AK 发一条验证码短信，确认手机收到（HTTP 200）。

---

## 2. 邮箱 SMTP 授权码重置 — 你做

1. 登录邮箱网页端（发送告警/通知所用邮箱）→ **设置 → 账户 / 安全 → 开启 SMTP 服务 → 重置/生成授权码**（旧授权码立即失效）。
2. 把新授权码写入本地 `.env` 的 `EMAIL_HOST_PASSWORD`（如后续启用邮件时）。
3. **验证**：调 `sendemail` 接口或发一封「测试邮件」，SMTP 登录不再返回 `535 Authentication failed`。

---

## 3. DB_PASSWORD 复核 / 重旋 — 已做（#234），按需复核

> #234 已完成 MySQL 凭据轮换，MySQL80 服务当前 **Running**，应已为新密。

**复核（确认 #234 真生效，无需读取值）：**
```bash
cd /c/Users/EDY/WorkBuddy/testhub_platform
# 用当前 .env 的 DB_PASSWORD 连库，能连上即说明已是新密
python -c "import django,os; os.environ['DJANGO_SETTINGS_MODULE']='backend.settings'; django.setup(); from django.db import connection; connection.ensure_connection(); print('DB OK', connection.settings_dict['HOST'], connection.settings_dict['NAME'])"
```
- 若连接成功 → DB 项完成，跳过下面重旋步骤。
- 若失败 → 说明 `.env` 与 MySQL 当前密码不一致，按下面重旋。

**重旋（仅在复核失败时）：**
```sql
-- 在 MySQL 客户端（或 MySQL Workbench）以有改密权限的账号执行：
ALTER USER '<DB_USER>'@'localhost' IDENTIFIED BY '<新DB_PASSWORD>';
FLUSH PRIVILEGES;
```
然后把 `<新DB_PASSWORD>` 同步写入 `.env` 的 `DB_PASSWORD=`，并重启 Django(:8900)。验证：`manage.py check --database default` 通过 + 任一业务接口读写正常。

---

## 4. SECRET_KEY 切换 — 你授权后我本地做（或你自行）

> 副作用：所有已登录会话 / JWT / 密码重置链接立即失效，需通知使用者重新登录。dev 环境可接受。

1. 生成新值（现场生成，不要复用聊天里贴过的任何值）：
   ```bash
   python -c "import secrets;print(secrets.token_urlsafe(64))"
   ```
2. 把输出写入 `.env` 的 `SECRET_KEY=`（替换旧值），同时更新部署环境 / CI secret 中的同名项。
3. 重启 Django 服务（:8900），使新 SECRET_KEY 生效。
4. 通知相关使用者重新登录。
5. **验证**：重启后用一个账号登录一次成功即可。

---

## 5. 历史重写（移除 `.env.bak`）— 破坏性，你授权后我本地做

> 目的：把含明文旧凭据的 `.env.bak` 从 git 历史彻底剔除。**此操作不能替代上面的凭据轮换**——只有吊销换新才真正止血。
> 预演结果（2026-11-13）：仓库 329 个提交，`.env.bak` 涉及 2 个（`19a0f7d` 引入、`ae91bce` 删除），`19a0f7d` 之后 **101 个提交 hash 会变**。

1. **预演**（只读，先看计划，不改动）：
   ```bash
   bash scripts/purge_env_bak_from_history.sh
   ```
2. **执行**（自动做 mirror 备份 → filter-repo 移除 → gc）：
   ```bash
   bash scripts/purge_env_bak_from_history.sh --confirm
   ```
   - 脚本会先把整个仓库 `git clone --mirror` 到仓库父目录作为备份（**备份包内含旧历史与真实凭据，处置完必须删除或离线保管**）。
   - 优先用 `git filter-repo`；未安装则退回 `git filter-branch`。
3. **手动推送**（脚本不会 push）：
   ```bash
   git push --force-with-lease origin main
   ```
4. **其它含旧历史的远程分支**：检查 `git branch -r` 中是否还有分支含 `19a0f7d`（如已合并的 `feat/p1-closure`）；已合并的可直接在 GitHub 删除，未合并的需同样 `--force-with-lease` 同步或删除。
5. **通知协作者**：删除本地仓、重新 clone，**禁止 pull/merge 旧历史**（否则会把旧 `.env.bak` 拉回来）。
6. **核验**：GitHub 上确认提交 `19a0f7d` 已不可访问；`git log --all --oneline -- '.env.bak'` 无输出。

---

## 6. 完成勾选清单

- [ ] **① 短信 AK/SK**：旧 AK 已禁用/删除，新 AK 仅最小权限，已验证旧 AK 失效、新 AK 可发信
- [ ] **② 邮箱授权码**：已重置，已验证 SMTP 不再 `535`
- [ ] **③ DB_PASSWORD**：#234 已改密且复核连库成功（或已按重旋步骤处理并验证）
- [ ] **④ SECRET_KEY**：已换新并重启 Django，已通知使用者重新登录
- [ ] **⑤ 历史重写**：预演 → `--confirm` 执行 → `--force-with-lease` 推送 → 删除/同步其它远程分支 → 协作者重 clone → GitHub 确认 `19a0f7d` 不可访问
- [ ] **备份包处置**：`testhub_mirror_*.git` 已删除或离线保管（绝不留本机可随意访问处）
- [ ] **无二次泄露**：新值只存在于 `.env` 与部署侧 secret，未写入任何文档 / issue / 提交信息

---

## 7. 红线提醒

- 任何情况下不要把真实凭据回填到文档、issue、提交信息或聊天。
- `.env` / `.env.bak` 已被 `.gitignore` 拦截（`*.bak` / `.env.bak` / `.env.*.bak`），提交前仍请用 `git status` 复核未被跟踪文件误带凭据。
- 历史重写后，本地任何未推送的旧分支/标签都不要再 push，避免把旧 `.env.bak` 重新推回远端。
