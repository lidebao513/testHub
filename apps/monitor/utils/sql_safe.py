"""SQL 注入防护（纯函数，不依赖 Django / requests，便于单元测试）。

背景（H9）：`probes._sl651_db_check` 用 f-string 把 `db['table']` / `db['time_field']`
/`db['where_clause']` 直接拼进 SQL。若这些配置项可被不可信输入影响，即构成 SQL 注入。

防护策略：
- 标识符（表名、字段名）必须是严格的 SQL 标识符（仅 `[A-Za-z_][A-Za-z0-9_]*`）；
- WHERE 条件不允许出现能改变语句结构/堆叠查询/注释截断的危险字符与关键字
  （分号、注释 `--` `#` `/*` `*/`、控制字符，以及 UNION/DROP/DELETE 等 DML/DCL 关键字）。
  允许中文值与常规比较运算符，因此不会破坏既有合法谓词（如 `device_type = '水雨情'`）。
"""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 危险令牌：分号（堆叠查询）、注释截断、以及会改变语义的 DML/DCL 关键字。
_DANGEROUS_TOKENS = re.compile(
    r"(;|--|#|/\*|\*/|"
    r"\b(union|select|insert|update|delete|drop|alter|truncate|create|replace|"
    r"exec|execute|grant|revoke|into|from)\b)",
    re.IGNORECASE,
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def is_safe_sql_identifier(name: str) -> bool:
    """表名 / 字段名必须是合法 SQL 标识符（非空且仅含字母数字下划线，且以字母/下划线开头）。"""
    return bool(name) and bool(_IDENT_RE.match(name))


def is_safe_where_clause(where: str) -> bool:
    """WHERE 条件白名单校验：允许中文与常规比较，禁止危险字符/关键字/控制字符。

    空串（即无 WHERE）视为安全。
    """
    if not where:
        return True
    if _CONTROL_CHARS.search(where):
        return False
    return not _DANGEROUS_TOKENS.search(where)
