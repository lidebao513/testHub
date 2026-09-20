"""集成验证：

1) 实时：启动真实 testgen sidecar（services/testgen/.venv），验证客户端连通性、
   /ready 不泄漏密钥、/parse-input 掩码密码（不触发真实 LLM 生成）；
2) 离线编排：用仿 testgen 返回用例，跑 mapping + flow 端到端，验证回流字段正确。

本机若缺 testgen venv 或 8100 端口被占，实时部分自动跳过；离线编排始终运行。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import unittest

from apps.testgen_integration import client, flow, mapping

# apps/testgen_integration/tests/test_integration.py -> 4 级 dirname 到仓库根
_REPO = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_TESTGEN_DIR = os.path.join(_REPO, "services", "testgen")
_VENV_PY = os.path.join(_TESTGEN_DIR, ".venv", "Scripts", "python.exe")
_BASE = "http://127.0.0.1:8100"


def _sidecar_available() -> bool:
    try:
        return client.health().get("status") == "ok"
    except Exception:
        return False


class TestgenSidecarLive(unittest.TestCase):
    _proc = None

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_VENV_PY):
            raise unittest.SkipTest("testgen venv 不存在，跳过实时集成")
        # 若端口已被占用且可达，直接复用，不重复启动
        if _sidecar_available():
            return
        cls._proc = subprocess.Popen(
            [
                _VENV_PY, "-m", "uvicorn", "service.app:app",
                "--host", "127.0.0.1", "--port", "8100", "--log-level", "warning",
            ],
            cwd=_TESTGEN_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if _sidecar_available():
                return
            time.sleep(0.5)
        cls.tearDownClass()
        raise unittest.SkipTest("sidecar 启动超时或不达")

    @classmethod
    def tearDownClass(cls):
        if cls._proc is not None:
            cls._proc.terminate()
            try:
                cls._proc.wait(timeout=5)
            except Exception:
                cls._proc.kill()

    def test_health_ok(self):
        self.assertEqual(client.health()["status"], "ok")

    def test_ready_no_secret_leak(self):
        r = client._session.get(f"{_BASE}/ready", timeout=5)
        body = r.text
        for leak in ("PASSWORD", "OTP", "API_KEY", "secret", "TOP_SECRET"):
            self.assertNotIn(leak, body)

    def test_parse_input_masks_password(self):
        r = client._session.post(
            f"{_BASE}/api/v1/parse-input",
            json={
                "text": "https://demo.test.com/login user=admin "
                "password=TopSecret123! otp=884211"
            },
            timeout=5,
        )
        body = r.text
        self.assertNotIn("TopSecret123!", body)
        self.assertIn("***", body)

    def test_get_cases_empty_project(self):
        # 不存在的项目应返回空列表（不报错）
        cases = client.get_cases(999999)
        self.assertIsInstance(cases, list)


class TestOfflineOrchestration(unittest.TestCase):
    """mapping + flow 端到端：不依赖真实 sidecar，验证回流字段正确。"""

    def _memory_upsert(self):
        store: dict = {}

        def upsert(testgen_id, payload):
            key = (payload["project_id"], testgen_id)
            if key in store:
                store[key] = payload
                return flow.UPDATED
            store[key] = payload
            return flow.CREATED

        return store, upsert

    def test_full_pipeline_mapping(self):
        cases = [
            {
                "tc_no": "TP-A1", "title": "登录正常", "case_type": "正常", "priority": "P1",
                "precondition": "已注册",
                "doc_steps": json.dumps([{"kind": "断言", "action": "登录", "expect": "成功"}]),
                "ctype": "api", "tp_id": "TP-A1",
            },
            {
                "tc_no": "TP-A2", "title": "登录安全", "case_type": "安全", "priority": "P0",
                "doc_steps": json.dumps([{"kind": "断言", "action": "错误密码", "expect": "拒绝"}]),
                "ctype": "api", "tp_id": "TP-A2",
            },
        ]
        store, upsert = self._memory_upsert()
        result = flow.sync_cases(cases, project_id=5, author_id=2, upsert=upsert)
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(store), 2)

        a1 = store[(5, "TP-A1")]
        self.assertEqual(a1["test_type"], "api")
        self.assertEqual(a1["priority"], "high")
        self.assertIn("tg:TP-A1", a1["tags"])
        self.assertIn("成功", a1["expected_result"])

        a2 = store[(5, "TP-A2")]
        self.assertEqual(a2["test_type"], "security")
        self.assertEqual(a2["priority"], "critical")
        self.assertIn("拒绝", a2["expected_result"])

    def test_idempotent_rerun(self):
        cases = [{"tc_no": "TP-B", "title": "t", "case_type": "正常", "priority": "P2"}]
        store, upsert = self._memory_upsert()
        r1 = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        r2 = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        self.assertEqual(r1["created"], 1)
        self.assertEqual(r2["updated"], 1)
        self.assertEqual(len(store), 1)  # 不翻倍


if __name__ == "__main__":
    unittest.main()
