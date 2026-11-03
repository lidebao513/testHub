"""POST /api/v1/stage-analyze · /api/v1/stage-compare · GET /api/v1/stages 端点测试（C3 → #121）。

验证点：
- stage-analyze：扫描/合并 mock 后能登记快照并落 analysis 产物（fp_id 做稳定键）；
- stage-compare：经真 compare_stages 对比两快照，返回新增/失效/保留结构；
- stages 列表：全量与按分支过滤；
- 未知 stage_id 对比时返回 500 语义之外的显式错误（KeyError → 500，保持与其他端点一致的 fail-fast）。

StageStore 用 tmp_path 注入（patch service.app._stage_store），不触碰真实工作区。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from engine.stage_snapshot import StageSnapshot, StageStore
from service import app as service_app


@pytest.fixture()
def client():
    return TestClient(service_app.app, raise_server_exceptions=False)


@pytest.fixture()
def stage_store(tmp_path):
    return StageStore(tmp_path / "stages")


def _snapshot(repo: str, branch: str, commit: str, seq: int) -> StageSnapshot:
    short = commit[:12]
    return StageSnapshot(
        repo=repo,
        branch=branch,
        commit=commit,
        created_at="2026-11-03T10:00:00+00:00",
        stage_id=f"{branch}@{short}_{seq}",
        artifact_dir=f"{repo}/{branch}@{short}",
    )


def _art(stage_id: str, tps: list[dict]) -> dict:
    return {"stage_id": stage_id, "test_points": tps, "issues": []}


class TestStageAnalyze:
    def test_registers_snapshot_and_saves_artifact(self, client, stage_store, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        snap = _snapshot(str(repo_dir), "master", "a" * 40, 1)

        class _FP:
            def __init__(self, fp_id: str, name: str):
                self.fp_id = fp_id
                self.ftype = "api"
                self.file_path = "a.py"
                self.name = name
                self.title = name
                self.module = "m"

        fps = [_FP("fp1", "GET /x"), _FP("fp2", "POST /y")]

        with (
            patch("service.app._stage_store", return_value=stage_store),
            patch("engine.stage_snapshot.create_snapshot", return_value=snap) as m_create,
            patch("engine.scan.Scanner") as m_scanner,
            patch("engine.fp_extract.extract_functional_points") as m_extract,
        ):
            m_scanner.return_value.index.return_value = ["a.py"]
            m_extract.return_value.functional_points = fps
            resp = client.post(
                "/api/v1/stage-analyze",
                json={"local_path": str(repo_dir), "branch": "master"},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["stage_id"] == snap.stage_id
        assert body["stats"]["functional_points"] == 2
        m_create.assert_called_once()

        # 产物已按 fp_id 映射为 test_points 并落盘
        loaded = stage_store.load_artifact(snap, "analysis")
        assert {t["id"] for t in loaded["test_points"]} == {"fp1", "fp2"}
        assert loaded["issues"] == []

    def test_requires_local_path(self, client):
        resp = client.post("/api/v1/stage-analyze", json={})
        assert resp.status_code == 422  # pydantic 必填校验


class TestStageCompare:
    def test_compare_two_stages(self, client, stage_store, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        s1 = _snapshot(str(repo_dir), "master", "a" * 40, 1)
        s2 = _snapshot(str(repo_dir), "master", "b" * 40, 2)
        stage_store.add(s1)
        stage_store.add(s2)
        stage_store.save_artifact(s1, "analysis", _art(s1.stage_id, [{"id": "t1"}, {"id": "t2"}]))
        stage_store.save_artifact(s2, "analysis", _art(s2.stage_id, [{"id": "t2"}, {"id": "t3"}]))

        with patch("service.app._stage_store", return_value=stage_store):
            resp = client.post(
                "/api/v1/stage-compare",
                json={"repo": str(repo_dir), "from_ref": s1.stage_id, "to_ref": s2.stage_id},
            )

        assert resp.status_code == 200, resp.text
        report = resp.json()["report"]
        assert {t["id"] for t in report["added_tps"]} == {"t3"}
        assert {t["id"] for t in report["removed_tps"]} == {"t1"}
        assert report["diff_stat"] == {}  # 未要求 diff 统计

    def test_unknown_stage_id_errors(self, client, stage_store, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        with patch("service.app._stage_store", return_value=stage_store):
            resp = client.post(
                "/api/v1/stage-compare",
                json={"repo": str(repo_dir), "from_ref": "nope", "to_ref": "nada"},
            )
        assert resp.status_code >= 400


class TestListStages:
    def test_list_all_and_filter_by_branch(self, client, stage_store, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        s1 = _snapshot(str(repo_dir), "master", "a" * 40, 1)
        s2 = _snapshot(str(repo_dir), "dev", "b" * 40, 1)
        stage_store.add(s1)
        stage_store.add(s2)

        with patch("service.app._stage_store", return_value=stage_store):
            all_resp = client.get("/api/v1/stages", params={"repo": str(repo_dir)})
            dev_resp = client.get("/api/v1/stages", params={"repo": str(repo_dir), "branch": "dev"})

        assert all_resp.status_code == 200
        assert {s["stage_id"] for s in all_resp.json()["stages"]} == {s1.stage_id, s2.stage_id}
        assert [s["stage_id"] for s in dev_resp.json()["stages"]] == [s2.stage_id]

    def test_requires_repo(self, client):
        resp = client.get("/api/v1/stages", params={})
        assert resp.status_code == 422
