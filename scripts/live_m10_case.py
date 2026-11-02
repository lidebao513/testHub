"""M10 真机验证：通过平台真实 HTTP 链路跑一条 AI 用例。

链路：POST /api/ui-automation/ai-execution-records/run_adhoc/ → 后台线程
→ run_full_process_sync → analyze_task → run_task（含 ActionGuard）
→ 轮询 GET /api/ui-automation/ai-executions/{id}/ 直到终态。

用 e2etest 账号登录平台自身（localhost:3900），属测试数据。
"""

import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8900"
USER, PWD = "e2etest", "E2e@123456"

TASK = (
    "打开 http://localhost:3900/login 。"
    "在「用户名」输入框输入 e2etest，在「密码」输入框输入 E2e@123456，"
    "然后点击「登录」按钮。"
    "登录成功后确认页面已经离开登录页（浏览器地址不再包含 /login），即视为完成。"
)


def main() -> int:
    s = requests.Session()
    s.trust_env = False

    r = s.post(f"{BASE}/api/auth/login/", json={"username": USER, "password": PWD}, timeout=20)
    if r.status_code != 200:
        print(f"login failed {r.status_code}: {r.text[:200]}")
        return 1
    token = r.json().get("access") or r.json().get("data", {}).get("access", "")
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[1] login ok, token len={len(token)}", flush=True)

    r = s.post(
        f"{BASE}/api/ui-automation/ai-execution-records/run_adhoc/",
        json={"task_description": TASK, "enable_gif": False},
        headers=headers,
        timeout=30,
    )
    print(f"[2] run_adhoc -> {r.status_code}: {r.text[:300]}", flush=True)
    if r.status_code not in (200, 201, 202):
        return 1
    payload = r.json()
    record = payload.get("data") or payload.get("execution_record") or payload
    exec_id = record.get("id") if isinstance(record, dict) else None
    if not exec_id:
        print(f"!! cannot find execution id in {payload}")
        return 1
    print(f"[3] execution id={exec_id}, polling...", flush=True)

    deadline = time.time() + 15 * 60
    last_status = None
    last_logs_len = 0
    while time.time() < deadline:
        time.sleep(10)
        rr = s.get(f"{BASE}/api/ui-automation/ai-execution-records/{exec_id}/", headers=headers, timeout=30)
        if rr.status_code != 200:
            print(f"    poll {rr.status_code}", flush=True)
            continue
        data = rr.json()
        status = data.get("status")
        logs = data.get("logs") or ""
        tasks = data.get("planned_tasks") or []
        if status != last_status or len(logs) != last_logs_len:
            brief = " | ".join(
                f"{t.get('id')}:{t.get('status')}" for t in tasks if isinstance(t, dict)
            )
            print(
                f"    [{time.strftime('%H:%M:%S')}] status={status} logs={len(logs)}B tasks=[{brief}]",
                flush=True,
            )
            last_status, last_logs_len = status, len(logs)
        if status in ("passed", "failed", "stopped", "error"):
            print(f"[4] terminal status={status} duration={data.get('duration')}", flush=True)
            print("---- logs tail ----", flush=True)
            print(logs[-3000:], flush=True)
            print("---- tasks ----", flush=True)
            for t in tasks:
                if isinstance(t, dict):
                    print(f"  {t.get('id')} {t.get('status')} :: {str(t.get('description'))[:60]}", flush=True)
            print("---- steps ----", flush=True)
            for i, st in enumerate(data.get("steps_completed") or []):
                if isinstance(st, dict):
                    print(f"  #{i} {str(st.get('action') or st.get('type'))[:80]}", flush=True)
            json.dump(data, open("logs/live_m10_record.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            return 0
    print("[!] timeout after 15min", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
