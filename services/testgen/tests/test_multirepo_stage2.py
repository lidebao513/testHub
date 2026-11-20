"""多仓改造阶段 2 守护：快照命名空间隔离 + 逐仓 diff/产物不串。

diff/repo_facts/change_targeting 的隔离由「逐仓独立跑 pipeline」天然保证
（每仓独立 result.files / diff_hunks / repo_facts）；本文件显式守住两点：
1. StageStore namespace 让同名仓快照不覆盖（缺省空 namespace 零回归）；
2. 多仓编排下每个子仓的 changed_files / repo_facts 不串味。
"""

from __future__ import annotations

from engine.pipeline import PipelineOptions, RepoSpec, _derive_repo_opts
from engine.stage_snapshot import StageSnapshot, StageStore


def test_stage_store_namespace_isolates_same_slug(tmp_path):
    """两个仓 URL 末段同为 repo，靠 namespace 隔离，互不覆盖。"""
    be = StageStore(tmp_path / "stages", namespace="p12/backend")
    fe = StageStore(tmp_path / "stages", namespace="p12/frontend")
    snap = StageSnapshot(
        repo="/x/repo",
        branch="master",
        commit="c1",
        stage_id="master@c1_1",
        artifact_dir="repo/master@c1",
    )
    be.add(snap)
    assert [s.stage_id for s in be.list_all("/x/repo")] == ["master@c1_1"]
    assert fe.list_all("/x/repo") == [], "另一仓命名空间下不应看到本仓快照"
    be.save_artifact(snap, "analysis", {"fp": 1})
    assert (be.root / snap.artifact_dir / "analysis.json").exists()
    assert not (fe.root / snap.artifact_dir / "analysis.json").exists()


def test_stage_store_default_namespace_zero_regression(tmp_path):
    """不传 namespace 时 root 就是 analysis_root，路径与旧版一致。"""
    store = StageStore(tmp_path / "stages")
    assert store.root == tmp_path / "stages"
    assert store.namespace == ""
    snap = StageSnapshot(
        repo="/x/repo", branch="master", commit="c1", stage_id="s1", artifact_dir="repo/master@c1"
    )
    store.add(snap)
    assert (tmp_path / "stages" / "repo" / "stages.json").exists()


def test_namespace_sanitized(tmp_path):
    """namespace 里的非法字符被归一化为安全目录名。"""
    store = StageStore(tmp_path, namespace="../p 12/repo:x")
    assert ".." not in store.namespace
    assert ":" not in store.namespace
    assert store.root.parent == tmp_path, "namespace 必须是 tmp_path 的直接子目录（无穿越）"
    assert store.namespace and all(c.isalnum() or c in "_-" for c in store.namespace)


def test_derived_repo_opts_isolate_diff_inputs():
    """每个子 opts 的变更集/勾选/ref 独立，逐仓 diff 不串味。"""
    parent = PipelineOptions(
        local_path="/parent",
        base="shared-base",
        target="shared-target",
        changed_files=["shared.py"],
        project_name="mr",
    )
    be = RepoSpec(
        repo_key="backend",
        local_path="/be",
        base="v1",
        target="v2",
        changed_files=["be/api.py"],
        selected_paths=["be/"],
    )
    fe = RepoSpec(
        repo_key="frontend",
        local_path="/fe",
        base="v3",
        target="v4",
        changed_files=["fe/app.tsx"],
        selected_paths=["fe/"],
    )
    be_opts = _derive_repo_opts(parent, be, 7, 101)
    fe_opts = _derive_repo_opts(parent, fe, 7, 102)

    assert be_opts.local_path == "/be" and fe_opts.local_path == "/fe"
    assert be_opts.base == "v1" and fe_opts.base == "v3"
    assert be_opts.target == "v2" and fe_opts.target == "v4"
    assert be_opts.changed_files == ["be/api.py"]
    assert fe_opts.changed_files == ["fe/app.tsx"]
    assert be_opts.selected_paths == ["be/"] and fe_opts.selected_paths == ["fe/"]
    assert be_opts.repo_binding_id == 101 and fe_opts.repo_binding_id == 102
    assert be_opts.project_id == fe_opts.project_id == 7
    # 子 opts 的 repo_specs 必须清空，否则 run_pipeline 会再次进入多仓编排（无限递归）
    assert be_opts.repo_specs == [] and fe_opts.repo_specs == []
    # 父 opts 不能被改动（deepcopy）
    assert parent.local_path == "/parent" and parent.changed_files == ["shared.py"]
