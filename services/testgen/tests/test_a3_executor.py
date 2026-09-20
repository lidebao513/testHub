"""A3 · 接口层执行器（http_probe）契约测试。

为什么用假会话而不是真网络：执行器的**判定逻辑**是本轮交付的核心，必须可在无网环境
下逐条断言；真连被测环境由 CLI `pipeline --url ... --execute` 承担。

覆盖点：
- 各行为维度的判定与 `tp_expand._expect_of` 的预期文案一一对应；
- 「鉴权缺失」维度必须**不带**凭证发请求（否则测不出鉴权）；
- 写操作默认不执行（防污染被测环境），放行后才执行；
- 不可执行的情形一律 `skipped` 且带原因，**绝不伪装成通过**；
- 凭证不得出现在结论 / 备注里。
"""

from __future__ import annotations

from core.contracts import CaseSpec
from core.enums import ExecStatus, VerifyLayer
from engine import executor


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "{}"


class _FakeSession:
    """记录每次请求的假会话（不发真实网络请求）。"""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse(self.status_code)

    def close(self) -> None:
        pass


def _case(
    *,
    category: str = "正常",
    dimension: str = "正常-可用性",
    method: str = "GET",
    path: str = "/api/v1/invoices",
    layer: str = "接口",
) -> CaseSpec:
    return CaseSpec(
        tc_no="TP-00000001",
        title="[正常] 查询账单",
        ctype="api",
        steps=[
            {
                "action": "http_probe",
                "layer": layer,
                "kind": "api",
                "method": method,
                "path": path,
                "dimension": dimension,
                "expect": "e",
            }
        ],
        case_type=category,
    )


def _run(case: CaseSpec, status_code: int = 200, **opts_kwargs: object):
    session = _FakeSession(status_code)
    options = executor.ExecutorOptions(base_url="http://api.local", **opts_kwargs)  # type: ignore[arg-type]
    return executor.execute_case(case, options, session=session), session


# ---------------------------------------------------------------- 维度判定
def test_normal_dimension_passes_on_2xx():
    result, session = _run(_case())
    assert result.status == ExecStatus.PASS.value
    assert session.calls[0]["url"] == "http://api.local/api/v1/invoices"


def test_normal_dimension_fails_on_5xx():
    result, _ = _run(_case(), status_code=500)
    assert result.status == ExecStatus.FAIL.value
    assert "未返回成功（500）" in result.notes[0]


def test_boundary_dimension_passes_on_422_and_fails_on_200():
    boundary = _case(category="边界", dimension="边界-参数缺失/非法")
    assert _run(boundary, status_code=422)[0].status == ExecStatus.PASS.value
    assert _run(boundary, status_code=200)[0].status == ExecStatus.FAIL.value


def test_abnormal_dimension_passes_on_4xx():
    abnormal = _case(category="异常", dimension="异常-资源不存在", path="/api/v1/invoices/{id}")
    assert _run(abnormal, status_code=404)[0].status == ExecStatus.PASS.value


def test_auth_missing_dimension_sends_no_credentials():
    """鉴权缺失维度**必须**不带凭证：带了就测不出「无凭证被拒」。"""
    security = _case(category="安全", dimension="安全-鉴权缺失")
    result, session = _run(security, status_code=401, auth_token="dummy-bearer-token")
    assert result.status == ExecStatus.PASS.value
    headers = session.calls[0]["headers"]
    assert headers == {}, "无凭证请求不得携带 Authorization 头"
    assert "dummy-bearer-token" not in str(session.calls[0])


def test_auth_missing_dimension_fails_when_open():
    security = _case(category="安全", dimension="安全-鉴权缺失")
    result, _ = _run(security, status_code=200)
    assert result.status == ExecStatus.FAIL.value
    assert "未被拒绝" in result.notes[0]


def test_auth_missing_dimension_accepts_login_redirect():
    security = _case(category="安全", dimension="安全-鉴权缺失")
    result, _ = _run(security, status_code=302)
    assert result.status == ExecStatus.PASS.value
    assert "重定向" in result.notes[0]


def test_privilege_escalation_dimension_flags_low_confidence():
    esc = _case(category="安全", dimension="安全-越权", method="DELETE", path="/api/v1/x/{id}")
    result, session = _run(esc, status_code=403, auth_token="dummy-bearer-token", allow_write=True)
    assert result.status == ExecStatus.PASS.value
    assert "Authorization" in session.calls[0]["headers"], "越权维度需带本人凭证"
    # G-3：越权结论改为诚实归因（资源归属未识别时说明需双身份复测），不再「置信度较低」自贬
    assert all("置信度较低" not in n for n in result.notes)
    assert any("资源归属未从路由识别" in n or "鉴权边界" in n for n in result.notes)
    assert "dummy-bearer-token" not in str(result.notes), "结论文本不得回显凭证"


# ---------------------------------------------------------------- 路径参数
def test_path_parameter_is_materialized_and_noted():
    case = _case(path="/api/v1/invoices/{invoice_id}")
    result, session = _run(case)
    assert session.calls[0]["url"] == "http://api.local/api/v1/invoices/1"
    assert any("路径参数已用占位值" in n for n in result.notes)


# ---------------------------------------------------------------- 不执行的情形
def test_ui_layer_without_session_is_skipped_with_reason():
    """F13 后 UI 层会真开浏览器：没有会话时必须如实 skipped，且**绝不**伪装通过。"""
    result, session = _run(_case(layer=VerifyLayer.UI.value))
    assert result.status == ExecStatus.SKIPPED.value
    assert "不等于通过" in result.notes[0]
    assert session.calls == [], "UI 层不得误发 HTTP 请求"


def test_business_function_is_skipped_with_reason():
    result, _ = _run(_case(method="FUNC", path="calculate_total"))
    assert result.status == ExecStatus.SKIPPED.value
    assert "业务函数" in result.notes[0]


def test_write_methods_are_skipped_by_default():
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        result, session = _run(_case(method=method, path="/api/v1/invoices"))
        assert result.status == ExecStatus.SKIPPED.value, method
        assert "写操作" in result.notes[0]
        assert session.calls == [], f"{method} 默认不得真发请求"


def test_write_methods_run_when_explicitly_allowed():
    result, session = _run(_case(method="POST"), status_code=201, allow_write=True)
    assert result.status == ExecStatus.PASS.value
    assert session.calls[0]["json"] == {}


def test_missing_base_url_is_skipped():
    session = _FakeSession(200)
    result = executor.execute_case(_case(), executor.ExecutorOptions(), session=session)
    assert result.status == ExecStatus.SKIPPED.value
    assert "未提供被测地址" in result.notes[0]
    assert session.calls == []


# ---------------------------------------------------------------- 异常与汇总
def test_network_error_becomes_error_conclusion_not_exception():
    class _Boom:
        def request(self, *_a: object, **_k: object) -> None:
            raise ConnectionError("refused")

    result = executor.execute_case(
        _case(), executor.ExecutorOptions(base_url="http://x"), session=_Boom()
    )
    assert result.status == ExecStatus.ERROR.value
    assert "ConnectionError" in result.notes[0]


def test_summarize_counts_every_bucket():
    results = [
        executor.ExecutionResult(tc_no="a", status=ExecStatus.PASS.value),
        executor.ExecutionResult(tc_no="b", status=ExecStatus.FAIL.value),
        executor.ExecutionResult(tc_no="c", status=ExecStatus.ERROR.value),
        executor.ExecutionResult(tc_no="d", status=ExecStatus.SKIPPED.value),
    ]
    summary = executor.summarize(results)
    assert summary["total"] == 4
    assert summary["executed"] == 3
    assert (summary["pass"], summary["fail"], summary["error"], summary["skipped"]) == (1, 1, 1, 1)
    assert summary["results"][0]["tc_no"] == "a"


def test_execute_all_uses_one_session_and_never_leaks_token(monkeypatch):
    """批量执行复用单一会话（连接复用），且结论里不出现 token。"""
    sessions: list[_FakeSession] = []

    def fake_new_session() -> _FakeSession:
        session = _FakeSession(200)
        sessions.append(session)
        return session

    monkeypatch.setattr(executor, "_new_session", fake_new_session)
    cases = [_case(), _case(method="POST", path="/api/v1/x")]
    summary = executor.execute_all(
        cases,
        executor.ExecutorOptions(base_url="http://api.local", auth_token="dummy-bearer-token"),
    )
    assert len(sessions) == 1, "必须只建一个会话"
    assert summary["total"] == 2
    assert "dummy-bearer-token" not in str(summary)
