"""引擎 · 两阶段产物对比编排（C2，对应方案 A-L3/L4/L5）。

读两阶段分析产物（测试点/问题/覆盖率），产出对比报告：
测试点 新增/失效/保留；问题 新增/修复/保留；diff 统计（基于两阶段 commit 的 git diff）。

复用：engine.diff_tag.compute_changed_files / compute_diff_hunks。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from engine import diff_tag
from engine.stage_snapshot import StageSnapshot, StageStore


@dataclass
class StageCompareReport:
    """两阶段对比报告。"""

    from_stage: str = ""
    to_stage: str = ""
    diff_stat: dict[str, Any] = field(default_factory=dict)
    added_tps: list[dict[str, Any]] = field(default_factory=list)
    removed_tps: list[dict[str, Any]] = field(default_factory=list)
    kept_tps: list[dict[str, Any]] = field(default_factory=list)
    added_issues: list[dict[str, Any]] = field(default_factory=list)
    fixed_issues: list[dict[str, Any]] = field(default_factory=list)
    kept_issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _index(items: list[dict[str, Any]] | None, key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for it in items or []:
        k = it.get(key)
        if k is not None:
            out[str(k)] = it
    return out


def compare_artifacts(from_art: dict[str, Any], to_art: dict[str, Any]) -> StageCompareReport:
    """对比两份分析产物（均含 test_points / issues）。"""
    ft = _index(from_art.get("test_points"), "id")
    tt = _index(to_art.get("test_points"), "id")
    fi = _index(from_art.get("issues"), "id")
    ti = _index(to_art.get("issues"), "id")
    return StageCompareReport(
        from_stage=from_art.get("stage_id", ""),
        to_stage=to_art.get("stage_id", ""),
        added_tps=[tt[k] for k in tt if k not in ft],
        removed_tps=[ft[k] for k in ft if k not in tt],
        kept_tps=[tt[k] for k in tt if k in ft],
        added_issues=[ti[k] for k in ti if k not in fi],
        fixed_issues=[fi[k] for k in fi if k not in ti],
        kept_issues=[ti[k] for k in ti if k in fi],
    )


def _diff_stat(repo: str, from_commit: str, to_commit: str) -> dict[str, Any]:
    changed = diff_tag.compute_changed_files(repo, from_commit, to_commit)
    hunks = diff_tag.compute_diff_hunks(repo, from_commit, to_commit)
    added_lines = sum(max(n, 0) for spans in hunks.values() for _, n in spans)
    return {
        "changed_files": sorted(changed),
        "changed_file_count": len(changed),
        "hunk_count": sum(len(v) for v in hunks.values()),
        "added_lines": added_lines,
    }


def compare_stages(
    store: StageStore,
    repo: str,
    from_ref: str,
    to_ref: str,
    *,
    repo_path: str | None = None,
) -> StageCompareReport:
    """载入两阶段产物并对比；repo_path 给定时附带 git diff 统计。"""
    fs: StageSnapshot = store.resolve(repo, from_ref)
    ts: StageSnapshot = store.resolve(repo, to_ref)
    fa = store.load_artifact(fs, "analysis")
    ta = store.load_artifact(ts, "analysis")
    fa["stage_id"] = fs.stage_id
    ta["stage_id"] = ts.stage_id
    report = compare_artifacts(fa, ta)
    if repo_path:
        try:
            report.diff_stat = _diff_stat(repo_path, fs.commit, ts.commit)
        except Exception:  # noqa: BLE001 - diff 统计失败不影响产物对比
            report.diff_stat = {"error": "diff 统计失败（commit 不可解析或非同仓）"}
    return report


__all__ = ["StageCompareReport", "compare_artifacts", "compare_stages"]
