"""编排测试：全量跑通、幂等对账、增量标签、产物落盘、防幻觉校验层。"""

import json
import re
from pathlib import Path

import pytest

from core import store
from core.config import get_settings
from core.contracts import FunctionalPoint
from core.enums import FType, Tag, TPType, is_http_method
from core.errors import EngineError
from engine import diff_tag, pipeline, semantic_enrich
from output.writer import OutputWriter


def _run(repo, **overrides):
    opts = pipeline.default_options(str(repo), mode=overrides.pop("mode", "full"))
    opts.scopes = {TPType.NORMAL.value, TPType.BOUNDARY.value}
    for k, v in overrides.items():
        setattr(opts, k, v)
    return pipeline.run_pipeline(opts)


def test_full_pipeline_end_to_end(fresh_db, sample_repo):
    result = _run(sample_repo)
    assert result.project_id
    assert result.counts["files"] > 0
    assert result.counts["functional_points"] > 0
    assert result.counts["test_points"] > 0
    assert result.counts["cases"] == result.counts["test_points"]
    assert result.traceability["orphan_tp_count"] == 0
    assert result.traceability["orphan_case_count"] == 0
    assert result.errors == []


def test_pipeline_is_idempotent(fresh_db, sample_repo):
    first = _run(sample_repo)
    assert first.case_stats["created"] > 0
    second = _run(sample_repo)
    assert second.case_stats["created"] == 0, "重复运行不得新增重复用例"
    assert second.case_stats["reused"] == first.case_stats["created"]
    assert second.case_stats["updated"] == 0


def test_removed_function_point_marks_case_obsolete(fresh_db, sample_repo):
    _run(sample_repo)
    api = sample_repo / "billing" / "api.py"
    text = api.read_text(encoding="utf-8")
    # 移除 POST 路由（连同其函数体），模拟「功能点消失」
    lines = text.splitlines()
    kept: list[str] = []
    skipping = False
    for ln in lines:
        if ln.startswith('@router.post("/invoices")'):
            skipping = True
            continue
        if skipping and ln.startswith("def "):
            skipping = False
        elif skipping:
            continue
        kept.append(ln)
    api.write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = _run(sample_repo)
    assert result.case_stats["obsolete"] > 0, "功能点消失后，其用例应转为 obsolete"


def test_incremental_mode_tags_update(fresh_db, git_repo):
    """真仓库增量：变更文件下的测试点必须标『更新』，且不得静默降级。

    回归要点：`refs_available` 曾用 `git rev-parse --verify --quiet A B` 校验两个 ref，
    而该命令一次只接受一个 revision → 永远判定不可用 → 全部静默标『全量』。
    """
    result = _run(git_repo, mode="incremental", base="HEAD~1", target="HEAD")

    assert not any("降级" in n for n in result.notes), f"增量通道被误降级：{result.notes}"
    assert any("增量通道" in n for n in result.notes)
    assert result.tag_summary.get(Tag.UPDATE.value, 0) > 0, "变更文件必须产出『更新』测试点"
    assert result.tag_summary.get(Tag.FULL.value, 0) > 0, "未变更文件仍应保留『全量』测试点"


def test_incremental_bad_ref_raises_instead_of_silent_full(fresh_db, git_repo):
    """F7 回归：显式给出却不可解析的 ref **必须报错**，不得静默降级为全量。

    历史缺陷：`except (ValueError, OSError)` 后降级为全量继续跑，于是「全部功能点都被
    标成全量」这个明显错误的结果**静默**流到下游（失效的 `test-20260906/07` 就是这类）。
    """
    with pytest.raises(EngineError) as exc:
        _run(git_repo, mode="incremental", base="nope", target="nope2")
    message = str(exc.value)
    assert "基线不可解析" in message
    assert "git rev-parse --verify" in message  # 报错必须给出可执行的修法


def test_incremental_without_base_is_loud_but_not_fatal(fresh_db, git_repo):
    """仅「未指定」基线时才降级为全量，且必须在备注里说清楚为什么。"""
    result = _run(git_repo, mode="incremental")
    assert any("未指定基线" in n for n in result.notes), result.notes
    assert result.tag_summary.get(Tag.UPDATE.value, 0) == 0
    assert result.counts["test_points"] > 0


def test_incremental_worktree_target_compares_working_tree(fresh_db, git_repo):
    """`--target WORKTREE`：与当前工作区比较（未提交改动也应标『更新』）。"""
    api = git_repo / "billing" / "api.py"
    api.write_text(
        api.read_text(encoding="utf-8")
        + '\n\n@router.get("/uncommitted")\n'
        + "def uncommitted_probe():\n"
        + '    """未提交的新接口。"""\n'
        + "    return {}\n",
        encoding="utf-8",
    )
    result = _run(git_repo, mode="incremental", base="HEAD", target=diff_tag.WORKTREE_TARGET)
    assert any("增量通道" in n for n in result.notes), result.notes
    assert not any("未指定基线" in n for n in result.notes)
    assert result.tag_summary.get(Tag.UPDATE.value, 0) > 0


def test_llm_design_enabled_reports_no_effect(fresh_db, sample_repo):
    """F10a/F10b：「配了以为生效」必须被消除——开关打开但能力未产出用例时明确上报。

    能力已实现（F10b），但开启后若 LLM 未配置 / 候选全被护栏拒绝，必须如实写进
    `result.errors` 说明「未新增任何 LLM 用例」，而非让人误以为生效。

    本测试为 hermetic：mock `engine.llm_design.design_cases` 返回空 added（等同 LLM 不可用），
    避免依赖真实 LLM 调用成败，且完全不触网；重点校验 stage_llm_design 的「诚实上报」逻辑。
    """
    from unittest.mock import patch

    from engine.llm_design import DesignResult

    get_settings().llm_design_enabled = True
    with patch(
        "engine.llm_design.design_cases",
        return_value=DesignResult(added=[], notes=["LLM 调用失败（mock 不可用）"]),
    ):
        result = _run(sample_repo)

    joined = " ".join(result.errors)
    assert "LLM 用例设计" in joined, result.errors
    assert "未新增任何 LLM 用例" in joined  # 必须说清「没生效」，而不是让人以为生效了
    assert "尚未实现" not in joined  # 能力已落地，不应再报「未实现」
    assert result.counts["cases"] == result.counts["test_points"], "不得凭空多出用例"


def test_evidence_attached_by_rule_engine(fresh_db, sample_repo):
    result = _run(sample_repo)
    assert all(tp.evidence for tp in result.test_points), "每条测试点都应有证据引用"
    assert all(tp.origin == "rule" for tp in result.test_points)
    assert all(tp.confidence == 1.0 for tp in result.test_points)


def test_all_ids_keep_contract_format(fresh_db, sample_repo):
    """所有产出编号必须严格符合契约格式（含规则凑对产生的变体）。

    回归要点：边界凑对曾用 `tp.tp_id + "-M"` 字符串拼接，产出 `TP-xxxxxxxx-M`，
    长度与格式都不符契约（D-10），任何按 `^TP-[0-9a-f]{8}$` 校验的下游都会整体拒收。
    """
    result = _run(sample_repo, scopes={TPType.NORMAL.value, TPType.BOUNDARY.value})
    fp_re = re.compile(r"^FP-[0-9a-f]{8}$")
    tp_re = re.compile(r"^TP-[0-9a-f]{8}$")
    assert result.functional_points
    assert result.test_points
    assert [fp.fp_id for fp in result.functional_points if not fp_re.match(fp.fp_id)] == []
    assert [tp.tp_id for tp in result.test_points if not tp_re.match(tp.tp_id)] == []


def test_boundary_pair_only_for_http_interfaces(fresh_db, sample_repo):
    """边界凑对只对接口类生效——页面路由没有「路径参数缺失」语义。"""
    result = _run(sample_repo, scopes={TPType.NORMAL.value, TPType.BOUNDARY.value})
    paired = [tp for tp in result.test_points if "参数缺失" in tp.title]
    assert paired, "接口边界测试点应凑对出「参数缺失」用例"
    assert all(is_http_method(tp.method) for tp in paired), "非 HTTP 来源不得凑对"


def test_outputs_written(fresh_db, sample_repo):
    result = _run(sample_repo)
    files = OutputWriter().write_all(
        result.project_id or 0, result.test_points, result.cases, result.to_dict()
    )
    payload = json.loads(Path(files["test_cases"]).read_text(encoding="utf-8"))
    assert payload["contract_version"] == "1.0"
    assert payload["count"] == len(result.cases)
    assert all(c["expect"] for c in payload["cases"])
    assert Path(files["markdown"]).read_text(encoding="utf-8").startswith("# 测试用例清单")
    assert json.loads(Path(files["summary"]).read_text(encoding="utf-8"))["mode"] == "full"


# ---------------------------------------------------------------- 校验层（防幻觉）
def _fp(name: str) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-x",
        ftype=FType.API.value,
        file_path="a.py",
        name=name,
        title=name,
    )


def test_validator_rejects_fabricated_endpoint():
    fps = [_fp("GET /api/v1/real")]
    ok, rejected = semantic_enrich.validate_candidates(
        [{"kind": "extra_case", "area": "GET /api/v1/fake", "fp_id": "FP-none"}], fps
    )
    assert ok == []
    assert "疑似臆造" in rejected[0]["reason"]


def test_validator_accepts_real_endpoint():
    fps = [_fp("GET /api/v1/real")]
    ok, rejected = semantic_enrich.validate_candidates(
        [{"kind": "extra_case", "area": "GET /api/v1/real", "fp_id": "FP-x"}], fps
    )
    assert len(ok) == 1
    assert rejected == []


def test_validator_rejects_unknown_kind():
    ok, rejected = semantic_enrich.validate_candidates(
        [{"kind": "made_up", "area": "GET /api/v1/real", "fp_id": "FP-x"}],
        [_fp("GET /api/v1/real")],
    )
    assert ok == []
    assert "kind 非法" in rejected[0]["reason"]


def test_implicit_rule_marked_unverified():
    fps = [_fp("GET /api/v1/real")]
    candidate = {
        "kind": "implicit",
        "area": "GET /api/v1/real",
        "fp_id": "FP-x",
        "title": "疑似需要审批流",
        "category": TPType.NORMAL.value,
    }
    ok, _ = semantic_enrich.validate_candidates([candidate], fps)
    tp = semantic_enrich._to_test_point(ok[0], fps)
    assert tp is not None
    assert tp.unverified is True
    assert tp.origin == "llm_implicit"
    assert "疑似隐性规则" in tp.title


def test_llm_disabled_keeps_rule_output(fresh_db, sample_repo):
    result = _run(sample_repo, llm=semantic_enrich.EnrichOptions(enabled=False))
    assert result.counts["llm_added"] == 0
    assert any("仅规则引擎" in n or "未启用" in n for n in result.notes)


def test_store_traceability_after_persist(fresh_db, sample_repo):
    result = _run(sample_repo)
    trace = store.traceability(result.project_id or 0)
    assert trace["fp_count"] > 0
    assert trace["orphan_tp_count"] == 0
    assert trace["orphan_case_count"] == 0
