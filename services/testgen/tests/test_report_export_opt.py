"""P2-9 · 报告导出（PDF / Word / Excel）可选依赖测试。

`output/report_writer.export_report` 对 json/md/html 用内置渲染器、对 pdf/docx/xlsx
惰性引入可选依赖；本测试验证：可选依赖装好后，三种格式都能**真实产出非空文件**，
且缺依赖时（importorskip 保护下不会跑到）会友好报错而非静默半截文件。

测试本身不影响主链路：未装可选依赖时整个文件被 skip。
"""

from __future__ import annotations

import pytest

from output.report_writer import export_report


# 可选依赖未装 → 整个模块 skip（与 export_report 的 ImportError 保护一致）
pytest.importorskip("reportlab")
pytest.importorskip("docx")
pytest.importorskip("openpyxl")

from core.enums import ReportFormat


_MIN_REPORT: dict = {
    "project": {"id": 1, "name": "导出测试项目"},
    "generated_at": "2026-09-17T00:00:00",
    "report_version": "1.0",
    "contract_version": "v1.0",
    "batch": {"batch_id": "B-EXPORT", "state": "completed"},
    "counts": {"cases_active": 2, "test_points": 2, "functional_points": 2},
    "summary": {"headline": "冒烟通过", "bullets": ["用例 2/2 通过"]},
    "coverage": {"fp_total": 2, "fp_covered": 2, "tp_total": 2, "tp_covered": 2, "case_active": 2},
    "cases": [
        {"case_type": "api", "title": "健康检查", "exec_status": "pass"},
        {"case_type": "api", "title": "鉴权拦截", "exec_status": "fail"},
    ],
}


@pytest.mark.parametrize(
    "fmt", [ReportFormat.PDF.value, ReportFormat.WORD.value, ReportFormat.EXCEL.value]
)
def test_optional_export_produces_nonempty_file(fmt, tmp_path):
    """每种可选格式都应写出非空文件。"""
    target = tmp_path / f"report.{fmt}"
    out = export_report(_MIN_REPORT, fmt, str(target))
    assert out == str(target)
    assert target.exists()
    assert target.stat().st_size > 0


def test_builtin_formats_still_work(tmp_path):
    """回归守护：内置三件套（json/md/html）不依赖可选包，始终可用。"""
    for fmt in (ReportFormat.JSON.value, ReportFormat.MARKDOWN.value, ReportFormat.HTML.value):
        target = tmp_path / f"r.{fmt}"
        export_report(_MIN_REPORT, fmt, str(target))
        assert target.stat().st_size > 0
