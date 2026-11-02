"""M10 真机验证：临时切换 browser_use_text 模型 → 走真实 HTTP 链路跑一条 AI 用例 → 还原配置。

模型默认取免费档 qwen3.7-flash-2026-07-15；如需换模型用环境变量 M10_TMP_MODEL 指定。
**付费模型（max/plus/pro/2.4t）必须额外设置 M10_ALLOW_PAID_MODEL=1 才会执行**——
它们按 12~36 元/百万 token 计费，免费额度耗尽后会静默转按量，跑一次真机用例就是几元到十几元。
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

# 从 scripts/ 运行时项目根不在 sys.path，django.setup() 会找不到 backend 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django  # noqa: E402

django.setup()

from apps.requirement_analysis.models import AIModelConfig  # noqa: E402

BASE = "http://127.0.0.1:8900"
USER, PWD = "e2etest", "E2e@123456"

# 默认走免费档；付费模型需显式开关，避免误用旗舰模型产生账单
TMP_MODEL = os.getenv("M10_TMP_MODEL", "qwen3.7-flash-2026-07-15").strip()
_PAID_MODEL_HINTS = ("max", "plus", "pro", "2.4t")
if any(hint in TMP_MODEL.lower() for hint in _PAID_MODEL_HINTS):
    if os.getenv("M10_ALLOW_PAID_MODEL") != "1":
        raise SystemExit(
            f"拒绝执行：{TMP_MODEL} 属计费模型（免费额度耗尽后按量扣费）。\n"
            f"确需使用请显式设置 M10_ALLOW_PAID_MODEL=1。"
        )

TASK = (
    sys.argv[1]
    if len(sys.argv) > 1
    else (
        "打开 http://localhost:3900/login 。"
        "在「用户名」输入框输入 e2etest，在「密码」输入框输入 E2e@123456，"
        "然后点击「登录」按钮。"
        "登录成功后确认页面已经离开登录页（浏览器地址不再包含 /login），即视为完成。"
    )
)


def main() -> int:
    cfg = AIModelConfig.objects.filter(role='browser_use_text', is_active=True).first()
    if not cfg:
        print("!! no browser_use_text config")
        return 1
    original = cfg.model_name
    print(f"[0] original model={original} -> temporary {TMP_MODEL}", flush=True)
    cfg.model_name = TMP_MODEL
    cfg.save(update_fields=['model_name'])

    s = requests.Session()
    s.trust_env = False
    try:
        r = s.post(f"{BASE}/api/auth/login/", json={"username": USER, "password": PWD}, timeout=20)
        token = r.json().get("access", "")
        headers = {"Authorization": f"Bearer {token}"}
        r = s.post(
            f"{BASE}/api/ui-automation/ai-execution-records/run_adhoc/",
            json={"task_description": TASK, "enable_gif": False},
            headers=headers,
            timeout=30,
        )
        print(f"[1] run_adhoc {r.status_code}: {r.text[:200]}", flush=True)
        exec_id = r.json().get("execution_id")
        if not exec_id:
            return 1

        deadline = time.time() + 8 * 60
        last_status, last_len = None, -1
        while time.time() < deadline:
            time.sleep(10)
            rr = s.get(
                f"{BASE}/api/ui-automation/ai-execution-records/{exec_id}/", headers=headers, timeout=30
            )
            if rr.status_code != 200:
                continue
            d = rr.json()
            status, logs = d.get("status"), d.get("logs") or ""
            tasks = d.get("planned_tasks") or []
            if status != last_status or len(logs) != last_len:
                brief = " | ".join(f"{t.get('id')}:{t.get('status')}" for t in tasks if isinstance(t, dict))
                print(
                    f"[{time.strftime('%H:%M:%S')}] status={status} logs={len(logs)}B tasks=[{brief}]",
                    flush=True,
                )
                last_status, last_len = status, len(logs)
            if status in ("passed", "failed", "stopped", "error"):
                print(f"\n===== TERMINAL {status} duration={d.get('duration')} =====", flush=True)
                print("---- logs tail ----", flush=True)
                print(logs[-5000:], flush=True)
                print("---- tasks ----", flush=True)
                for t in tasks:
                    if isinstance(t, dict):
                        print(f"  {t.get('id')} {t.get('status')} :: {str(t.get('description'))[:70]}", flush=True)
                print(f"---- steps ({len(d.get('steps_completed') or [])}) ----", flush=True)
                for i, st in enumerate(d.get("steps_completed") or []):
                    if isinstance(st, dict):
                        line = st.get("action") or st.get("description") or json.dumps(st, ensure_ascii=False)
                        print(f"  #{i} {str(line)[:140]}", flush=True)
                with open(f"logs/live_m10_record_{exec_id}.json", "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
                return 0
        print("[!] timeout", flush=True)
        return 2
    finally:
        cfg.refresh_from_db()
        cfg.model_name = original
        cfg.save(update_fields=['model_name'])
        print(f"[9] restored model={original}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
