"""多仓改造阶段 1 守护：请求契约 + 编排循环 + 共享 project + 幂等签名。

红线：repo_specs 空时所有行为与单仓逐字节一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import store
from engine import pipeline
from engine.pipeline import PipelineOptions, RepoSpec


@pytest.fixture()
def repo_conn(fresh_db):
    connection = store.connect()
    yield connection
    connection.close()


def _make_repo(root: Path, key: str) -> Path:
    """造一个最小仓：相同路径 api.py + 相同路由 GET /health（跨仓同路径同函数）。"""
    src = root / key
    src.mkdir(parents=True, exist_ok=True)
    (src / "api.py").write_text(
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/api/v1")\n'
        "\n"
        '\n@router.get("/health")\n'
        "def health():\n"
        '    """健康检查。"""\n'
        '    return {"ok": True}\n',
        encoding="utf-8",
    )
    return src


def _specs(root: Path, keys: list[str]) -> list[RepoSpec]:
    return [RepoSpec(repo_key=k, local_path=str(_make_repo(root, k))) for k in keys]


# ---------------------------------------------------------------------------
# 请求契约
# ---------------------------------------------------------------------------
def test_request_repos_fills_options():
    from service.app import PipelineRequest, RepoSpecRequest, build_opts_from_request

    req = PipelineRequest(
        project_name="mr-http",
        repos=[
            RepoSpecRequest(repo_key="backend", local_path="/tmp/be", changed_files=["api.py"]),
            RepoSpecRequest(repo_key="frontend", local_path="/tmp/fe"),
        ],
    )
    opts = build_opts_from_request(req)
    assert [s.repo_key for s in opts.repo_specs] == ["backend", "frontend"]
    assert opts.repo_specs[0].changed_files == ["api.py"]


def test_request_no_repos_keeps_single_repo():
    from service.app import PipelineRequest, build_opts_from_request

    opts = build_opts_from_request(PipelineRequest(local_path="/tmp/single"))
    assert opts.repo_specs == []


def test_idem_key_differs_by_repo_set():
    from service.app import PipelineRequest, RepoSpecRequest, _idem_key_for_generate

    base = dict(project_name="same")
    r_ab = PipelineRequest(
        **base,
        repos=[
            RepoSpecRequest(repo_key="a", repo_url="u1"),
            RepoSpecRequest(repo_key="b", repo_url="u2"),
        ],
    )
    r_ba = PipelineRequest(
        **base,
        repos=[
            RepoSpecRequest(repo_key="b", repo_url="u2"),
            RepoSpecRequest(repo_key="a", repo_url="u1"),
        ],
    )
    r_ac = PipelineRequest(
        **base,
        repos=[
            RepoSpecRequest(repo_key="a", repo_url="u1"),
            RepoSpecRequest(repo_key="c", repo_url="u3"),
        ],
    )
    r_single = PipelineRequest(**base, repo_url="u1")
    ka, kb, kc, ks = (_idem_key_for_generate(r) for r in (r_ab, r_ba, r_ac, r_single))
    assert ka == kb, "仓集合相同、顺序不同 → 同一幂等键"
    assert ka != kc, "不同仓集合 → 不同幂等键"
    assert ks not in (ka, kc), "多仓与单仓不得撞键"


# ---------------------------------------------------------------------------
# 编排循环
# ---------------------------------------------------------------------------
def test_multirepo_one_project_three_bindings(tmp_path, repo_conn):
    specs = _specs(tmp_path, ["backend", "frontend", "shared"])
    opts = PipelineOptions(project_name="mr-proj", mode="full", repo_specs=specs, persist=True)
    result = pipeline.run_multirepo_pipeline(opts)

    assert result.project_id
    bindings = store.list_repo_bindings(result.project_id, repo_conn)
    assert {b["repo_key"] for b in bindings} == {"backend", "frontend", "shared"}
    assert len({b["project_id"] for b in bindings}) == 1, "三仓必须归属同一个 project"


def test_multirepo_same_path_yields_distinct_fp_ids(tmp_path, repo_conn):
    specs = _specs(tmp_path, ["backend", "frontend"])
    opts = PipelineOptions(project_name="mr-fp", mode="full", repo_specs=specs, persist=True)
    result = pipeline.run_multirepo_pipeline(opts)

    fps = store.list_functional_points(result.project_id, repo_conn)
    by_repo: dict[str, set[str]] = {}
    for fp in fps:
        bid = fp["repo_binding_id"]
        by_repo.setdefault(str(bid), set()).add(fp["contract_id"])
    assert len(by_repo) == 2, "两仓各有功能点"
    all_ids = [cid for ids in by_repo.values() for cid in ids]
    assert len(all_ids) == len(set(all_ids)), "跨仓同路径同函数的 fp_id 不得相同"
    assert all(str(cid).startswith("FP-") for cid in all_ids)


def test_regenerate_one_repo_does_not_delete_another(tmp_path, repo_conn):
    specs = _specs(tmp_path, ["backend", "frontend"])
    opts = PipelineOptions(project_name="mr-regen", mode="full", repo_specs=specs, persist=True)
    result = pipeline.run_multirepo_pipeline(opts)
    pid = result.project_id
    bmap = store.repo_binding_map(pid, repo_conn)
    be_count = sum(
        1
        for fp in store.list_functional_points(pid, repo_conn)
        if fp["repo_binding_id"] == bmap["backend"]
    )
    fe_count = sum(
        1
        for fp in store.list_functional_points(pid, repo_conn)
        if fp["repo_binding_id"] == bmap["frontend"]
    )
    assert be_count > 0 and fe_count > 0

    # 只重跑 backend 一个仓（单仓 opts + project_id/binding_id 预设）
    child = _derive_child(pid, bmap["backend"], specs[0])
    pipeline.run_pipeline(child)
    after_be = sum(
        1
        for fp in store.list_functional_points(pid, repo_conn)
        if fp["repo_binding_id"] == bmap["backend"]
    )
    after_fe = sum(
        1
        for fp in store.list_functional_points(pid, repo_conn)
        if fp["repo_binding_id"] == bmap["frontend"]
    )
    assert after_be > 0, "重跑后 backend 仍有功能点"
    assert after_fe == fe_count, "重跑 backend 不得删掉 frontend 的功能点"


def _derive_child(pid: int, bid: int, spec: RepoSpec) -> PipelineOptions:
    child = PipelineOptions(
        project_name="mr-regen",
        mode="full",
        persist=True,
        project_id=pid,
        repo_binding_id=bid,
        repo_key=spec.repo_key,
        local_path=spec.local_path,
    )
    return child


def test_empty_repo_specs_dispatches_to_single_repo(tmp_path, repo_conn):
    repo = _make_repo(tmp_path, "only")
    opts = PipelineOptions(project_name="mr-single", local_path=str(repo), mode="full")
    result = pipeline.run_pipeline_auto(opts)
    assert result.project_id
    assert store.list_repo_bindings(result.project_id, repo_conn) == [], "单仓不建 binding"
    assert store.list_functional_points(result.project_id, repo_conn), "单仓仍正常产出"
