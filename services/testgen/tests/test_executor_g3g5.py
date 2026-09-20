"""G-3（安全结论可信）/ G-5（异常流覆盖）单元测试。

覆盖三层：
1. 生成侧（tp_expand）：资源归属识别、异常流/越权/令牌过期维度展开、置信度与未验证标记；
2. 执行侧（executor）：限流突发判定、best-effort 维度诚实跳过、越权资源归属高置信归因；
3. 一致性：越权 note 不再「置信度较低」自贬，且包含资源实体。
"""

from __future__ import annotations

from core.contracts import CaseSpec, FunctionalPoint
from core.enums import AuthMode, Dimension, ExecStatus, FType, TPType
from engine import executor, tp_expand
from engine.tp_expand import _resource_entity_of


# ---------------------------------------------------------------------------
# 生成侧
# ---------------------------------------------------------------------------
def _fp(
    name: str, ftype: str = FType.API.value, title: str = "功能", module: str = "m"
) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-test", ftype=ftype, file_path="a.py", name=name, title=title, module=module
    )


def test_resource_entity_extraction():
    # 路径参数前一个名词段 = 归属资源
    assert _resource_entity_of(_fp("DELETE /api/v1/invoices/{invoice_id}")) == "invoices"
    # 无路径参数取最后一个名词段
    assert _resource_entity_of(_fp("GET /api/v1/reports")) == "reports"
    # 非 API 来源无资源归属语义
    assert _resource_entity_of(_fp("page /dashboard", FType.PAGE.value)) == ""


def test_api_plan_includes_abnormal_and_priv_and_token():
    fp = _fp("DELETE /api/v1/invoices/{invoice_id}")
    dims = {(c, d) for c, d in tp_expand.plan_of(fp)}
    assert (TPType.ABNORMAL.value, Dimension.RES_NOT_FOUND.value) in dims
    assert (TPType.ABNORMAL.value, Dimension.RATE_LIMIT.value) in dims
    assert (TPType.ABNORMAL.value, Dimension.IDEMPOTENT.value) in dims
    assert (TPType.ABNORMAL.value, Dimension.DEGRADED.value) in dims
    assert (TPType.SECURITY.value, Dimension.PRIV_ESC.value) in dims
    # G-3：令牌过期已纳入 API 计划
    assert (TPType.SECURITY.value, Dimension.TOKEN_EXPIRED.value) in dims


def test_priv_esc_carries_resource_and_owner_scoped():
    fp = _fp("DELETE /api/v1/invoices/{invoice_id}")
    tps = tp_expand.expand_functional_point(fp, tp_expand.ExpandContext())
    pe = [t for t in tps if t.dimension == Dimension.PRIV_ESC.value]
    assert len(pe) == 1
    # 资源归属透出
    assert pe[0].resource == "invoices"
    assert pe[0].owner_scoped is True
    # 期望文案从「操作该资源」升级为「操作他人 invoices 资源」
    assert "invoices" in pe[0].expect
    assert "他人" in pe[0].expect


def test_confidence_and_unverified_flags():
    fp = _fp("DELETE /api/v1/invoices/{invoice_id}")
    tps = tp_expand.expand_functional_point(fp, tp_expand.ExpandContext())
    by_dim = {t.dimension: t for t in tps}
    # 限流可突发自动验证，置信度高于纯故障注入类
    assert by_dim[Dimension.RATE_LIMIT.value].confidence == 0.7
    assert by_dim[Dimension.RATE_LIMIT.value].unverified is False
    # 需故障注入/双身份的维度：低置信 + 未验证
    for d in (
        Dimension.IDEMPOTENT.value,
        Dimension.DEGRADED.value,
        Dimension.TIMEOUT.value,
        Dimension.TOKEN_EXPIRED.value,
    ):
        assert by_dim[d].confidence == 0.4
        assert by_dim[d].unverified is True
    # 正常维度高置信且已验证
    assert by_dim[Dimension.AVAIL.value].confidence == 1.0
    assert by_dim[Dimension.AVAIL.value].unverified is False


def test_page_plan_includes_ui_abnormal():
    fp = _fp("page /dashboard", FType.PAGE.value)
    dims = {(c, d) for c, d in tp_expand.plan_of(fp)}
    assert (TPType.ABNORMAL.value, Dimension.UI_NETWORK_INTERRUPT.value) in dims
    assert (TPType.ABNORMAL.value, Dimension.UI_ERROR_DISPLAY.value) in dims
    assert (TPType.ABNORMAL.value, Dimension.UI_EMPTY_STATE.value) in dims
    assert (TPType.ABNORMAL.value, Dimension.UI_SERVER_ERROR.value) in dims


# ---------------------------------------------------------------------------
# 执行侧
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _FakeSession:
    """鸭子类型 requests.Session：按预设状态码序列返回。"""

    def __init__(self, statuses: list[int]) -> None:
        self._statuses = list(statuses)
        self._i = 0

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResp:
        s = self._statuses[self._i]
        self._i += 1
        return _FakeResp(s)

    def close(self) -> None:
        pass


def _case(  # noqa: PLR0913 - 测试用例构造器，参数多为可读性展开
    *,
    category: str,
    dimension: str,
    method: str = "GET",
    path: str = "/api/x",
    resource: str = "",
    auth_mode: str = AuthMode.REQUIRED.value,
) -> CaseSpec:
    return CaseSpec(
        tc_no="TP-test",
        title="t",
        ctype="api",
        case_type=category,
        steps=[
            {
                "layer": "接口",
                "method": method,
                "path": path,
                "dimension": dimension,
                "auth_mode": auth_mode,
                "resource": resource,
            }
        ],
    )


def test_rate_limit_burst_passes_on_429():
    opts = executor.ExecutorOptions(base_url="http://t")
    sess = _FakeSession([200, 200, 200, 200, 200, 200, 200, 429])
    res = executor.execute_case(
        _case(category=TPType.ABNORMAL.value, dimension=Dimension.RATE_LIMIT.value),
        opts,
        session=sess,
    )
    assert res.status == ExecStatus.PASS.value
    assert "429" in res.notes[0]


def test_rate_limit_skipped_when_no_429():
    opts = executor.ExecutorOptions(base_url="http://t")
    sess = _FakeSession([200] * executor._RATE_LIMIT_BURST)
    res = executor.execute_case(
        _case(category=TPType.ABNORMAL.value, dimension=Dimension.RATE_LIMIT.value),
        opts,
        session=sess,
    )
    # 未触发 429 → 诚实跳过（不判失败）
    assert res.status == ExecStatus.SKIPPED.value
    assert "未触发限流" in res.notes[0]


def test_best_effort_dims_skipped_not_failed():
    cases = [
        (TPType.ABNORMAL.value, Dimension.IDEMPOTENT.value, "POST", "幂等"),
        (TPType.ABNORMAL.value, Dimension.DEGRADED.value, "GET", "降级"),
        (TPType.ABNORMAL.value, Dimension.TIMEOUT.value, "GET", "超时"),
        (TPType.SECURITY.value, Dimension.TOKEN_EXPIRED.value, "GET", "令牌过期"),
    ]
    for category, dim, method, keyword in cases:
        opts = executor.ExecutorOptions(base_url="http://t", allow_write=True)
        res = executor.execute_case(_case(category=category, dimension=dim, method=method), opts)
        assert res.status == ExecStatus.SKIPPED.value, dim
        assert keyword in res.notes[0], dim


def test_priv_esc_with_resource_high_conf_note():
    opts = executor.ExecutorOptions(base_url="http://t", allow_write=True)
    sess = _FakeSession([403])
    res = executor.execute_case(
        _case(
            category=TPType.SECURITY.value,
            dimension=Dimension.PRIV_ESC.value,
            method="DELETE",
            path="/api/v1/invoices/1",
            resource="invoices",
        ),
        opts,
        session=sess,
    )
    assert res.status == ExecStatus.PASS.value
    joined = " ".join(res.notes)
    assert "invoices" in joined
    # G-3：不再「置信度较低」自贬，改为诚实归因
    assert "置信度较低" not in joined


def test_priv_esc_without_resource_honest_note():
    opts = executor.ExecutorOptions(base_url="http://t", allow_write=True)
    sess = _FakeSession([403])
    res = executor.execute_case(
        _case(
            category=TPType.SECURITY.value,
            dimension=Dimension.PRIV_ESC.value,
            method="DELETE",
            path="/api/v1/items/1",
            resource="",
        ),
        opts,
        session=sess,
    )
    assert res.status == ExecStatus.PASS.value
    joined = " ".join(res.notes)
    assert "资源归属未从路由识别" in joined
    assert "置信度较低" not in joined
