"""P1 · 收窄「业务函数 → 功能点」提取范围的回归测试。

断言：
1. strict 模式排除测试/脚手架/构建脚本/配置模式类文件中的业务函数；
2. 但保留被显式认定为「独立能力」的目录（tools/agents/mcp_servers/workspace/skills）；
3. API 路由**不受**业务噪声逻辑影响（避免误伤真实接口）；
4. loose 模式保留全部（legacy 行为）；
5. scan.NOISE_DIR_PARTS 扩展的整文件噪声目录（regression/_fx_test 等）在入口即被排除。
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.enums import BUSINESS_EXTRACT_LOOSE, BUSINESS_EXTRACT_STRICT
from engine.fp_extract import (
    _extract_api_and_business,
    _file_is_business_noise,
    extract_functional_points,
)
from engine.scan import SourceFile


WHITE = ["tools", "agents", "mcp_servers", "workspace/skills"]

API_BIZ = """
from flask import Flask
app = Flask(__name__)
@app.route("/api/x")
def get_x():
    return 1
def helper():
    return 2
"""

BIZ_ONLY = """
def load_config():
    return {}
def another():
    return 1
"""


def _src(rel: str, text: str = "") -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(f"/tmp/{rel.replace('/', '_')}"), text=text)


def _tree(code: str) -> ast.Module:
    return ast.parse(code)


# ---------------------------------------------------------------- 1. 纯函数单测
def test_loose_never_noise() -> None:
    for rel in ["conf/settings.py", "regression/run.py", "tools/foo.py", "backend/x.py"]:
        assert _file_is_business_noise(rel, BUSINESS_EXTRACT_LOOSE, WHITE) is False


def test_strict_noise_dirs() -> None:
    for rel in [
        "conf/settings.py",
        "schema/define.py",
        "regression/run.py",
        "_fx_test/suite.py",
        "fixtures/data.py",
        "scripts/build.py",
        "selftest/run.py",
    ]:
        assert _file_is_business_noise(rel, BUSINESS_EXTRACT_STRICT, WHITE) is True


def test_strict_whitelist_overrides() -> None:
    for rel in ["tools/foo.py", "agents/bar.py", "mcp_servers/svc.py", "workspace/skills/s.py"]:
        assert _file_is_business_noise(rel, BUSINESS_EXTRACT_STRICT, WHITE) is False


def test_strict_noise_filename_prefix() -> None:
    for rel in [
        "backend/test_something.py",
        "backend/conftest_helpers.py",
        "backend/verify_flow.py",
        "backend/selftest_mod.py",
    ]:
        assert _file_is_business_noise(rel, BUSINESS_EXTRACT_STRICT, WHITE) is True


def test_strict_normal_file_not_noise() -> None:
    assert (
        _file_is_business_noise(
            "backend/research-agent-source/tools/sub/foo.py", BUSINESS_EXTRACT_STRICT, WHITE
        )
        is False
    )


# ---------------------------------------------------------------- 2. 分支集成
def test_conf_strict_drops_business_keeps_api() -> None:
    sf = _src("conf/settings.py")
    fns = _extract_api_and_business(sf, _tree(API_BIZ), BUSINESS_EXTRACT_STRICT, WHITE)
    types = {f.ftype for f in fns}
    assert "api" in types  # API 路由保留
    assert "business" not in types  # 业务函数排除


def test_conf_strict_business_only_dropped() -> None:
    sf = _src("conf/settings.py")
    fns = _extract_api_and_business(sf, _tree(BIZ_ONLY), BUSINESS_EXTRACT_STRICT, WHITE)
    assert fns == []


def test_conf_loose_keeps_business() -> None:
    sf = _src("conf/settings.py")
    fns = _extract_api_and_business(sf, _tree(BIZ_ONLY), BUSINESS_EXTRACT_LOOSE, WHITE)
    assert any(f.ftype == "business" for f in fns)


def test_tools_whitelist_keeps_business() -> None:
    sf = _src("tools/foo.py")
    fns = _extract_api_and_business(sf, _tree(BIZ_ONLY), BUSINESS_EXTRACT_STRICT, WHITE)
    assert any(f.ftype == "business" for f in fns)


# ---------------------------------------------------------------- 3. 入口透传
def test_extract_functional_points_passthrough() -> None:
    sf = _src("conf/settings.py", text=BIZ_ONLY)
    files = {"conf/settings.py": sf}
    res = extract_functional_points(
        files, business_extract_mode=BUSINESS_EXTRACT_STRICT, business_include_dirs=WHITE
    )
    assert res.functional_points == []  # conf 目录业务函数全部收窄


def test_noise_dir_whole_file_excluded() -> None:
    # P1-1：NOISE_DIR_PARTS 扩展的目录在入口整文件排除（含 API 路由）
    sf = _src("regression/run.py", text=API_BIZ)
    files = {"regression/run.py": sf}
    res = extract_functional_points(files)
    assert res.functional_points == []
