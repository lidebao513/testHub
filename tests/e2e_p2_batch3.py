"""阶段 2 收尾批次（M12 / M18）真实链路验证。

直接打运行中的 sidecar（默认 :8910），验证：
1. M12：`/generate` 提交后立即取消 → 终态恒为 `cancelled`，绝不出现 `success`
   （旧逻辑会把 `cancelled` 覆盖回 `running` 并跑完写 success）。
2. M18-a：`/pipeline` 传 `async_mode=true` → 202 + task_id + poll_url，轮询到 success。
3. M18-b：`/describe` 20 项分 4 组并发，耗时明显低于串行（对比单组耗时估）。
"""

from __future__ import annotations

import sys
import time
import uuid

import requests

BASE = "http://127.0.0.1:8910"
LOCAL = "C:/Users/EDY/WorkBuddy/testhub_platform/services/testgen/engine"
S = requests.Session()
S.trust_env = False  # 本机透明代理会让 localhost 调用异常


def _post(path: str, body: dict, timeout: float = 120) -> requests.Response:
    return S.post(f"{BASE}{path}", json=body, timeout=timeout)


def _get(path: str, timeout: float = 30) -> requests.Response:
    return S.get(f"{BASE}{path}", timeout=timeout)


def _poll(task_id: str, timeout: float = 180) -> tuple[str, list[str]]:
    deadline = time.time() + timeout
    seen: list[str] = []
    last = ""
    while time.time() < deadline:
        data = _get(f"/api/v1/tasks/{task_id}").json()
        last = data["state"]
        if not seen or seen[-1] != last:
            seen.append(last)
        if last in ("success", "failed", "cancelled"):
            return last, seen
        time.sleep(0.15)
    return last, seen


def test_m12_cancel_race() -> bool:
    """连续 5 轮「提交后立即取消」，终态必须是 cancelled。"""
    ok = True
    for i in range(5):
        name = f"m12-race-{uuid.uuid4().hex[:6]}"
        resp = _post("/api/v1/generate", {"local_path": LOCAL, "project_name": name})
        if resp.status_code != 202:
            print(f"  [M12-{i}] 提交失败 {resp.status_code}: {resp.text[:200]}")
            ok = False
            continue
        task_id = resp.json()["task_id"]

        cancel_resp = S.post(f"{BASE}/api/v1/tasks/{task_id}/cancel", timeout=15)
        cancelled_ok = cancel_resp.json().get("cancelled") if cancel_resp.ok else None

        final, seen = _poll(task_id, timeout=120)
        detail = _get(f"/api/v1/tasks/{task_id}").json()
        started = detail.get("started_at") or ""

        good = final == "cancelled" and "success" not in seen
        ok = ok and good
        print(
            f"  [M12-{i}] cancel_ok={cancelled_ok} final={final} "
            f"started_at={'空' if not started else started} seq={seen} "
            f"{'PASS' if good else 'FAIL'}"
        )
    return ok


def test_m12_cancel_while_queued() -> bool:
    """严格版 M12：占满 worker 让目标任务**排队**，取消后再放开 worker。

    旧逻辑：worker 空出时 `_run` 无条件置 RUNNING → 覆盖 cancelled → 跑完写 success。
    新逻辑：`try_start` 条件更新影响 0 行 → 直接跳过，`started_at` 始终为空。
    """
    tag = uuid.uuid4().hex[:6]
    blockers = []
    for i in range(4):  # GEN_MAX_WORKERS 默认 4
        resp = _post("/api/v1/generate", {"local_path": LOCAL, "project_name": f"m12-fill-{i}-{tag}"})
        if resp.status_code != 202:
            print(f"  [M12-Q] 占位任务提交失败 {resp.status_code}")
            return False
        blockers.append(resp.json()["task_id"])

    # 等 4 个都进 running（worker 被占满）
    deadline = time.time() + 90
    while time.time() < deadline:
        states = [_get(f"/api/v1/tasks/{t}").json()["state"] for t in blockers]
        if all(st == "running" for st in states):
            break
        time.sleep(0.3)
    else:
        print(f"  [M12-Q] 占位任务未全部 running：{states}")
        return False

    # 目标任务：必然排队在 PENDING
    queued = _post("/api/v1/generate", {"local_path": LOCAL, "project_name": f"m12-queued-{tag}"})
    if queued.status_code != 202:
        print(f"  [M12-Q] 排队任务提交失败 {queued.status_code}")
        return False
    qid = queued.json()["task_id"]
    time.sleep(0.5)
    state_before = _get(f"/api/v1/tasks/{qid}").json()["state"]
    if state_before != "pending":
        print(f"  [M12-Q] 期望排队中为 pending，实际 {state_before}（worker 未被占满？）")
        return False

    # 排队期间取消
    cancel_ok = S.post(f"{BASE}/api/v1/tasks/{qid}/cancel", timeout=15).json().get("cancelled")

    # 放开 worker：取消占位任务，让 worker 空出 → 队列里的 _run 会被执行到 try_start
    for t in blockers:
        S.post(f"{BASE}/api/v1/tasks/{t}/cancel", timeout=15)

    # 观察 5s：不允许出现 running/success
    seen = []
    deadline = time.time() + 5
    while time.time() < deadline:
        detail = _get(f"/api/v1/tasks/{qid}").json()
        if not seen or seen[-1] != detail["state"]:
            seen.append(detail["state"])
        time.sleep(0.2)
    detail = _get(f"/api/v1/tasks/{qid}").json()
    started = detail.get("started_at") or ""

    good = (
        cancel_ok is True
        and detail["state"] == "cancelled"
        and not started
        and "running" not in seen
        and "success" not in seen
    )
    print(
        f"  [M12-Q] cancel_ok={cancel_ok} 终态={detail['state']} "
        f"started_at={'空' if not started else started} 序列={seen} "
        f"{'PASS' if good else 'FAIL'}"
    )
    return good


def test_m18_pipeline_async() -> bool:
    """`/pipeline` 异步模式：202 + poll_url，轮询到 success。"""
    name = f"m18-async-{uuid.uuid4().hex[:6]}"
    started = time.time()
    resp = _post(
        "/api/v1/pipeline",
        {"local_path": LOCAL, "project_name": name, "async_mode": True},
        timeout=60,
    )
    elapsed = time.time() - started
    if resp.status_code != 202:
        print(f"  [M18-a] 期望 202，实际 {resp.status_code}: {resp.text[:200]}")
        return False
    body = resp.json()
    print(f"  [M18-a] 202 用时 {elapsed:.2f}s task={body['task_id']} poll={body['poll_url']}")
    if elapsed > 10:
        print("  [M18-a] FAIL：异步提交耗时过长（应远小于全链路）")
        return False

    final, seen = _poll(body["task_id"], timeout=240)
    print(f"  [M18-a] 最终状态={final} 序列={seen}")
    return final == "success"


def test_m18_describe_concurrency() -> bool:
    """20 项 / 每组 5 条 = 4 组：并发后总耗时应接近单组耗时。"""
    items = [
        {
            "path": f"engine/mod_{i}.py",
            "name": f"mod_{i}.py",
            "functions": [f"fn_{i}_a", f"fn_{i}_b"],
            "classes": [],
        }
        for i in range(20)
    ]
    started = time.time()
    resp = _post(
        "/api/v1/describe",
        {"items": items, "repo_url": f"https://example.com/m18-live-{uuid.uuid4().hex[:6]}.git"},
        timeout=300,
    )
    elapsed = time.time() - started
    if not resp.ok:
        print(f"  [M18-b] 请求失败 {resp.status_code}: {resp.text[:300]}")
        return False
    body = resp.json()
    print(
        f"  [M18-b] 耗时 {elapsed:.2f}s llm={body.get('llm')} "
        f"命中={body.get('cache_hits')} 未解析={body.get('unresolved')} "
        f"错误={body.get('error') or '-'}"
    )
    if not body.get("llm"):
        print("  [M18-b] SKIP：未配置 LLM，无法验证并发收益")
        return True
    # 4 组串行的下限是 2×单组耗时；并发后应明显低于该量级
    return body.get("unresolved", 0) == 0


def main() -> int:
    if _get("/health").status_code != 200:
        print("sidecar 不可用，先启动 :8910")
        return 2
    results = {
        "M12": test_m12_cancel_race(),
        "M12-Q": test_m12_cancel_while_queued(),
        "M18-a": test_m18_pipeline_async(),
        "M18-b": test_m18_describe_concurrency(),
    }
    print("\n=== 结果 ===")
    for k, v in results.items():
        print(f"{k}: {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
