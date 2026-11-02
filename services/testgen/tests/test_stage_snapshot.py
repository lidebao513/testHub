"""分支阶段锚定与 manifest 单测（C1）。

覆盖：StageStore 纯 IO（add/list/get/latest/resolve/artifact）、损坏恢复；
resolve_commit 本地归一化 + 缩写 SHA 拒绝；materialize_commit 本地 git archive；
create_snapshot 端到端（真实 git repo fixture）与 materializer 注入。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engine.stage_snapshot import (
    StageSnapshot,
    StageStore,
    create_snapshot,
    materialize_commit,
    resolve_commit,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add b"], cwd=repo, check=True)
    return repo


def test_stage_store_add_list_get_latest(tmp_path: Path) -> None:
    store = StageStore(tmp_path)
    s1 = StageSnapshot(
        repo="r", branch="master", commit="c1", stage_id="master@c1_1", artifact_dir="r/master@c1"
    )
    s2 = StageSnapshot(
        repo="r", branch="master", commit="c2", stage_id="master@c2_2", artifact_dir="r/master@c2"
    )
    store.add(s1)
    store.add(s2)
    assert len(store.list_by_branch("r", "master")) == 2
    assert store.get("r", "master@c1_1").commit == "c1"
    assert store.latest("r", "master").commit == "c2"
    with pytest.raises(ValueError):
        store.add(s1)  # 重复


def test_stage_store_resolve(tmp_path: Path) -> None:
    store = StageStore(tmp_path)
    store.add(
        StageSnapshot(
            repo="r",
            branch="master",
            commit="abcdef1234567890",
            stage_id="master@abc_1",
            artifact_dir="r/master@abc",
        )
    )
    assert store.resolve("r", "master@abc_1").commit == "abcdef1234567890"
    assert store.resolve("r", "abcdef").commit == "abcdef1234567890"
    with pytest.raises(KeyError):
        store.resolve("r", "nope")


def test_stage_store_corrupt_recovery(tmp_path: Path) -> None:
    p = tmp_path / "r" / "stages.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ broken json", encoding="utf-8")
    store = StageStore(tmp_path)
    assert store.list_by_branch("r", "master") == []  # 损坏恢复为空，不崩
    assert (tmp_path / "r" / "stages.json.bak").exists()


def test_stage_store_artifact_roundtrip(tmp_path: Path) -> None:
    store = StageStore(tmp_path)
    snap = StageSnapshot(
        repo="r", branch="master", commit="c1", stage_id="m@c1_1", artifact_dir="r/m@c1"
    )
    store.add(snap)
    data = {"tps": [{"id": "t1"}]}
    store.save_artifact(snap, "test_points", data)
    assert store.load_artifact(snap, "test_points") == data
    with pytest.raises(FileNotFoundError):
        store.load_artifact(snap, "missing")


def test_resolve_commit_local(git_repo: Path) -> None:
    sha = resolve_commit(str(git_repo), "HEAD", source="local")
    assert len(sha) == 40
    with pytest.raises(ValueError):
        resolve_commit(str(git_repo), sha[:7], source="local")  # 缩写拒绝


def test_materialize_commit_local(git_repo: Path) -> None:
    sha = resolve_commit(str(git_repo), "HEAD", source="local")
    dest = git_repo.parent / "out"
    files = materialize_commit(str(git_repo), sha, dest, source="local")
    assert "a.py" in files and "b.py" in files
    assert (dest / "a.py").exists()


def test_create_snapshot_local(git_repo: Path, tmp_path: Path) -> None:
    store = StageStore(tmp_path)
    snap = create_snapshot(store, str(git_repo), "master", source="local")
    assert snap.source == "local"
    assert len(snap.commit) == 40
    assert snap.mode == "full"
    assert (store.root / snap.artifact_dir / "a.py").exists()
    snap2 = create_snapshot(store, str(git_repo), "master", source="local")
    assert snap2.stage_id.endswith("_2")
    assert store.latest(str(git_repo), "master").stage_id == snap2.stage_id


def test_create_snapshot_with_materializer_stub(git_repo: Path, tmp_path: Path) -> None:
    store = StageStore(tmp_path)
    real_sha = resolve_commit(str(git_repo), "HEAD", source="local")
    captured: dict[str, str] = {}

    def stub(repo: str, commit: str, dest: Path) -> list[str]:
        captured["commit"] = commit
        (dest / "x.py").write_text("x", encoding="utf-8")
        return ["x.py"]

    snap = create_snapshot(
        store,
        str(git_repo),
        "main",
        commit=real_sha,
        source="local",
        materializer=stub,
    )
    assert snap.commit == real_sha
    assert captured["commit"] == real_sha
    assert (store.root / snap.artifact_dir / "x.py").exists()
