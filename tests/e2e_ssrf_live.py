"""1c 活体 e2e：用真实 socket / 真实 302 重定向验证 SSRF 防护。

不属于 pytest 用例（端口/线程类），手动执行：
    ./venv/Scripts/python.exe tests/e2e_ssrf_live.py

验证点：
1. 内网/回环目标默认放行 —— 保留「测内网接口」的产品能力；
2. 真实 302 → 169.254.169.254 的重定向攻击被拦在第二跳之前（服务端只收到 1 次请求）；
3. 元数据地址在**建立连接前**即被拦截；
4. strict 策略下回环一律拒绝；
5. webhook 地址脱敏不残留 key/uuid/token。
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

from apps.api_testing.ssrf_guard import (  # noqa: E402
    SsrfBlocked,
    SsrfPolicy,
    mask_webhook_url,
    safe_request,
)

HITS = {"count": 0}
PORT = 18917
BASE = f"http://127.0.0.1:{PORT}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        HITS["count"] += 1
        if self.path == "/ok":
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-safe":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/redirect-meta":
            # 经典 SSRF 跳板：公网/内网可达的地址 302 到云元数据
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and cond
        results.append(("PASS" if cond else "FAIL", name, detail))

    try:
        # 1) 内网/回环默认放行
        HITS["count"] = 0
        resp = safe_request("GET", f"{BASE}/ok", policy=SsrfPolicy(strict=False), timeout=5)
        check("默认策略放行回环目标（保留内网测试能力）",
              resp.status_code == 200 and resp.text == "hello",
              f"status={resp.status_code} body={resp.text!r}")

        # 2) 安全重定向正常跟随
        HITS["count"] = 0
        resp = safe_request("GET", f"{BASE}/redirect-safe", policy=SsrfPolicy(strict=False),
                            timeout=5)
        check("安全 302 正常跟随（2 跳）",
              resp.status_code == 200 and HITS["count"] == 2,
              f"status={resp.status_code} hits={HITS['count']}")

        # 3) 重定向到元数据被拦，且第二跳从未发出
        HITS["count"] = 0
        try:
            safe_request("GET", f"{BASE}/redirect-meta", policy=SsrfPolicy(strict=False),
                         timeout=5)
            check("302 → 169.254.169.254 被拦截", False, "未抛 SsrfBlocked")
        except SsrfBlocked as exc:
            check("302 → 169.254.169.254 被拦截", True, str(exc)[:80])
        check("元数据跳转未真正发出（服务端仅 1 次命中）", HITS["count"] == 1,
              f"hits={HITS['count']}")

        # 4) 直连元数据在建连前即拦截
        try:
            safe_request("GET", "http://169.254.169.254/latest/meta-data/",
                         policy=SsrfPolicy(strict=False), timeout=3)
            check("直连元数据在建连前拦截", False, "未抛 SsrfBlocked")
        except SsrfBlocked as exc:
            check("直连元数据在建连前拦截", True, str(exc)[:80])

        # 5) strict 策略拒绝回环
        try:
            safe_request("GET", f"{BASE}/ok", policy=SsrfPolicy(strict=True), timeout=3)
            check("strict 策略拒绝回环", False, "未抛 SsrfBlocked")
        except SsrfBlocked as exc:
            check("strict 策略拒绝回环", True, str(exc)[:80])

        # 6) webhook 脱敏
        masked = mask_webhook_url(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=REALKEY"
        )
        check("webhook URL 脱敏不残留凭据",
              "REALKEY" not in masked and masked.endswith("/***"), masked)
    finally:
        server.shutdown()

    print("=" * 72)
    for status, name, detail in results:
        print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 72)
    print("E2E RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
