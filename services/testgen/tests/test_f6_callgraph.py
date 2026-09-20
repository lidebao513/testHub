"""F6 · 业务调用图吞并契约测试。

问题：与 API 路由同文件的辅助函数（路由里调用的 `compute_total` 之类）各自产出一条
业务用例 → 功能点**虚高**，且这些「用例」没有可独立验证的行为
（正确性只在某个路由的上下文里成立）。

判定必须**保守**（宁可少吞不可误吞），因为误吞会丢掉真实的独立能力：
- 只被路由调用链用到的 → 吞并；
- 同时被非路由函数调用（如 `main`、CLI 入口）→ 保留；
- 带装饰器的（框架入口，如任务 / 命令）→ 保留；
- 从调用图看「无人调用」的顶层函数 → 保留。
"""

from __future__ import annotations

from pathlib import Path

from engine import fp_extract
from engine.scan import SourceFile


_SRC = '''"""订单服务。"""
from fastapi import APIRouter

router = APIRouter()


def fmt(value):
    """格式化数值。"""
    return str(value)


def compute_total(items):
    """计算合计。"""
    return fmt(sum(items))


@router.get("/orders")
def list_orders():
    """订单列表。"""
    return [compute_total([])]


def public_entry():
    """对外独立入口。"""
    return compute_total([])
'''


def _files(text: str, rel: str = "svc/orders.py") -> dict[str, SourceFile]:
    return {rel: SourceFile(rel=rel, abspath=Path(rel), text=text)}


def _names(text: str) -> set[str]:
    extracted = fp_extract.extract_functional_points(_files(text), include_business=True)
    return {fp.name for fp in extracted.functional_points}


def test_helper_only_used_by_route_is_absorbed():
    """`fmt` 只被 `compute_total`（→ 路由）调用 → 属实现细节，不单独产出功能点。"""
    assert "fmt" not in _names(_SRC)


def test_function_also_used_outside_route_chain_is_kept():
    """`compute_total` 还被 `public_entry`（非路由）调用 → 是真实独立能力，必须保留。"""
    assert "compute_total" in _names(_SRC)


def test_route_itself_is_always_kept():
    assert "GET /orders" in _names(_SRC)


def test_top_level_entry_without_callers_is_kept():
    assert "public_entry" in _names(_SRC)


def test_decorated_function_is_kept_even_if_only_route_uses_it():
    """带装饰器通常意味着框架入口（CLI 命令 / 定时任务）——不得吞并。"""
    text = """\"\"\"任务。\"\"\"
from fastapi import APIRouter
from tasks import task

router = APIRouter()


@task(name="sync")
def sync_job():
    \"\"\"同步任务。\"\"\"
    return None


@router.get("/orders")
def list_orders():
    \"\"\"订单列表。\"\"\"
    return sync_job()
"""
    names = _names(text)
    assert "sync_job" in names, "带装饰器的函数不得被当作辅助函数吞并"


def test_transitive_chain_is_absorbed():
    """多级调用链：路由 → a → b，b 只被 a 调用 → b 属实现细节。"""
    text = """\"\"\"链式调用。\"\"\"
from fastapi import APIRouter

router = APIRouter()


def step_two(x):
    \"\"\"第二步。\"\"\"
    return x


def step_one(x):
    \"\"\"第一步。\"\"\"
    return step_two(x)


@router.get("/x")
def handle():
    \"\"\"处理。\"\"\"
    return step_one(1)
"""
    names = _names(text)
    assert "step_two" not in names
    assert "step_one" not in names


def test_absorbed_count_is_reported():
    """吞并数必须如实上报——否则「功能点变少了」会被误读成「扫描漏了」。"""
    extracted = fp_extract.extract_functional_points(_files(_SRC), include_business=True)
    assert extracted.business_absorbed >= 1
    assert extracted.business_absorbed_examples
    assert any("fmt" in e for e in extracted.business_absorbed_examples)


def test_absorb_is_opt_out_by_default_in_helper_path():
    """`_route_reachable_helpers` 只报告集合，不产生副作用（可反复调用）。"""
    import ast

    tree = ast.parse(_SRC)
    qualnames = fp_extract._qualified_names(tree)
    first = fp_extract._route_reachable_helpers(tree, qualnames)
    second = fp_extract._route_reachable_helpers(tree, qualnames)
    assert first == second
    assert "fmt" in first
