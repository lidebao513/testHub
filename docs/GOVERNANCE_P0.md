# P0 工程治理：远程 CI + main 分支保护（runbook）

> 状态：已落地（P0 任务 #100 / #105）。本文件是治理配置的**唯一事实来源**，说明 CI 分级、分支保护策略、应用/回滚命令与后续提升路径。
> 相关规范：`CODE_STANDARDS.md` §4（提交/分支）、§5（testgen 八环独立 job）。

---

## 1. 目标

1. **防误推 main**：main 禁止直推，所有改动必须走 PR，且必需检查通过方可合并（含管理员，`enforce_admins=true`）。
2. **把八环门禁固化到远程**：testgen 八环（secret/lint/format/enum/structure/type/security/test）在**每次** PR 上运行，结果作为必需检查。
3. **提交/PR 约定可执行化**：PR 标题强制 `<type>: <描述>`（type 取 `CODE_STANDARDS.md` §4.1 固定集合）。

---

## 2. CI 工作流与检查分级

| Workflow | Job（status check 名） | 级别 | 说明 |
|---|---|---|---|
| `.github/workflows/testgen-gate.yml` | `gate` | **必需** | testgen 八环门禁（`scripts/gate_all.py`）。PR 触发**不按路径过滤**，保证每次 PR 必现。 |
| `.github/workflows/ci.yml` | `pr-lint` | **必需** | PR 标题约定校验（零依赖 shell）。 |
| `.github/workflows/ci.yml` | `django-tests` | **必需**（2026-11-03 提升） | sqlite test settings，跑已知绿范围 `tests apps/ui_automation/tests apps/llm_judge/tests`（420 用例，约 3 分钟）。 |
| `.github/workflows/ci.yml` | `frontend-lint` | **必需**（2026-11-03 提升） | ESLint，**0 error 基线**（约 20 秒）。格式检查不在本 job，见下。 |

**`django-tests` / `frontend-lint` 的提升依据**（原为 advisory，达标后转必需）：
- `django-tests`：四类假红已修完（缺 `pytest-django`、裸 `pytest` 的 `sys.path`、CI 无 `.env` 的 `SECRET_KEY`、
  安全用例缺 `REDIS_URL` 占位），连续多轮 PR 稳定全绿。
- `frontend-lint`：11 处存量错误逐个修完（含 1 处 `el-descriptions` 重复属性会算出 `NaN` 分栏的真隐患、
  1 处 `<script setup lang="ts">` 缺 parser 的解析错误），现为 0 error / 525 warning。
- **剩下 525 条 warning 不阻断**：全是 vue 模板风格（`attributes-order` 479、`first-attribute-linebreak` 37 等），
  `--fix` 会动 93 个 `.vue` 文件，属存量格式，留给专项批次。ESLint 的退出码只由 error 决定，故 `npm run lint` 为 0。

**格式检查为什么不挂 ESLint**：`prettier --check` 实测 **162/204 个文件从未按 prettier 排过版**。
把 Prettier 当 ESLint 规则会让约 1 万条格式 warn 淹没真实错误，且本地 `--fix` 会静默改写上百个文件。
现按职责拆分：ESLint 管正确性（`npm run lint`），Prettier 管格式（`npm run format` / `format:check`，
配置见 `frontend/.prettierrc.json`）。

---

## 3. main 分支保护策略（已应用）

| 配置项 | 值 | 含义 |
|---|---|---|
| 必需状态检查 | `gate`, `pr-lint`, `frontend-lint`, `django-tests` | 四项全通过才可合并 |
| `strict`（须领先 main） | `false` | 不强制每次 rebase，降低单人协作摩擦（可改 `true`） |
| `enforce_admins` | `true` | 管理员同样受保护，禁止绕过直推 main |
| 必需评审 | 无 | 单人仓库，不强制他人 review |
| 允许 force push | `false` | 禁止强推 |
| 允许删除分支 | `false` | 禁止删除 main |
| 必须线性历史 | `false` | 允许 merge commit |

---

## 4. 应用 / 回滚命令（gh CLI，需对仓库有 admin 权限）

> 前置：`ci.yml` 与 `testgen-gate.yml` **必须已在 main 上**，否则 `gate`/`pr-lint` 检查不会产生，PR 会因「expected — waiting for status」被永久锁定。

### 4.1 应用保护

```bash
gh api -X PUT repos/lidebao513/testHub/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": false, "contexts": ["gate", "pr-lint", "frontend-lint", "django-tests"] },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false
}
JSON
```

### 4.2 查看当前保护

```bash
gh api repos/lidebao513/testHub/branches/main/protection
gh api repos/lidebao513/testHub/branches/main/protection/required_status_checks
```

### 4.3 回滚（临时放宽，用于紧急修复 CI 自身问题）

```bash
# 仅去掉必需检查，保留「禁止直推 / 禁 force push / 禁删除」
gh api -X PATCH repos/lidebao513/testHub/branches/main/protection/required_status_checks \
  -H "Accept: application/vnd.github+json" --input - <<'JSON'
{ "strict": false, "contexts": [] }
JSON
```

> 完全移除保护（**慎用**）：`gh api -X DELETE repos/lidebao513/testHub/branches/main/protection`

---

## 5. 建议项 → 必需项的提升路径

**已完成（2026-11-03）**：`django-tests` 与 `frontend-lint` 均已从 advisory 提升为必需，当前四项必需。

对新引入的检查，提级步骤（与本次做法一致）：

1. 先观察若干轮 PR：job 必须**连续全绿**，且失败原因都是真问题而不是环境差异
   （典型反例：`test_p1_1a_security` 只因「本机有 `.env`、CI 没有」而红）；
2. 删除 job 的 `continue-on-error: true`；
3. **先合并该 PR**（此时 job 会以「失败但被忽略」出现），再把 job 名加入必需检查：
   ```bash
   gh api -X POST repos/lidebao513/testHub/branches/main/protection/required_status_checks/contexts \
     -f 'contexts[]=<job-name>'
   ```
   > 顺序很重要：**先让检查在 main 上真实跑过、再设为必需**。反过来会让所有 PR
   > 卡在「expected — waiting for status」，包括修复它自己的那个 PR（自我锁死）。
4. 更新第 2、3 节与本文件 4.1 的 `contexts`。

---

## 6. 变更记录

- 2026-11-03：落地 P0 治理。新增 `ci.yml`（`pr-lint` 必需；`django-tests`/`frontend-lint` 建议）、补齐 `frontend/.eslintrc.cjs`、`testgen-gate.yml` 的 PR 触发去掉路径过滤（保证 `gate` 必现），并对 `main` 应用分支保护（必需 `gate`+`pr-lint`，`enforce_admins=true`）。
- 2026-11-03（同日追加）：`django-tests` 与 `frontend-lint` 提升为**必需检查**（前者 420 用例稳定全绿，后者修到 0 error）。
  同时把 Prettier 从 ESLint 规则通道摘出（`.eslintrc.cjs` 的 `prettier/prettier: off` + 新增 `frontend/.prettierrc.json`
  + `format` / `format:check` 脚本），`lint` 脚本去掉 `--fix` 与 CI 对齐。当前必需检查四项。
