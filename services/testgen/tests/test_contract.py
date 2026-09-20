"""契约测试：编号稳定性、八要素、追溯键、取值归一。

这是「契约守门员」——任何破坏 v1.0 契约的改动都会在这里失败。
"""

from core.contracts import (
    CONTRACT_VERSION,
    EIGHT_ELEMENTS,
    CaseSpec,
    FunctionalPoint,
    TestPoint,
    fp_id_of,
    normalize_test_type,
    precondition_of,
    priority_of,
    tp_id_of,
)
from core.enums import FType, Tag, TPType


def test_contract_version_frozen():
    assert CONTRACT_VERSION == "1.0"


def test_fp_id_is_stable_and_path_sensitive():
    a = fp_id_of(FType.API.value, "billing/api.py", "GET /api/v1/invoices")
    b = fp_id_of(FType.API.value, "billing/api.py", "GET /api/v1/invoices")
    c = fp_id_of(FType.API.value, "billing/other.py", "GET /api/v1/invoices")
    assert a == b, "同一输入必须产出同一编号"
    assert a != c, "路径变化必须产出不同编号"
    assert a.startswith("FP-") and len(a) == 11


def test_tp_id_stable_and_category_sensitive():
    base = tp_id_of("FP-abc12345", TPType.NORMAL.value, area="GET /x")
    again = tp_id_of("FP-abc12345", TPType.NORMAL.value, area="GET /x")
    other = tp_id_of("FP-abc12345", TPType.SECURITY.value, area="GET /x")
    assert base == again
    assert base != other
    assert base.startswith("TP-")


def test_tp_id_not_positional():
    """回归守护：编号不得随「枚举序号」变化（修复契约缺陷 D-2）。"""
    first = tp_id_of("FP-1", "正常", area="a")
    second = tp_id_of("FP-1", "正常", area="a", ordinal=1)
    assert first != second, "同一功能点的多条同构测试点需可消歧"


def test_normalize_test_type_accepts_legacy_alias():
    assert normalize_test_type("新增") == Tag.UPDATE.value
    assert normalize_test_type("更新") == Tag.UPDATE.value
    assert normalize_test_type("全量") == Tag.FULL.value
    assert normalize_test_type(None) == Tag.FULL.value


def test_priority_rule():
    assert priority_of(TPType.ABNORMAL.value, "任意") == "P1"
    assert priority_of(TPType.SECURITY.value, "任意") == "P1"
    assert priority_of(TPType.BOUNDARY.value, "任意") == "P1"
    assert priority_of(TPType.NORMAL.value, "用户登录") == "P1"
    assert priority_of(TPType.NORMAL.value, "报表") == "P2"


def test_precondition_rule():
    assert "非法输入的构造方式" in precondition_of(TPType.ABNORMAL.value)
    assert "token" in precondition_of(TPType.NORMAL.value)


def _case(**overrides) -> CaseSpec:
    base = {
        "tc_no": "TP-0001",
        "title": "[正常] 查询列表",
        "ctype": "api",
        "steps": [{"expect": "返回 2xx"}],
        "module": "billing",
        "case_type": TPType.NORMAL.value,
        "priority": "P2",
        "precondition": "服务已启动",
        "doc_steps": [{"seq": 1}],
    }
    base.update(overrides)
    return CaseSpec(**base)  # type: ignore[arg-type]


def test_eight_elements_complete():
    assert len(EIGHT_ELEMENTS) == 8
    assert _case().missing_elements() == []


def test_missing_element_detected():
    assert "precondition" in _case(precondition="").missing_elements()


def test_to_dict_carries_version_and_expect():
    payload = _case().to_dict()
    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["expect"] == "返回 2xx"


def test_functional_point_and_test_point_roundtrip():
    fp = FunctionalPoint(
        fp_id="FP-1",
        ftype=FType.API.value,
        file_path="a/b.py",
        name="GET /x",
        title="查询",
    )
    tp = TestPoint(tp_id="TP-1", fp_contract_id=fp.fp_id, category=TPType.NORMAL.value)
    assert tp.to_dict()["contract_version"] == CONTRACT_VERSION
    assert tp.to_dict()["evidence"] == []  # v1.1 字段有缺省值，不破坏 v1.0 消费方
