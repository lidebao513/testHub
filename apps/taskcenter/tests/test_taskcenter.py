"""任务中心服务层测试：落库、状态同步、重试、暂停、关闭。"""
from unittest import mock

from django.test import TestCase

from apps.taskcenter import services
from apps.taskcenter.models import BackgroundTask


class _FakeUser:
    def __init__(self, id, username):
        self.id = id
        self.username = username


class TaskCenterServiceTest(TestCase):
    def test_record_generate_and_close(self):
        t = services.record_generate(user=_FakeUser(1, "admin"), task_id="gen-abc", local_path="/x")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.source, "gen-abc")
        services.close_task(t)
        t.refresh_from_db()
        self.assertTrue(t.is_closed)

    def test_record_generate_with_project(self):
        """生成任务带项目关联：project 落 payload_summary，任务名带项目前缀。"""
        t = services.record_generate(
            user=_FakeUser(1, "admin"),
            task_id="gen-prj",
            repo_url="https://codeup.aliyun.com/org/repo.git",
            project_id=28,
            project_name="testgen-integ-demo",
            gen_range="full",
        )
        self.assertIn("[testgen-integ-demo]", t.name)
        import json as _json

        payload = _json.loads(t.payload_summary)
        self.assertEqual(payload["project_id"], 28)
        self.assertEqual(payload["project_name"], "testgen-integ-demo")
        self.assertEqual(payload["gen_range"], "full")

    def test_pause_calls_sidecar_cancel(self):
        t = BackgroundTask.objects.create(
            name="t", task_type="generate", source="gen-xyz", status="running"
        )
        with mock.patch("apps.testgen_integration.client.cancel_task") as m:
            m.return_value = {"ok": True}
            services.pause_task(t)
        m.assert_called_once_with("gen-xyz")
        t.refresh_from_db()
        self.assertEqual(t.status, "paused")

    def test_pause_rejects_terminal(self):
        t = BackgroundTask.objects.create(name="t", task_type="generate", status="success")
        with self.assertRaises(ValueError):
            services.pause_task(t)

    def test_retry_reissues_generate(self):
        t = BackgroundTask.objects.create(
            name="t",
            task_type="generate",
            status="failed",
            payload_summary='{"local_path":"/x","repo_url":"","scopes":["正常"],"changed_files":[],"mode":""}',
        )
        with mock.patch("apps.testgen_integration.client.generate") as m:
            m.return_value = {"task_id": "gen-new"}
            services.retry_task(t, user=_FakeUser(2, "bob"))
        self.assertTrue(m.called)
        t.refresh_from_db()
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.source, "gen-new")
        self.assertEqual(t.error, "")

    def test_retry_rejects_non_generate(self):
        t = BackgroundTask.objects.create(name="t", task_type="sync", status="failed")
        with self.assertRaises(ValueError):
            services.retry_task(t)

    def test_record_sync_and_verdict(self):
        s = services.record_sync(user=_FakeUser(3, "carol"), summary='{"created":1}')
        self.assertEqual(s.status, "success")
        self.assertEqual(s.task_type, "sync")
        v = services.record_verdict(user_id=4, username="dave", summary='{"safe":2}')
        self.assertEqual(v.status, "success")
        self.assertEqual(v.task_type, "verdict")
