"""P0 安全/缺陷修复的全面自测（统一批次）。

覆盖（逐项"行为级"验证，而非仅单元函数）：
- H2/H3  settings.py：SECRET_KEY 缺失 fail-fast；DEBUG 默认 False；ALLOWED_HOSTS 依 DEBUG 分支
- H8     testgen require_auth 默认 fail-closed + AUTH_DISABLED 逃生口（含"配了令牌却不带"必拒）
- M11    生成幂等键补 selected_paths（同仓不同子集不再命中旧任务）
- M4     一次性授权码 写/读 统一为字符串，兼容 bytes/str，非法转 ValueError（杜绝 redeem 500）
- H9     SQL 注入防护：标识符 + WHERE 白名单，且已真正接入 probes._sl651_db_check
- M17    playwright 死代码（js_result / js_selector_code）已删除

说明：M4/H9 的被测逻辑是纯函数（不依赖 Django/Redis），用 importlib 直接加载；
probes 通过注入假 pymysql 验证"守卫已接入函数体"；H8/M11 属于 testgen 包，在 testgen venv 内测试；
H2/H3 通过 exec 真实源码片段（含 stub config）验证分支行为，避免 Django 初始化。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest


# 让 `import service.app` / `core.config` 可用（无论 pytest rootdir 如何）。
_ROOT = Path(__file__).resolve().parents[3]  # .../testhub_platform
_TESTGEN_ROOT = _ROOT / "services" / "testgen"
if str(_TESTGEN_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTGEN_ROOT))

from core.config import load_settings
from service import app
from service.app import PipelineRequest, _idem_key_for_generate, require_auth


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TK = _load_module("tk_mod", _ROOT / "apps" / "users" / "token_exchange.py")
_SQL = _load_module("sql_mod", _ROOT / "apps" / "monitor" / "utils" / "sql_safe.py")


# ---------------------------------------------------------------------------
# H2/H3：settings.py 的 fail-fast / DEBUG 默认 False / ALLOWED_HOSTS 分支
# ---------------------------------------------------------------------------
class _ImproperlyConfigured(Exception):
    pass


def _make_config(env: dict):
    def _as_bool(v):
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def config(key, default=..., cast=lambda x: x):
        if cast is bool:
            cast = _as_bool  # 复刻 decouple：cast=bool 解析 "False"→False
        present = key in env and env[key] is not None
        if not present:
            if default is ...:
                return None
            return cast(default) if default is not None else None
        return cast(env[key])

    return config


def _exec_settings_block(env: dict, *, expect_warning: bool = False):
    text = (_ROOT / "backend" / "settings.py").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("def parse_csv"))
    end = next(i for i, line in enumerate(lines) if "cast=parse_csv)" in line)
    src = "\n".join(lines[start : end + 1])
    ns = {"config": _make_config(env), "ImproperlyConfigured": _ImproperlyConfigured}
    if expect_warning:
        with pytest.warns(UserWarning):
            exec(src, ns)
    else:
        exec(src, ns)
    return ns


def test_h2_production_missing_secret_key_raises():
    # 生产（DEBUG=False）且未配置 SECRET_KEY → 必须拒绝启动（fail-fast）。
    with pytest.raises(_ImproperlyConfigured):
        _exec_settings_block({"DEBUG": "False"})


def test_h2_production_with_secret_key_ok():
    ns = _exec_settings_block({"DEBUG": "False", "SECRET_KEY": "prod-secret-xyz"})
    assert ns["SECRET_KEY"] == "prod-secret-xyz"
    assert ns["ALLOWED_HOSTS"] == ["localhost", "127.0.0.1"]


def test_h3_debug_true_allows_placeholder_and_wildcard_hosts():
    # 开发（DEBUG=True）缺失 SECRET_KEY 时仅告警 + 占位值，ALLOWED_HOSTS=['*']。
    ns = _exec_settings_block({"DEBUG": "True"}, expect_warning=True)
    assert ns["SECRET_KEY"] == "django-insecure-dev-placeholder-change-me"
    assert ns["ALLOWED_HOSTS"] == ["*"]


def test_h3_debug_false_default_no_wildcard():
    # 生产（DEBUG 默认 False，且已配置 SECRET_KEY）→ 不应使用通配符。
    ns = _exec_settings_block({"SECRET_KEY": "x"})
    assert ns["DEBUG"] is False  # 默认关闭
    assert ns["ALLOWED_HOSTS"] == ["localhost", "127.0.0.1"]


def test_h2_old_weak_default_removed():
    text = (_ROOT / "backend" / "settings.py").read_text(encoding="utf-8")
    assert "django-insecure-your-secret-key-here" not in text
    assert "SECRET_KEY = config('SECRET_KEY', default=None)" in text


# ---------------------------------------------------------------------------
# H8：require_auth fail-closed
# ---------------------------------------------------------------------------
def test_h8_fail_closed_when_no_token_and_not_disabled(monkeypatch):
    # monkeypatch：用例结束自动还原，避免把 auth_token 泄漏给后续用例（曾致 test_p5 全 401）
    monkeypatch.setattr(app, "settings", load_settings(env={"AUTH_TOKEN": "", "AUTH_DISABLED": ""}))
    with pytest.raises(app.UnauthorizedError):
        require_auth(x_auth_token=None)


def test_h8_disabled_allows_without_token(monkeypatch):
    # monkeypatch：用例结束自动还原，避免把 auth_token 泄漏给后续用例（曾致 test_p5 全 401）
    monkeypatch.setattr(
        app, "settings", load_settings(env={"AUTH_TOKEN": "", "AUTH_DISABLED": "1"})
    )
    assert require_auth(x_auth_token=None) is None


def test_h8_token_required_when_set(monkeypatch):
    # monkeypatch：用例结束自动还原，避免把 auth_token 泄漏给后续用例（曾致 test_p5 全 401）
    monkeypatch.setattr(
        app, "settings", load_settings(env={"AUTH_TOKEN": "secret", "AUTH_DISABLED": "1"})
    )
    assert require_auth(x_auth_token="secret") is None
    with pytest.raises(app.UnauthorizedError):
        require_auth(x_auth_token="wrong")


def test_h8_set_but_no_token_provided_is_rejected(monkeypatch):
    # 最关键：配置了 AUTH_TOKEN 但请求未带令牌 → 必须拒绝（旧逻辑在此处会误放行）。
    # monkeypatch：用例结束自动还原，避免把 auth_token 泄漏给后续用例（曾致 test_p5 全 401）
    monkeypatch.setattr(
        app, "settings", load_settings(env={"AUTH_TOKEN": "secret", "AUTH_DISABLED": ""})
    )
    with pytest.raises(app.UnauthorizedError):
        require_auth(x_auth_token=None)


# ---------------------------------------------------------------------------
# M11：幂等键补 selected_paths
# ---------------------------------------------------------------------------
def _req(**overrides) -> PipelineRequest:
    base = dict(
        local_path="/repo",
        repo_url="",
        mode="",
        base=None,
        target=None,
        scopes=[],
        changed_files=[],
        selected_paths=[],
        project_name="P",
    )
    base.update(overrides)
    return PipelineRequest(**base)


def test_m11_idem_key_includes_selected_paths():
    key_all = _idem_key_for_generate(_req(selected_paths=[]))
    key_subset = _idem_key_for_generate(_req(selected_paths=["a.py", "b.py"]))
    assert key_all != key_subset
    assert "a.py,b.py" in key_subset
    # 顺序无关：集合相同应得到相同键
    k1 = _idem_key_for_generate(_req(selected_paths=["b.py", "a.py"]))
    assert k1 == key_subset


def test_m11_different_subset_does_not_collide():
    # 同一仓库 + 不同勾选子集必须得到不同键（修复前会命中旧任务）。
    k1 = _idem_key_for_generate(_req(selected_paths=["a.py"]))
    k2 = _idem_key_for_generate(_req(selected_paths=["b.py"]))
    assert k1 != k2


# ---------------------------------------------------------------------------
# M4：token 写/读 统一字符串，兼容 bytes/str，非法转 ValueError
# ---------------------------------------------------------------------------
def test_m4_encode_decode_roundtrip():
    assert _TK.decode_exchange_user_id(_TK.encode_exchange_user_id(5)) == 5


def test_m4_decode_handles_bytes_and_str():
    assert _TK.decode_exchange_user_id(b"42") == 42
    assert _TK.decode_exchange_user_id("42") == 42


def test_m4_decode_none_raises_value_error():
    with pytest.raises(ValueError):
        _TK.decode_exchange_user_id(None)


def test_m4_decode_invalid_raises_value_error():
    with pytest.raises(ValueError):
        _TK.decode_exchange_user_id("not-an-int")


# ---------------------------------------------------------------------------
# H9：SQL 注入防护（纯函数 + 接入 probes 的集成验证）
# ---------------------------------------------------------------------------
def test_h9_identifier_validation():
    assert _SQL.is_safe_sql_identifier("users")
    assert _SQL.is_safe_sql_identifier("_t1")
    assert not _SQL.is_safe_sql_identifier("")
    assert not _SQL.is_safe_sql_identifier("1abc")
    assert not _SQL.is_safe_sql_identifier("a;b")


def test_h9_where_allows_legit_chinese_predicate():
    assert _SQL.is_safe_where_clause("")
    assert _SQL.is_safe_where_clause("status = '水雨情' AND level > 3")


def test_h9_where_blocks_injection():
    assert not _SQL.is_safe_where_clause("1=1; DROP TABLE x")
    assert not _SQL.is_safe_where_clause("x -- comment")
    assert not _SQL.is_safe_where_clause("x/*y*/z")
    assert not _SQL.is_safe_where_clause("UNION SELECT * FROM users")
    assert not _SQL.is_safe_where_clause("a\nOR 1=1")


def _load_probes_with_fake_pymysql():
    # 桩掉 probes 的第三方依赖，并注入假 pymysql，验证守卫"已接入函数体"。
    urllib3 = types.ModuleType("urllib3")
    urllib3.exceptions = types.ModuleType("urllib3.exceptions")
    urllib3.exceptions.InsecureRequestWarning = Warning
    urllib3.disable_warnings = lambda *a, **k: None
    sys.modules.setdefault("urllib3", urllib3)

    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    sys.modules.setdefault("requests", requests)

    jsonpath_ng = types.ModuleType("jsonpath_ng")
    jsonpath_ng.exceptions = types.ModuleType("jsonpath_ng.exceptions")
    jsonpath_ng.exceptions.JsonPathParserError = Exception
    jsonpath_ng.parse = lambda *a, **k: None
    sys.modules.setdefault("jsonpath_ng", jsonpath_ng)
    sys.modules.setdefault("jsonpath_ng.exceptions", jsonpath_ng.exceptions)

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return None

    class _FakeConn:
        def cursor(self, *a, **k):
            return _FakeCursor()

        def close(self):
            pass

    class FakePyMySQL:
        connect_calls: ClassVar[list] = []

        class cursors:
            DictCursor = object

        @staticmethod
        def connect(**kw):
            FakePyMySQL.connect_calls.append(kw)
            return _FakeConn()

    sys.modules["pymysql"] = FakePyMySQL

    # probes.py 用相对导入 `from .sql_safe import ...`，需把它放进真实包路径下加载。
    for pkg in ("apps", "apps.monitor", "apps.monitor.utils"):
        m = types.ModuleType(pkg)
        m.__path__ = []  # 标记为包
        sys.modules.setdefault(pkg, m)
    sql_path = _ROOT / "apps" / "monitor" / "utils" / "sql_safe.py"
    sql_spec = importlib.util.spec_from_file_location("apps.monitor.utils.sql_safe", sql_path)
    sql_mod = importlib.util.module_from_spec(sql_spec)
    sys.modules["apps.monitor.utils.sql_safe"] = sql_mod
    sql_spec.loader.exec_module(sql_mod)
    probes_path = _ROOT / "apps" / "monitor" / "utils" / "probes.py"
    probes_spec = importlib.util.spec_from_file_location("apps.monitor.utils.probes", probes_path)
    probes = importlib.util.module_from_spec(probes_spec)
    sys.modules["apps.monitor.utils.probes"] = probes
    probes_spec.loader.exec_module(probes)
    return probes, FakePyMySQL


def test_h9_guard_wired_into_sl651_and_blocks_injection():
    probes, fake_mysql = _load_probes_with_fake_pymysql()
    # 恶意表名 → 守卫在连接前拒绝，connect 不应被调用。
    fake_mysql.connect_calls.clear()
    bad = {
        "host": "h",
        "port": 3306,
        "user": "u",
        "password": "",
        "db": "d",
        "table": "sensor; DROP TABLE x",
        "time_field": "create_time",
    }
    ok, msg, _ = probes._sl651_db_check(bad)
    assert ok is False
    assert "非法" in msg
    assert fake_mysql.connect_calls == []

    # 合法配置 → 守卫放行，connect 被调用（证明守卫未误杀合法路径）。
    fake_mysql.connect_calls.clear()
    good = {
        "host": "h",
        "port": 3306,
        "user": "u",
        "password": "",
        "db": "d",
        "table": "sensor_data",
        "time_field": "create_time",
        "where_clause": "device_type = '水雨情'",
    }
    probes._sl651_db_check(good)
    assert len(fake_mysql.connect_calls) == 1


# ---------------------------------------------------------------------------
# M17：playwright 死代码已删除（回归守卫）
# ---------------------------------------------------------------------------
def test_m17_dead_code_removed():
    pw = (_ROOT / "apps" / "ui_automation" / "playwright_engine.py").read_text(encoding="utf-8")
    assert "js_result" not in pw
    assert "js_selector_code" not in pw
    # H9 守卫确实接入了 probes（而非停留在纯函数层）。
    probes_src = (_ROOT / "apps" / "monitor" / "utils" / "probes.py").read_text(encoding="utf-8")
    assert "is_safe_sql_identifier(table)" in probes_src
