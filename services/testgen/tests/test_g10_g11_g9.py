"""G-10（凭证/OTP 生命周期）/ G-11（报告导出对齐）/ G-9（双身份越权复测通道）测试守卫。

全部用桩驱动，不依赖浏览器 / 网络 / 可选第三方依赖（reportlab/docx/openpyxl）。
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

import pytest

from core.contracts import CaseSpec
from core.enums import Dimension, ReportFormat, TPType
from engine import tenant_retest
from engine.runtime_ui import RuntimeUiOptions, _fetch_otp
from output.report_writer import ReportExportError, export_report


# ===========================================================================
# G-10 凭证 / OTP 生命周期
# ===========================================================================
def test_fetch_otp_static_returns_configured() -> None:
    opts = RuntimeUiOptions(login_otp="123456")
    assert _fetch_otp(opts, refresh=False) == "123456"
    # refresh=True 但无刷新命令，回退静态 OTP
    assert _fetch_otp(opts, refresh=True) == "123456"


def test_fetch_otp_refresh_runs_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    # 用 monkeypatch 隔离 subprocess（沙箱内真实执行命令不可靠），验证刷新解析逻辑
    class _FakeCompleted:
        stdout = "654321\n"

    opts = RuntimeUiOptions(login_otp="000000", login_otp_refresh_cmd="echo 654321")
    # refresh=False 仍取静态
    assert _fetch_otp(opts, refresh=False) == "000000"
    # refresh=True 执行命令取首行（隔离 subprocess 真实执行，验证解析逻辑）
    import engine.runtime_ui as _ru

    monkeypatch.setattr(_ru.subprocess, "run", lambda *a, **k: _FakeCompleted())
    assert _fetch_otp(opts, refresh=True) == "654321"


def test_fetch_otp_refresh_cmd_fails_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # 刷新命令执行抛异常 → 失败回退空串（刷新失败不致命）
    import engine.runtime_ui as _ru

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("cmd not found")

    monkeypatch.setattr(_ru.subprocess, "run", _boom)
    opts = RuntimeUiOptions(login_otp="", login_otp_refresh_cmd="nonexistent_otp_cmd_xyz")
    assert _fetch_otp(opts, refresh=True) == ""


def test_login_options_carry_otp_refresh_cmd() -> None:
    from engine.runtime_ui import options_from_settings

    settings = SimpleNamespace(
        runtime_login_user="u",
        runtime_login_password="p",
        runtime_login_otp="",
        runtime_login_otp_refresh_cmd="echo newotp",
        runtime_login_url="",
        runtime_base_url="http://x",
        runtime_ui_base_url="http://x",
        runtime_routes=[],
        runtime_max_pages=10,
        playwright_headless=True,
        playwright_channel="msedge",
        runtime_ui_timeout=30,
        runtime_auth_token="",
        runtime_ui_mobile_enabled=False,
    )
    opts = options_from_settings(settings)  # type: ignore[arg-type]
    assert opts.login_otp_refresh_cmd == "echo newotp"


# ===========================================================================
# G-9 双身份越权复测通道
# ===========================================================================
def _case(
    *,
    tc_no: str,
    dims: list[str],
    method: str = "GET",
    path: str = "/invoices/{id}",
    case_type: str = TPType.SECURITY.value,
) -> CaseSpec:
    steps = [{"dimension": d, "method": method, "path": path, "expect": "应被拒"} for d in dims]
    return CaseSpec(
        tc_no=tc_no,
        title=f"用例{tc_no}",
        ctype="api",
        steps=steps,
        case_type=case_type,
        doc_steps=steps,
    )


class _FakeSession:
    """实现 RetestSession 协议：记录请求，按预设返回。"""

    def __init__(
        self, *, get_json: Any = None, other_status: int = 403, other_json: Any = None
    ) -> None:
        self._get_json = get_json if get_json is not None else {"id": "r-1"}
        self._other_status = other_status
        self._other_json = other_json
        self.calls: list[tuple[str, str]] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, json: Any = None
    ) -> tuple[int, Any]:
        self.calls.append((method, url))
        # 列表接口（以 /invoices 结尾，无具体 id）返回属主数据；其余返回他人侧预设
        if method == "GET" and url.rstrip("/").endswith("/invoices"):
            return 200, self._get_json
        return self._other_status, self._other_json


def test_is_tenant_case_filters() -> None:
    assert tenant_retest.is_tenant_case(_case(tc_no="t1", dims=[Dimension.TENANT_READ.value]))
    assert tenant_retest.is_tenant_case(_case(tc_no="t2", dims=[Dimension.TENANT_WRITE.value]))
    assert not tenant_retest.is_tenant_case(_case(tc_no="t3", dims=[Dimension.PRIV_ESC.value]))
    assert not tenant_retest.is_tenant_case(
        _case(tc_no="t4", dims=["x"], case_type=TPType.NORMAL.value)
    )


def test_tenant_cases_of_from_mixed() -> None:
    cases = [
        _case(tc_no="a", dims=[Dimension.TENANT_READ.value]),
        _case(tc_no="b", dims=[Dimension.PRIV_ESC.value]),
        _case(tc_no="c", dims=[Dimension.TENANT_WRITE.value]),
    ]
    picked = tenant_retest.tenant_cases_of(cases)
    assert {c.tc_no for c in picked} == {"a", "c"}


def test_retest_skipped_without_other_identity() -> None:
    owner = _FakeSession()
    r = tenant_retest.retest_case(
        _case(tc_no="a", dims=[Dimension.TENANT_READ.value]), owner, None, "http://x"
    )
    assert r["result"] == "skipped"
    assert "无他人凭证" in r["reason"]


def test_retest_read_pass() -> None:
    owner = _FakeSession(get_json={"id": "r-9"})
    other = _FakeSession(other_status=403, other_json={"error": "forbidden"})
    r = tenant_retest.retest_case(
        _case(tc_no="a", dims=[Dimension.TENANT_READ.value]), owner, other, "http://x"
    )
    assert r["result"] == "pass"
    assert r["status"] == 403
    assert r["resource_id"] == "r-9"
    # 他人 GET 命中 /invoices/r-9
    assert any("invoices/r-9" in u for _, u in other.calls)


def test_retest_read_fail_when_other_data_leaks() -> None:
    # 他人可读到属主数据（响应体含属主 id）→ 越权泄露
    owner = _FakeSession(get_json={"id": "r-9"})
    other = _FakeSession(other_status=200, other_json={"id": "r-9", "name": "owner"})
    r = tenant_retest.retest_case(
        _case(tc_no="a", dims=[Dimension.TENANT_READ.value]), owner, other, "http://x"
    )
    assert r["result"] == "fail"
    assert "越权泄露" in r["reason"]


def test_retest_write_pass() -> None:
    owner = _FakeSession(get_json={"id": "r-9"})
    other = _FakeSession(other_status=403)
    r = tenant_retest.retest_case(
        _case(tc_no="w", dims=[Dimension.TENANT_WRITE.value], method="PUT"),
        owner,
        other,
        "http://x",
    )
    assert r["result"] == "pass"
    assert r["status"] == 403


def test_retest_write_fail() -> None:
    owner = _FakeSession(get_json={"id": "r-9"})
    other = _FakeSession(other_status=200)
    r = tenant_retest.retest_case(
        _case(tc_no="w", dims=[Dimension.TENANT_WRITE.value], method="PUT"),
        owner,
        other,
        "http://x",
    )
    assert r["result"] == "fail"


def test_retest_all_counts() -> None:
    owner = _FakeSession(get_json={"id": "r-9"})
    other = _FakeSession(other_status=403)
    cases = [
        _case(tc_no="a", dims=[Dimension.TENANT_READ.value]),
        _case(tc_no="w", dims=[Dimension.TENANT_WRITE.value], method="PUT"),
        _case(tc_no="b", dims=[Dimension.PRIV_ESC.value]),
    ]
    out = tenant_retest.retest_all(cases, owner, other, "http://x")
    assert out["total_tenant_cases"] == 2
    assert out["counts"]["pass"] == 2
    assert out["counts"]["skipped"] == 0


# ===========================================================================
# G-11 报告导出对齐
# ===========================================================================
def test_report_format_has_optional_exports() -> None:
    assert ReportFormat.PDF.value == "pdf"
    assert ReportFormat.WORD.value == "docx"
    assert ReportFormat.EXCEL.value == "xlsx"
    assert "pdf" in set(__import__("core.enums", fromlist=["REPORT_FORMATS"]).REPORT_FORMATS)


def test_export_unsupported_format_raises() -> None:
    with pytest.raises(ReportExportError):
        export_report({"exec_summary": {}}, "xyz", "out.xyz")


def test_export_pdf_honest_failure_when_dep_missing(tmp_path: Any) -> None:
    body = {"exec_summary": {"headline": "x"}}
    if importlib.util.find_spec("reportlab") is None:
        with pytest.raises(ReportExportError) as exc:
            export_report(body, "pdf", str(tmp_path / "REPORT.pdf"))
        assert "reportlab" in str(exc.value)
    else:
        out = export_report(body, "pdf", str(tmp_path / "REPORT.pdf"))
        assert (tmp_path / "REPORT.pdf").exists() or out.endswith(".pdf")


def test_export_xlsx_honest_failure_when_dep_missing(tmp_path: Any) -> None:
    body = {"exec_summary": {"headline": "x"}}
    if importlib.util.find_spec("openpyxl") is None:
        with pytest.raises(ReportExportError):
            export_report(body, "xlsx", str(tmp_path / "REPORT.xlsx"))
    else:
        export_report(body, "xlsx", str(tmp_path / "REPORT.xlsx"))


def test_report_writer_exports_deps_map() -> None:
    deps = __import__(
        "core.enums", fromlist=["REPORT_FORMAT_OPTIONAL_DEPS"]
    ).REPORT_FORMAT_OPTIONAL_DEPS
    assert deps["pdf"] == "reportlab"
    assert deps["docx"] == "python-docx"
    assert deps["xlsx"] == "openpyxl"
