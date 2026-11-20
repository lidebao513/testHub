"""第一步 A+B · 业务函数语义回填与预期具体化。

A：业务函数无 docstring 时标题/验证项不再是英文裸名，而是「函数名 + 真实签名」
   的中文可读语义；有中文 docstring 时优先用 docstring。
B：业务函数的预期从放之四海皆准的空话，变成带上真实入参/返回类型的可判定命题。

两条硬约束（回归守护）：
- `FunctionalPoint.description` 仍为 `func() @ path:line`，供增量收窄解析；
- API / PAGE / UI 其他类型的行为不变（本改动只作用于 BUSINESS 维度）。
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import FunctionalPoint
from core.enums import Dimension, FType, TPType
from engine import fp_extract, tp_expand
from engine.scan import SourceFile


# 有明确入参与返回注解、无 docstring 的业务函数——截图里「看不出测什么」的典型。
_NO_DOC_SRC = """
def get_domain_detail(domain_id: str, top_n: int = 10) -> DomainDetail:
    return _lookup(domain_id, top_n)


def _lookup(domain_id, top_n):
    return None
"""

# 有中文 docstring 的业务函数——应直接用 docstring 做语义，签名作补充。
_CN_DOC_SRC = '''
def resolve_tenant(token: str) -> Tenant:
    """根据访问令牌解析出所属租户对象。"""
    return None
'''


def _fps(src: str, rel: str = "cockpit/api.py") -> list[FunctionalPoint]:
    files = {rel: SourceFile(rel=rel, abspath=Path(rel), text=src)}
    return fp_extract.extract_functional_points(files, include_business=True).functional_points


def _by_name(fps: list[FunctionalPoint], name: str) -> FunctionalPoint:
    return next(fp for fp in fps if fp.name == name)


# ---------------------------------------------------------------------------
# A · 语义回填
# ---------------------------------------------------------------------------
def test_signature_hint_extracts_args_and_return():
    fp = _by_name(_fps(_NO_DOC_SRC), "get_domain_detail")
    assert fp.signature_hint, "无 docstring 的函数必须有签名摘要"
    assert "入参 domain_id" in fp.signature_hint
    assert "top_n" in fp.signature_hint
    assert "DomainDetail" in fp.signature_hint
    # self/cls 不应出现在摘要里
    assert "self" not in fp.signature_hint and "cls" not in fp.signature_hint


def test_no_doc_semantic_is_readable_not_bare_name():
    fp = _by_name(_fps(_NO_DOC_SRC), "get_domain_detail")
    # 含中文 + 签名，且不再是纯英文裸名
    assert "入参" in fp.semantic and "domain_id" in fp.semantic
    assert fp.semantic != "get domain detail"


def test_cn_docstring_semantic_prefers_doc():
    fp = _by_name(_fps(_CN_DOC_SRC), "resolve_tenant")
    assert "解析出所属租户" in fp.semantic
    # 中文 docstring 直接做语义，签名不改变主体
    assert fp.semantic.startswith("根据访问令牌解析")


def test_testpoint_title_and_semantic_use_chinese_core():
    fps = _fps(_NO_DOC_SRC)
    tps = tp_expand.expand_all(fps)
    biz = [tp for tp in tps if tp.fp_contract_id == _by_name(fps, "get_domain_detail").fp_id]
    assert biz, "业务函数应能展开测试点"
    normal = next(tp for tp in biz if tp.category == TPType.NORMAL.value)
    # 标题主体含中文语义，不再是 `[正常] cockpit · get_domain_detail · ...`
    assert "入参" in normal.title
    assert "get_domain_detail" not in normal.title.split("·")[0] or "入参" in normal.title
    # 验证项与标题一致地用中文语义
    assert "入参" in normal.semantic
    assert "验证功能可用性" in normal.semantic


# ---------------------------------------------------------------------------
# B · 预期具体化
# ---------------------------------------------------------------------------
def test_business_expect_carries_real_signature():
    fp = _by_name(_fps(_NO_DOC_SRC), "get_domain_detail")
    expect = tp_expand._expect_of(fp, TPType.NORMAL.value, Dimension.BIZ_LOGIC.value)
    assert "domain_id" in expect and "DomainDetail" in expect


def test_all_four_biz_dimensions_are_concrete():
    fp = _by_name(_fps(_NO_DOC_SRC), "get_domain_detail")
    dims = [
        Dimension.BIZ_LOGIC.value,
        Dimension.BIZ_INPUT_VALID.value,
        Dimension.BIZ_BOUNDARY.value,
        Dimension.BIZ_ABNORMAL.value,
    ]
    for d in dims:
        expect = tp_expand._expect_of(fp, TPType.NORMAL.value, d)
        assert "domain_id" in expect, f"{d} 维度预期应带真实入参"


def test_expect_falls_back_to_base_without_signature():
    """无签名信息（v1.0 老数据）时预期退回固定话术，不抛异常。"""
    fp = FunctionalPoint(
        fp_id="FP-old",
        ftype=FType.BUSINESS.value,
        file_path="a.py",
        name="legacy",
        title="a · legacy",
    )
    expect = tp_expand._expect_of(fp, TPType.NORMAL.value, Dimension.BIZ_LOGIC.value)
    assert expect == tp_expand._BUSINESS_EXPECT[Dimension.BIZ_LOGIC.value]


# ---------------------------------------------------------------------------
# 硬约束回归
# ---------------------------------------------------------------------------
def test_description_format_unchanged():
    """增量收窄依赖 `func() @ path:line`，格式不得被语义回填破坏。"""
    fp = _by_name(_fps(_NO_DOC_SRC), "get_domain_detail")
    assert fp.description == "get_domain_detail() @ cockpit/api.py:2"


def test_signature_hint_default_empty_keeps_v1_contract():
    """带默认值的预留字段不影响 v1.0 构造与序列化。"""
    fp = FunctionalPoint(
        fp_id="FP-1", ftype=FType.BUSINESS.value, file_path="a.py", name="f", title="a · f"
    )
    assert fp.signature_hint == ""
    assert fp.to_dict()["contract_version"] == "1.0"
    assert "signature_hint" in fp.to_dict()


# ---------------------------------------------------------------------------
# 标题截断修复（长类名前缀）
# ---------------------------------------------------------------------------
_LONG_CLS_SRC = """
class DomainService:
    def get_domain_detail(self, domain_id: str, top_n: int = 10) -> DomainDetail:
        return None
"""


def test_long_class_prefix_stripped_from_title_not_truncated():
    """长类名前缀不应把标题截成半截参数名；剥掉类名后参数应完整显示。"""
    fp = _by_name(_fps(_LONG_CLS_SRC), "DomainService.get_domain_detail")
    core = tp_expand._readable_core(fp)
    assert "get_domain_detail" in core
    assert "domain_id" in core and "top_n" in core, "入参应完整，不被截成 `t…`"
    assert "DomainService" not in core, "类名前缀对测什么无帮助，应剥掉"
    assert "…" not in core, "不应出现截断省略号"


def test_long_class_prefix_title_and_semantic_consistent():
    """标题与验证项都走 _readable_core，剥前缀后应一致、无截断。"""
    fps = _fps(_LONG_CLS_SRC)
    fp = _by_name(fps, "DomainService.get_domain_detail")
    tps = tp_expand.expand_all(fps)
    biz = [tp for tp in tps if tp.fp_contract_id == fp.fp_id]
    assert biz
    normal = next(tp for tp in biz if tp.category == TPType.NORMAL.value)
    assert "…" not in normal.title.split("·")[0], "标题主体不应被截断"
    assert "top_n" in normal.title, "标题应含完整入参"
    assert "top_n" in normal.semantic, "验证项应含完整入参"
    assert "…" not in normal.semantic


def test_long_class_prefix_expect_still_full_signature():
    """剥类名只影响标题/验证项，预期仍带完整入参与返回类型。"""
    fp = _by_name(_fps(_LONG_CLS_SRC), "DomainService.get_domain_detail")
    expect = tp_expand._expect_of(fp, TPType.NORMAL.value, Dimension.BIZ_LOGIC.value)
    assert "domain_id" in expect and "DomainDetail" in expect


def test_short_name_semantic_unchanged():
    """无类名的语义保持原样，不被改动。"""
    fp = _by_name(_fps(_NO_DOC_SRC), "get_domain_detail")  # 无类名前缀
    core = tp_expand._readable_core(fp)
    assert core == fp.semantic, "无类名语义应原样返回"
    assert "…" not in core


def test_short_class_prefix_also_stripped_uniformly():
    """统一剥类名：即使不超长，带类名的标题也应剥掉前缀，风格与长名一致。"""
    src = """
class DomainService:
    def resolve_domain(self, name: str, use_cache: bool = True) -> str:
        return name
"""
    fp = _by_name(_fps(src), "DomainService.resolve_domain")
    core = tp_expand._readable_core(fp)
    assert "DomainService" not in core, "短类名前缀也应剥掉，保持风格统一"
    assert core.startswith("resolve_domain"), "应保留方法名"
    assert "name" in core and "use_cache" in core, "参数应完整"
    assert "…" not in core


def test_ui_element_title_uses_element_text_not_generic_semantic():
    """运行时 UI 元素标题必须带元素名：同页多个按钮的 semantic 前缀相同，
    若取 semantic 前 48 字会撞成同一标题；fp.title 把元素文字放最前，可区分。"""
    fp = FunctionalPoint(
        fp_id="FP-x1",
        ftype=FType.UI.value,
        file_path="runtime:area::home",
        name="area::home#button:深色模式",
        title="按钮「深色模式 黑底白字，夜间更护眼」@ 福享 Agent｜area::home",
        semantic="页面 area::home 可见按钮交互元素：文字=深色模式 黑底白字，夜间更护眼，动作=点击",
    )
    core = tp_expand._readable_core(fp)
    assert "深色模式" in core, "标题应保住元素文字"
    assert "可见按钮交互" not in core, "不应取通用 semantic 前缀"
