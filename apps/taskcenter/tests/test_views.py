"""任务中心视图层测试：通过 HTTP（DRF 测试客户端）验证端点接线、鉴权与完整操作流。"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.taskcenter.models import BackgroundTask

User = get_user_model()


class TaskCenterAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _make(self, **kw):
        kw.setdefault("name", "任务")
        kw.setdefault("task_type", "generate")
        kw.setdefault("status", "running")
        return BackgroundTask.objects.create(**kw)

    def test_list_requires_auth(self):
        anon = APIClient()
        resp = anon.get(reverse("taskcenter:list"))
        self.assertEqual(resp.status_code, 401)

    def test_list_excludes_closed_by_default(self):
        self._make(name="open", status="success")
        self._make(name="closed", status="success", is_closed=True)
        resp = self.client.get(reverse("taskcenter:list"))
        self.assertTrue(resp.json()["ok"])
        names = [t["name"] for t in resp.json()["tasks"]]
        self.assertIn("open", names)
        self.assertNotIn("closed", names)

    def test_list_include_closed(self):
        self._make(name="closed", status="success", is_closed=True)
        resp = self.client.get(reverse("taskcenter:list") + "?include_closed=1")
        names = [t["name"] for t in resp.json()["tasks"]]
        self.assertIn("closed", names)

    def test_detail_returns(self):
        t = self._make(source="gen-d", status="running")
        resp = self.client.get(reverse("taskcenter:detail", args=[t.id]))
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["task"]["id"], t.id)

    def test_retry_happy_path(self):
        t = self._make(
            status="failed",
            payload_summary='{"local_path":"/x","repo_url":"","scopes":[],"changed_files":[],"mode":""}',
        )
        with mock.patch("apps.testgen_integration.client.generate") as m:
            m.return_value = {"task_id": "gen-retry"}
            resp = self.client.post(reverse("taskcenter:retry", args=[t.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["task"]["status"], "pending")

    def test_retry_rejects_terminal_non_retryable(self):
        t = self._make(task_type="sync", status="failed")
        resp = self.client.post(reverse("taskcenter:retry", args=[t.id]))
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])

    def test_pause_happy_path_calls_sidecar(self):
        t = self._make(source="gen-p", status="running")
        with mock.patch("apps.testgen_integration.client.cancel_task") as m:
            resp = self.client.post(reverse("taskcenter:pause", args=[t.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["task"]["status"], "paused")
        m.assert_called_once_with("gen-p")

    def test_pause_rejects_terminal(self):
        t = self._make(status="success")
        resp = self.client.post(reverse("taskcenter:pause", args=[t.id]))
        self.assertEqual(resp.status_code, 400)

    def test_close_soft_hides(self):
        t = self._make(status="running")
        resp = self.client.post(reverse("taskcenter:close", args=[t.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["task"]["is_closed"])
        lst = self.client.get(reverse("taskcenter:list"))
        ids = [x["id"] for x in lst.json()["tasks"]]
        self.assertNotIn(t.id, ids)

    def test_unknown_task_404(self):
        resp = self.client.get(reverse("taskcenter:detail", args=["nope"]))
        self.assertEqual(resp.status_code, 404)
