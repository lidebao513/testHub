"""M2 自测：五维安全判定（testgen /verdict + testhub 客户端 + 回流编排）。

1) 纯函数：``engine.verdict.run_five_dimension_verdict``（复用 executor._judge，不重写逻辑）；
2) 实时：启动真实 sidecar，验证 /api/v1/verdict 与 client.verdict()；
3) 离线编排：flow.run_security_verdict 注入式验证「非 security 跳过 / 应拒却放通自动建缺陷」。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from apps.testgen_integration import client, flow

# apps/testgen_integration/tests/test_verdict.py -> 4 级 dirname 到仓库根
_REPO = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_TESTGEN_DIR = os.path.join(_REPO, "services", "testgen")
_VENV_PY = os.path.join(_TESTGEN_DIR, ".venv", "Scripts", "python.exe")
_BASE = "http://127.0.0.1:8100"

# 纯函数单测需要 import engine.verdict（services/testgen 为 engine 包根）
if _TESTGEN_DIR not in sys.path:
    sys.path.insert(0, _TESTGEN_DIR)


def _import_verdict_fn():
    try:
        from engine.verdict import run_five_dimension_verdict

        return run_five_dimension_verdict
    except Exception as exc:  # noqa: BLE001
        raise unittest.SkipTest(f"无法导入 engine.verdict（{exc}）")


# ===========================================================================
# 1) 纯函数单测（判定逻辑不重写，仅验证复用与语义）
# ===========================================================================
class TestVerdictPure(unittest.TestCase):
    def setUp(self):
        self.fn = _import_verdict_fn()

    def test_denied_but_passed_priv_esc(self):
        """越权用例：期望被拒(200 放通) → unsafe，dimension=priv_esc。"""
        r = self.fn({
            "category": "安全", "dimension": "安全-越权",
            "observed_status": 200, "expected_denied": True,
        })
        self.assertEqual(r["verdict"], "unsafe")
        self.assertIn("priv_esc", r["dimension"])
        self.assertIn("应被拒绝却放通", r["reason"])

    def test_denied_ok_auth(self):
        """鉴权缺失用例：期望被拒(403) → safe，dimension=auth。"""
        r = self.fn({
            "category": "安全", "dimension": "安全-鉴权缺失",
            "observed_status": 403, "expected_denied": True,
        })
        self.assertEqual(r["verdict"], "safe")
        self.assertIn("auth", r["dimension"])

    def test_english_alias_normalized(self):
        """英文别名(category=security, dimension=priv_esc)应被归一化到原生中文判定。"""
        r = self.fn({
            "category": "security", "dimension": "priv_esc",
            "observed_status": 200, "expected_denied": True,
        })
        self.assertEqual(r["verdict"], "unsafe")
        self.assertIn("priv_esc", r["dimension"])

    def test_inconclusive_without_status(self):
        """缺少 observed_status → 不可判定（诚实降级，不臆造结论）。"""
        r = self.fn({"category": "安全", "dimension": "安全-越权", "expected_denied": True})
        self.assertEqual(r["verdict"], "inconclusive")

    def test_body_sensitive_forces_unsafe(self):
        """正常用例但响应体泄露敏感 → 直接判 unsafe（无论状态码）。"""
        r = self.fn({
            "category": "正常", "observed_status": 200,
            "observed_body_has_sensitive": True,
        })
        self.assertEqual(r["verdict"], "unsafe")
        self.assertTrue(r["evidence"].get("leak_suspected"))

    def test_normal_pass(self):
        """普通功能用例 200 → safe。"""
        r = self.fn({"category": "正常", "observed_status": 200})
        self.assertEqual(r["verdict"], "safe")


# ===========================================================================
# 2) 实时：真实 sidecar 的 /verdict 与 client.verdict()
# ===========================================================================
def _sidecar_available() -> bool:
    try:
        return client.health().get("status") == "ok"
    except Exception:
        return False


class TestVerdictLive(unittest.TestCase):
    _proc = None

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_VENV_PY):
            raise unittest.SkipTest("testgen venv 不存在，跳过实时集成")
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

    def test_endpoint_verdict_unsafe(self):
        r = client._session.post(
            f"{_BASE}/api/v1/verdict",
            json={"category": "安全", "dimension": "安全-越权",
                  "observed_status": 200, "expected_denied": True},
            timeout=10,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["verdict"], "unsafe")
        self.assertIn("priv_esc", body["dimension"])
        # 响应不含凭证明文
        self.assertNotIn("password", str(body).lower())
        self.assertNotIn("token", str(body).lower())

    def test_client_verdict_unsafe(self):
        body = client.verdict({
            "category": "security", "dimension": "auth_miss",
            "observed_status": 200, "expected_denied": True,
        })
        self.assertEqual(body["verdict"], "unsafe")
        self.assertIn("auth", body["dimension"])


# ===========================================================================
# 3) 离线编排：run_security_verdict 注入式
# ===========================================================================
class TestFlowSecurityVerdict(unittest.TestCase):
    def test_non_security_skipped(self):
        calls = []
        res = flow.run_security_verdict(
            [{"case_id": "C1", "title": "登录", "tags": ["normal"]}],
            verdict_fn=lambda f: calls.append(f) or {"verdict": "safe"},
            create_defect=lambda p: None,
        )
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["security_cases"], 0)
        self.assertEqual(calls, [])  # 非 security 不调 /verdict

    def test_unsafe_creates_defect(self):
        defects = []
        res = flow.run_security_verdict(
            [{
                "case_id": "C2", "title": "越权访问", "tags": ["security"],
                "case_type": "安全", "dimension": "安全-越权",
                "observed_status": 200, "expected_denied": True,
                "project_id": 7, "testcase_id": 42,
            }],
            verdict_fn=lambda f: {"verdict": "unsafe", "dimension": "priv_esc",
                                  "reason": "应被拒绝却放通：越权访问未被拒绝（200）",
                                  "evidence": {"observed_status": 200}},
            create_defect=defects.append,
        )
        self.assertEqual(res["security_cases"], 1)
        self.assertEqual(res["unsafe"], 1)
        self.assertEqual(res["defects_created"], 1)
        self.assertEqual(defects[0]["severity"], flow.SEVERITY_FOR_UNSAFE)
        self.assertEqual(defects[0]["priority"], flow.PRIORITY_FOR_UNSAFE)
        self.assertEqual(defects[0]["project_id"], 7)
        self.assertEqual(defects[0]["related_testcase_id"], 42)

    def test_safe_no_defect(self):
        defects = []
        res = flow.run_security_verdict(
            [{
                "case_id": "C3", "title": "无凭证拒绝", "tags": ["security"],
                "case_type": "安全", "dimension": "安全-鉴权缺失",
                "observed_status": 403, "expected_denied": True,
            }],
            verdict_fn=lambda f: {"verdict": "safe", "dimension": "auth",
                                  "reason": "无凭证访问被拒（403）", "evidence": {}},
            create_defect=defects.append,
        )
        self.assertEqual(res["unsafe"], 0)
        self.assertEqual(defects, [])

    def test_verdict_error_isolated(self):
        """单条裁判异常不应中断整批，且不建缺陷。"""
        defects = []
        res = flow.run_security_verdict(
            [{
                "case_id": "C4", "title": "x", "tags": ["security"],
                "case_type": "安全", "dimension": "安全-越权",
                "observed_status": 200, "expected_denied": True,
            }],
            verdict_fn=lambda f: (_ for _ in ()).throw(RuntimeError("boom")),
            create_defect=defects.append,
        )
        self.assertEqual(res["security_cases"], 1)
        self.assertEqual(defects, [])
        self.assertEqual(res["details"][0]["verdict"], "error")


if __name__ == "__main__":
    unittest.main()
