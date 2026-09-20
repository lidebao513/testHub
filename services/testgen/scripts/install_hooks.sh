#!/bin/sh
# ============================================================================
# 安装仓库根 git 钩子（纯 shell 实现，不依赖 pre-commit 框架）
#
# 背景
#   1) 本机沙箱中 pre-commit 框架（原生 exe）无法解析 git，导致提交被误拦；
#      故改用「等价语义」的纯 shell 钩子，直接调用各项目的 scripts/gate_all.py。
#   2) 同一仓库下现有两个项目（work/test-accel、work/testgen-service），
#      本脚本按「谁有暂存改动就跑谁的门禁」分派，避免无关项目拖慢提交。
#
# 注意
#   所有传给原生 python 的路径都用 Windows 形式（pwd -W），否则原生 python
#   会把 MSYS 的 /c/... 误读成 C:\c\...。
#
# 用法：sh scripts/install_hooks.sh
# 卸载：rm .git/hooks/pre-commit .git/hooks/commit-msg
# ============================================================================
set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJ_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
REPO_ROOT=$(cd "$PROJ_DIR/../.." && pwd)
HOOKS_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
  echo "[install_hooks] 未找到 $HOOKS_DIR，请确认仓库根为 $REPO_ROOT" >&2
  exit 1
fi

cat > "$HOOKS_DIR/pre-commit" <<'HOOK'
#!/bin/sh
# 质量门禁分派（由 work/testgen-service/scripts/install_hooks.sh 生成）
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd -W)
cd "$REPO_ROOT" || exit 1

PROJECTS="work/test-accel work/testgen-service"
STAGED=$(git diff --cached --name-only 2>/dev/null)

RAN=0
FAILED=""
for PROJ in $PROJECTS; do
  if printf '%s\n' "$STAGED" | grep -q "^$PROJ/"; then
    if [ -f "$PROJ/scripts/gate_all.py" ]; then
      # 优先用项目自带 venv（门禁的 pytest 需要项目依赖，裸 python 往往缺依赖）
      # 路径必须写成绝对路径：下面会在子 shell 里 cd 进项目，相对路径会失效
      RUN_PY=""
      for CAND in "$REPO_ROOT/$PROJ/venv/Scripts/python.exe" "$REPO_ROOT/$PROJ/venv/bin/python"; do
        if [ -x "$CAND" ]; then RUN_PY="$CAND"; break; fi
      done
      if [ -z "$RUN_PY" ]; then
        for PY in python python3 py; do
          if command -v "$PY" >/dev/null 2>&1; then RUN_PY="$PY"; break; fi
        done
      fi
      if [ -z "$RUN_PY" ]; then
        echo "[quality-gate] 未找到可用 python，跳过 $PROJ（请先创建 venv 或把 python 加入 PATH）" >&2
        continue
      fi
      echo "[quality-gate] 检测到 $PROJ 有改动，执行其质量门禁…（$RUN_PY）"
      ( cd "$PROJ" && "$RUN_PY" scripts/gate_all.py ) || FAILED="$FAILED $PROJ"
      RAN=1
    fi
  fi
done

if [ "$RAN" -eq 0 ]; then
  echo "[quality-gate] 本次改动不涉及受管项目源码，跳过门禁。"
  exit 0
fi

if [ -n "$FAILED" ]; then
  echo "[quality-gate] 门禁未通过的项目：$FAILED" >&2
  exit 1
fi
echo "[quality-gate] 全部门禁通过"
HOOK

cat > "$HOOKS_DIR/commit-msg" <<'HOOK'
#!/bin/sh
# 提交信息规范校验（由 work/testgen-service/scripts/install_hooks.sh 生成）
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd -W)
MSG_FILE="$1"
[ -n "$MSG_FILE" ] || exit 0
case "$MSG_FILE" in
  /*|[A-Za-z]:*) ;;
  *) MSG_FILE="$REPO_ROOT/$MSG_FILE" ;;
esac
for CAND in "$REPO_ROOT/work/testgen-service/scripts/check_commit_msg.py" \
            "$REPO_ROOT/work/test-accel/scripts/check_commit_msg.py"; do
  if [ -f "$CAND" ]; then
    for PY in python python3 py; do
      if command -v "$PY" >/dev/null 2>&1; then exec "$PY" "$CAND" "$MSG_FILE"; fi
    done
    exit 0
  fi
done
exit 0
HOOK

chmod +x "$HOOKS_DIR/pre-commit" "$HOOKS_DIR/commit-msg"
echo "[install_hooks] 已安装："
echo "  $HOOKS_DIR/pre-commit"
echo "  $HOOKS_DIR/commit-msg"
echo "[install_hooks] 受管项目：work/test-accel work/testgen-service"
