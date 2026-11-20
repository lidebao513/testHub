"""页面功能点审计（#305 回归工具）：假路径 / 重复 / 路由覆盖一次看清。

用法（本机）：

    services/testgen/.venv/Scripts/python.exe scripts/audit_page_fps.py frontend/src

输出「修复后」与「旧实现复算」两组数字，用于证明：
  - 不再把文件名拼成 URL（`/Home.vue`）；
  - 同一路由不重复计点；
  - 无路由屏幕只保留「交互元素可用」一个维度（不生成 7 个需要地址的假维度）。

只读：仅扫描目录并做静态提取，不写库、不连 sidecar。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# 允许从仓库根直接跑：把 services/testgen 加进 sys.path（引擎包在那里）
_TESTGEN = Path(__file__).resolve().parents[1] / "services" / "testgen"
sys.path.insert(0, str(_TESTGEN))

from core.enums import FType
from engine.fp_extract import _PAGE_PATH_RE, _is_screen, extract_functional_points
from engine.scan import Scanner


def audit(root: str) -> int:
    files = Scanner(root).index()
    res = extract_functional_points(
        files, include_business=False, extract_pages=True, extract_components=True
    )
    pages = [fp for fp in res.functional_points if fp.ftype == FType.PAGE.value]
    names = [p.name for p in pages]
    routed = [p for p in pages if p.has_route]
    fake = [p for p in pages if not p.has_route and p.name.startswith("/")]

    print(f"文件数            : {len(files)}")
    print(
        f"PAGE 功能点（新） : {len(pages)}  唯一: {len(set(names))}  重复: {len(names) - len(set(names))}"
    )
    print(f"  有路由          : {len(routed)}")
    print(f"  无路由（屏幕）  : {len(pages) - len(routed)}（假 URL: {len(fake)}）")
    dups = {k: v for k, v in Counter(names).items() if v > 1}
    print(f"  重复明细        : {dups or '无'}")

    # 旧实现复算：屏幕文件无 path 声明 → "/<文件名>"；同文件内 path 去重、跨文件不去重
    old_names: list[str] = []
    for rel, sf in files.items():
        if sf.is_noise:
            continue
        paths = list(dict.fromkeys(m.group(1) for m in _PAGE_PATH_RE.finditer(sf.text)))
        if not _is_screen(rel, sf.text):
            continue
        if paths:
            old_names.extend(paths)
        else:
            old_names.append("/" + sf.name)
    old_fake = [n for n in old_names if n.startswith("/") and "." in n]
    old_dups = {k: v for k, v in Counter(old_names).items() if v > 1}
    print()
    print(f"PAGE 功能点（旧） : {len(old_names)}  唯一: {len(set(old_names))}")
    print(f"  旧假 URL        : {len(old_fake)}")
    print(
        f"  旧重复明细      : {dict(list(old_dups.items())[:8])}{' …' if len(old_dups) > 8 else ''}"
    )
    print(f"  收敛            : {len(old_names)} → {len(pages)}（-{len(old_names) - len(pages)}）")
    return 0 if not fake and not dups else 1


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(_TESTGEN.parents[1] / "frontend" / "src")
    raise SystemExit(audit(target))
