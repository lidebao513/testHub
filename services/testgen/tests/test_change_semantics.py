"""T3 语义维度单测。

背景：维度原本只由「来源类型」决定（页面 → 交互元素可用），于是「改分页/改去重/
改兜底」产出的用例全一样，用户看不出测什么。T3 从 diff hunk 识别变更语义，
把维度换成「这次到底改了什么」。

覆盖：
- 8 类语义各 1 个正例（真实改动片段，非编造关键词堆砌）；
- 8 类语义各 1 个反例（关键词只在**整行注释**里 → 不命中；只有 1 个关键词 → 不成立）；
- scope 过滤（只勾「正常」时语义里的异常维度不生成）；
- 维度上限（1 个核心类型维度保底 + 语义优先占位，总数 ≤ max_dims）；
- 开关 off（semantic_dims 为空）→ 与基线行为逐条一致；
- 灰度解析（off / on / project:30 / 非法值）。
"""

from pathlib import Path

import pytest

from core.config import ConfigError, Settings
from core.contracts import FunctionalPoint
from core.enums import Dimension, FType, TPType
from engine import change_semantics as CS
from engine import pipeline, tp_expand
from engine.scan import SourceFile


# 真实改动片段（取自 research-agent 21fb554c→31344820 的两个前端页面）
_PCSHELL = """\
  const createdSessionsRef = useRef<{ office: Map<string, ChatSession> }>({ office: new Map() });
  const handleSessionCreated = useCallback((session: ChatSession) => {
    createdSessionsRef.current[mode].set(session.id, session);
    setSessions((previous) => {
      if (previous.some((item) => item.id === session.id)) return previous;
      sessionsOffsetRef.current += 1;
      return [session, ...previous];
    });
  }, []);
  sessionsOffsetRef.current = result.fetchedCount + locallyCreated.length;
"""

_PCCHAT = """\
  onSessionCreated: (session: ChatSession) => void;
  const title = normalizedTitle.length > 24 ? `${normalizedTitle.slice(0, 24)}…` : normalizedTitle;
  onSessionCreated?.({ title: title || '未命名对话', mode: channel === 'daily_perf' ? 'daily_perf' : chatMode });
"""

_SERVER = """\
  "title": (_shorten_title(user_message) or "未命名对话") if user_message is not None else "新会话",
  if user_message is not None and (meta.get("title") in (None, "", "新会话")):
      meta["title"] = _shorten_title(user_message) or "未命名对话"
"""


def _tags(text: str, kind: str = "frontend") -> list[str]:
    return CS.tag_names(CS.detect(text, kind))


# ---------------------------------------------------------------------------
# 正例：8/8 语义在真实改动片段上命中
# ---------------------------------------------------------------------------
def test_pagination_detected_on_offset_sync_change() -> None:
    assert CS.TAG_PAGINATION in _tags(_PCSHELL)


def test_dedupe_detected_on_some_map_delete_change() -> None:
    assert CS.TAG_DEDUPE in _tags(_PCSHELL)


def test_list_mutation_detected_on_prepend_change() -> None:
    assert CS.TAG_LIST_MUTATION in _tags(_PCSHELL)


def test_branch_mode_detected_on_mode_ternary() -> None:
    assert CS.TAG_BRANCH_MODE in _tags(_PCCHAT)


def test_contract_shape_detected_on_props_type_change() -> None:
    assert CS.TAG_CONTRACT_SHAPE in _tags(_PCCHAT)


def test_fallback_detected_on_default_title() -> None:
    assert CS.TAG_FALLBACK in _tags(_PCCHAT)


def test_guard_detected_on_truncation_guard() -> None:
    assert CS.TAG_GUARD_NEGATIVE in _tags(_PCCHAT)


def test_auth_touch_detected_on_token_permission() -> None:
    text = "token = get_token(request)\nif not check_permission(token): raise HTTPException(403)"
    assert CS.TAG_AUTH_TOUCH in _tags(text, "backend")


# ---------------------------------------------------------------------------
# 反例：注释行 / 单关键词都不成立（误匹配 ≤20% 的第一道闸）
# ---------------------------------------------------------------------------
def test_comment_only_lines_are_ignored() -> None:
    rel = "frontend/app/src/pages/pc/Demo.tsx"
    src = "// 分页 offset 与 hasMore 需要同步，去重也别忘了\n"
    sf = SourceFile(rel=rel, abspath=Path(rel), text=src)

    text = CS.hunk_text(sf, [(1, 1)])

    assert text == ""
    assert CS.detect(text, "frontend") == []


def test_single_keyword_is_not_enough() -> None:
    assert CS.TAG_PAGINATION not in _tags("offset = 0", "frontend")
    assert CS.TAG_FALLBACK not in _tags("if x: return y or z", "backend")


def test_block_comment_continuation_is_ignored() -> None:
    """块注释的**折行**不带 `/*` 前缀，曾把注释里的「分页/新增一条」当成变更语义。"""
    rel = "frontend/app/src/pages/pc/Demo.tsx"
    src = (
        "  /* 首屏请求尚未返回时创建的新会话也需合并。\n"
        "     服务端按创建时间倒序分页，本地新增一条同步后移分页 offset，避免续页漏数据。 */\n"
        "  const next = [session, ...previous];\n"
    )
    sf = SourceFile(rel=rel, abspath=Path(rel), text=src)

    text = CS.hunk_text(sf, [(1, 3)])

    assert "分页" not in text and "新增一条" not in text
    assert text.strip() == "const next = [session, ...previous];"


def test_hunk_starting_inside_block_comment_is_ignored() -> None:
    """hunk 起点正好在块注释内部（首行没有 `/*`）→ 该行同样不算代码。"""
    rel = "frontend/app/src/pages/pc/Demo.tsx"
    src = (
        "/* 首屏请求尚未返回时创建的新会话也需合并。\n"
        "   服务端按创建时间倒序分页，本地新增一条同步后移分页 offset，避免续页漏数据。\n"
        "*/\n"
        "const a = 1;\n"
    )
    sf = SourceFile(rel=rel, abspath=Path(rel), text=src)

    text = CS.hunk_text(sf, [(2, 1)])  # 只取注释的第二行

    assert text == ""


def test_jsx_comment_continuation_is_ignored() -> None:
    """JSX 注释 `{/* … */}` 的续行以中文开头、以 `*/}` 结尾 → 不算代码。

    现场（PcShell.tsx:598-599）：续行里「…仍按路径重置错误边界」的「边界」把一次
    会话列表改动误判成「零值/空值守卫」语义。
    """
    rel = "frontend/app/src/pages/pc/Demo.tsx"
    src = (
        "            {/* 新会话取得真实 ID、会话间切换都属于同一聊天工作区，不能随 pathname\n"
        "                重挂载并清空本地消息与流式状态；其他页面仍按路径重置错误边界。 */}\n"
        "            <PcErrorBoundary key={k}>\n"
    )
    sf = SourceFile(rel=rel, abspath=Path(rel), text=src)

    assert CS.TAG_GUARD_NEGATIVE not in _tags(CS.hunk_text(sf, [(1, 2)]))
    assert "边界" not in CS.hunk_text(sf, [(1, 2)])


def test_model_word_is_not_branch_mode() -> None:
    # "model" 里含 "mode"，但不应被当成「分支模式改动」
    assert CS.TAG_BRANCH_MODE not in _tags("client = OpenAI(model=settings.llm_model)", "backend")


# ---------------------------------------------------------------------------
# 语义 → 维度
# ---------------------------------------------------------------------------
def test_dimensions_are_priority_ordered_and_capped() -> None:
    tags = CS.detect(_PCSHELL, "frontend")
    dims = CS.dimensions_of(tags, max_dims=3)

    assert dims == [
        (TPType.BOUNDARY.value, Dimension.PAGINATION_OFFSET_SYNC.value),
        (TPType.NORMAL.value, Dimension.PAGINATION_NO_OVERLAP.value),
        (TPType.NORMAL.value, Dimension.LIST_DEDUPE.value),
    ]


def test_unknown_language_keeps_both_kind_signals() -> None:
    # 未知语言族时不漏检：前端信号（ChatSession）与后端信号（meta[）都算
    tags = CS.detect('ChatSession = dict\nmeta["title"] = "x"\ntitle = 1', "")
    assert CS.TAG_CONTRACT_SHAPE in CS.tag_names(tags)


# ---------------------------------------------------------------------------
# 展开：scope 过滤 / 上限 / 核心维度保底 / 开关关闭
# ---------------------------------------------------------------------------
def _page_fp() -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP::PcShell",
        ftype=FType.PAGE.value,
        file_path="frontend/app/src/pages/pc/PcShell.tsx",
        name="frontend/app/src/pages/pc/PcShell.tsx",
        title="pc · 屏幕 PcShell",
        description="frontend/app/src/pages/pc/PcShell.tsx",
        has_route=False,
    )


def _dims_of(tps: list) -> list[tuple[str, str]]:
    return [(tp.category, tp.dimension) for tp in tps]


def _semantic_dims() -> list[tuple[str, str]]:
    return CS.dimensions_of(CS.detect(_PCSHELL, "frontend"), max_dims=3)


def test_expand_merges_semantic_with_core_dimension() -> None:
    fp = _page_fp()
    ctx = tp_expand.ExpandContext(
        scopes={"正常", "异常", "安全", "边界"},
        narrow_fp_ids={fp.fp_id},
        semantic_dims={fp.fp_id: _semantic_dims()},
    )
    tps = tp_expand.expand_functional_point(fp, ctx)

    dims = [d for _, d in _dims_of(tps)]
    # 1 个核心类型维度保底 + 3 个语义维度，总数 = 上限 4
    assert dims[0] == Dimension.INTERACTIVE.value
    assert len(dims) == 4
    assert Dimension.PAGINATION_OFFSET_SYNC.value in dims
    assert {tp.origin for tp in tps} == {"rule", "semantic"}


def test_scope_filters_semantic_abnormal_dims() -> None:
    fp = _page_fp()
    semantic = [
        (TPType.NORMAL.value, Dimension.LIST_DEDUPE.value),
        (TPType.ABNORMAL.value, Dimension.FALLBACK_DEFAULT.value),
    ]
    ctx = tp_expand.ExpandContext(scopes={TPType.NORMAL.value}, semantic_dims={fp.fp_id: semantic})
    tps = tp_expand.expand_functional_point(fp, ctx)

    assert all(tp.category == TPType.NORMAL.value for tp in tps)
    assert Dimension.FALLBACK_DEFAULT.value not in [d for _, d in _dims_of(tps)]


def test_semantic_dims_never_replace_core_dimension() -> None:
    fp = _page_fp()
    four = [
        (TPType.NORMAL.value, Dimension.LIST_DEDUPE.value),
        (TPType.NORMAL.value, Dimension.LIST_INSERT_VISIBLE.value),
        (TPType.NORMAL.value, Dimension.PAGINATION_NO_OVERLAP.value),
        (TPType.BOUNDARY.value, Dimension.GUARD_ZERO_VS_EMPTY.value),
    ]
    ctx = tp_expand.ExpandContext(semantic_dims={fp.fp_id: four})
    tps = tp_expand.expand_functional_point(fp, ctx)

    dims = [d for _, d in _dims_of(tps)]
    assert len(dims) == 4
    assert dims[0] == Dimension.INTERACTIVE.value  # 核心维度保底，不被语义挤掉


def test_switch_off_equals_baseline() -> None:
    """开关关闭（semantic_dims 为空）→ 与基线逐条一致（只有核心维度）。"""
    fp = _page_fp()
    off = tp_expand.expand_functional_point(
        fp,
        tp_expand.ExpandContext(scopes={"正常"}, narrow_fp_ids={fp.fp_id}),
    )

    assert _dims_of(off) == [(TPType.NORMAL.value, Dimension.INTERACTIVE.value)]


def test_semantic_origin_survives_rule_enrich() -> None:
    """回归：语义增强的 attach_evidence 曾把「无证据」测试点的 origin 统一改回 rule。

    现场：T3 首次跑生成时产物里 13 条测试点 origin 全是 rule（语义维度也变成 rule），
    因为 semantic_enrich 在 tp_expand 之后对无 evidence 的测试点覆盖了来源标签。
    来源标签是下游区分「本体 / 影响面」的唯一依据，必须保留。
    """
    from engine import semantic_enrich

    fp = _page_fp()
    ctx = tp_expand.ExpandContext(semantic_dims={fp.fp_id: _semantic_dims()})
    tps = tp_expand.expand_functional_point(fp, ctx)

    semantic_enrich.attach_evidence(tps, [fp])

    assert all(tp.evidence for tp in tps)
    assert any(tp.origin == "semantic" for tp in tps)


# ---------------------------------------------------------------------------
# 灰度开关解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("spec", "project", "expected"),
    [
        ("off", 30, False),
        ("on", 7, True),
        ("project:30", 30, True),
        ("project:30", 7, False),
        ("project:30,31", 31, True),
        (None, 30, True),  # 默认 project:30
    ],
)
def test_semantic_dims_gray_switch(spec, project, expected) -> None:
    s = Settings()
    if spec is not None:
        s.semantic_dims = spec
    assert s.semantic_dims_enabled(project) is expected


def test_semantic_dims_env_parsing() -> None:
    from core.config import load_settings

    assert load_settings({"SEMANTIC_DIMS": "project:30,31"}).semantic_dims == "project:30,31"
    assert load_settings({"TESTGEN_SEMANTIC_DIMS": "on"}).semantic_dims == "on"
    with pytest.raises(ConfigError):
        load_settings({"SEMANTIC_DIMS": "project:abc"})


# ---------------------------------------------------------------------------
# 编排：语义维度按文件归属到功能点
# ---------------------------------------------------------------------------
def test_semantic_dims_by_fp_maps_file_to_fp(monkeypatch) -> None:
    rel = "frontend/app/src/pages/pc/PcShell.tsx"
    fp = _page_fp()
    result = pipeline.PipelineResult(project_id=30, has_code=True)
    result.files = {rel: SourceFile(rel=rel, abspath=Path(rel), text=_PCSHELL)}
    result.functional_points = [fp]
    result.diff_hunks = {rel: [(1, len(_PCSHELL.splitlines()))]}

    class _Cfg:
        semantic_max_dims = 4

        @staticmethod
        def semantic_dims_enabled(project_id: int | None) -> bool:
            return project_id == 30

    monkeypatch.setattr(pipeline, "get_settings", lambda: _Cfg)
    opts = pipeline.PipelineOptions(mode="incremental")

    out = pipeline._semantic_dims_by_fp(opts, result)

    assert out[fp.fp_id] == _semantic_dims()
    assert result.counts["semantic_fp"] == 1
    assert any("变更语义" in n for n in result.notes)


def test_semantic_dims_by_fp_off_for_other_project(monkeypatch) -> None:
    rel = "frontend/app/src/pages/pc/PcShell.tsx"
    fp = _page_fp()
    result = pipeline.PipelineResult(project_id=999, has_code=True)
    result.files = {rel: SourceFile(rel=rel, abspath=Path(rel), text=_PCSHELL)}
    result.functional_points = [fp]
    result.diff_hunks = {rel: [(1, 1)]}

    class _Cfg:
        semantic_max_dims = 4

        @staticmethod
        def semantic_dims_enabled(project_id: int | None) -> bool:
            return project_id == 30

    monkeypatch.setattr(pipeline, "get_settings", lambda: _Cfg)
    opts = pipeline.PipelineOptions(mode="incremental")

    assert pipeline._semantic_dims_by_fp(opts, result) == {}
