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
| `.github/workflows/ci.yml` | `django-tests` | 建议（advisory） | sqlite test settings，跑已知绿范围 `tests apps/ui_automation/tests apps/llm_judge/tests`（263 用例）。 |
| `.github/workflows/ci.yml` | `frontend-lint` | 建议（advisory） | ESLint（`.eslintrc.cjs` 已补）。 |

**为什么 `django-tests` / `frontend-lint` 暂不设为必需**（避免误锁合并）：
- `django-tests`：`requirements.txt` 传递依赖较重（`easyocr`→`torch`、`selenium`、`airtest` 等），首跑耗时/稳定性待观察。
- `frontend-lint`：补 `.eslintrc.cjs` 后仍有存量 **11 处**错误（7 `no-unused-vars` / 1 解析错误 / 2 `ref`-as-operand / 1 重复属性），属技术债。
- 两者均以 `continue-on-error: true` 运行：报告真实结果，但不阻断合并。

---

## 3. main 分支保护策略（已应用）

| 配置项 | 值 | 含义 |
|---|---|---|
| 必需状态检查 | `gate`, `pr-lint` | 两者通过才可合并 |
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
  "required_status_checks": { "strict": false, "contexts": ["gate", "pr-lint"] },
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

当 `django-tests` / `frontend-lint` 在 CI 连续出绿后：

1. 删除对应 job 的 `continue-on-error: true`；
2. 将 job 名加入第 4.1 节 `contexts` 数组（如 `["gate", "pr-lint", "django-tests"]`）；
3. 重新执行 4.1 的 PUT 命令。

前端存量 11 处错误清单（修复后可立即提升）见 CI 首跑日志或本地 `npx eslint`。

---

## 6. 变更记录

- 2026-11-03：落地 P0 治理。新增 `ci.yml`（`pr-lint` 必需；`django-tests`/`frontend-lint` 建议）、补齐 `frontend/.eslintrc.cjs`、`testgen-gate.yml` 的 PR 触发去掉路径过滤（保证 `gate` 必现），并对 `main` 应用分支保护（必需 `gate`+`pr-lint`，`enforce_admins=true`）。
