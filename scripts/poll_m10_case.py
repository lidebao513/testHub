"""轮询指定 AI 执行记录直到终态并打印结果（M10 真机验证）。"""

import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8900"
USER, PWD = "e2etest", "E2e@123456"
EXEC_ID = sys.argv[1] if len(sys.argv) > 1 else "26"


def main() -> int:
    s = requests.Session()
    s.trust_env = False
    r = s.post(f"{BASE}/api/auth/login/", json={"username": USER, "password": PWD}, timeout=20)
    token = r.json().get("access", "")
    headers = {"Authorization": f"Bearer {token}"}
    print(f"polling execution {EXEC_ID}", flush=True)

    deadline = time.time() + 20 * 60
    last_status, last_len = None, -1
    while time.time() < deadline:
        rr = s.get(f"{BASE}/api/ui-automation/ai-execution-records/{EXEC_ID}/", headers=headers, timeout=30)
        if rr.status_code != 200:
            print(f"  poll {rr.status_code} {rr.text[:120]}", flush=True)
            time.sleep(10)
            continue
        d = rr.json()
        status = d.get("status")
        logs = d.get("logs") or ""
        tasks = d.get("planned_tasks") or []
        if status != last_status or len(logs) != last_len:
            brief = " | ".join(f"{t.get('id')}:{t.get('status')}" for t in tasks if isinstance(t, dict))
            print(f"[{time.strftime('%H:%M:%S')}] status={status} logs={len(logs)}B tasks=[{brief}]", flush=True)
            last_status, last_len = status, len(logs)
        if status in ("passed", "failed", "stopped", "error"):
            print(f"\n===== TERMINAL status={status} duration={d.get('duration')} =====", flush=True)
            print("---- logs tail ----", flush=True)
            print(logs[-4000:], flush=True)
            print("---- tasks ----", flush=True)
            for t in tasks:
                if isinstance(t, dict):
                    print(f"  {t.get('id')} {t.get('status')} :: {str(t.get('description'))[:70]}", flush=True)
            print(f"---- steps ({len(d.get('steps_completed') or [])}) ----", flush=True)
            for i, st in enumerate(d.get("steps_completed") or []):
                if isinstance(st, dict):
                    print(f"  #{i} {json.dumps(st, ensure_ascii=False)[:160]}", flush=True)
            with open(f"logs/live_m10_record_{EXEC_ID}.json", "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            return 0
        time.sleep(10)
    print("timeout", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
