"""L8 真实链路验证：`run_now` → Celery → 执行日志回写。

前置：MySQL80 / Redis(:6379) / Django(:8900) / Celery worker 均已启动。
不带 broker（停掉 Redis）时的兜底路径由 `tests/test_l8_task_dispatch.py` 覆盖。
"""

from __future__ import annotations

import sys
import time

import requests

BASE = "http://127.0.0.1:8900"
USER, PWD = "e2etest", "E2e@123456"

s = requests.Session()
s.trust_env = False


_ACCESS = {"token": ""}


def _login() -> bool:
    """登录拿 JWT access；后续走 Bearer 认证（不触发 SessionAuthentication 的 CSRF 校验）。

    注意：路由前缀是 `/api/auth/`——`/api/v1/` 已被 testgen 反向代理占用，
    直连 8900 打 `/api/v1/users/login/` 会被代理拦成 403「未授权的代理访问」。
    """
    resp = s.post(f"{BASE}/api/auth/login/", json={"username": USER, "password": PWD}, timeout=20)
    print(f"登录: {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:300])
        return False
    _ACCESS["token"] = resp.json().get("access", "")
    return bool(_ACCESS["token"])


def _headers() -> dict:
    return {"Authorization": f"Bearer {_ACCESS['token']}"} if _ACCESS["token"] else {}


def _list_tasks() -> list[dict]:
    resp = s.get(f"{BASE}/api/api-testing/scheduled-tasks/", headers=_headers(), timeout=20)
    data = resp.json()
    return data.get("results", data) if isinstance(data, dict) else data


def _ensure_request() -> int:
    """确保存在一个可执行的 ApiRequest（空库时先建一个指向 sidecar /health 的 GET）。"""
    resp = s.get(f"{BASE}/api/api-testing/requests/", headers=_headers(), timeout=20)
    data = resp.json()
    items = data.get("results", data) if isinstance(data, dict) else data
    if items:
        print(f"复用已有请求 id={items[0]['id']}")
        return int(items[0]["id"])

    created = s.post(
        f"{BASE}/api/api-testing/requests/",
        json={
            "name": "L8 健康检查请求",
            "method": "GET",
            "url": "http://127.0.0.1:8910/health",
        },
        headers=_headers(),
        timeout=20,
    )
    print(f"创建请求: {created.status_code} {created.text[:200]}")
    created.raise_for_status()
    return int(created.json()["id"])


def _ensure_task() -> int:
    tasks = _list_tasks()
    for t in tasks:
        if t.get("task_type") == "API_REQUEST":
            print(f"复用已有任务 id={t['id']} name={t.get('name')}")
            return int(t["id"])

    req_id = _ensure_request()
    resp = s.post(
        f"{BASE}/api/api-testing/scheduled-tasks/",
        json={
            "name": "L8 验证任务",
            "task_type": "API_REQUEST",
            "trigger_type": "INTERVAL",
            "interval_seconds": 3600,
            "api_request": req_id,
        },
        headers=_headers(),
        timeout=20,
    )
    print(f"创建任务: {resp.status_code} {resp.text[:200]}")
    resp.raise_for_status()
    return int(resp.json()["id"])


def _log_status(log_id: int) -> str:
    resp = s.get(f"{BASE}/api/api-testing/task-execution-logs/{log_id}/", headers=_headers(), timeout=20)
    if resp.status_code != 200:
        return f"HTTP{resp.status_code}"
    return resp.json().get("status", "")


def main() -> int:
    if not _login():
        print("登录失败")
        return 2
    task_id = _ensure_task()

    resp = s.post(
        f"{BASE}/api/api-testing/scheduled-tasks/{task_id}/run_now/",
        headers=_headers(),
        timeout=60,
    )
    print(f"run_now: {resp.status_code} {resp.text[:300]}")
    if resp.status_code != 200:
        return 1
    body = resp.json()
    mode = body.get("dispatch_mode")
    celery_id = body.get("celery_task_id")
    log_id = body.get("execution_id")
    print(f"dispatch_mode={mode} celery_task_id={celery_id} execution_id={log_id}")

    ok = mode == "celery" and bool(celery_id)

    deadline = time.time() + 90
    status = ""
    while time.time() < deadline:
        status = _log_status(log_id)
        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    print(f"执行日志终态: {status}")

    # 任务本身执行成功与否取决于是否绑定了 api_request（本用例允许 FAILED），
    # 关键是被 worker 真正跑过（PENDING → 终态）。
    ran = status in ("COMPLETED", "FAILED")
    print("\n=== 结果 ===")
    print(f"celery 派发: {'PASS' if ok else 'FAIL'}")
    print(f"worker 执行: {'PASS' if ran else 'FAIL'}")
    return 0 if (ok and ran) else 1


if __name__ == "__main__":
    sys.exit(main())
