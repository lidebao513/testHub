"""G2（sidecar 可达性 503 友好报错）+ G7（Web 平台操作入口）自测。

纯离线：sidecar 调用全部用 mock 替换，不触真实服务/DB；模板渲染用 loader 校验。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import requests
from django.test import TestCase as DjangoTestCase

from apps.testgen_integration import client as tg_client
from apps.testgen_integration import views
from apps.testgen_integration.client import classify_sidecar_error


class FakeReq:
    """最小请求桩：仅暴露 .data（DRF 视图只读 request.data）。"""

    def __init__(self, data):
        self.data = data


class TestGenerateProjectValidation(DjangoTestCase):
    """生成接口的项目关联校验：非法 project_id 在触达 sidecar 前拒绝；合法则落任务中心。"""

    def _req(self, data, user=None):
        req = FakeReq(data)
        if user is not None:
            req.user = user
        return req

    def test_invalid_project_rejected_before_sidecar_call(self):
        with patch("apps.testgen_integration.views.generate") as m:
            resp = views.TestgenGenerateView().post(
                self._req({"local_path": "/x", "project_id": 999999})
            )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(m.called)

    def test_non_int_project_rejected(self):
        resp = views.TestgenGenerateView().post(
            self._req({"local_path": "/x", "project_id": "abc"})
        )
        self.assertEqual(resp.status_code, 400)

    def test_valid_project_records_task_with_project(self):
        from django.contrib.auth import get_user_model

        from apps.projects.models import Project
        from apps.taskcenter.models import BackgroundTask

        U = get_user_model()
        user = U.objects.create_user(username="gen_proj_u", password="x-Temp#123")
        proj = Project.objects.create(name="P1", owner=user)
        with patch("apps.testgen_integration.views.generate") as m:
            m.return_value = {"task_id": "gen-prj-ok", "state": "pending"}
            resp = views.TestgenGenerateView().post(
                self._req(
                    {
                        "repo_url": "https://codeup.aliyun.com/org/repo.git",
                        "project_id": proj.id,
                        "gen_range": "incremental",
                    },
                    user=user,
                )
            )
        self.assertEqual(resp.status_code, 200)
        t = BackgroundTask.objects.get(source="gen-prj-ok")
        self.assertTrue(t.name.startswith("[P1]"))
        self.assertIn('"project_id": ' + str(proj.id), t.payload_summary)
        self.assertIn('"gen_range": "incremental"', t.payload_summary)

    # ---- code_source（远程只读取码凭据）校验 ----

    def test_code_source_missing_org_rejected(self):
        with patch("apps.testgen_integration.views.generate") as m:
            resp = views.TestgenGenerateView().post(
                self._req(
                    {
                        "repo_url": "https://codeup.aliyun.com/org/repo.git",
                        "code_source": {
                            "credential_type": "pat",
                            "access_key": "abcd1234",
                        },
                    }
                )
            )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(m.called)

    def test_code_source_missing_access_key_rejected(self):
        with patch("apps.testgen_integration.views.generate") as m:
            resp = views.TestgenGenerateView().post(
                self._req(
                    {
                        "repo_url": "https://codeup.aliyun.com/org/repo.git",
                        "code_source": {"credential_type": "pat", "org_id": "e1"},
                    }
                )
            )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(m.called)

    def test_code_source_ak_sk_requires_secret(self):
        with patch("apps.testgen_integration.views.generate") as m:
            resp = views.TestgenGenerateView().post(
                self._req(
                    {
                        "repo_url": "https://codeup.aliyun.com/org/repo.git",
                        "code_source": {
                            "credential_type": "ak_sk",
                            "org_id": "e1",
                            "access_key": "AKID",
                        },
                    }
                )
            )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(m.called)

    def test_code_source_valid_masked_in_task(self):
        from django.contrib.auth import get_user_model

        from apps.taskcenter.models import BackgroundTask

        U = get_user_model()
        user = U.objects.create_user(username="gen_cs_u", password="x-Temp#123")
        with patch("apps.testgen_integration.views.generate") as m:
            m.return_value = {"task_id": "gen-cs-ok", "state": "pending"}
            resp = views.TestgenGenerateView().post(
                self._req(
                    {
                        "repo_url": "https://codeup.aliyun.com/org/repo.git",
                        "code_source": {
                            "credential_type": "pat",
                            "org_id": "org-8888",
                            "access_key": "abcdefghijklmnop",
                        },
                    },
                    user=user,
                )
            )
        self.assertEqual(resp.status_code, 200)
        t = BackgroundTask.objects.get(source="gen-cs-ok")
        self.assertIn("org-8888", t.payload_summary)
        self.assertNotIn("abcdefghijklmnop", t.payload_summary)  # 原文绝不落库
        self.assertIn("abcd****op", t.payload_summary)


class TestG2SidecarErrors(unittest.TestCase):
    def test_classify_sidecar_error(self):
        self.assertEqual(
            classify_sidecar_error(requests.exceptions.ConnectionError("x")), 503
        )
        self.assertEqual(classify_sidecar_error(requests.exceptions.Timeout("x")), 503)
        self.assertEqual(
            classify_sidecar_error(requests.exceptions.HTTPError("x")), 502
        )
        self.assertEqual(classify_sidecar_error(ValueError("x")), 502)

    def test_is_sidecar_available_false_when_down(self):
        with patch.object(
            tg_client._session,
            "get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            ok, detail = tg_client.is_sidecar_available()
        self.assertFalse(ok)
        self.assertIn("不可达", detail)

    def test_generate_view_503_when_sidecar_down(self):
        with patch(
            "apps.testgen_integration.views.generate",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            resp = views.TestgenGenerateView().post(FakeReq({"local_path": "/x"}))
        self.assertEqual(resp.status_code, 503)
        self.assertIn("不可达", resp.data["error"])

    def test_sync_view_503_when_sidecar_down(self):
        with patch(
            "apps.testgen_integration.views.get_cases",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            resp = views.TestgenSyncView().post(
                FakeReq({"project_id": "28", "testgen_project_id": "2"})
            )
        self.assertEqual(resp.status_code, 503)
        self.assertIn("不可达", resp.data["error"])

    def test_security_verdict_503_when_sidecar_down(self):
        with patch(
            "apps.testgen_integration.views.is_sidecar_available",
            return_value=(False, "down"),
        ):
            payload, status = views.execute_security_verdict(
                {"observations": [{"case_id": 1, "tags": ["security"]}]}, user_id=1
            )
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertIn("不可达", payload["error"])

    def test_health_view(self):
        with patch(
            "apps.testgen_integration.views.is_sidecar_available",
            return_value=(True, "ok"),
        ):
            resp = views.TestgenHealthView().get(FakeReq({}))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["available"])
        self.assertEqual(resp.data["base_url"], tg_client.base_url())

    def test_task_status_view_503_when_down(self):
        with patch(
            "apps.testgen_integration.views.poll",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            resp = views.TestgenTaskStatusView().get(FakeReq({}), task_id="abc")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("不可达", resp.data["error"])

    def test_task_status_view_ok(self):
        with patch(
            "apps.testgen_integration.views.poll",
            return_value={"state": "success", "task_id": "abc"},
        ):
            resp = views.TestgenTaskStatusView().get(FakeReq({}), task_id="abc")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["state"], "success")


class TestG7ConsolePage(unittest.TestCase):
    def test_template_renders(self):
        from django.template.loader import render_to_string

        html = render_to_string("testgen/console.html")
        self.assertIn("testgen", html.lower())
        self.assertIn("/api/testgen/generate", html)
        self.assertIn("/api/testgen/sync", html)
        self.assertIn("/api/testgen/security-verdict", html)
        self.assertIn("/api/defects/defects/", html)
