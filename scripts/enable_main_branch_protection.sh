#!/usr/bin/env bash
# 开启 testHub 仓库 main 分支保护（require PR + 评审 + 管理员同样受约束）。
#
# 前置：
#   1) 安装 gh（Windows Git Bash 下）：
#        winget install --id GitHub.cli
#      或浏览器下载 https://cli.github.com/ 安装包。
#   2) 登录（浏览器点一下授权，这一步必须由你来，助手无法代点）：
#        gh auth login
#   3) 确认对 lidebao513/testHub 有 admin 权限（组织/个人仓库 owner 即可）。
#
# 运行：
#       bash scripts/enable_main_branch_protection.sh
#
# 说明（重要）：
#   - 本仓库的 CI 工作流 testgen-gate 是「路径触发」的（仅 services/testgen/** 变更才跑），
#     因此【不】把它设为 required status check——否则不改动 testgen 的 PR 会因该检查永不出现而卡死无法合并。
#   - 若你希望 CI 也卡所有 PR，请把 .github/workflows/testgen-gate.yml 的 paths 过滤去掉，
#     再在本脚本 JSON 里把 "required_status_checks" 打开（contexts 填 ["gate"]）。
#   - 当前策略：禁止直推 main、必须 PR、至少需要 1 个审批、管理员同样受限、禁止 force push / 删除。

set -euo pipefail

REPO="lidebao513/testHub"
BRANCH="main"

if ! command -v gh >/dev/null 2>&1; then
  echo "❌ 未检测到 gh CLI。请先安装并登录："
  echo "   winget install --id GitHub.cli"
  echo "   gh auth login"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "❌ 尚未登录 GitHub。请先执行：gh auth login（浏览器点一下授权）"
  exit 1
fi

# 预检管理员权限：无 admin 时 PUT 保护规则会返回 403，提前给出明确提示。
PERM=$(gh api "repos/${REPO}" --jq '.permissions.admin' 2>/dev/null || echo "unknown")
if [ "$PERM" != "true" ]; then
  echo "❌ 当前账号对 ${REPO} 无 admin 权限，无法设置分支保护（需 owner/admin）。"
  echo "   可用 gh api repos/${REPO} --jq '.permissions' 自查；组织仓库请让 owner 开启。"
  exit 1
fi

# 构造保护规则 body（GitHub REST API v3 分支保护）
BODY='{
  "required_status_checks": null,
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}'

echo "🔒 正在为 ${REPO}:${BRANCH} 开启分支保护..."
gh api "repos/${REPO}/branches/${BRANCH}/protection" \
  --method PUT \
  --header "Accept: application/vnd.github+json" \
  --input - <<< "$BODY"

echo "✅ 完成。验证："
gh api "repos/${REPO}/branches/${BRANCH}/protection" --jq '{required_pull_request_reviews, enforce_admins, allow_force_pushes, allow_deletions}'
