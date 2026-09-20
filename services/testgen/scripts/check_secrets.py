#!/usr/bin/env python3
# ============================================================================
# 密钥 / 凭据预提交扫描（gitleaks 类，纯 Python 离线实现）
# ----------------------------------------------------------------------------
# 作用：在提交前 / CI 中扫描仓库文本文件，发现高置信凭据（AWS / JWT / GitHub /
#       GitLab / Slack / Google / Stripe / OpenAI / Twilio / 私钥块 / 通用
#       key=value 赋值 / Slack webhook）即拦截，防止密钥意外入库。
#
# 与 gitleaks 的差异：无外部二进制依赖，规则集为「高精度子集」+ 可配置允许名单，
#   团队若需完整规则（熵检测 / 更多厂商），可改用官方 gitleaks 二进制（见文末说明）。
#
# 用法：
#   python scripts/check_secrets.py            # 扫描全仓（gate_all 调用方式）
#   python scripts/check_secrets.py --staged   # 仅扫描 git 已暂存文件（直接 commit hook 用）
#   python scripts/check_secrets.py --report secrets-report.json
#
# 退出码：发现未豁免凭据 → 1；零发现 → 0。供 pre-commit / CI 拦截。
#
# 允许名单：仓库根 .secrets-allowlist.json（记录已确认的既有项及原因，便于审计与逐步清理）
# ============================================================================
import argparse
import json
import os
import re
import subprocess


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELF = os.path.basename(__file__)
ALLOWLIST_PATH = os.path.join(ROOT, ".secrets-allowlist.json")

# 跳过的目录（第三方 / 依赖 / 缓存 / 运行态 / 被测目标）
SKIP_DIRS = {
    ".git",
    "venv",
    ".venv",
    "env",
    "node_modules",
    "__pycache__",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    "htmlcov",
    "dist",
    "build",
    ".eggs",
    "data",
}
# 跳过的文件后缀（非文本 / 二进制产物）
SKIP_SUFFIX = (
    ".pyc",
    ".pyo",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".whl",
    ".egg",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".woff",
    ".woff2",
    ".ttf",
    ".lock",
)
# 整路径跳过的文件（自身 / 允许名单 / 第三方被测仓库）
SKIP_FILES = {SELF, ".secrets-allowlist.json"}
SKIP_PATH_FRAGMENTS = ("/work/targets/", "/targets/", "/.workbuddy/")

# ---- 规则定义 ----
# 高置信厂商前缀规则：对所有文件生效（含文档），误报率极低
PREFIX_RULES = [
    ("AWS_ACCESS_KEY_ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[0-9A-Za-z_]{22,})\b"),
    ),
    ("GITLAB_PAT", re.compile(r"\bglpat-[A-Za-z0-9_-]{20}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "SLACK_WEBHOOK",
        re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    ),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("STRIPE_SECRET", re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{16,}\b")),
    ("OPENAI_API_KEY", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("TWILIO_SECRET", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        "PRIVATE_KEY_BLOCK",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
]

# 通用赋值规则：仅对代码类文件生效（跳过 .md/.txt/rst 等文档，降低误报）
KEY_ASSIGN = re.compile(
    r"(?i)\b(?:api[_-]?key|apikey|secret|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|auth[_-]?token|password|passwd|pwd|private[_-]?key|"
    r"encryption[_-]?key|encrypt[_-]?key)\b\s*[:=]\s*['\"]([^'\"\n]{8,})['\"]"
)
# 占位判定：值像示例/占位则放行
PLACEHOLDER_HINTS = (
    "example",
    "sample",
    "changeme",
    "placeholder",
    "your_",
    "your-",
    "<",
    "xxxx",
    "dummy",
    "test_",
    "test@",
    "replace",
    "todo",
    "fixme",
    "none",
    "null",
    "undefined",
    "****",
    "设为",
    "此处",
    "示例",
)

DOC_SUFFIX = (".md", ".markdown", ".txt", ".rst", ".adoc")


def _load_allowlist():
    if not os.path.isfile(ALLOWLIST_PATH):
        return {"paths": [], "line_substrings": [], "regexes": []}
    try:
        with open(ALLOWLIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"paths": [], "line_substrings": [], "regexes": []}
    data.setdefault("paths", [])
    data.setdefault("line_substrings", [])
    data.setdefault("regexes", [])
    data["_regexes"] = [re.compile(r) for r in data["regexes"]]
    return data


def _is_skipped_path(rel: str) -> bool:
    return any(frag in rel for frag in SKIP_PATH_FRAGMENTS)


def _should_scan(fp: str) -> bool:
    if fp.endswith(SKIP_SUFFIX):
        return False
    base = os.path.basename(fp)
    if base in SKIP_FILES:
        return False
    rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
    return not _is_skipped_path("/" + rel + "/")


def _dir_allowed(dp_parts) -> bool:
    return any(seg in SKIP_DIRS for seg in dp_parts)


def _line_suppressed(line: str, allow: dict) -> bool:
    s = line.strip()
    # 注释行 / nosec / noqa / pragma
    if s.startswith("#") or "# nosec" in line or "# noqa" in line or "# pragma" in line:
        return True
    # 文档内允许的明文示例（在文档中显式标注 example 的行已由占位判定覆盖）
    if any(sub in line for sub in allow["line_substrings"]):
        return True
    return any(rx.search(line) for rx in allow.get("_regexes", []))


def _mask(val: str) -> str:
    v = val.strip()
    if len(v) <= 6:
        return "****"
    return v[:4] + "****" + v[-2:]


def scan_file(fp: str, allow: dict):
    hits: list = []
    try:
        with open(fp, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except Exception:
        return hits
    is_doc = fp.lower().endswith(DOC_SUFFIX)
    for i, line in enumerate(lines, 1):
        if _line_suppressed(line, allow):
            continue
        rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
        # 1) 高置信前缀规则（含文档）
        for name, rx in PREFIX_RULES:
            for m in rx.finditer(line):
                hits.append((rel, i, name, m.group(0)))
        # 2) 通用赋值规则（跳过文档，避免示例误报）
        if not is_doc:
            for m in KEY_ASSIGN.finditer(line):
                val = m.group(1)
                low = val.lower()
                if any(h in low for h in PLACEHOLDER_HINTS):
                    continue
                # f-string 模板里的值多为动态构造，跳过
                if 'f"' in line or "f'" in line or "f(" in line:
                    continue
                key = m.group(0).split("=")[0].split(":")[0].strip()
                hits.append((rel, i, "GENERIC_ASSIGN:" + key, val))
    return hits


def _staged_files():
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except Exception:
        return []
    return [os.path.join(ROOT, p) for p in out if p.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true", help="仅扫描 git 已暂存文件")
    ap.add_argument("--report", help="将发现写入 JSON 报告（不影响退出码策略）")
    args = ap.parse_args()

    allow = _load_allowlist()
    # 允许名单整路径跳过（归一化为包含匹配）
    allow_paths = [p.replace(os.sep, "/") for p in allow["paths"]]

    files = []
    if args.staged:
        files = [f for f in _staged_files() if _should_scan(f)]
    else:
        for dp, _, fs in os.walk(ROOT):
            parts = dp.split(os.sep)
            if _dir_allowed(parts):
                continue
            for fn in fs:
                fp = os.path.join(dp, fn)
                if not _should_scan(fp):
                    continue
                rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
                if any(
                    rel == p or rel.endswith("/" + p) or ("/" + p + "/") in ("/" + rel + "/")
                    for p in allow_paths
                ):
                    continue
                files.append(fp)

    findings = []
    for fp in files:
        for rel, i, name, val in scan_file(fp, allow):
            # 整路径允许名单（再次兜底）
            if any(rel == p or rel.endswith("/" + p) for p in allow_paths):
                continue
            disp = val if name.startswith("GENERIC_ASSIGN") else _mask(val)
            findings.append({"file": rel, "line": i, "rule": name, "match": disp})

    # 输出
    if findings:
        print(f"[check_secrets] 发现 {len(findings)} 处疑似凭据（需处置 / 加白）：")
        for f in findings:
            print(f"  ! {f['file']}:{f['line']}  [{f['rule']}]  {f['match']}")
        print("\n处理建议：")
        print("  1) 若是真密钥：立即移入 .env（已被 .gitignore 排除）+ 如已泄露请轮换；")
        print("  2) 若是已知测试/示例占位：在 .secrets-allowlist.json 登记原因后重新运行；")
        print("  3) 团队需要完整规则：改用官方 gitleaks 二进制（pre-commit mirror 自动下载）。")
        if args.report:
            with open(args.report, "w", encoding="utf-8") as rf:
                json.dump(findings, rf, ensure_ascii=False, indent=2)
        return 1

    print("[check_secrets] 未发现问题凭据 ✅")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as rf:
            json.dump([], rf, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
