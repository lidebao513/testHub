"""1c 活体 HTTP 级 e2e：真实 Django 路由 + 真实 DB + 真实出站 socket。

与 e2e_ssrf_live.py（只测守卫纯函数与 socket 行为）互补，本脚本走完整链路：

    DRF APIClient → URL 路由 → AIServiceConfigViewSet.test_connection
    → safe_request → requests → 本地 trap server

验证点：
1. 正常内网目标经 SSRF 守卫**放行并真实外呼**（保留「测内网接口」能力），
   且 forwarded Authorization 头携带配置里的真实 Key（1d 之后验证解密链路）；
2. 云元数据目标在建连前被拦，返回 **400 且客户端可读原因**（不是 500），
   并且 trap server **零命中**（证明没有真的发出去）；
3. 「内网可达地址 → 302 → 元数据」的经典跳板被拦在第二跳之前，
   trap server 只收到首跳 1 次请求；
4. 路径穿越/协议伪造等非法输入同样稳定返回 400。

所有写库操作包在事务里并最终回滚，不在开发库留下脏数据。
手动执行：
    ./venv/Scripts/python.exe tests/e2e_ssrf_http.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
# 保证 e2e 使用默认策略（非 strict），否则回环会被拦，失去「默认放行内网」的验证意义
for _flag in ("API_TESTING_SSRF_STRICT", "API_TESTING_SSRF_ALLOW_HOSTS",
              "API_TESTING_SSRF_MAX_REDIRECTS"):
    os.environ.pop(_flag, None)

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.api_testing.models import AIServiceConfig  # noqa: E402

PORT = 18918
BASE = f"http://127.0.0.1:{PORT}"
TEST_ENDPOINT = "/api/api-testing/ai-service-configs/test_connection/"
TEST_KEY = "sk-e2e-plaintext-key-0001"

HITS: list[dict] = []


class TrapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                self.rfile.read(length)
            except OSError:
                pass
        HITS.append({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
        })

    def _send(self, code: int, body: bytes = b"{}", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self._record()
        if self.path.startswith("/redir/chat/completions"):
            # 跳板：内部可达地址 302 到云元数据
            self._send(302, b"", {"Location": "http://169.254.169.254/latest/meta-data/"})
        elif self.path.endswith("/chat/completions"):
            self._send(200, b'{"choices":[{"message":{"content":"ok"}}]}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_GET(self):  # noqa: N802
        self._record()
        self._send(200, b'{"ok":true}')


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), TrapHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    results: list[tuple[str, str, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append(("PASS" if cond else "FAIL", name, detail))

    User = get_user_model()
    try:
        with transaction.atomic():
            user, _ = User.objects.get_or_create(
                username="__e2e_ssrf_probe__",
                defaults={"email": "e2e@example.invalid", "is_active": True},
            )
            client = APIClient()
            client.force_authenticate(user=user)

            def make_config(base_url: str, name: str) -> AIServiceConfig:
                return AIServiceConfig.objects.create(
                    name=name,
                    service_type="openai",
                    role="description",
                    api_key=TEST_KEY,
                    base_url=base_url,
                    model_name="gpt-4o-mini",
                    created_by=user,
                )

            # ---------- 1) 正常内网目标：放行并真实外呼 ----------
            HITS.clear()
            cfg_ok = make_config(BASE, "e2e-ok")
            r1 = client.post(TEST_ENDPOINT, {"config_id": cfg_ok.id}, format="json")
            check(
                "正常目标经守卫放行并真实外呼（200 success）",
                r1.status_code == 200 and r1.data.get("status") == "success",
                f"status={r1.status_code} data={r1.data}",
            )
            check(
                "trap server 恰好收到 1 次外呼",
                len(HITS) == 1,
                f"hits={len(HITS)} {HITS}",
            )
            check(
                "外呼携带配置中的真实 API Key（解密链路正确）",
                bool(HITS) and HITS[0]["authorization"] == f"Bearer {TEST_KEY}",
                f"auth={HITS[0]['authorization'] if HITS else None!r}",
            )

            # ---------- 2) 云元数据：建连前拦截，400 且零外呼 ----------
            HITS.clear()
            cfg_meta = make_config("http://169.254.169.254", "e2e-meta")
            r2 = client.post(TEST_ENDPOINT, {"config_id": cfg_meta.id}, format="json")
            check(
                "元数据目标返回 400（而非 500），错误可读",
                r2.status_code == 400 and "169.254.169.254" in json.dumps(
                    r2.data, ensure_ascii=False),
                f"status={r2.status_code} data={r2.data}",
            )
            check("元数据目标零外呼", len(HITS) == 0, f"hits={len(HITS)}")

            # ---------- 3) 302 跳板：拦在第二跳之前 ----------
            HITS.clear()
            cfg_redir = make_config(f"{BASE}/redir", "e2e-redirect")
            r3 = client.post(TEST_ENDPOINT, {"config_id": cfg_redir.id}, format="json")
            check(
                "内网地址 302 → 元数据被拦截（400）",
                r3.status_code == 400,
                f"status={r3.status_code} data={r3.data}",
            )
            check(
                "跳板只发出首跳 1 次请求（第二跳从未发出）",
                len(HITS) == 1,
                f"hits={len(HITS)} {[h['path'] for h in HITS]}",
            )

            # ---------- 4) 非法协议 / 空主机 ----------
            HITS.clear()
            cfg_file = make_config("file:///etc", "e2e-file")
            r4 = client.post(TEST_ENDPOINT, {"config_id": cfg_file.id}, format="json")
            check(
                "非 http(s) 协议返回 400",
                r4.status_code == 400,
                f"status={r4.status_code} data={r4.data}",
            )
            check("非法协议零外呼", len(HITS) == 0, f"hits={len(HITS)}")

            transaction.set_rollback(True)
    finally:
        server.shutdown()

    print("=" * 78)
    for status, name, detail in results:
        print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 78)
    ok = all(s == "PASS" for s, _, _ in results)
    print("E2E(HTTP) RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
