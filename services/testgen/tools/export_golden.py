#!/usr/bin/env python3
# ============================================================================
# 黄金样本导出（golden fixtures）—— P0 备份与冻结
# ----------------------------------------------------------------------------
# 作用：把平台数据库里的「功能点 / 测试点 / 用例」按项目导出为 JSON，
#       作为后续新旧实现「影子双跑」比对的固定标尺。
#
# 只读：本脚本以 read-only 模式打开数据库，绝不写入。
#
# 用法：
#   python tools/export_golden.py                       # 默认导出所有项目
#   python tools/export_golden.py --project 2           # 只导出 project_id=2
#   python tools/export_golden.py --out ../../backup/testgen-golden
# ============================================================================
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import Any


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "testgen.db")
DEFAULT_OUT = os.path.abspath(os.path.join(ROOT, "..", "..", "backup", "testgen-golden"))

# 导出顺序即依赖顺序：功能点 → 测试点 → 用例
TABLES: tuple[tuple[str, str], ...] = (
    ("functional_points", "functional_points"),
    ("test_points", "test_points"),
    ("cases", "cases"),
)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """以只读方式打开数据库（file: URI + mode=ro），避免任何意外写入。"""
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, table: str, project_id: int) -> list[dict[str, Any]]:
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE project_id = ? ORDER BY id",
        (project_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def _projects(conn: sqlite3.Connection, only: int | None) -> list[dict[str, Any]]:
    if only is None:
        cur = conn.execute("SELECT id, name, local_path FROM projects ORDER BY id")
    else:
        cur = conn.execute("SELECT id, name, local_path FROM projects WHERE id = ?", (only,))
    return [dict(r) for r in cur.fetchall()]


def _dump(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出黄金样本（只读）")
    ap.add_argument("--db", default=DEFAULT_DB, help="平台数据库路径")
    ap.add_argument("--out", default=DEFAULT_OUT, help="导出根目录")
    ap.add_argument("--project", type=int, default=None, help="仅导出指定 project_id")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="批次日期")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[golden] 数据库不存在：{args.db}", file=sys.stderr)
        return 2

    out_dir = os.path.join(args.out, args.date)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    conn = _connect_ro(args.db)
    try:
        projects = _projects(conn, args.project)
        if not projects:
            print("[golden] 未找到匹配项目", file=sys.stderr)
            return 3

        manifest: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_db": os.path.abspath(args.db),
            "source_db_sha256": _sha256(args.db),
            "source_db_bytes": os.path.getsize(args.db),
            "projects": [],
        }

        for p in projects:
            pid = int(p["id"])
            slug = f"project_{pid}"
            entry: dict[str, Any] = {
                "project_id": pid,
                "name": p["name"],
                "local_path": p["local_path"],
                "files": {},
                "counts": {},
            }
            for table, _ in TABLES:
                rows = _rows(conn, table, pid)
                rel = f"{slug}/{table}.json"
                _dump(os.path.join(out_dir, rel), rows)
                entry["files"][table] = rel
                entry["counts"][table] = len(rows)
            manifest["projects"].append(entry)
            print(
                f"[golden] {slug} ({p['name']}): "
                + ", ".join(f"{k}={v}" for k, v in entry["counts"].items())
            )

        _dump(os.path.join(out_dir, "MANIFEST.json"), manifest)
    finally:
        conn.close()

    print(f"[golden] 导出完成 → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
