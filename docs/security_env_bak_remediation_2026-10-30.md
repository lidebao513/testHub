# `.env.bak` 凭据泄露处置（2026-10-30）

> 状态：**仓库侧已处置完毕（`ae91bce`），凭据轮换与历史重写待执行**。
> 本文档只记录键名不记录值；任何情况下不要把真实凭据回填到文档、issue 或提交信息。

## 1. 事件事实

| 项 | 内容 |
|---|---|
| 泄露文件 | `.env.bak`（`.env` 的备份副本，101 行） |
| 引入提交 | `19a0f7d`（2026-08-14，「增加案例执行明细」） |
| 是否已推送 | **是**，`origin/main` 包含该提交；远端为 GitHub 公开仓库 `lidebao513/testHub` |
| 当前状态 | 索引已移除、本地文件保留、`.gitignore` 已拦截；**历史提交中仍可取回** |
| 暴露的凭据（5 项） | `SECRET_KEY`、`DB_PASSWORD`、`SMS_ACCESS_KEY_ID`、`SMS_ACCESS_KEY_SECRET`、`EMAIL_HOST_PASSWORD` |

根因：`.gitignore` 当时只写了 `.env`，另外写了一条 `.bak` —— 而 gitignore 里不带通配的 `.bak` 只匹配「名字恰好是 `.bak`」的条目，**不匹配 `.env.bak`**，所以备份文件一路入库。

## 2. 已完成（仓库侧，`ae91bce`）

1. `.gitignore`：新增 `.env.bak` / `.env.*.bak` / `.env.local` / `.env.*.local`，并把失效的 `.bak` 改为 `*.bak`；
2. `git rm --cached .env.bak`：从索引移除（**本地文件保留**，服务不受影响）；
3. 验证：`git check-ignore -v .env.bak` → 命中 `.gitignore:32:*.bak`；`git status` 里不再出现该文件。

顺带审计结论（**无需处理**，仅登记避免重复排查）：

- `.env.example` / `services/testgen/.env.example`：全部是 `your-xxx` 占位符，`LLM_API_KEY=` / `RUNTIME_LOGIN_PASSWORD=` 为空值，**不含真值**；
- `services/testgen/CREDENTIALS.md`：是凭据**规范**文档（写的是「绝不入库」「从 secret manager 取」），只有占位符与变量引用，**不含真值**。

## 3. 待执行 A：凭据轮换（**必须做，优先级最高**）

> 删历史只是止损的一半。公开仓库可能已被 fork、被爬虫/代码搜索索引，**只有吊销换新才真正止血**。
> 顺序建议：先换 AK/SK（可被直接盗刷费用）→ 再换邮箱授权码 → 再换 DB 密码 → 最后换 `SECRET_KEY`（会让所有登录态失效）。

| # | 凭据 | 在哪改 | 轮换动作 | 改完要同步的地方 | 验证 |
|---|---|---|---|---|---|
| 1 | `SMS_ACCESS_KEY_ID` / `SMS_ACCESS_KEY_SECRET` | 阿里云 RAM 控制台 | **禁用/删除旧 AccessKey** → 新建 AK → 只授予短信发送最小策略 | 本地 `.env`、部署环境/CI secret | 调一次短信验证码接口，看是否 200 且收到短信；旧 AK 调 API 应返回 `InvalidAccessKeyId` |
| 2 | `EMAIL_HOST_PASSWORD` | 邮箱后台（SMTP 授权码） | 重置授权码（旧码立即失效） | 本地 `.env`、监控告警的邮件发送配置 | 用「测试邮件」或 `sendemail` 接口发一封，检查 SMTP 登录不再 `535` |
| 3 | `DB_PASSWORD` | MySQL `ALTER USER '…'@'…' IDENTIFIED BY '…'` | 改密 | 本地 `.env`、备份脚本、同机其它连同一账号的项目 | `manage.py check --database default` 与任一业务接口读写正常 |
| 4 | `SECRET_KEY`（Django） | 本地生成 | `python -c "import secrets;print(secrets.token_urlsafe(64))"` | 本地 `.env`、部署环境 | ⚠️ **所有已登录会话/JWT/密码重置链接全部失效**，需通知使用者重新登录；重启 Django 后登录一次确认 |

轮换完成后请勾选：

- [ ] AK/SK 已吊销并换新，旧 AK 已确认失效
- [ ] 邮箱 SMTP 授权码已重置
- [ ] DB 密码已改，服务与备份链路均正常
- [ ] `SECRET_KEY` 已换新，并已通知相关使用者重新登录
- [ ] 新值只存在于 `.env`（已 gitignore）与部署侧 secret，未写入任何文档/提交

## 4. 待执行 B：重写历史，移除 `19a0f7d` 中的文件

脚本：`scripts/purge_env_bak_from_history.sh`（默认预演，`--confirm` 才执行，**不会自动 push**）。

影响面（执行前必须知道）：

| 项 | 数值/说明 |
|---|---|
| 提交总数 | 255（含本次处置提交 `ae91bce`） |
| 涉及 `.env.bak` 的提交 | 2（`19a0f7d` 添加、`ae91bce` 删除） |
| 会被重写 hash 的提交 | 首次引入 `19a0f7d` 之后的 **47 个**（含当前 HEAD） |
| 远端 | `origin/main` 需 `--force-with-lease` 推送 |
| 协作者 | 必须删除本地仓重新 clone，**禁止 pull/merge** 旧历史 |
| 备份 | 脚本自动 `git clone --mirror` 到仓库父目录，备份包内含真实凭据，处置完应删除或离线保存 |

执行步骤：

```bash
bash scripts/purge_env_bak_from_history.sh            # 预演，先看计划
bash scripts/purge_env_bak_from_history.sh --confirm  # 执行（自动备份）
git push --force-with-lease origin main               # 手动，确认新历史可用后再推
```

若机器上没有 `git-filter-repo`：`pip install git-filter-repo`（脚本会退回 `filter-branch`，较慢但可用）。

## 5. 为什么两件事都要做

| 只做轮换 | 只删历史 | 两件都做 |
|---|---|---|
| 历史里仍是明文，任何人 clone 都能看到旧值（但已失效，危害有限） | **凭据仍有效，删历史等于掩耳盗铃**（fork/缓存/搜索索引仍可取） | 旧值失效 + 仓库干净，才是真正闭环 |

## 6. 回归验证（本次仓库侧改动是否影响运行）

- 纯 `.gitignore` + 索引变更，无代码改动，服务无需重启；
- 提交 `ae91bce` 只含 2 个文件（`.env.bak` 删除、`.gitignore` 修改），未夹带工作区其它 40+ 处未提交改动；
- 本地 `.env.bak` 文件仍在磁盘（`ls .env.bak` 可查），如需彻底清除请自行删除。
