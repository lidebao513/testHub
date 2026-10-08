"""G3/G5 自测：安全裁判端点逻辑 + 缺陷 ORM 直写字段校验（纯离线，不依赖 sidecar/DB）。

- ``execute_security_verdict`` 以注入式 verdict_fn 验证端点编排（不触真实 testgen）；
- ``create_security_defect_orm`` 字段对齐：有效 payload 直接透传给 ``Defect.objects.create``，
  不翻译；缺 project_id / reporter_id 时提前抛 ValueError（且不触发 Django 加载）。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from apps.testgen_integration import views
from apps.testgen_integration.defects import create_security_defect_orm


class TestExecuteSecurityVerdictLogic(unittest.TestCase):
    def test_missing_observations_returns_400(self):
        payload, status = views.execute_security_verdict({}, user_id=1)
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("observations", payload["error"])

    def test_non_list_observations_returns_400(self):
        _, status = views.execute_security_verdict({"observations": "x"}, user_id=1)
        self.assertEqual(status, 400)

    def test_runs_with_injected_verdict(self):
        fake = {
            "total": 1,
            "security_cases": 1,
            "unsafe": 1,
            "defects_created": 1,
            "skipped": 0,
            "details": [],
        }
        obs = [
            {"case_id": "C1", "title": "越权", "tags": ["security"], "project_id": 7}
        ]
        with (
            patch(
                "apps.testgen_integration.flow.run_security_verdict", return_value=fake
            ) as m,
            patch(
                "apps.testgen_integration.views.is_sidecar_available",
                return_value=(True, "ok"),
            ),
        ):
            payload, status = views.execute_security_verdict(
                {"observations": obs}, user_id=1
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["defects_created"], 1)
        # 注入式断言：verdict_fn=client.verdict，create_defect=G5 ORM 直写回调
        _, kwargs = m.call_args
        self.assertIs(kwargs["verdict_fn"], views.verdict)
        self.assertIs(kwargs["create_defect"], create_security_defect_orm)
        self.assertEqual(kwargs["reporter_id"], 1)

    def test_default_project_id_backfilled(self):
        fake = {
            "total": 1,
            "security_cases": 1,
            "unsafe": 0,
            "defects_created": 0,
            "skipped": 0,
            "details": [],
        }
        obs = [{"case_id": "C1", "tags": ["security"], "project_id": None}]
        with (
            patch(
                "apps.testgen_integration.flow.run_security_verdict", return_value=fake
            ) as m,
            patch(
                "apps.testgen_integration.views.is_sidecar_available",
                return_value=(True, "ok"),
            ),
        ):
            views.execute_security_verdict(
                {"observations": obs, "project_id": 99}, user_id=1
            )
        passed_obs = m.call_args.args[0]
        self.assertEqual(passed_obs[0]["project_id"], 99)


class TestCreateSecurityDefectORM(unittest.TestCase):
    def test_missing_project_id_raises(self):
        with self.assertRaises(ValueError):
            create_security_defect_orm({"title": "x", "reporter_id": 1})

    def test_missing_reporter_id_raises(self):
        with self.assertRaises(ValueError):
            create_security_defect_orm({"title": "x", "project_id": 7})

    def test_valid_payload_passthrough_to_create(self):
        """G5 契约：有效 payload 字段直接透传 Defect.objects.create（不翻译）。"""
        captured: dict = {}
        fake_defect = object()

        class _FakeQS:
            def create(self, **kw):
                captured.update(kw)
                return fake_defect

        class _FakeDefect:
            objects = _FakeQS()

        with patch("apps.defects.models.Defect", _FakeDefect):
            result = create_security_defect_orm(
                {
                    "title": "t",
                    "description": "d",
                    "severity": "critical",
                    "priority": "p0",
                    "defect_type": "security",
                    "source": "api_testing",
                    "project_id": 7,
                    "reporter_id": 1,
                    "related_testcase_id": 42,
                }
            )
        self.assertIs(result, fake_defect)
        self.assertEqual(captured["project_id"], 7)
        self.assertEqual(captured["reporter_id"], 1)
        self.assertEqual(captured["related_testcase_id"], 42)
        self.assertEqual(captured["defect_type"], "security")
        self.assertEqual(captured["source"], "api_testing")


if __name__ == "__main__":
    unittest.main()
