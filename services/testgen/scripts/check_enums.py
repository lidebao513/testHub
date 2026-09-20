"""枚举一致性门禁（字段感知版）：扫描项目 .py，标记仍硬编码「枚举值字面量」的代码上下文。

用法：
    python scripts/check_enums.py
    python scripts/check_enums.py --fix-report      # 仅输出报告（不退出非 0）

判定原则（字段感知）：
- 仅当字面量出现在「已知枚举字段」语境时才报警，避免误伤通用字典键（ok/updated…）或 f-string 文案：
  1) 字典赋值：  "<field>": "<val>"  或  '<field>': '<val>'
  2) 比较：      <field> == "<val>" / <field> != "<val>"（含 field["x"] 形式）
  3) 成员：      "<val>" in ...
  4) 返回：      return "<val>"
- 白名单（不报错）：注释行(#)、f-string 行(f" / f')、SQL 语句、范围关键词(全部/所有/完整/all/ALL)。
- enums.py 自身是唯一真值源，跳过；selftest_*/verify_* 为历史脚手架，跳过。
- 碰撞值（"UI"/"接口"）按上下文区分：method 字段上的 "UI"/"PAGE" 属 MethodMarker，由迁移脚本替换为
  MethodMarker.*.value；verify_layer 上的 "接口"/"UI" 属 VerifyLayer，后续新代码显式引用。门禁对
  这两类仅作提示性报告（不计入失败退出码），避免与 MethodMarker 混淆。

退出码：发现违规 → 1；零违规 → 0。供 CI / 提交前跑。
"""

import os
import re
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 让脚本可 `python scripts/check_enums.py` 直接运行（须在同层 `from core.enums import` 之前）
sys.path.insert(0, ROOT)
EXCLUDE_DIRS = {"venv", "node_modules", "__pycache__", ".git"}
SKIP_FILES = {"enums.py", "check_enums.py", "migrate_enums.py"}  # 自身/迁移脚本
SKIP_PREFIXES = ("selftest_", "verify_", "test_")  # 历史脚手架

SOURCE_DIRS = (
    "core",
    "engine",
    "service",
    "cli",
    "workspace",
    "output",
    "scripts",
    "tools",
)

from core.enums import (
    AuthMode,
    CaseLifecycleStatus,
    Dimension,
    ExecStatus,
    FType,
    MethodMarker,
    PullStatus,
    ReportFormat,
    ReviewStatus,
    RunBatchState,
    Tag,
    TPType,
    VerifyLayer,
)


# 所有枚举取值字面量（不含范围关键词）
_LITERALS = set()
for _e in (
    TPType,
    Tag,
    FType,
    VerifyLayer,
    Dimension,
    MethodMarker,
    ExecStatus,
    CaseLifecycleStatus,
    ReviewStatus,
    PullStatus,
    RunBatchState,
    ReportFormat,
    AuthMode,
):
    for _m in _e:
        _LITERALS.add(_m.value)
VALS_RE = "|".join(re.escape(v) for v in sorted(_LITERALS, key=len, reverse=True))

# 已知枚举字段（出现在这些字段名语境下的字面量才报警）
FIELDS = [
    "tp_type",
    "tag",
    "ftype",
    "dimension",
    "verify_layer",
    "status",
    "state",
    "method",
    "review_status",
    "layer",
    "auth_mode",
]
FA = "|".join(FIELDS)

# 范围关键词（不报警）
SCOPE_KW = {"全部", "所有", "全量", "完整", "all", "ALL"}

_PATTERNS = [
    re.compile(rf'["\']({FA})["\']\s*:\s*["\'](?P<v>{VALS_RE})["\']'),
    re.compile(rf'\b({FA})\b[^"\n]*?(==|!=)\s*["\'](?P<v>{VALS_RE})["\']'),
    re.compile(rf'["\'](?P<v>{VALS_RE})["\']\s+in\s+'),
    re.compile(rf'return\s+["\'](?P<v>{VALS_RE})["\']'),
]

_SQL_KW = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "WHERE",
    "NOT IN",
    "VALUES",
    "status NOT",
    "review_status",
    "cases.status",
    "runs.status",
)


def _is_whitelisted(line: str) -> bool:
    s = line.strip()
    if s.startswith("#"):
        return True
    if 'f"' in line or "f'" in line or "f(" in line:  # f-string 文案
        return True
    return any(k in line for k in _SQL_KW)


def _strip_comment(line: str) -> str:
    """去掉行尾注释（不在引号内的首个 #），避免误报注释里的字面量。"""
    in_s = None
    for i, ch in enumerate(line):
        if ch in "\"'":
            if in_s is None:
                in_s = ch
            elif in_s == ch:
                in_s = None
        elif ch == "#" and in_s is None:
            return line[:i]
    return line


def scan_file(path: str):
    hits: list = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except Exception:
        return hits
    for i, line in enumerate(lines, 1):
        if _is_whitelisted(line):
            continue
        code = _strip_comment(line)
        for pat in _PATTERNS:
            for m in pat.finditer(code):
                v = m.group("v")
                if v in SCOPE_KW:
                    continue
                # UI/接口 碰撞提示（不计入失败）
                note = ""
                if v == "UI" or v == "接口":
                    note = "  [碰撞提示: 确认是 VerifyLayer 还是 MethodMarker/显示名]"
                hits.append((i, v, line.strip(), note))
    return hits


def _iter_source_py():
    """产出需扫描的源码 .py（各层源码 + 门禁/工具脚本；排除门禁自身与 enums.py 定义）。

    与 main() 的正向扫描口径对齐：scripts/ 里的工具脚本同样是「枚举值的使用方」，
    其引用应计入反向校验，避免误报孤儿。
    """
    for sub in SOURCE_DIRS:
        for dp, _, fs in os.walk(os.path.join(ROOT, sub)):
            parts = dp.split(os.sep)
            if any(seg in EXCLUDE_DIRS for seg in parts):
                continue
            for f in fs:
                if not f.endswith(".py"):
                    continue
                if f in SKIP_FILES or f.startswith(SKIP_PREFIXES):
                    continue
                yield os.path.join(dp, f)


def reverse_check():
    """反向校验：enums.py 定义的枚举成员，是否真被项目代码引用（防孤儿枚举）。

    引用判定：代码中出现「字面量值」或「枚举成员名(Enum.X)」任一即算已用。
    孤儿仅作提示（不计入失败退出码），允许预留/历史枚举成员。
    """
    classes = [
        TPType,
        Tag,
        FType,
        VerifyLayer,
        Dimension,
        MethodMarker,
        ExecStatus,
        CaseLifecycleStatus,
        ReviewStatus,
        PullStatus,
        RunBatchState,
        ReportFormat,
        AuthMode,
    ]
    blobs = {}
    for path in _iter_source_py():
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                blobs[path] = fh.read()
        except Exception:
            continue
    combined = "\n".join(blobs.values())
    orphans = []
    for ecls in classes:
        for member in ecls:
            val = member.value
            ref = f"{ecls.__name__}.{member.name}"
            if val in combined or ref in combined:
                continue
            orphans.append((ecls.__name__, member.name, val))
    return orphans


def main():
    violations = []
    for dp, _, fs in os.walk(ROOT):
        parts = dp.split(os.sep)
        if any(seg in EXCLUDE_DIRS for seg in parts):
            continue
        for f in fs:
            if not f.endswith(".py"):
                continue
            if f in SKIP_FILES:
                continue
            if f.startswith(SKIP_PREFIXES):
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, ROOT)
            for i, v, snippet, note in scan_file(full):
                violations.append((rel, i, v, snippet, note))
    # 过滤：仅 UI/接口 碰撞提示不计入失败
    hard = [x for x in violations if not x[4]]
    soft = [x for x in violations if x[4]]
    if violations:
        print(
            f"[check_enums] 发现 {len(hard)} 处裸枚举字面量（应改用 core.enums）+ "
            f"{len(soft)} 处碰撞提示："
        )
        for rel, i, v, snippet, note in violations:
            tag = "!" if not note else "~"
            print(f"  {tag} {rel}:{i}  '{v}'  -> {snippet[:90]}{note}")
        print("\n建议：from core.enums import ... 后改用 XxxEnum.VALUE.value")
        if hard:
            return 1
    print(f"[check_enums] 零硬性违规 ✅（扫描基线 {ROOT}，已排除脚手架/文档/SQL/f-string）")
    # 反向校验：孤儿枚举成员（仅提示）
    orphans = reverse_check()
    if orphans:
        print(
            f"[check_enums] 反向校验：{len(orphans)} 个枚举成员未被 backend 引用（预留/历史，不计入失败）："
        )
        for cls, name, val in orphans:
            print(f"  ~ {cls}.{name} = '{val}'")
    else:
        print("[check_enums] 反向校验：所有枚举成员均被引用 ✅")
    return 0


if __name__ == "__main__":
    rc = main()
    if "--fix-report" in sys.argv:
        rc = 0
    sys.exit(rc)
