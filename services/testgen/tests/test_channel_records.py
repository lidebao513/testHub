"""通道独立记录（需求1）的单测：拆分正确性 + 凭证不泄露 + 空通道不写文件。

覆盖：
- `stage_partition_channels`：FP 按 `file_path` 前缀 `runtime:` 拆分为 code/url 两组；
  TP / Case 按 `fp_contract_id` 指回的存活 FP 归属通道；
- `write_channel_records`：仅非空通道写 `code_channel.*` / `url_channel.*`；`channels_summary.json` 总写；
- 凭证红线：地址通道来源已是脱敏 `runtime:<url>`，产物中**不含**账号 / 密码 / 动态口令。
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from engine.pipeline import PipelineResult, stage_partition_channels
from output.channel_writer import write_channel_records


# 模拟一个真实凭证子串（永远不应出现在任何产物里）
_LEAK = "TopSecretP@ss2026!"

_CODE_FP = FunctionalPoint(
    fp_id="FP-code1",
    ftype="api",
    file_path="backend/foo.py",
    name="GET /orders",
    title="查询订单",
    module="订单",
)
_URL_FP = FunctionalPoint(
    fp_id="FP-url1",
    ftype="page",
    file_path="runtime:https://demo.example.com/pc/orders",
    name="/pc/orders",
    title="订单页",
    module="订单",
)

_CODE_TP = TestPoint(
    tp_id="TP-code1",
    fp_contract_id="FP-code1",
    category="正常",
    module="订单",
    title="订单列表可达",
    source="backend/foo.py",
    method="GET",
)
_URL_TP = TestPoint(
    tp_id="TP-url1",
    fp_contract_id="FP-url1",
    category="正常",
    module="订单",
    title="订单页可达",
    source="runtime:https://demo.example.com/pc/orders",
)

_CODE_CASE = CaseSpec(
    tc_no="TC-001",
    title="订单列表正常查询",
    ctype="api",
    steps=[{"expect": "返回 200 与订单列表"}],
    module="订单",
    case_type="正常",
    priority="P2",
    precondition="",
    doc_steps=[{"seq": 1, "type": "call", "desc": "GET /orders"}],
    tp_id="TP-code1",
    fp_contract_id="FP-code1",
)
_URL_CASE = CaseSpec(
    tc_no="TC-002",
    title="订单页正常访问",
    ctype="e2e",
    steps=[{"expect": "页面渲染订单列表"}],
    module="订单",
    case_type="正常",
    priority="P2",
    precondition="",
    doc_steps=[{"seq": 1, "type": "open", "desc": "打开 /pc/orders"}],
    tp_id="TP-url1",
    fp_contract_id="FP-url1",
)


def _both_channels_result() -> PipelineResult:
    r = PipelineResult(source_kind="code+url")
    r.functional_points = [_CODE_FP, _URL_FP]
    r.test_points = [_CODE_TP, _URL_TP]
    r.cases = [_CODE_CASE, _URL_CASE]
    return r


def test_partition_splits_code_and_url() -> None:
    r = _both_channels_result()
    stage_partition_channels(None, r, None)
    assert [fp.fp_id for fp in r.code_fps] == ["FP-code1"]
    assert [fp.fp_id for fp in r.url_fps] == ["FP-url1"]
    assert [tp.tp_id for tp in r.code_tps] == ["TP-code1"]
    assert [tp.tp_id for tp in r.url_tps] == ["TP-url1"]
    assert [c.tc_no for c in r.code_cases] == ["TC-001"]
    assert [c.tc_no for c in r.url_cases] == ["TC-002"]
    assert r.channel_summary["code"] == {"fp": 1, "tp": 1, "case": 1}
    assert r.channel_summary["url"] == {"fp": 1, "tp": 1, "case": 1}
    assert r.channel_summary["source_kind"] == "code+url"


def test_partition_pure_code_leaves_url_empty() -> None:
    r = PipelineResult(source_kind="code")
    r.functional_points = [_CODE_FP]
    r.test_points = [_CODE_TP]
    r.cases = [_CODE_CASE]
    stage_partition_channels(None, r, None)
    assert len(r.code_fps) == 1 and len(r.url_fps) == 0
    assert len(r.code_cases) == 1 and len(r.url_cases) == 0


def test_write_channel_records_only_non_empty(tmp_path: Path) -> None:
    # 纯代码运行：只应写出 code_channel.*，不应有 url_channel.*
    r = PipelineResult(source_kind="code")
    r.project_id = 999
    r.functional_points = [_CODE_FP]
    r.test_points = [_CODE_TP]
    r.cases = [_CODE_CASE]
    stage_partition_channels(None, r, None)
    paths = write_channel_records(r.project_id, r, base=tmp_path)
    assert (tmp_path / "999" / "code_channel.json").exists()
    assert (tmp_path / "999" / "code_channel.md").exists()
    assert not (tmp_path / "999" / "url_channel.json").exists()
    assert (tmp_path / "999" / "channels_summary.json").exists()
    assert "code_json" in paths and "url_json" not in paths


def test_write_channel_records_both_when_present(tmp_path: Path) -> None:
    r = _both_channels_result()
    r.project_id = 998
    stage_partition_channels(None, r, None)
    write_channel_records(r.project_id, r, base=tmp_path)
    assert (tmp_path / "998" / "code_channel.json").exists()
    assert (tmp_path / "998" / "url_channel.json").exists()


def test_runtime_source_is_desensitized() -> None:
    """凭证红线（真实守卫）：地址通道来源必须剥离 `user:pass@`，产物里绝不出现明文密码。

    脱敏发生在上游 `runtime_ui._desensitize_runtime_source`（to_functional_points 调用前），
    因此即便页面 URL 含凭据，`runtime:<url>` 这种 file_path 也不含账号密码。
    """
    from engine.runtime_ui import _desensitize_runtime_source

    clean = _desensitize_runtime_source("https://demo.example.com/pc/orders")
    assert clean == "https://demo.example.com/pc/orders"
    stripped = _desensitize_runtime_source(f"https://u:{_LEAK}@demo.example.com/pc/orders")
    assert _LEAK not in stripped
    assert stripped == "https://demo.example.com/pc/orders"
    # 端口保留、路径保留
    assert (
        _desensitize_runtime_source("https://admin:s3cr3t@host:8090/a/b") == "https://host:8090/a/b"
    )


def test_channel_records_contain_no_credentials(tmp_path: Path) -> None:
    """正向守卫：用「已脱敏」的地址通道功能点跑通道写出，产物里不应出现明文凭证子串。

    （脱敏由 runtime_ui 上游保证；此处守护「通道独立记录产物不引入凭证」这一契约。）
    """
    url_fp = FunctionalPoint(
        fp_id="FP-url1",
        ftype="page",
        file_path="runtime:https://demo.example.com/pc/orders",  # 正确形态：不含凭据
        name="/pc/orders",
        title="订单页",
        module="订单",
    )
    r = PipelineResult(source_kind="url")
    r.project_id = 997
    r.functional_points = [url_fp]
    r.test_points = [
        TestPoint(tp_id="TP-url1", fp_contract_id="FP-url1", category="正常", module="订单")
    ]
    r.cases = [
        CaseSpec(
            tc_no="TC-002",
            title="订单页正常访问",
            ctype="e2e",
            steps=[{"expect": "页面渲染"}],
            module="订单",
            case_type="正常",
            priority="P2",
            doc_steps=[{"seq": 1, "type": "open", "desc": "o"}],
            tp_id="TP-url1",
            fp_contract_id="FP-url1",
        )
    ]
    stage_partition_channels(None, r, None)
    paths = write_channel_records(r.project_id, r, base=tmp_path)
    blob = "\n".join(Path(p).read_text(encoding="utf-8") for p in paths.values())
    assert _LEAK not in blob
