"""1d 活体 HTTP 级 e2e：H5 / M3 / M5 走真实 URL 路由与真实 DB。

独立进程运行（本仓库在 pytest 进程内 import 完整 urlconf 会破坏 capture）：
    ./venv/Scripts/python.exe tests/e2e_1d_live.py

验证点：
H5 —— 创建配置后响应无明文 Key；列表/详情只给 api_key_masked；库里是密文；
      出站调用仍能用解密后的真 Key（复用本地 trap server）。
M3 —— 匿名访问模板/报告目录 401；登录会话 200 且带 nosniff；
      目录穿越与非白名单类型 404。
M5 —— 非 POST 405；非法 JSON 400；缺验证码 400；且响应体不含异常原文。

所有写库操作包事务并回滚，不留脏数据。
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
os.environ.pop("API_TESTING_SSRF_STRICT", None)

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402

from apps.api_testing.models import AIServiceConfig  # noqa: E402

PORT = 18919
BASE = f"http://127.0.0.1:{PORT}"
PLAIN = "sk-1d-plaintext-9876"
HITS: list[str] = []


class TrapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        HITS.append(self.headers.get("Authorization", ""))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
                username="__e2e_1d_user__",
                defaults={"email": "e2e1d@example.invalid", "is_active": True},
            )
            user.set_password("Passw0rd!23")
            user.save()

            client = Client()
            client.force_login(user)

            # ---------------- H5 ----------------
            payload = {
                "name": "e2e-h5", "service_type": "openai", "role": "description",
                "api_key": PLAIN, "base_url": BASE, "model_name": "gpt-4o-mini",
            }
            r = client.post("/api/api-testing/ai-service-configs/", data=payload)
            created = r.status_code == 201
            body_txt = r.content.decode("utf-8", "ignore")
            cfg_id = (r.json() or {}).get("id") if created else None
            check("H5 创建配置成功（201）", created, f"status={r.status_code}")
            check("H5 创建响应不含明文 Key", PLAIN not in body_txt, body_txt[:160])
            check(
                "H5 创建响应含 api_key_masked",
                "api_key_masked" in body_txt and "****" in body_txt,
                str((r.json() or {}).get("api_key_masked")),
            )

            if cfg_id:
                row = AIServiceConfig.objects.get(id=cfg_id)
                check(
                    "H5 库内为密文（Fernet 令牌）",
                    row.api_key.startswith("gAAAAA") and PLAIN not in row.api_key,
                    row.api_key[:24] + "...",
                )
                check("H5 解密可取回明文", row.get_api_key() == PLAIN)

                r = client.get("/api/api-testing/ai-service-configs/")
                list_txt = r.content.decode("utf-8", "ignore")
                check("H5 列表接口 200", r.status_code == 200, f"status={r.status_code}")
                check("H5 列表不含明文 Key", PLAIN not in list_txt, list_txt[:160])

                # 出站仍使用解密后的真实 Key
                HITS.clear()
                r = client.post(
                    "/api/api-testing/ai-service-configs/test_connection/",
                    data={"config_id": cfg_id},
                )
                check(
                    "H5 出站携带解密后的真 Key",
                    r.status_code == 200 and HITS and HITS[0] == f"Bearer {PLAIN}",
                    f"status={r.status_code} auth={HITS[0] if HITS else None}",
                )

            # ---------------- M3 ----------------
            anon = Client()
            r = anon.get("/app-automation-templates/common/plus_sign.png")
            check("M3 匿名访问模板目录 401", r.status_code == 401, f"status={r.status_code}")
            r = anon.get("/app-automation-reports/index.html")
            check("M3 匿名访问报告目录 401", r.status_code == 401, f"status={r.status_code}")

            r = client.get("/app-automation-templates/common/plus_sign.png")
            check(
                "M3 登录后可访问模板资源（200 + nosniff）",
                r.status_code == 200 and r.get("X-Content-Type-Options") == "nosniff",
                f"status={r.status_code} nosniff={r.get('X-Content-Type-Options')}",
            )

            # 目录穿越 / 非白名单（Django 解析器规范化后的路径尝试）
            for bad in (
                "/app-automation-templates/%2e%2e/%2e%2e/backend/settings.py",
                "/app-automation-templates/common/plus_sign.txt",
                "/app-automation-templates/",
            ):
                r = client.get(bad)
                check(
                    f"M3 非法路径被拒（{bad}）",
                    r.status_code in (301, 302, 404),
                    f"status={r.status_code}",
                )

            # ---------------- M5（已统一到正式 RegisterView） ----------------
            r = client.get("/api/auth/register/")
            check("M5 非 POST 被拒（405）", r.status_code == 405, f"status={r.status_code}")

            r = client.post(
                "/api/auth/register/", data="not-json",
                content_type="application/json",
            )
            txt = r.content.decode("utf-8", "ignore")
            check(
                "M5 非法 JSON 400 且无异常原文",
                r.status_code == 400 and "Traceback" not in txt and "str(e)" not in txt,
                f"status={r.status_code}",
            )

            r = client.post(
                "/api/auth/register/",
                data=json.dumps({"phone": "13800000009", "username": "x"}),
                content_type="application/json",
            )
            msg = (r.json() or {}).get("error", "")
            check(
                "M5 缺验证码返回可读 400",
                r.status_code == 400 and "短信验证码" in msg,
                f"status={r.status_code} msg={msg}",
            )

            transaction.set_rollback(True)
    finally:
        server.shutdown()

    print("=" * 80)
    for status, name, detail in results:
        print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    print("=" * 80)
    ok = all(s == "PASS" for s, _, _ in results)
    print("E2E(1d) RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
