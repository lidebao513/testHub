#!/usr/bin/env python3
# ============================================================================
# 提交信息格式校验（Conventional Commits）—— pre-commit commit-msg 钩子
# ----------------------------------------------------------------------------
# 用法（pre-commit 传入提交信息文件路径）：
#   python scripts/check_commit_msg.py <commit-msg-file>
# 或手动：
#   python scripts/check_commit_msg.py  # 读 .git/COMMIT_EDITMSG
#
# 规则：首行须匹配  type[(scope)]: subject
#   type ∈ feat|fix|refactor|docs|test|chore|perf|build|ci|style
#   subject 非空、≤72 字符、不以句号结尾
# 豁免：合并提交(Merge)、revert、以 (merge|revert|Merge) 开头
# 退出码：不符合 → 1；符合 → 0
# ============================================================================
import os
import re
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TYPES = {
    "feat",
    "fix",
    "refactor",
    "docs",
    "test",
    "chore",
    "perf",
    "build",
    "ci",
    "style",
}
HEADER_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9_/-]+)\))?!?:\s*(?P<subject>.+)$")


def _read_msg(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except Exception:
        return ""
    # 仅校验「首行」：跳过纯空行与 git 注释行（以 # 开头），取首个有效行
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        return s
    return ""


def validate(msg: str):
    errors = []
    if not msg:
        return ["提交信息为空"]
    first = msg.splitlines()[0].strip() if msg.splitlines() else ""
    if not first:
        return ["提交信息首行为空"]

    # 豁免：合并 / revert
    if re.match(r"^(merge|revert|Merge|Revert)", first):
        return []

    m = HEADER_RE.match(first)
    if not m:
        errors.append(
            "首行不符合 Conventional Commits 格式：应为 `type(scope): subject`\n"
            "      例：feat(analyzer): 支持 hunk 级测试点打标"
        )
        return errors

    typ = m.group("type")
    subject = m.group("subject").strip()
    if typ not in TYPES:
        errors.append(f"type `{typ}` 非法，须为 {sorted(TYPES)} 之一")
    if not subject:
        errors.append("subject 不能为空")
    if len(first) > 72:
        errors.append(f"首行过长（{len(first)}>72），请精简")
    if subject.endswith("。"):
        errors.append("subject 不应以句号结尾")
    return errors


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, ".git", "COMMIT_EDITMSG")
    msg = _read_msg(path)
    errors = validate(msg)
    if errors:
        print("[check_commit_msg] 提交信息不合规：")
        for e in errors:
            print("  -", e)
        print("\n格式要求：type(scope): subject")
        print("  type ∈", sorted(TYPES))
        print("  示例：fix(gate): 修复 bandit 不读 pyproject skips 的问题")
        return 1
    print("[check_commit_msg] 提交信息格式合规 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
