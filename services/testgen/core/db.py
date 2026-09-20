"""数据访问基础设施：SQLite 连接、schema 初始化与轻量迁移。

约定：
- 所有写操作走参数化查询，**禁止字符串拼接 SQL 值**；
- 表结构与契约 v1.0 对齐（列名与 legacy 一致，保证可对账）；
- 迁移一律「可回滚的加列」——`ensure_column()` 幂等，不删列、不改列类型。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from core.config import get_settings


SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,
        local_path  TEXT NOT NULL,
        git_url     TEXT,
        branch      TEXT,
        type        TEXT DEFAULT 'repo',
        status      TEXT DEFAULT 'active',
        base_url    TEXT,
        created_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS functional_points (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id    INTEGER NOT NULL,
        commit_ref    TEXT,
        file_path     TEXT,
        name          TEXT,
        description   TEXT,
        ftype         TEXT,
        review_status TEXT DEFAULT 'approved',
        created_at    TEXT,
        contract_id   TEXT,
        title         TEXT,
        module        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_points (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL,
        tp_id           TEXT,
        fp_contract_id  TEXT,
        category        TEXT,
        module          TEXT,
        semantic        TEXT,
        title           TEXT,
        source          TEXT,
        method          TEXT,
        area            TEXT,
        expect          TEXT,
        dimension       TEXT,
        tag             TEXT,
        review_status   TEXT DEFAULT 'pending',
        created_at      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cases (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL,
        fp_id           INTEGER,
        title           TEXT,
        steps           TEXT,
        ctype           TEXT,
        script_path     TEXT,
        review_status   TEXT DEFAULT 'approved',
        status          TEXT DEFAULT 'generated',
        created_at      TEXT,
        tp_id           TEXT,
        fp_contract_id  TEXT,
        tc_no           TEXT,
        module          TEXT,
        case_type       TEXT,
        priority        TEXT,
        precondition    TEXT,
        doc_steps       TEXT,
        version         INTEGER DEFAULT 1,
        review_comment  TEXT,
        test_type       TEXT,
        last_result     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS change_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER,
        kind        TEXT,
        detail      TEXT,
        created_at  TEXT
    )
    """,
    # F12 执行留痕：逐条结论（legacy 列名对齐 + 只增列）
    # `case_id` 指向 cases.id（不加外键约束：用例可能被对账作废/重建，历史留痕须保留）；
    # `tp_id` 为冗余业务键，便于无需 join 即可按测试点追溯执行历史。
    """
    CREATE TABLE IF NOT EXISTS runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL,
        batch_id        TEXT,
        case_id         INTEGER,
        tp_id           TEXT,
        status          TEXT,
        detail          TEXT,
        duration_ms     INTEGER DEFAULT 0,
        screenshot_path TEXT,
        log_path        TEXT,
        mode            TEXT,
        source_kind     TEXT,
        created_at      TEXT
    )
    """,
    # F12 执行留痕：批次终态（legacy 列名对齐 + 只增列）
    # total/done/status_counts/state/started_at/finished_at 与 legacy 同名；
    # filters/webhook_url 为 F17（服务化：异步 + webhook）预留，当前留空。
    """
    CREATE TABLE IF NOT EXISTS run_batches (
        batch_id      TEXT PRIMARY KEY,
        project_id    INTEGER NOT NULL,
        mode          TEXT,
        source_kind   TEXT,
        total         INTEGER DEFAULT 0,
        done          INTEGER DEFAULT 0,
        status_counts TEXT DEFAULT '{}',
        state         TEXT,
        started_at    TEXT,
        finished_at   TEXT,
        error         TEXT,
        filters       TEXT,
        webhook_url   TEXT,
        updated_at    TEXT
    )
    """,
    # P5 服务化：生成任务异步化（仅「生成测试用例」环节，执行环节不在本服务当前范围）。
    # 状态机：pending → running → success / failed / cancelled；每个写操作独立连接，线程安全。
    """
    CREATE TABLE IF NOT EXISTS gen_tasks (
        task_id         TEXT PRIMARY KEY,
        project_id      INTEGER,
        kind            TEXT,
        idempotency_key TEXT,
        state           TEXT,
        progress        REAL DEFAULT 0,
        stage           TEXT,
        request         TEXT,
        result          TEXT,
        error           TEXT,
        created_at      TEXT,
        started_at      TEXT,
        finished_at     TEXT,
        updated_at      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
)

# schema 版本：新增 runs / run_batches（F12 执行留痕）→ 1 → 2
SCHEMA_VERSION = "2"

INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_fp_project ON functional_points(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_fp_contract ON functional_points(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_tp_project ON test_points(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_tp_tp_id ON test_points(tp_id)",
    "CREATE INDEX IF NOT EXISTS idx_tp_fp ON test_points(fp_contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_cases_project ON cases(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_cases_tp ON cases(tp_id)",
    "CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status)",
    # 唯一性防线：同一项目下编号必须唯一。
    # 历史教训：编号算法一旦碰撞（如同名方法共用 fp_id），同一 tp_id 会落出多条用例行，
    # 而对账逻辑以 tp_id 为键，重复行永远匹配不上 → 永久残留成「孤儿用例」。
    # 用唯一索引把问题挡在写入侧，而不是靠事后体检。
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_fp_project_contract"
    " ON functional_points(project_id, contract_id) WHERE contract_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tp_project_tp_id"
    " ON test_points(project_id, tp_id) WHERE tp_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cases_project_tp_id"
    " ON cases(project_id, tp_id) WHERE tp_id IS NOT NULL",
    # F12：执行留痕查询索引（按项目列批次、按批次取逐条、按用例追溯历史）
    "CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_case ON runs(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_batches_project ON run_batches(project_id)",
)

# v1.1 预留列（只加不改）
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("test_points", "evidence", "TEXT"),
    ("test_points", "confidence", "REAL"),
    ("test_points", "origin", "TEXT"),
    ("test_points", "unverified", "INTEGER DEFAULT 0"),
    ("functional_points", "semantic", "TEXT"),
)


def db_path() -> Path:
    return get_settings().db_path


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """建立连接（WAL + 外键），行工厂为 sqlite3.Row。"""
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """建表 + 索引 + 幂等补列；可反复调用。"""
    own = conn is None
    conn = conn or connect()
    try:
        cur = conn.cursor()
        for ddl in SCHEMA:
            cur.execute(ddl)
        for ddl in INDEXES:
            cur.execute(ddl)
        for table, column, coltype in _ADDITIVE_COLUMNS:
            ensure_column(conn, table, column, coltype)
        cur.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> bool:
    """幂等加列；已存在则返回 False。"""
    if column in table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    return True
