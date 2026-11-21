"""多仓改造阶段 5 守护：CLI --repo-spec + 多仓总验收四点。"""

from __future__ import annotations

import json

import pytest

from cli.main import _parse_repo_specs
from core import store
from engine import pipeline


def test_parse_repo_specs_json(tmp_path):
    payload = [
        {
            "repo_key": "backend",
            "local_path": "D:\\code\\be",
            "base": "v1",
            "target": "v2",
            "changed_files": ["a\\b.py"],
        },
        {"repo_key": "frontend", "repo_url": "https://x/fe.git"},
        {"local_path": "/no-key"},  # 缺 repo_key 被忽略
    ]
    specs = _parse_repo_specs(json.dumps(payload))
    assert [s.repo_key for s in specs] == ["backend", "frontend"]
    assert specs[0].local_path == "D:/code/be"
    assert specs[0].changed_files == ["a/b.py"]
    assert specs[0].base == "v1" and specs[0].target == "v2"


def test_parse_repo_specs_from_file(tmp_path):
    f = tmp_path / "repos.json"
    f.write_text(json.dumps([{"repo_key": "a", "local_path": "/a"}]), encoding="utf-8")
    specs = _parse_repo_specs(f"@{f}")
    assert [s.repo_key for s in specs] == ["a"]


def test_parse_repo_specs_invalid():
    with pytest.raises(ValueError):
        _parse_repo_specs("not json")
    with pytest.raises(ValueError):
        _parse_repo_specs('{"repo_key": "a"}')  # 非数组


def test_parse_repo_specs_empty():
    assert _parse_repo_specs("") == []


# --- 多仓总验收四点整合（复用阶段 1/2/4 已验证的实现，这里做一次端到端汇总断言） ---
def test_multirepo_acceptance_end_to_end(tmp_path, fresh_db):
    """四点在一条 E2E 里回归：ID 不冲突 / 不互删 / 幂等键不撞 / 报告分组。"""
    from engine import report as report_engine
    from service.app import PipelineRequest, RepoSpecRequest, _idem_key_for_generate

    for key in ("backend", "frontend"):
        src = tmp_path / key
        src.mkdir()
        (src / "api.py").write_text(
            "from fastapi import APIRouter\n"
            'router = APIRouter(prefix="/api/v1")\n'
            '\n@router.get("/health")\ndef health():\n    return {}\n',
            encoding="utf-8",
        )
    specs = [
        pipeline.RepoSpec(repo_key="backend", local_path=str(tmp_path / "backend")),
        pipeline.RepoSpec(repo_key="frontend", local_path=str(tmp_path / "frontend")),
    ]
    opts = pipeline.PipelineOptions(project_name="accept", repo_specs=specs, persist=True)
    result = pipeline.run_multirepo_pipeline(opts)
    pid = result.project_id
    conn = store.connect()

    # 1) ID 不冲突：跨仓同路径同函数 → 不同 fp_id
    fps = store.list_functional_points(pid, conn)
    fp_ids = [f["contract_id"] for f in fps]
    assert fp_ids and len(fp_ids) == len(set(fp_ids))

    # 2) 不互删：两仓各有归属行
    bmap = store.repo_binding_map(pid, conn)
    assert set(bmap) == {"backend", "frontend"}
    assert all(any(f["repo_binding_id"] == bid for f in fps) for bid in bmap.values())

    # 3) 幂等键不撞：不同仓集合 → 不同键
    r_ab = PipelineRequest(
        project_name="accept",
        repos=[RepoSpecRequest(repo_key="backend"), RepoSpecRequest(repo_key="frontend")],
    )
    r_a = PipelineRequest(project_name="accept", repos=[RepoSpecRequest(repo_key="backend")])
    assert _idem_key_for_generate(r_ab) != _idem_key_for_generate(r_a)

    # 4) 报告分组：build_report 输出 repo_summary
    snap = store.report_snapshot(pid, conn=conn)
    summary = report_engine.repo_summary(pid, snap)
    assert summary["single_repo"] is False
    assert {r["repo_key"] for r in summary["repos"]} == {"backend", "frontend"}
    conn.close()
