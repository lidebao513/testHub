"""client 单测：红线守卫 + 请求体构造（mock requests，不依赖真实 sidecar）。"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from apps.testgen_integration import client


def _fake_resp(json_data: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.raise_for_status.return_value = None
    return m


class TestRedLine(unittest.TestCase):
    def test_generate_execute_true_rejected_before_request(self):
        # 红线在发出 HTTP 请求前就拒绝，post 绝不应被调用
        with patch.object(client._session, "post") as post:
            with self.assertRaises(ValueError):
                client.generate(execute=True)
            post.assert_not_called()

    def test_generate_payload_has_execute_false(self):
        payload = client._build_generate_payload(local_path="/x")
        self.assertFalse(payload["execute"])
        self.assertNotIn("exec_url", payload)


class TestGenerateCall(unittest.TestCase):
    @patch.object(client._session, "post")
    def test_generate_posts_to_api_v1_generate(self, post):
        post.return_value = _fake_resp({"task_id": "T1", "poll_url": "/x", "state": "pending"})
        resp = client.generate(local_path="/repo", scopes=["正常", "安全"])
        self.assertEqual(resp["task_id"], "T1")
        self.assertTrue(post.called)
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/api/v1/generate"))
        self.assertFalse(kwargs["json"]["execute"])
        self.assertEqual(kwargs["json"]["local_path"], "/repo")
        self.assertEqual(kwargs["json"]["scopes"], ["正常", "安全"])


class TestHealthAndPull(unittest.TestCase):
    @patch.object(client._session, "get")
    def test_health(self, get):
        get.return_value = _fake_resp({"status": "ok", "service": "testgen-service"})
        self.assertEqual(client.health()["status"], "ok")
        self.assertTrue(get.call_args[0][0].endswith("/health"))

    @patch.object(client._session, "get")
    def test_poll(self, get):
        get.return_value = _fake_resp({"task_id": "T1", "state": "success"})
        self.assertEqual(client.poll("T1")["state"], "success")

    @patch.object(client._session, "get")
    def test_get_cases(self, get):
        get.return_value = _fake_resp({"cases": [{"tc_no": "TP-1"}]})
        self.assertEqual(client.get_cases(1), [{"tc_no": "TP-1"}])

    @patch.object(client._session, "post")
    def test_pull(self, post):
        post.return_value = _fake_resp({"ok": True})
        r = client.pull(repo_url="https://x/y.git")
        self.assertTrue(r["ok"])
        self.assertTrue(post.call_args[0][0].endswith("/api/v1/pull"))


class TestQualitySummary(unittest.TestCase):
    """M13/M14：quality_summary 需真正并发（as_completed），且顶层 ok 以「是否抛异常」为准。"""

    def test_all_ok_true_when_all_succeed(self):
        """四项均返回业务数据（无 ok 键）时 ok 必须为 True——旧逻辑取 s['ok'] 会恒为 False。"""
        with patch.object(client, "quality_trend", lambda pid: {"points": [1]}), \
                patch.object(client, "quality_flaky", lambda pid: {"cases": []}), \
                patch.object(client, "quality_filter_options", lambda pid: {"dims": []}), \
                patch.object(client, "quality_change_logs", lambda pid: {"changes": []}):
            summary = client.quality_summary(1)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["trend"], {"points": [1]})
        self.assertEqual(summary["flaky"], {"cases": []})

    def test_all_ok_false_when_one_raises(self):
        """任一项抛异常 → 顶层 ok=False，失败项带 error，其余项正常返回。"""
        def boom(pid):
            raise RuntimeError("sidecar down")

        with patch.object(client, "quality_trend", lambda pid: {"points": []}), \
                patch.object(client, "quality_flaky", boom), \
                patch.object(client, "quality_filter_options", lambda pid: {"dims": []}), \
                patch.object(client, "quality_change_logs", lambda pid: {"changes": []}):
            summary = client.quality_summary(1)
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["flaky"]["ok"])
        self.assertIn("sidecar down", summary["flaky"]["error"])
        self.assertEqual(summary["trend"], {"points": []})


if __name__ == "__main__":
    unittest.main()
