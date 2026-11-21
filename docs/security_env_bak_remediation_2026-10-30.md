# `.env.bak` 凭据泄露处置（2026-10-30）

> 状态：**仓库侧已处置完毕（`ae91bce`）；凭据轮换经 2026-11-20 复核后大部分无需执行，历史重写待决策（见 §7）**。
> 本文档只记录键名不记录值；任何情况下不要把真实凭据回填到文档、issue 或提交信息。
> ⚠️ **第 1 节"暴露 5 项真凭据"与第 3 节轮换清单基于早期误判，已被 §7 的事实复核推翻，请以 §7 为准。**

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

> 本文档与清理脚本本身已入库：`7aecc07`（docs）；`.gitattributes` 换行策略（`*.sh` 固定 LF）随 `74f3c50` 落地，避免 clone 后脚本带 CRLF 无法执行。

1. `.gitignore`：新增 `.env.bak` / `.env.*.bak` / `.env.local` / `.env.*.local`，并把失效的 `.bak` 改为 `*.bak`；
2. `git rm --cached .env.bak`：从索引移除（**本地文件保留**，服务不受影响）；
3. 验证：`git check-ignore -v .env.bak` → 命中 `.gitignore:32:*.bak`；`git status` 里不再出现该文件。
4. 顺带清理（`74f3c50`）：`media/judge_kb/financial_kb.json.bak` 停止跟踪（运行时备份、全仓无代码引用、原文件 `financial_kb.json` 仍在库中，本地文件保留）；`.gitignore` 新增 `*.rdb`（Memurai/Redis 落盘快照）。

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
| 提交总数 | 257（随提交增长；**执行前以脚本输出为准**） |
| 涉及 `.env.bak` 的提交 | 2（`19a0f7d` 添加、`ae91bce` 删除） |
| 会被重写 hash 的提交 | 首次引入 `19a0f7d` 之后的 **49 个**（含当前 HEAD；同上，以脚本输出为准） |
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

## 7. 2026-11-20 事实复核（推翻第 1/3 节的部分结论）

> 逐版本读取 git 历史 + 实测连接，全程只判别"占位符/真值/长度"，未记录任何真值。
> 结论：**真正泄露过的只有 3 类旧值，且当前在用的每一项都不等于历史泄露值**——
> 即"当前凭据"从未泄露，历史上泄露的均为早已弃用的旧值。

### 7.1 逐项核查结果

| 凭据 | `.env.bak`（19a0f7d，唯一真值版本） | 历史 `.env`（4c5ecd1/15855cb/13ab8e0，均在公开历史） | 当前 `.env` | 结论 |
|---|---|---|---|---|
| `SMS_ACCESS_KEY_ID` / `SECRET` | **占位符** `your-aliyun-access-key-id` | 无该键 | **空**（未启用） | 从未泄露真值 → **无需吊销/建新/回填** |
| `DB_PASSWORD` | 12 位弱口令（真值） | 3 种真值 | 31 位，**不等于任何历史值** | 历史旧密码实测**已连不上 MySQL** → **无需改密** |
| `SECRET_KEY` | **占位符** | 1 种真值（15855cb） | 86 位，**不等于历史值** | 当前密钥从未泄露 → **无需更换/无需通知重登** |
| `EMAIL_HOST_PASSWORD` | **占位符** | 2 种真值：163 账号名为占位符；唯一真实账号 `qudong.test@outlook.com`（office365） | **空**（未启用） | 见 7.2，**唯一需用户确认项** |

### 7.2 唯一残留风险：历史 Outlook 邮箱授权码

- 提交 `15855cb`（已在公开历史）里含真实账号 `qudong.test@outlook.com` 的 office365 SMTP 授权码。
- 这是全部核查中**唯一"确属真实账号 + 真授权码"**的泄露项；其余邮箱/短信项均为占位符或已弃用旧值。
- **用户确认（2026-11-20）**：该 Outlook 账号**已弃用/不存在**，授权码自然失效 → **P0-A 凭据轮换全部关闭，无需任何平台操作**。

### 7.3 "无需轮换"的依据（为什么这回和原文档说的不一样）

原文档按"键名存在即判泄露"写清单，未区分占位符与真值、也未比对当前值。本次复核用三条实测证据收窄：

1. `git show 19a0f7d:.env.bak` 中 4/5 项是 `your-...` 占位符，从未进过真凭据；
2. 当前 `.env` 的 `DB_PASSWORD`、`SECRET_KEY` 均 **不等于** 历史任何真值（集合比对）；
3. 历史旧 DB 密码用 pymysql 实测连接 `127.0.0.1:3306` **失败**，当前密码**成功**。

### 7.4 历史重写（动作 B）新发现：原脚本覆盖不全

- 脚本 `scripts/purge_env_bak_from_history.sh` **只删 `.env.bak` 一个路径**。
- 但真实 SECRET_KEY / DB 密码 / 邮箱授权码主要藏在 **`.env`（无 bak）本身**，见于 4c5ecd1/15855cb/13ab8e0，**脚本不会清它们**。
- 由于这些值均已弃用/失效，"清历史"的边际收益已很低；若仍要清，需**同时处理 `.env` 与 `.env.bak` 两个路径**并重写脚本，且 force push 会影响 105 个提交的 hash（以预演输出为准）。
- **用户决策（2026-11-20）**：**暂缓重写历史**——当前凭据已全部核实安全，避免 force push 重写 105 个提交的协作中断与误伤。仅做本地收尾：物理删除磁盘上的 `.env.bak`（**已执行**，该文件本就不在 git 索引中，删除不影响工作区）。
- 若将来仍需清历史：需先把脚本从"只删 `.env.bak`"扩展为"同时删 `.env` 与 `.env.bak` 两个路径"，再 force push + 通知协作者重新 clone。
