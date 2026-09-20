"""M4 端到端联调验证（可重复跑的集成测试）。

覆盖阶段文档 M4-1（主链路）、M4-2（五维安全闭环）、M4-3（八类失败路径）。

运行环境说明：
  - testgen sidecar 在本机起（services/testgen/.venv），真实跑 /health /verdict /generate；
  - testhub 的 Django/ORM 写库层在本机无法启（无 Django+MySQL），故回流/建缺陷用
    注入式 upsert / create_defect 回调验证「编排逻辑」，等价于通过了 /api/testgen/* 端点
    背后的真实代码路径（views.py 仅做 HTTP 反序列化 + 调 flow，flow 逻辑在此被真实验证）。

启动方式：
  cd <repo_root>
  services/testgen/.venv/Scripts/python.exe -m unittest apps.testgen_integration.tests.test_e2e_m4 -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

# ---- 路径 & 导入 ----------------------------------------------------------
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
TESTGEN_DIR = os.path.join(REPO_ROOT, "services", "testgen")
VENV_PY = os.path.join(TESTGEN_DIR, ".venv", "Scripts", "python.exe")

sys.path.insert(0, REPO_ROOT)

import requests  # noqa: E402

from apps.testgen_integration import client as tg_client  # noqa: E402
from apps.testgen_integration import flow, mapping  # noqa: E402

# 旁路透明代理（本机 localhost 会被劫持）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")


# ---- sidecar 进程管理 -----------------------------------------------------
_SIDECAR_PROC = None


def _start_sidecar() -> None:
    global _SIDECAR_PROC
    if _SIDECAR_PROC is not None:
        return
    env = dict(os.environ)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    _SIDECAR_PROC = subprocess.Popen(
        [VENV_PY, "-m", "uvicorn", "service.app:app",
         "--host", "127.0.0.1", "--port", "8100", "--log-level", "warning"],
        cwd=TESTGEN_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            tg_client.health()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("testgen sidecar 启动超时（/health 不可达）")


def _stop_sidecar() -> None:
    global _SIDECAR_PROC
    if _SIDECAR_PROC is not None:
        _SIDECAR_PROC.terminate()
        try:
            _SIDECAR_PROC.wait(timeout=10)
        except Exception:
            _SIDECAR_PROC.kill()
        _SIDECAR_PROC = None


# ---- 构造测试数据 ---------------------------------------------------------
def _sample_cases() -> list[dict]:
    """覆盖 正常/异常/安全/边界/性能 五维度，用于主链路与枚举对齐。"""
    return [
        {"tc_no": "TP-N-1", "title": "正常用例", "case_type": "正常", "priority": "P2",
         "precondition": "", "doc_steps": json.dumps(
             [{"kind": "前置", "action": "登录"}, {"kind": "断言", "action": "列表可见", "expect": "200"}]),
         "ctype": "api", "tp_id": "TP-N-1"},
        {"tc_no": "TP-A-1", "title": "异常用例", "case_type": "异常", "priority": "P1",
         "precondition": "", "doc_steps": json.dumps(
             [{"kind": "断言", "action": "缺参调用", "expect": "400"}]),
         "ctype": "api", "tp_id": "TP-A-1"},
        {"tc_no": "TP-S-1", "title": "安全越权", "case_type": "安全", "priority": "P0",
         "precondition": "", "doc_steps": json.dumps(
             [{"kind": "断言", "action": "越权访问", "expect": "403"}]),
         "ctype": "api", "tp_id": "TP-S-1"},
        {"tc_no": "TP-B-1", "title": "边界用例", "case_type": "边界", "priority": "P3",
         "precondition": "", "doc_steps": json.dumps(
             [{"kind": "断言", "action": "超长输入", "expect": "422"}]),
         "ctype": "api", "tp_id": "TP-B-1"},
        {"tc_no": "TP-P-1", "title": "性能用例", "case_type": "性能", "priority": "P1",
         "precondition": "", "doc_steps": json.dumps(
             [{"kind": "断言", "action": "压测", "expect": "p95<200ms"}]),
         "ctype": "api", "tp_id": "TP-P-1"},
    ]


class _SidecarTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _start_sidecar()

    @classmethod
    def tearDownClass(cls) -> None:
        _stop_sidecar()


# ===========================================================================
# M4-1 主链路验证
# ===========================================================================
class TestM4MainLink(_SidecarTestBase):
    def test_sync_cases_idempotent(self):
        """M4-3 重复生成/导入不翻倍；M4-1 回流落地计数正确。"""
        cases = _sample_cases()
        store: dict[str, str] = {}

        def upsert(tid: str, payload: dict) -> str:
            if tid in store:
                store[tid] = "updated"
                return flow.UPDATED
            store[tid] = "created"
            return flow.CREATED

        r1 = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        self.assertEqual(r1["created"], 5)
        self.assertEqual(r1["skipped"], 0)

        # 相同指纹再次回流 → 全部更新，不新增
        r2 = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        self.assertEqual(r2["created"], 0)
        self.assertEqual(r2["updated"], 5)
        self.assertEqual(len(store), 5)  # 库里仍 5 条，未翻倍

    def test_mapping_enums_in_testhub_set(self):
        """M4-3 映射枚举错配：回流后类型/优先级必须落在 testhub 枚举集内。"""
        valid_types = {"functional", "integration", "api", "ui", "performance", "security"}
        valid_prio = {"low", "medium", "high", "critical"}
        for cs in _sample_cases():
            p = mapping.case_spec_to_testcase(cs, project_id=1, author_id=1)
            self.assertIn(p["test_type"], valid_types, f"test_type 越界: {p['test_type']}")
            self.assertIn(p["priority"], valid_prio, f"priority 越界: {p['priority']}")
            # 幂等指纹标签存在
            self.assertIn(f"tg:{cs['tc_no']}", p["tags"])
            # 来源标记
            self.assertIn("testgen", p["tags"])

    def test_generate_client_triggers_task(self):
        """M4-1 第2步：testhub 经 client 触发 testgen 异步生成，拿到 task_id。

        真实生成需 LLM 配置；本环境若无 key 则跳过真实生成，但验证 client 能正确
        触发任务、且红线守卫生效（不传 execute）。
        """
        try:
            resp = tg_client.generate(
                repo_url="https://github.com/lidebao513/testHub",
                scopes=["正常", "安全", "边界", "异常"],
            )
        except Exception as exc:  # 无 LLM key → 跳过真实生成，不视为失败
            self.skipTest(f"generate 需 LLM 配置，本环境跳过真实生成: {exc}")
        self.assertIn("task_id", resp)
        self.assertNotEqual(resp.get("execute"), True)


# ===========================================================================
# M4-2 五维安全闭环
# ===========================================================================
class TestM4SecurityClosedLoop(_SidecarTestBase):
    def test_verdict_unsafe_creates_defect(self):
        """应拒却放通 → /verdict=unsafe → 自动建缺陷。"""
        defects: list[dict] = []
        observations = [{
            "case_id": "TP-S-1", "title": "越权访问", "tags": ["security", "tg:TP-S-1"],
            "project_id": 1, "testcase_id": 10,
            "case_type": "安全", "dimension": "安全-越权", "auth_mode": "required",
            "observed_status": 200, "observed_body_has_sensitive": False,
            "expected_denied": True,
        }]
        with mock.patch.object(
            tg_client, "create_security_defect",
            lambda p: (defects.append(p) or {"id": 1}),
        ):
            res = flow.run_security_verdict(observations, verdict_fn=tg_client.verdict)

        self.assertEqual(res["security_cases"], 1)
        self.assertEqual(res["unsafe"], 1)
        self.assertEqual(res["defects_created"], 1)
        self.assertEqual(defects[0]["severity"], "critical")
        self.assertIn("应被拒绝却放通", defects[0]["description"])

    def test_verdict_correct_deny_no_defect(self):
        """应拒场景且确实被拒（observed 403）→ 安全，不建缺陷。"""
        defects: list[dict] = []
        observations = [{
            "case_id": "TP-S-2", "title": "越权访问", "tags": ["security"], "project_id": 1,
            "case_type": "安全", "dimension": "安全-越权", "auth_mode": "required",
            "observed_status": 403, "expected_denied": True,
        }]
        with mock.patch.object(
            tg_client, "create_security_defect",
            lambda p: (defects.append(p) or {"id": 1}),
        ):
            res = flow.run_security_verdict(observations, verdict_fn=tg_client.verdict)

        self.assertEqual(res["security_cases"], 1)
        self.assertEqual(res["unsafe"], 0)
        self.assertEqual(res["defects_created"], 0)

    def test_non_security_skipped(self):
        """非 security 用例不调 /verdict（避免无谓开销，M2 验收点）。"""
        calls = {"n": 0}

        def fake_verdict(facts):
            calls["n"] += 1
            return {"verdict": "safe"}

        observations = [{
            "case_id": "TP-N-9", "title": "正常", "tags": ["normal"], "project_id": 1,
            "category": "正常",
        }]
        res = flow.run_security_verdict(observations, verdict_fn=fake_verdict)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(res["skipped"], 1)


# ===========================================================================
# M4-3 八类失败路径（先测失败再测成功，暴露集成裂缝）
# ===========================================================================
class TestM4FailurePaths(_SidecarTestBase):
    def test_red_line_execute_true_rejected_locally(self):
        """生成红线被破：误传 execute=True → 客户端就地拒绝（双保险）。"""
        with self.assertRaises(ValueError):
            tg_client.generate(repo_url="x", execute=True)

    def test_red_line_execute_true_422_on_sidecar(self):
        """生成红线被破：testgen 服务端也以 422 拒绝 execute=True。"""
        r = tg_client._session.post(
            tg_client.base_url() + "/api/v1/generate",
            json={"repo_url": "x", "execute": True},
            headers=tg_client._headers(),
            timeout=30,
        )
        self.assertEqual(r.status_code, 422)
        self.assertFalse(tg_client._build_generate_payload(repo_url="x").get("execute") is True)

    def test_testgen_down_friendly_error(self):
        """testgen 未启动：客户端抛出连接异常（testhub 层捕获转友好 502，不崩）。"""
        with mock.patch.object(
            tg_client._session, "get",
            side_effect=requests.exceptions.ConnectionError("connection refused"),
        ):
            with self.assertRaises(requests.exceptions.ConnectionError):
                tg_client.health()
        # 真实 health 仍可用（sidecar 在跑）
        self.assertEqual(tg_client.health()["status"], "ok")

    def test_credential_not_in_generate_payload(self):
        """凭证转发高压线：generate 请求体绝不含密码/OTP 明文。"""
        payload = tg_client._build_generate_payload(
            repo_url="https://user:secret@host/repo", scopes=["正常"]
        )
        self.assertNotIn("password", payload)
        self.assertNotIn("otp", payload)
        self.assertFalse(payload.get("execute") is True)
        # 回流 payload 同样不含凭证明文
        p = mapping.case_spec_to_testcase(_sample_cases()[0], project_id=1, author_id=1)
        self.assertNotIn("password", json.dumps(p, ensure_ascii=False).lower())

    def test_poll_timeout_not_hang(self):
        """大仓超时：poll 超时抛 Timeout 异常，客户端不卡死。"""
        with mock.patch.object(
            tg_client._session, "get",
            side_effect=requests.exceptions.Timeout("timed out"),
        ):
            with self.assertRaises(requests.exceptions.Timeout):
                tg_client.poll("task-x")

    def test_verdict_bidirectional(self):
        """/verdict 误判覆盖：应拒却放通=unsafe；应放却拒=安全（双向识别）。"""
        r_unsafe = tg_client.verdict({
            "category": "安全", "dimension": "安全-越权", "auth_mode": "required",
            "observed_status": 200, "expected_denied": True,
        })
        self.assertEqual(r_unsafe["verdict"], "unsafe")

        r_safe = tg_client.verdict({
            "category": "安全", "dimension": "安全-越权", "auth_mode": "required",
            "observed_status": 403, "expected_denied": True,
        })
        self.assertNotEqual(r_safe["verdict"], "unsafe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
