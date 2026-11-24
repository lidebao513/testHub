"""地址通道三种生成做法的实测对比（阶段 D / 任务 #434）。

对比口径（同一被测地址、同一变更集）：
  ① pure_url        —— 纯地址全站发现（mode=full，不靶向）
  ② code_only       —— 纯代码扫描（incremental + changed_files，不发现运行时）
  ③ url_plus_changes—— 地址为主 + 变更辅助（mode=full + target_runtime，验证 #435 放宽）

隔离红线：全程 ``persist=False``、``write_channel_records=False``，不落库、
不覆盖主证据项目（X1 曾把 project 30 覆盖成 4066 条）。

用法（必须让 cwd 落在 services/testgen，且用其 venv）：
    cd services/testgen
    ./.venv/Scripts/python.exe ../../scripts/compare_url_approaches.py

输出：docs/metrics/2026-11-23-url-three-approaches.{json,md}
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TESTGEN = ROOT / "services" / "testgen"
sys.path.insert(0, str(TESTGEN))

from core.config import get_settings
from engine import pipeline

BASE_URL = "http://127.0.0.1:3900/"
USER = "url_compare"
PWD = "Compare@2026"
CHANGED = [
    "frontend/src/views/requirement-analysis/UrlFirstConsole.vue",
    "frontend/src/layout/index.vue",
]
OUT = ROOT / "docs" / "metrics" / "2026-11-23-url-three-approaches.json"
MD = OUT.with_suffix(".md")

KEYS = (
    "cases",
    "test_points",
    "functional_points",
    "runtime_ui_pages",
    "runtime_ui_reachable",
    "runtime_ui_elements",
    "runtime_ui_routes",
    "runtime_ui_fp_added",
    "runtime_fp_targeted",
    "runtime_fp_skipped_by_target",
)


def _sources() -> dict[str, str]:
    """轻量取码桥的本地等价物：只读变更文件文本，不展开代码功能点。"""
    out: dict[str, str] = {}
    for rel in CHANGED:
        path = ROOT / rel
        if path.is_file():
            out[rel] = path.read_text(encoding="utf-8")
    return out


def _opts(kind: str, sources: dict[str, str]) -> Any:
    url_mode = kind in ("pure_url", "url_plus_changes")
    opts = pipeline.default_options(
        "" if url_mode else str(ROOT),
        mode="full" if url_mode else "incremental",
    )
    # —— 隔离：不落库、不写通道产物、不调 LLM、不执行 ——
    opts.persist = False
    opts.write_channel_records = False
    opts.expert_mode = False
    opts.llm.enabled = False
    opts.target_req.execute = False
    opts.scopes = {"正常", "异常", "边界"}
    # —— 三种做法的差异只在下面四行 ——
    opts.changed_files = [] if kind == "pure_url" else list(CHANGED)
    opts.selected_paths = list(CHANGED) if kind == "code_only" else []
    opts.target_runtime = kind == "url_plus_changes"
    opts.target_sources = dict(sources) if kind == "url_plus_changes" else {}
    if url_mode:
        opts.target_req.enabled = True
        opts.target_req.base_url = BASE_URL
        opts.target_req.login_user = USER
        opts.target_req.login_password = PWD
    return opts


def _markdown(payload: dict[str, Any]) -> str:
    rows = []
    for kind, data in payload["results"].items():
        counts = data["counts"]
        rows.append(
            "| {kind} | {mode}/{source} | {dur}s | {cases} | {fp} | {pages} | {elems} "
            "| {targeted}/{dropped} |".format(
                kind=kind,
                mode=data["mode"],
                source=data["source_kind"] or "-",
                dur=data["duration_s"],
                cases=counts.get("cases", 0),
                fp=counts.get("functional_points", 0),
                pages=counts.get("runtime_ui_pages", 0),
                elems=counts.get("runtime_ui_elements", 0),
                targeted=counts.get("runtime_fp_targeted", 0),
                dropped=counts.get("runtime_fp_skipped_by_target", 0),
            )
        )
    return "\n".join(
        [
            "# 地址通道三种生成做法实测对比（2026-11-23）",
            "",
            f"- 被测地址：`{payload['base_url']}`",
            f"- 变更文件：{', '.join('`' + c + '`' for c in payload['changed_files'])}",
            f"- 隔离口径：{payload['isolated']}",
            f"- 采集时间：{payload['captured_at']}",
            "",
            "| 做法 | 模式/来源 | 耗时 | 用例 | 功能点 | 发现页面 | 发现元素 | 靶向保留/剔除 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
            *rows,
            "",
            "## 说明",
            "",
            "- ① 纯地址全站发现：覆盖最全但噪声最大，适合首次建基线 / 定期巡检。",
            "- ② 纯代码扫描：最快，但看不到真实渲染元素与运行时接口。",
            (
                "- ③ 地址为主 + 变更辅助：主产物是真实可达页面/元素，变更只做靶向收窄；"
                "本轮同时验证 full 模式在显式变更集下也能靶向（#435）。"
            ),
            (
                "- 数字来自 `result.counts`，三种做法使用同一被测地址与同一变更集；"
                "耗时含浏览器发现（启用了地址通道的做法）。"
            ),
            "",
            "机器可读明细见同目录 `.json`。",
            "",
        ]
    )


def main() -> None:
    settings = get_settings()
    print(
        "settings",
        {
            "runtime_ui_enabled": settings.runtime_ui_enabled,
            "playwright_channel": settings.playwright_channel,
        },
        flush=True,
    )
    sources = _sources()
    print("取码桥文本", len(sources), "个文件", flush=True)

    results: dict[str, Any] = {}
    for kind in ("pure_url", "code_only", "url_plus_changes"):
        print(f"=== {kind} start", flush=True)
        started = time.monotonic()
        try:
            result = pipeline.run_pipeline(_opts(kind, sources))
        except Exception as exc:  # noqa: BLE001 - 实验脚本需记录失败并继续下一组
            results[kind] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"=== {kind} FAILED: {exc}", flush=True)
            continue
        elapsed = round(time.monotonic() - started, 1)
        counts = {k: int(result.counts.get(k) or 0) for k in KEYS if k in result.counts}
        results[kind] = {
            "mode": result.mode,
            "source_kind": result.source_kind,
            "duration_s": elapsed,
            "counts": counts,
            "quality": getattr(result, "quality", None) or {},
            "notes_tail": list(result.notes[-3:]),
            "errors": list(result.errors[:3]),
        }
        print(
            f"=== {kind} done {elapsed}s cases={counts.get('cases')} "
            f"pages={counts.get('runtime_ui_pages')}",
            flush=True,
        )

    payload: dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": BASE_URL,
        "changed_files": CHANGED,
        "isolated": "persist=False / write_channel_records=False：不落库、不覆盖主证据项目",
        "results": results,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    MD.write_text(_markdown(payload), encoding="utf-8")
    print("written", OUT, MD, flush=True)


if __name__ == "__main__":
    main()
