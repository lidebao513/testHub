"""产物输出：把「代码通道 / 地址通道」各自独立产出的功能点 / 测试点 / 用例分别落盘（需求1）。

为什么需要
----------
合并后的 `PipelineResult` 只有一套 `functional_points / test_points / cases`（F5 把同名的
代码通道与地址通道功能点收敛为一条）。但用户要求「仓库和 url 分别跑出来的测试用例也要
单独的保存记录」——即按来源把两套记录分开留存，便于分别审计、分别回归、分别统计覆盖率。

拆分口径（与 `engine.pipeline._partition_channels` 同源）
------------------------------------------------------
- 功能点：看 `file_path` 是否以 `runtime:` 前缀开头（地址通道发现项）；
- 测试点 / 用例：看其 `fp_contract_id` 回指的存活功能点归属哪条通道。

凭证红线
--------
地址通道功能点的 `file_path` 已是脱敏形态 `runtime:<url>`（见 `engine/fp_merge`），
本模块**只写脱敏路径，绝不写出账号 / 密码 / 动态口令**（与 `core.config` 同一红线）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config import get_settings
from core.contracts import CONTRACT_VERSION, CaseSpec, FunctionalPoint, TestPoint


if TYPE_CHECKING:  # 避免 engine.output 与 output.engine 互相导入
    from engine.pipeline import PipelineResult

# 与 fp_merge._RUNTIME_PREFIX 同源：地址通道功能点 file_path 前缀
_RUNTIME_PREFIX = "runtime:"

_CHANNEL_LABELS: dict[str, str] = {
    "code": "代码通道（仓库静态分析）",
    "url": "地址通道（测试地址 + 账号密码 · 运行时发现）",
}


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _channel_markdown(
    channel: str, fps: list[FunctionalPoint], tps: list[TestPoint], cases: list[CaseSpec]
) -> str:
    """渲染单通道的人读报告：功能点概览 + 用例八要素清单。"""
    label = _CHANNEL_LABELS.get(channel, channel)
    lines = [
        f"# 测试用例清单 · {label}",
        "",
        f"> 通道：`{channel}`　生成时间：{datetime.now().isoformat(timespec='seconds')}"
        f"　契约版本：{CONTRACT_VERSION}",
        f"> 功能点 {len(fps)} · 测试点 {len(tps)} · 用例 {len(cases)}",
        "> 说明：地址通道功能点来源已脱敏为 `runtime:<url>`，本文件不含账号 / 密码 / 动态口令。",
        "",
        "## 一、功能点（Functional Points）",
        "",
        "| fp_id | 类型 | 模块 | 名称 | 来源 |",
        "|-------|------|------|------|------|",
    ]
    for fp in fps:
        src = "地址通道" if fp.file_path.startswith(_RUNTIME_PREFIX) else "代码通道"
        lines.append(
            f"| {fp.fp_id} | {fp.ftype} | {fp.module} | `{fp.name}` | {src}（`{fp.file_path}`） |"
        )
    lines.append("")
    lines.append("## 二、测试用例（八要素）")
    lines.append("")
    for c in cases:
        lines.append(f"### {c.tc_no} · {c.title}")
        lines.append("")
        lines.append(
            f"- 类型：{c.case_type}　优先级：{c.priority}　测试类型：{c.test_type}　模块：{c.module}"
        )
        lines.append(f"- 前置条件：{c.precondition}")
        lines.append(f"- 预期结果：{c.expect()}")
        lines.append("- 步骤：")
        for step in c.doc_steps:
            lines.append(f"  {step.get('seq')}. [{step.get('type')}] {step.get('desc')}")
        lines.append("")
    return "\n".join(lines)


def write_channel_records(
    project_id: int,
    result: PipelineResult,
    base: str | Path | None = None,
) -> dict[str, str]:
    """把各通道独立记录落盘（仅非空通道写文件）。返回写出路径清单。

    文件布局（在 `outputs/<project_id>/` 下）：
        code_channel.json / code_channel.md    —— 代码通道
        url_channel.json  / url_channel.md     —— 地址通道
        channels_summary.json                   —— 双通道计数索引（总是写出）
    """
    root = Path(base) if base is not None else get_settings().output_dir
    d = root / str(project_id)
    d.mkdir(parents=True, exist_ok=True)

    paths: dict[str, str] = {}
    channels = (
        ("code", result.code_fps, result.code_tps, result.code_cases),
        ("url", result.url_fps, result.url_tps, result.url_cases),
    )
    for name, fps, tps, cases in channels:
        if not fps and not tps and not cases:
            # 空通道不写文件：避免污染产物目录，也便于一眼判断「本次是否走了该通道」
            continue
        payload = {
            "channel": name,
            "channel_label": _CHANNEL_LABELS.get(name, name),
            "contract_version": CONTRACT_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "counts": {"fp": len(fps), "tp": len(tps), "case": len(cases)},
            # 地址通道 file_path 已是脱敏形态 runtime:<url>，不含任何凭证
            "functional_points": [fp.to_dict() for fp in fps],
            "test_points": [tp.to_dict() for tp in tps],
            "cases": [c.to_dict() for c in cases],
        }
        jpath = d / f"{name}_channel.json"
        jpath.write_text(_dumps(payload) + "\n", encoding="utf-8")
        paths[f"{name}_json"] = str(jpath)
        mpath = d / f"{name}_channel.md"
        mpath.write_text(_channel_markdown(name, fps, tps, cases), encoding="utf-8")
        paths[f"{name}_md"] = str(mpath)

    # 双通道计数索引（总是写出，便于快速判断本次走了哪条 / 哪两条通道）
    summary_payload = {
        "contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_id": project_id,
        "source_kind": result.channel_summary.get("source_kind", ""),
        "channels": result.channel_summary,
    }
    spath = d / "channels_summary.json"
    spath.write_text(_dumps(summary_payload) + "\n", encoding="utf-8")
    paths["summary_json"] = str(spath)
    return paths
