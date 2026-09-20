"""G-7（移动端/响应式）/ G-8（性能/并发）/ G-9（多租户数据隔离）单元测试。

全部用桩驱动，不依赖浏览器/网络：
- 生成侧（tp_expand）：新增维度展开、置信度/未验证标记；
- 执行侧（executor）：性能基线真实测延迟、并发/跨租户诚实跳过；
- 运行时（runtime_ui）：移动端视口发现去重与 /m/* 路由发现。
"""

from __future__ import annotations

from core.contracts import CaseSpec, FunctionalPoint
from core.enums import FULL_SCOPE, AuthMode, Dimension, ExecStatus, FType, TPType
from engine import executor, tp_expand
from engine.runtime_ui import (
    RuntimeUiOptions,
    RuntimeUiResult,
    UiElement,
    _discover_mobile,
    _Session,
)


# ---------------------------------------------------------------------------
# 生成侧
# ---------------------------------------------------------------------------
def _fp(
    name: str, ftype: str = FType.API.value, title: str = "功能", module: str = "m"
) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-test", ftype=ftype, file_path="a.py", name=name, title=title, module=module
    )


def test_perf_dims_in_api_plan():
    # G-8：每个 API 功能点都追加性能基线 + 并发维度（独立行为维度「性能」）
    for name in ("GET /a", "GET /a/{id}", "DELETE /a/{id}"):
        dims = {(c, d) for c, d in tp_expand.plan_of(_fp(name))}
        assert (TPType.PERFORMANCE.value, Dimension.PERF_BASELINE.value) in dims
        assert (TPType.PERFORMANCE.value, Dimension.PERF_CONCURRENCY.value) in dims


def test_tenant_read_write_in_api_plan():
    # G-9：含路径参数的接口，按方法区分跨租户读/写
    read_dims = {(c, d) for c, d in tp_expand.plan_of(_fp("GET /invoices/{id}"))}
    assert (TPType.SECURITY.value, Dimension.TENANT_READ.value) in read_dims
    write_dims = {(c, d) for c, d in tp_expand.plan_of(_fp("DELETE /invoices/{id}"))}
    assert (TPType.SECURITY.value, Dimension.TENANT_WRITE.value) in write_dims
    # 读接口不应出现跨租户写（写操作专属）
    assert (TPType.SECURITY.value, Dimension.TENANT_WRITE.value) not in read_dims


def test_tenant_write_owner_scoped_and_unverified():
    # G-9：跨租户写用例标记属主隔离 + 低置信 + 未验证（需双身份复测）
    fp = _fp("DELETE /invoices/{invoice_id}")
    tps = tp_expand.expand_functional_point(fp, tp_expand.ExpandContext())
    tw = [t for t in tps if t.dimension == Dimension.TENANT_WRITE.value]
    assert len(tw) == 1
    assert tw[0].resource == "invoices"
    assert tw[0].owner_scoped is True
    assert tw[0].confidence == 0.4
    assert tw[0].unverified is True
    assert "他人" in tw[0].expect


def test_perf_baseline_measurable_not_best_effort():
    # G-8：性能基线可真实探测（高置信、已验证）；并发才需 harness（低置信）
    fp = _fp("GET /invoices/{id}")
    # 性能维度默认不在 DEFAULT_SCOPE，需显式含「性能」才展开
    tps = tp_expand.expand_functional_point(fp, tp_expand.ExpandContext(scopes=set(FULL_SCOPE)))
    by_dim = {t.dimension: t for t in tps}
    assert by_dim[Dimension.PERF_BASELINE.value].confidence == 1.0
    assert by_dim[Dimension.PERF_BASELINE.value].unverified is False
    assert by_dim[Dimension.PERF_CONCURRENCY.value].confidence == 0.4
    assert by_dim[Dimension.PERF_CONCURRENCY.value].unverified is True


# ---------------------------------------------------------------------------
# 执行侧
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _FakeSession:
    """鸭子类型 requests.Session：固定返回 200。"""

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResp:
        return _FakeResp(200)

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


def test_perf_baseline_measures_latency_and_passes():
    # G-8：性能基线真实发请求，可达即 PASS，note 含实测耗时
    opts = executor.ExecutorOptions(base_url="http://t")
    res = executor.execute_case(
        _case(category=TPType.PERFORMANCE.value, dimension=Dimension.PERF_BASELINE.value),
        opts,
        session=_FakeSession(),
    )
    assert res.status == ExecStatus.PASS.value
    assert any("基线耗时" in n for n in res.notes)


def test_perf_concurrency_honest_skip():
    # G-8：并发不串数据需并行压测 harness，常规单请求诚实跳过（不判失败）
    opts = executor.ExecutorOptions(base_url="http://t")
    res = executor.execute_case(
        _case(category=TPType.PERFORMANCE.value, dimension=Dimension.PERF_CONCURRENCY.value),
        opts,
        session=_FakeSession(),
    )
    assert res.status == ExecStatus.SKIPPED.value
    assert "并行压测" in res.notes[0]


def test_tenant_read_honest_skip():
    # G-9：跨租户读需双身份复测，单令牌诚实跳过
    opts = executor.ExecutorOptions(base_url="http://t")
    res = executor.execute_case(
        _case(
            category=TPType.SECURITY.value,
            dimension=Dimension.TENANT_READ.value,
            method="GET",
            path="/invoices/1",
            resource="invoices",
        ),
        opts,
        session=_FakeSession(),
    )
    assert res.status == ExecStatus.SKIPPED.value
    assert "双身份" in res.notes[0]


def test_tenant_write_honest_skip_with_allow_write():
    # G-9：跨租户写（需放行写操作才越过写守卫）仍因需双身份诚实跳过
    opts = executor.ExecutorOptions(base_url="http://t", allow_write=True)
    res = executor.execute_case(
        _case(
            category=TPType.SECURITY.value,
            dimension=Dimension.TENANT_WRITE.value,
            method="DELETE",
            path="/invoices/1",
            resource="invoices",
        ),
        opts,
        session=_FakeSession(),
    )
    assert res.status == ExecStatus.SKIPPED.value
    assert "双身份" in res.notes[0]


def test_perf_kind_routing():
    assert (
        executor._dimension_kind(TPType.PERFORMANCE.value, Dimension.PERF_BASELINE.value) == "perf"
    )


# ---------------------------------------------------------------------------
# 运行时：G-7 移动端发现
# ---------------------------------------------------------------------------
class _MobilePage:
    """桩页面：evaluate 对「元素抽取脚本」返回元素、对「/m/ 路由脚本」返回专属路由。"""

    def __init__(self, elements: list[dict[str, object]], m_routes: list[str]) -> None:
        self._elements = elements
        self._m_routes = m_routes

    def evaluate(self, js: str) -> object:
        if isinstance(js, str) and "/m/" in js:
            return list(self._m_routes)
        return list(self._elements)


def _mobile_session(elements, m_routes) -> _Session:
    return _Session(_MobilePage(elements, m_routes), context=object())


def test_discover_mobile_marks_and_dedupes():
    # G-7：移动专属元素标记 mobile=True，且与桌面元素按 selector 去重
    result = RuntimeUiResult(base_url="http://t")
    result.elements.append(UiElement(selector="button.shared", kind="button", visible=True))
    page_elements = [
        {"selector": "button.shared", "kind": "button", "text": "共享", "visible": True},
        {"selector": "button.m-only", "kind": "button", "text": "手机专属", "visible": True},
    ]
    opts = RuntimeUiOptions(mobile_enabled=True)
    added = _discover_mobile(_mobile_session(page_elements, []), opts, result, None)
    # 共享元素去重，仅手机专属增量计入
    assert added == 1
    m_only = [e for e in result.elements if e.selector == "button.m-only"]
    assert m_only and m_only[0].mobile is True
    shared = [e for e in result.elements if e.selector == "button.shared"]
    assert shared and shared[0].mobile is False


def test_discover_mobile_finds_m_routes():
    # G-7：发现 /m/* 专属路由
    result = RuntimeUiResult(base_url="http://t")
    page_elements: list[dict[str, object]] = []
    opts = RuntimeUiOptions(mobile_enabled=True)
    _discover_mobile(_mobile_session(page_elements, ["/m/home", "/m/profile"]), opts, result, None)
    assert "/m/home" in result.discovered_routes
    assert "/m/profile" in result.discovered_routes
    assert any("移动端发现完成" in n for n in result.notes)
