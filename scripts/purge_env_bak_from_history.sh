#!/usr/bin/env bash
# 从 git 历史中彻底移除 .env.bak（含它出现过的所有历史版本）。
#
# 背景：.env.bak 于 19a0f7d（2026-08-14）被提交，并已随 origin/main 推送到
# GitHub 公开仓库，内含明文 SECRET_KEY / DB_PASSWORD / SMS_ACCESS_KEY_ID /
# SMS_ACCESS_KEY_SECRET / EMAIL_HOST_PASSWORD。仅从索引删除（git rm --cached）
# 只能阻止「将来」再提交，历史里仍可取回，因此必须重写历史。
#
# 重要：重写历史 **不能替代凭据轮换**。公开仓库可能已被 fork / 被爬虫缓存，
# 唯一可靠的止损是把这 5 个凭据全部吊销并换新（见
# docs/security_env_bak_remediation_2026-10-30.md 的轮换清单）。
#
# 用法：
#   bash scripts/purge_env_bak_from_history.sh              # 预演：只打印计划，不改仓库
#   bash scripts/purge_env_bak_from_history.sh --confirm    # 执行（先自动做 mirror 备份）
#
# 本脚本不会 push。执行完请按输出提示手动 force push，并通知协作者重新 clone。
set -euo pipefail

TARGET=".env.bak"
CONFIRM=0
[[ "${1:-}" == "--confirm" ]] && CONFIRM=1

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

echo "== 仓库：$ROOT"
echo "== 目标：$TARGET"

count="$(git rev-list --all --count)"
hits="$(git log --all --oneline -- "$TARGET" | wc -l | tr -d ' ')"
first_commit="$(git log --all --reverse --format=%H -- "$TARGET" | head -1)"
after=0
[[ -n "$first_commit" ]] && after="$(git rev-list --count "$first_commit..HEAD")"
echo "== 提交总数：$count ；涉及 $TARGET 的提交：$hits"
echo "== 首次引入：${first_commit:-无} ；其之后会被重写 hash 的提交：$after"

if [[ "$hits" == "0" ]]; then
  echo "== 历史中已无 $TARGET，无需重写。"
  exit 0
fi

if git ls-files --error-unmatch "$TARGET" >/dev/null 2>&1; then
  echo "!! $TARGET 仍被跟踪，请先执行：git rm --cached $TARGET && git commit"
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  if [[ "$CONFIRM" -eq 1 ]]; then
    echo "!! 工作区有未提交改动，重写历史前请先提交或 stash，避免丢失。"
    exit 1
  fi
  echo "!! 注意：工作区当前有未提交改动，正式执行前需先提交或 stash（预演不受影响）。"
fi

BACKUP="${ROOT%/}/../testhub_mirror_$(date +%Y%m%d_%H%M%S).git"

if [[ "$CONFIRM" -eq 0 ]]; then
  cat <<EOF

== 预演（未做任何修改）。确认后加 --confirm 执行，届时会：
   1) git clone --mirror "$ROOT" "$BACKUP"
   2) 优先用 git filter-repo 移除 $TARGET；未安装则退回 git filter-branch
   3) git reflog expire --expire=now --all && git gc --prune=now --aggressive
   4) 打印 force push 与协作者通知清单

   风险提示：首次引入提交（${first_commit:0:7}）之后有 $after 个提交的 hash 会改变，
   远端需要 --force-with-lease，其他人必须重新 clone（不要 pull/merge）。
EOF
  exit 0
fi

echo "== 1/4 备份到 $BACKUP"
git clone --mirror "$ROOT" "$BACKUP"

echo "== 2/4 重写历史"
if command -v git-filter-repo >/dev/null 2>&1; then
  git filter-repo --path "$TARGET" --invert-paths --force
elif python -c "import git_filter_repo" >/dev/null 2>&1; then
  python -m git_filter_repo --path "$TARGET" --invert-paths --force
else
  echo "   未找到 git filter-repo，退回 filter-branch（较慢）"
  FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch --force --index-filter \
    "git rm --cached --ignore-unmatch '$TARGET'" \
    --prune-empty --tag-name-filter cat -- --all
fi

echo "== 3/4 清理残留对象"
git for-each-ref --format='delete %(refname)' refs/original | git update-ref --stdin || true
git reflog expire --expire=now --all
git gc --prune=now --aggressive

echo "== 4/4 完成，剩余检查："
git log --all --oneline -- "$TARGET" | head -5 || true
echo "   若上面无输出，说明历史已清干净。"

cat <<EOF

== 接下来必须手动完成（脚本不代做）：
   1) 确认新历史可用：git log --oneline -5
   2) 推送：git push --force-with-lease origin main
   3) 通知所有协作者：删除本地仓重新 clone，禁止 pull/merge 旧历史
   4) 到 GitHub 仓库确认 commit 19a0f7d 已不可访问
   5) 备份包 $BACKUP 内含旧历史与真实凭据，处置完请删除或放保险箱
   6) **凭据轮换才是止损手段**：见 docs/security_env_bak_remediation_2026-10-30.md
EOF
