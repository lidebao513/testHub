"""扫描所有模型的「数据库列 vs 模型字段」漂移。

输出两类：
  DB_ONLY   —— 表里有列、模型没字段（写入时可能因 NOT NULL 无默认值而 500）
  MODEL_ONLY—— 模型有字段、表里没列（运行时报 Unknown column）
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from django.apps import apps  # noqa: E402
from django.db import connection  # noqa: E402

db_only, model_only = [], []

with connection.cursor() as cur:
    cur.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_TYPE "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
    )
    db_cols = {}
    for t, c, nullable, default, ctype in cur.fetchall():
        db_cols.setdefault(t, {})[c] = (nullable, default, ctype)

for model in apps.get_models():
    table = model._meta.db_table
    if table not in db_cols:
        continue
    model_cols = {f.column for f in model._meta.concrete_fields}
    real_cols = set(db_cols[table])
    for col in sorted(real_cols - model_cols):
        nullable, default, ctype = db_cols[table][col]
        db_only.append(f"{table}.{col}  {ctype} null={nullable} default={default}")
    for col in sorted(model_cols - real_cols):
        model_only.append(f"{table}.{col}")

print(f"=== DB_ONLY ({len(db_only)}) ===")
for x in db_only:
    print("  " + x)
print(f"=== MODEL_ONLY ({len(model_only)}) ===")
for x in model_only:
    print("  " + x)
sys.exit(0)
