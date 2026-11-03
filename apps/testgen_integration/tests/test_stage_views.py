"""stage 代理三端点单测（#121）：client 函数 + Django views（mock requests，不依赖真实 sidecar）。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from apps.testgen_integration import client as tg_client
from apps.testgen_integration import views as tg_views


def _fake_resp(json_data: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.raise_for_status.return_value = None
    return m


class StageClientTests(unittest.TestCase):
    """client 函数：URL / payload / 方法。"""

    @patch.object(tg_client._session, "post")
    def test_stage_analyze_posts(self, post):
        post.return_value = _fake_resp({"ok": True, "stage_id": "master@aaa_1"})
        resp = tg_client.stage_analyze(local_path="/repo", branch="dev")
        self.assertTrue(resp["ok"])
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/api/v1/stage-analyze"))
        self.assertEqual(kwargs["json"]["local_path"], "/repo")
        self.assertEqual(kwargs["json"]["branch"], "dev")

    @patch.object(tg_client._session, "post")
    def test_stage_compare_posts(self, post):
        post.return_value = _fake_resp({"ok": True, "report": {}})
        tg_client.stage_compare(repo="/repo", from_ref="A", to_ref="B", repo_path="/repo")
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/api/v1/stage-compare"))
        self.assertEqual(kwargs["json"]["from_ref"], "A")

    @patch.object(tg_client._session, "get")
    def test_list_stages_gets(self, get):
        get.return_value = _fake_resp({"ok": True, "stages": []})
        tg_client.list_stages(repo="/repo", branch="dev")
        args, kwargs = get.call_args
        self.assertTrue(args[0].endswith("/api/v1/stages"))
        self.assertEqual(kwargs["params"]["branch"], "dev")


class StageViewTests(APITestCase):
    """Django 代理端点：鉴权、参数校验、透传与 502 降级。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="stage_tester", password="pass123456"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_list_requires_auth(self):
        anon = APIClient()
        resp = anon.get("/api/testgen/stages", {"repo": "/repo"})
        self.assertEqual(resp.status_code, 401)

    def test_list_requires_repo(self):
        resp = self.client.get("/api/testgen/stages")
        self.assertEqual(resp.status_code, 400)

    def test_list_proxies(self):
        with patch.object(
            tg_views, "list_stages", return_value={"ok": True, "stages": [{"stage_id": "s1"}]}
        ) as m:
            resp = self.client.get("/api/testgen/stages", {"repo": "/repo", "branch": "dev"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["stages"], [{"stage_id": "s1"}])
        m.assert_called_once_with(repo="/repo", branch="dev")

    def test_analyze_requires_local_path(self):
        resp = self.client.post("/api/testgen/stage-analyze", {}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_analyze_proxies(self):
        with patch.object(
            tg_views,
            "stage_analyze",
            return_value={"ok": True, "stage_id": "master@aaa_1", "stats": {}},
        ) as m:
            resp = self.client.post(
                "/api/testgen/stage-analyze",
                {"local_path": "/repo", "branch": "master"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["stage_id"], "master@aaa_1")
        m.assert_called_once_with(local_path="/repo", branch="master", commit="")

    def test_compare_requires_params(self):
        resp = self.client.post(
            "/api/testgen/stage-compare", {"repo": "/repo"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_compare_proxies(self):
        with patch.object(
            tg_views, "stage_compare", return_value={"ok": True, "report": {"added_tps": []}}
        ):
            resp = self.client.post(
                "/api/testgen/stage-compare",
                {"repo": "/repo", "from_ref": "A", "to_ref": "B"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("report", resp.data)

    def test_sidecar_down_returns_502(self):
        with patch.object(
            tg_views, "list_stages", side_effect=RuntimeError("connection refused")
        ):
            resp = self.client.get("/api/testgen/stages", {"repo": "/repo"})
        self.assertEqual(resp.status_code, 502)
