"""多仓改造（v1.7）数据库迁移：单仓 → project 容器 + repo_bindings。

做四件事，全部幂等、可重复执行：
1. 备份现有 db（``--db`` 指定，默认取 testgen 的 data/testgen.db）；
2. 调 ``init_db`` 建 repo_bindings 表、给 6 张数据表加可空 repo_binding_id 列；
3. 为每个旧 project 建一条 ``repo_key='default'`` 的隐式 binding（project 退化为容器）；
4. 回填旧数据行的 repo_binding_id = 该项目的 default binding id，使唯一索引按仓隔离后
   旧数据仍落在 (project_id, binding_id, xxx_id) 上、唯一性不失效。

两个必须显式处理的 SQLite 陷阱：
- 表达式唯一索引：``CREATE UNIQUE INDEX IF NOT EXISTS`` 只看**名字**，旧库同名索引定义是
  ``(project_id, contract_id)``，IF NOT EXISTS 会跳过导致新定义不生效。故检测 sql 里是否
  含 ``repo_binding_id``，不含则 DROP 后用新定义重建。
- 回填顺序：必须先建 binding、回填 repo_binding_id，再重建唯一索引，否则 COALESCE 之前
  旧行都是 NULL 归到 0 桶不会冲突，但步骤顺序反了会短暂让新旧索引定义错位。

用法::

    python scripts/migrate_multirepo.py                # 迁移默认库
    python scripts/migrate_multirepo.py --db /path/x.db
    python scripts/migrate_multirepo.py --dry-run      # 只打印计划，不落盘

迁移后旧单仓行为完全不变（所有查询仍按 project_id，repo_binding_id 只是多了一列）。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 自举 sys.path：让脚本能用 services/testgen 下的 core 包。
_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "services" / "testgen"
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from core import db as db_mod  # noqa: I001 - 必须在 sys.path 自举之后导入


# 旧定义 → 新定义：名字相同，必须 DROP 重建（IF NOT EXISTS 不更新已存在索引的定义）。
_REINDEX: tuple[tuple[str, str], ...] = (
    (
        "uq_fp_project_contract",
        (
            "CREATE UNIQUE INDEX uq_fp_project_contract ON functional_points"
            " (project_id, COALESCE(repo_binding_id, 0), contract_id)"
            " WHERE contract_id IS NOT NULL"
        ),
    ),
    (
        "uq_tp_project_tp_id",
        (
            "CREATE UNIQUE INDEX uq_tp_project_tp_id ON test_points"
            " (project_id, COALESCE(repo_binding_id, 0), tp_id)"
            " WHERE tp_id IS NOT NULL"
        ),
    ),
    (
        "uq_cases_project_tp_id",
        (
            "CREATE UNIQUE INDEX uq_cases_project_tp_id ON cases"
            " (project_id, COALESCE(repo_binding_id, 0), tp_id)"
            " WHERE tp_id IS NOT NULL"
        ),
    ),
)

# 需要回填的表（都挂 project_id + repo_binding_id）。
_BACKFILL_TABLES: tuple[str, ...] = (
    "functional_points",
    "test_points",
    "cases",
    "change_log",
    "runs",
    "gen_tasks",
)


def _index_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def migrate(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, int]:
    """执行迁移，返回各步骤计数。dry_run 时只统计不写。"""
    stats: dict[str, int] = {"bindings_created": 0, "rows_backfilled": 0, "indexes_rebuilt": 0}

    if not dry_run:
        db_mod.init_db(conn)

    # dry-run 刻意跳过 init_db，迁移前的旧库上 repo_bindings 表尚不存在。
    # 用表存在性守卫，避免「预演」反过来要求库已经迁完。
    has_bindings = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repo_bindings'"
        ).fetchone()
    )

    projects = [int(r["id"]) for r in conn.execute("SELECT id FROM projects ORDER BY id")]
    for pid in projects:
        exists = (
            conn.execute(
                "SELECT id FROM repo_bindings WHERE project_id=? AND repo_key='default'", (pid,)
            ).fetchone()
            if has_bindings
            else None
        )
        if exists:
            continue
        stats["bindings_created"] += 1
        if not dry_run:
            conn.execute(
                "INSERT INTO repo_bindings(project_id, repo_key, local_path, repo_url, base,"
                " target, ref, created_at) SELECT id, 'default', local_path, git_url, '', '',"
                " branch, ? FROM projects WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), pid),
            )

    for pid in projects:
        binding = (
            conn.execute(
                "SELECT id FROM repo_bindings WHERE project_id=? AND repo_key='default'", (pid,)
            ).fetchone()
            if has_bindings
            else None
        )
        if not binding:
            continue
        bid = int(binding["id"])
        for table in _BACKFILL_TABLES:
            cur = conn.execute(
                f"UPDATE {table} SET repo_binding_id=? WHERE project_id=? AND repo_binding_id IS NULL",
                (bid, pid),
            )
            stats["rows_backfilled"] += cur.rowcount if not dry_run else 0

    for name, ddl in _REINDEX:
        sql = _index_sql(conn, name)
        if sql is None:
            # 库尚未 init（dry-run 或空库）：记一笔，跳过。
            continue
        if "repo_binding_id" in sql:
            continue
        stats["indexes_rebuilt"] += 1
        if not dry_run:
            conn.execute(f"DROP INDEX {name}")
            conn.execute(ddl)

    if not dry_run:
        conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="多仓改造数据库迁移（单仓 → project 容器）")
    parser.add_argument(
        "--db", type=Path, default=None, help="db 路径；默认取 core.config 的 db_path"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    args = parser.parse_args()

    target = args.db or db_mod.db_path()
    target = Path(target).expanduser().resolve()
    if not target.exists():
        print(f"[migrate] db 不存在：{target}（先启动一次服务以 init_db）")
        return 1

    if not args.dry_run and not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.pre-multirepo-{stamp}.bak")
        shutil.copy2(target, backup)
        print(f"[migrate] 备份：{backup}")

    conn = sqlite3.connect(str(target), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        stats = migrate(conn, dry_run=args.dry_run)
        mode = "DRY-RUN" if args.dry_run else "DONE"
        print(f"[migrate] {mode}: {stats}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
