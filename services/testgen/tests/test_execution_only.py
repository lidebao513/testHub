"""G-1 · 执行器端到端证明：执行器真发请求 + 项目级 run_execution 全流程 + F12 落库。

为什么用本地真实 HTTP 服务而不是假会话：G-1 的核心价值是「平台能给出可信的执行结论」，
必须用真实网络往返证明 `executor` 不是桩——假会话只能验证判定逻辑，证明不了「真发请求」。
本地服务确定性、无外部依赖、可在 CI 跑；真连被测环境由 CLI `testgen execute` / 服务端承担。

覆盖点：
- 接口层执行器对本地服务真实发 HTTP 请求，并按状态码给出 pass / fail / skipped；
- 项目级 `pipeline.run_execution`：加载已落库用例 → 真跑 → F12 把逐条结论写 `runs`、
  回填 `cases.last_result`、写 `run_batches` 批次终态。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import store
from core.contracts import CaseSpec
from core.db import connect, init_db
from core.enums import ExecStatus
from engine import executor, pipeline


class _MockApiHandler(BaseHTTPRequestHandler):
    """一个最小被测服务：不同路径返回不同状态码，便于断言各维度判定。"""

    def log_message(self, *args: object) -> None:  # 静默：测试不需要访问日志
        return

    def _send(self, code: int, body: bytes = b"{}") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send(200)
        elif self.path == "/api/secret":
            self._send(401)  # 鉴权缺失维度：无凭证应被拒
        elif self.path == "/api/missing":
            self._send(404)  # 异常维度：资源不存在
        elif self.path == "/api/bad":
            self._send(422)  # 边界维度：参数非法被校验
        else:
            self._send(200)

    def do_POST(self) -> None:
        if self.path == "/api/invoices":
            self._send(201)
        else:
            self._send(404)


@pytest.fixture()
def mock_server():
    """起一个本地真实 HTTP 服务（随机端口），测试结束自动关闭。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockApiHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def _case(
    *,
    tp: str,
    category: str = "正常",
    dimension: str = "正常-可用性",
    method: str = "GET",
    path: str = "/api/health",
) -> CaseSpec:
    return CaseSpec(
        tc_no=tp,
        tp_id=tp,
        title=f"[{category}] {path}",
        ctype="api",
        steps=[
            {
                "action": "http_probe",
                "layer": "接口",
                "kind": "api",
                "method": method,
                "path": path,
                "dimension": dimension,
                "expect": "e",
            }
        ],
        case_type=category,
    )


def test_executor_sends_real_requests_and_judges(mock_server):
    """G-1 证明：执行器对本地服务真发请求，并按维度给出真实判定（非桩）。"""
    cases = [
        _case(tp="TP-10000001", path="/api/health"),
        _case(tp="TP-10000002", category="安全", dimension="安全-鉴权缺失", path="/api/secret"),
        _case(tp="TP-10000003", category="异常", dimension="异常-资源不存在", path="/api/missing"),
        _case(tp="TP-10000004", category="边界", dimension="边界-参数缺失/非法", path="/api/bad"),
        _case(tp="TP-10000005", method="POST", path="/api/invoices"),  # 写操作默认 skipped
    ]
    summary = executor.execute_all(cases, executor.ExecutorOptions(base_url=mock_server))
    by_tc = {r["tc_no"]: r for r in summary["results"]}
    assert summary["total"] == 5
    assert by_tc["TP-10000001"]["status"] == ExecStatus.PASS.value
    assert by_tc["TP-10000002"]["status"] == ExecStatus.PASS.value
    assert by_tc["TP-10000003"]["status"] == ExecStatus.PASS.value
    assert by_tc["TP-10000004"]["status"] == ExecStatus.PASS.value
    assert by_tc["TP-10000005"]["status"] == ExecStatus.SKIPPED.value


def _seed_project_with_cases(server_url: str) -> int:
    """插入一个项目 + 两条接口层用例（status=generated，last_result 为空）。

    try/finally 保证 conn 一定关闭，避免异常路径下遗留连接占用 SQLite 锁，
    导致后续用例的 fresh_db（init_db）报「database is locked」。
    """
    conn = connect()
    pid = 0
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO projects(name, local_path) VALUES (?, ?)",
            ("exec-test", "/tmp/exec-test"),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        cases = [
            _case(tp="TP-20000001", path="/api/health"),
            _case(tp="TP-20000002", category="安全", dimension="安全-鉴权缺失", path="/api/secret"),
        ]
        for c in cases:
            conn.execute(
                "INSERT INTO cases(project_id, tc_no, title, ctype, steps, case_type,"
                " module, priority, precondition, doc_steps, tp_id, fp_contract_id,"
                " test_type, status, version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    c.tc_no,
                    c.title,
                    c.ctype,
                    json.dumps(c.steps, ensure_ascii=False),
                    c.case_type,
                    c.module,
                    c.priority,
                    c.precondition,
                    json.dumps(c.doc_steps, ensure_ascii=False),
                    c.tp_id,
                    c.fp_contract_id,
                    c.test_type,
                    c.status,
                    c.version,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return pid


def test_run_execution_executes_and_persists_f12(mock_server, fresh_db):
    """G-1 项目级执行：加载已落库用例 → 真跑 → F12 落库（runs / last_result / 批次）。"""
    pid = _seed_project_with_cases(mock_server)

    opts = pipeline.default_options()
    opts.target_req.exec_url = mock_server
    summary = pipeline.run_execution(pid, opts)

    assert summary["total"] == 2
    assert summary["pass"] == 2
    assert summary["state"] == "completed"
    assert summary["runs_written"] == 2
    assert summary["cases_backfilled"] == 2

    # 逐条结论写 runs
    runs = store.list_runs(pid, limit=10)
    assert len(runs) == 2
    assert all(r["status"] == ExecStatus.PASS.value for r in runs)

    # 批次终态
    batch = store.get_run_batch(summary["batch_id"])
    assert batch is not None
    assert batch["state"] == "completed"

    # cases.last_result 回填
    rows = store.list_cases(pid)
    assert all(r["last_result"] == ExecStatus.PASS.value for r in rows)


def test_run_execution_raises_when_no_cases(mock_server, fresh_db):
    """没有用例的项目执行应明确报错，而不是产出一份空结论假装成功。"""
    conn = connect()
    init_db(conn)
    conn.execute("INSERT INTO projects(name, local_path) VALUES (?, ?)", ("empty", "/tmp/empty"))
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    import pytest as _pytest

    from core.errors import EngineError

    opts = pipeline.default_options()
    opts.target_req.exec_url = mock_server
    with _pytest.raises(EngineError):
        pipeline.run_execution(pid, opts)
