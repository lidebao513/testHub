#!/usr/bin/env bash
# 开启 testHub 仓库 main 分支「轻量保护」：仅禁止 force push / 删分支；允许直推、不要求 PR、不卡管理员。
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
# 说明：
#   - 轻量策略：允许直接 push main、不要求 PR、管理员不受限；只设 allow_force_pushes=false / allow_deletions=false。
#     你照旧直接 push，但 git push --force 与删分支会被 Git 拒绝——白捡安全网、零额外流程负担。
#   - 若以后要「完整保护」（禁直推 + 必 PR + 必审批 + 管理员受限），把 enforce_admins 改 true、
#     required_pull_request_reviews 填回评审对象（{"required_approving_review_count":1, ...}）即可。
#   - CI 工作流 testgen-gate 路径触发，本就不设为 required status check。

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

# 构造保护规则 body（GitHub REST API v3 分支保护）——轻量版：仅禁 force push / 删分支。
BODY='{
  "required_status_checks": null,
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}'

echo "🔒 正在为 ${REPO}:${BRANCH} 开启「轻量保护」（仅禁止 force push / 删分支）..."
gh api "repos/${REPO}/branches/${BRANCH}/protection" \
  --method PUT \
  --header "Accept: application/vnd.github+json" \
  --input - <<< "$BODY"

echo "✅ 完成。验证："
gh api "repos/${REPO}/branches/${BRANCH}/protection" --jq '{required_pull_request_reviews, enforce_admins, allow_force_pushes, allow_deletions}'
