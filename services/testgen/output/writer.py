"""产物输出：把测试点 / 用例 / 摘要落成人可读 + 机可读的文件。

目录约定（输出区与只读代码区**物理分离**）：
    outputs/<project_id>/test_points.json     机读：测试点全量
    outputs/<project_id>/test_cases.json      机读：用例全量（八要素）
    outputs/<project_id>/TEST_CASES.md        人读：用例清单
    outputs/<project_id>/summary.json         机读：本次运行摘要
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import get_settings
from core.contracts import CONTRACT_VERSION, CaseSpec, TestPoint


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass
class OutputPaths:
    directory: Path
    test_points: Path
    test_cases: Path
    markdown: Path
    summary: Path


class OutputWriter:
    """按项目分目录写出产物。"""

    def __init__(self, base: str | Path | None = None) -> None:
        self.base = Path(base) if base is not None else get_settings().output_dir
        self.base.mkdir(parents=True, exist_ok=True)

    def paths(self, project_id: int) -> OutputPaths:
        d = self.base / str(project_id)
        d.mkdir(parents=True, exist_ok=True)
        return OutputPaths(
            directory=d,
            test_points=d / "test_points.json",
            test_cases=d / "test_cases.json",
            markdown=d / "TEST_CASES.md",
            summary=d / "summary.json",
        )

    # ------------------------------------------------------------ 单件写出
    @staticmethod
    def write_json(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dumps(payload) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def write_text(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # ------------------------------------------------------------ 业务写出
    def write_test_points(self, project_id: int, tps: list[TestPoint]) -> Path:
        p = self.paths(project_id)
        payload = {
            "contract_version": CONTRACT_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(tps),
            "test_points": [tp.to_dict() for tp in tps],
        }
        return self.write_json(p.test_points, payload)

    def write_cases(self, project_id: int, cases: list[CaseSpec]) -> Path:
        p = self.paths(project_id)
        payload = {
            "contract_version": CONTRACT_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(cases),
            "cases": [c.to_dict() for c in cases],
        }
        return self.write_json(p.test_cases, payload)

    def write_markdown(self, project_id: int, cases: list[CaseSpec]) -> Path:
        p = self.paths(project_id)
        lines = [
            f"# 测试用例清单（项目 {project_id}）",
            "",
            f"> 生成时间：{datetime.now().isoformat(timespec='seconds')}　"
            f"契约版本：{CONTRACT_VERSION}　用例数：{len(cases)}",
            "",
        ]
        for c in cases:
            lines.append(f"## {c.tc_no} · {c.title}")
            lines.append("")
            lines.append(f"- 类型：{c.case_type}　优先级：{c.priority}　测试类型：{c.test_type}")
            lines.append(f"- 模块：{c.module}")
            lines.append(f"- 前置条件：{c.precondition}")
            lines.append(f"- 预期结果：{c.expect()}")
            lines.append("- 步骤：")
            for step in c.doc_steps:
                lines.append(f"  {step.get('seq')}. [{step.get('type')}] {step.get('desc')}")
            lines.append("")
        return self.write_text(p.markdown, "\n".join(lines))

    def write_summary(self, project_id: int, summary: dict[str, Any]) -> Path:
        p = self.paths(project_id)
        payload = {
            "contract_version": CONTRACT_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            **summary,
        }
        return self.write_json(p.summary, payload)

    def write_all(
        self,
        project_id: int,
        tps: list[TestPoint],
        cases: list[CaseSpec],
        summary: dict[str, Any],
    ) -> dict[str, str]:
        return {
            "test_points": str(self.write_test_points(project_id, tps)),
            "test_cases": str(self.write_cases(project_id, cases)),
            "markdown": str(self.write_markdown(project_id, cases)),
            "summary": str(self.write_summary(project_id, summary)),
        }
