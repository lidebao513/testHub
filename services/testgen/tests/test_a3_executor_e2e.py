"""P1-3 · 接口层执行器真实 socket 端到端测试。

与 `test_a3_executor.py`（用 `_FakeSession` 桩替 requests 判定逻辑）互补：
本文件**起一个真实的本地 HTTP 服务** + 用真实 `requests.Session` 发请求，
覆盖「真实网络栈」这一层——判定逻辑、路径参数物化、鉴权头注入/缺失、
写操作默认跳过、超时转 error——这些是假会话测不到的。

不依赖任何外部目标/凭证；端口用 `0` 让 OS 分配，测试间互不打架。
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
import requests

from core.contracts import CaseSpec
from core.enums import Dimension, ExecStatus, TPType, VerifyLayer
from engine.executor import ExecutorOptions, execute_case


# ---------------------------------------------------------------------------
# 真实本地服务
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    # 收到的请求记录（跨线程，加锁）
    log: ClassVar[list[dict[str, Any]]] = []
    _lock = threading.Lock()

    def _record(self, method: str, path: str) -> None:
        with _Handler._lock:
            _Handler.log.append(
                {
                    "method": method,
                    "path": path,
                    "auth": self.headers.get("Authorization", ""),
                }
            )

    def _send(self, code: int, body: str = "ok") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._record("GET", self.path)
        if self.path == "/health" or self.path.startswith("/items/"):
            self._send(200, "ok")
        elif self.path == "/secure":
            auth = self.headers.get("Authorization", "")
            if auth == "Bearer placeholder-token":
                self._send(200, "granted")
            else:
                self._send(401, "no auth")
        elif self.path == "/profile":
            self._send(200, "profile")
        elif self.path == "/slow":
            time.sleep(2)  # 故意慢于客户端 timeout
            self._send(200, "late")
        else:
            self._send(404, "missing")

    def do_POST(self) -> None:
        self._record("POST", self.path)
        self._send(200, "created")

    def log_message(self, *args: Any) -> None:  # 静默默认访问日志
        return


def _free_port_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    return server


@pytest.fixture()
def server():
    with _Handler._lock:
        _Handler.log.clear()
    srv = _free_port_server()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    yield base
    srv.shutdown()
    srv.server_close()


def _case(tc_no, method, path, category=TPType.NORMAL.value, dimension=Dimension.AVAIL.value):
    return CaseSpec(
        tc_no=tc_no,
        title=f"{tc_no} {method} {path}",
        ctype="api",
        case_type=category,
        steps=[
            {
                "layer": VerifyLayer.INTERFACE.value,
                "method": method,
                "path": path,
                "dimension": dimension,
            }
        ],
    )


def _recorded() -> list[dict[str, Any]]:
    with _Handler._lock:
        return list(_Handler.log)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_normal_get_pass(server):
    """正常 GET 到 200 接口 → 通过，且服务端真实收到请求。"""
    opts = ExecutorOptions(base_url=server, timeout=5)
    res = execute_case(_case("TC-OK", "GET", "/health"), opts, session=requests.Session())
    assert res.status == ExecStatus.PASS.value
    assert any(r["method"] == "GET" and r["path"] == "/health" for r in _recorded())


def test_path_param_materialized(server):
    """路径参数 {id} 被物化为 path_param_value 后再请求。"""
    opts = ExecutorOptions(base_url=server, timeout=5, path_param_value="42")
    case = _case("TC-PATH", "GET", "/items/{id}")
    res = execute_case(case, opts, session=requests.Session())
    assert res.status == ExecStatus.PASS.value
    assert any(r["path"] == "/items/42" for r in _recorded())


def test_auth_header_injected(server):
    """带 auth_token 的用例应在真实请求里携带 Authorization 头。"""
    opts = ExecutorOptions(base_url=server, timeout=5, auth_token="placeholder-token")
    # 普通正常用例（非鉴权缺失维度）→ 应附带鉴权头
    res = execute_case(_case("TC-AUTH", "GET", "/profile"), opts, session=requests.Session())
    assert res.status == ExecStatus.PASS.value
    assert any(r["auth"] == "Bearer placeholder-token" for r in _recorded())


def test_security_auth_missing_no_header_then_rejected(server):
    """安全-鉴权缺失维度：不带凭证请求受保护接口 → 401 被判定为「无凭证被拒」通过。"""
    opts = ExecutorOptions(base_url=server, timeout=5)
    case = _case(
        "TC-SEC",
        "GET",
        "/secure",
        category=TPType.SECURITY.value,
        dimension=Dimension.AUTH_MISS.value,
    )
    res = execute_case(case, opts, session=requests.Session())
    # 不带凭证 → 服务端回 401 → 安全维度应判「被拒」通过
    assert res.status == ExecStatus.PASS.value
    # 且确实没带 Authorization 头
    assert all(r["auth"] == "" for r in _recorded() if r["path"] == "/secure")


def test_write_method_default_skipped(server):
    """写操作（POST）默认不执行 → 如实 skipped，且服务端不应收到请求。"""
    opts = ExecutorOptions(base_url=server, timeout=5)  # allow_write=False
    case = _case("TC-WRITE", "POST", "/submit")
    res = execute_case(case, opts, session=requests.Session())
    assert res.status == ExecStatus.SKIPPED.value
    assert not any(r["method"] == "POST" for r in _recorded())


def test_timeout_becomes_error(server):
    """请求慢于 timeout → 转 error 结论（绝不挂起、绝不伪装通过）。"""
    opts = ExecutorOptions(base_url=server, timeout=0.5)  # 远小于 /slow 的 2s
    case = _case("TC-TIME", "GET", "/slow")
    res = execute_case(case, opts, session=requests.Session())
    assert res.status == ExecStatus.ERROR.value
