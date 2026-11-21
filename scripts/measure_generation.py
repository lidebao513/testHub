"""采集一次 testgen 生成任务的产物度量，供批次间对比（只读，不改引擎）。

为什么需要它：改造期间每一批都要回答"这次比上次多了几条、多在哪"。如果用手工
翻页面或临时 curl，口径每次不一样，数字就不可比。本脚本把三个口径固定下来：

    A 服务端聚合：GET /api/v1/tasks/<id> → result.counts / case_stats / traceability
    B 产物文件：  services/testgen/outputs/<pid>/{summary,test_points,test_cases}.json
    C 数据库：    services/testgen/data/testgen.db 的 test_points / cases 表

三者必须给出相同的用例数，否则该批数据不可用于对比（先修管道再谈指标）。

用法：
    # 采集既有任务，输出基线快照
    python scripts/measure_generation.py --task-id gen-8317484a1e65 --tag baseline

    # 按固定 preset 重新跑一次再采集（每批对照实验用）
    python scripts/measure_generation.py --run --from-task gen-8317484a1e65 --tag T1

    # 与上一批做字段级 diff
    python scripts/measure_generation.py --task-id gen-xxx --tag T2 \\
        --diff docs/metrics/2026-11-18-T1.json

输出：
    docs/metrics/<YYYY-MM-DD>-<tag>.json   机器可读，供 diff
    docs/metrics/<YYYY-MM-DD>-<tag>.md     人工摘要
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TESTGEN = ROOT / "services" / "testgen"
OUT_DIR = ROOT / "docs" / "metrics"
DB_PATH = TESTGEN / "data" / "testgen.db"
BASE_URL = "http://127.0.0.1:8910"

# 固定参数：所有对照实验必须一致，改动需同步更新任务文档
PRESET: dict[str, Any] = {
    "base": "21fb554c2586d7a1ab2ffbb0c3a13d726b0b5b88",
    "target": "31344820c81c5e6ea7a8f03eb18d92626be224aa",
    "selected_paths": [
        "backend/research-agent-source/server.py",
        "frontend/app/src/api/adapter.ts",
        "frontend/app/src/api/backend.ts",
        "frontend/app/src/pages/pc/PcChatPage.tsx",
        "frontend/app/src/pages/pc/PcShell.tsx",
    ],
    "mode": "incremental",
    "scopes": ["正常", "异常", "边界"],
    "runtime_ui": False,
    "expert_ui": False,
    "llm_enhance": False,
}

# 只保留这些请求字段进快照：避免把口令/令牌写进 metrics
REQ_KEEP = (
    "mode",
    "base",
    "target",
    "scopes",
    "selected_paths",
    "changed_files",
    "local_path",
    "runtime_ui",
    "runtime_scope",
    "expert_ui",
    "llm_enhance",
    "include_business",
    "extract_pages",
    "async_mode",
)


def _load_token() -> str:
    env_file = TESTGEN / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("AUTH_TOKEN="):
                return line.split("=", 1)[1].strip()
    import os

    return os.environ.get("AUTH_TOKEN", "")


def _opener() -> urllib.request.OpenerDirector:
    """空 ProxyHandler：本机的透明代理会 502 掉 127.0.0.1 请求。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _api(path: str, token: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json", "X-Auth-Token": token},
        method="POST" if body is not None else "GET",
    )
    with _opener().open(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_task(task_id: str, token: str) -> dict[str, Any]:
    return _api(f"/api/v1/tasks/{task_id}", token)


def _wait_task(task_id: str, token: str, *, timeout: int = 600) -> dict[str, Any]:
    started = time.time()
    last = ""
    while time.time() - started < timeout:
        task = _get_task(task_id, token)
        state = str(task.get("state") or "")
        line = f"{state} | {task.get('stage')} | {round(float(task.get('progress') or 0) * 100)}%"
        if line != last:
            print(f"  {line}")
            last = line
        if state in ("success", "failed", "cancelled"):
            return task
        time.sleep(3)
    raise SystemExit(f"任务 {task_id} 超过 {timeout}s 未结束，请手工检查")


def _submit(from_task_id: str, token: str) -> str:
    """用基线任务的请求做模板，按 PRESET 覆写，发起一次新生成。

    注意：请求体在内存里保留 `repo_url` / `code_source` / `project_name` / `local_path`
    （取码与项目复用必需），但**这些字段不会进快照**——快照只存 `REQ_KEEP` 白名单。
    """
    src = _get_task(from_task_id, token).get("request") or {}
    body = dict(src)
    body.update(PRESET)
    # 凭证类一律清空：preset 关闭 runtime，本次不需要登录口令
    for key in ("login_url", "login_user", "login_password", "login_otp", "auto_input", "test_url"):
        body[key] = ""
    body["execute"] = False
    body["async_mode"] = True
    body.setdefault("local_path", src.get("local_path") or "")
    resp = _api("/api/v1/generate", token, body=body)
    return str(resp.get("task_id") or "")


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _collect_a(task: dict[str, Any]) -> dict[str, Any]:
    result = task.get("result") or {}
    req = task.get("request") or {}
    return {
        "task_id": task.get("task_id"),
        "state": task.get("state"),
        "project_id": task.get("project_id"),
        "counts": result.get("counts") or {},
        "case_stats": result.get("case_stats") or {},
        "traceability": result.get("traceability") or {},
        "channel_summary": result.get("channel_summary") or {},
        "scope_summary": result.get("scope_summary") or {},
        "tag_summary": result.get("tag_summary") or {},
        # T5：生成期质量快照（门禁 / 三 KPI / 三通道）。它是本次运行的产物，
        # 不在 DB 里，所以必须随快照存下来，否则事后无法复核 KPI 的分母。
        "quality": result.get("quality") or {},
        "errors": result.get("errors") or [],
        "notes": result.get("notes") or [],
        "request": {k: req.get(k) for k in REQ_KEEP if k in req},
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


def _collect_b(project_id: int) -> dict[str, Any]:
    out = TESTGEN / "outputs" / str(project_id)
    tps = _read_json(out / "test_points.json")
    cases = _read_json(out / "test_cases.json")
    summary = _read_json(out / "summary.json")
    tp_items = (tps or {}).get("test_points") or [] if isinstance(tps, dict) else (tps or [])
    # 产物落盘时用例清单键名是 `cases`（早期版本用过 `test_cases`，两个都认）
    if isinstance(cases, dict):
        case_items = cases.get("cases") or cases.get("test_cases") or []
    else:
        case_items = cases or []
    return {
        "output_dir": str(out),
        "summary_generated_at": (summary or {}).get("generated_at"),
        "test_points": tp_items,
        "cases": case_items,
    }


def _collect_c(project_id: int, tp_ids: list[str], tc_nos: list[str]) -> dict[str, Any]:
    if not DB_PATH.is_file():
        return {"available": False, "reason": f"未找到 {DB_PATH}"}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        tp_rows = _select_by(conn, "test_points", "tp_id", tp_ids, project_id)
        case_rows = _select_by(conn, "cases", "tc_no", tc_nos, project_id)
        fp_total = conn.execute(
            "select count(*) from functional_points where project_id=?", (project_id,)
        ).fetchone()[0]
        return {
            "available": True,
            "test_points_found": len(tp_rows),
            "cases_found": len(case_rows),
            "fp_rows_total": int(fp_total),
            "tp_missing": sorted(set(tp_ids) - {r["tp_id"] for r in tp_rows}),
            "case_missing": sorted(set(tc_nos) - {r["tc_no"] for r in case_rows}),
            "case_status": _count_by(case_rows, "status"),
            "case_priority": _count_by(case_rows, "priority"),
        }
    finally:
        conn.close()


def _select_by(
    conn: sqlite3.Connection, table: str, key: str, values: list[str], project_id: int
) -> list[dict[str, Any]]:
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    rows = conn.execute(
        f"select * from {table} where project_id=? and {key} in ({placeholders})",
        (project_id, *values),
    ).fetchall()
    return [dict(r) for r in rows]


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _distribution(
    tp_items: list[dict[str, Any]], case_items: list[dict[str, Any]]
) -> dict[str, Any]:
    def _counter(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "")
            out[value] = out.get(value, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    return {
        "dimension": _counter(tp_items, "dimension"),
        "origin": _counter(tp_items, "origin"),
        "category": _counter(tp_items, "category"),
        "method": _counter(tp_items, "method"),
        "case_type": _counter(case_items, "case_type"),
        "priority": _counter(case_items, "priority"),
        "ctype": _counter(case_items, "ctype"),
    }


def _duration_seconds(created: str | None, updated: str | None) -> float | None:
    if not created or not updated:
        return None
    try:
        a = datetime.fromisoformat(created)
        b = datetime.fromisoformat(updated)
    except ValueError:
        return None
    return round((b - a).total_seconds(), 1)


def _build(tag: str, task: dict[str, Any]) -> dict[str, Any]:
    a = _collect_a(task)
    project_id = int(a.get("project_id") or 0)
    b = _collect_b(project_id)
    tp_ids = [str(x.get("tp_id")) for x in b["test_points"]]
    tc_nos = [str(x.get("tc_no")) for x in b["cases"]]
    c = _collect_c(project_id, tp_ids, tc_nos)

    counts = a["counts"]
    cases_a = int(counts.get("cases") or 0)
    cases_b = len(b["cases"])
    cases_c = int(c.get("cases_found") or 0)
    consistent = cases_a == cases_b == cases_c

    metrics = {
        "cases_total": cases_b,
        "test_points": len(b["test_points"]),
        "cases_direct": sum(
            1 for x in b["test_points"] if str(x.get("origin") or "rule") in ("rule", "semantic")
        ),
        # T3：语义维度产出的用例单列（本体用例 = rule + semantic，但要看语义占多少）
        "cases_semantic": sum(
            1 for x in b["test_points"] if str(x.get("origin") or "") == "semantic"
        ),
        "cases_impact": sum(1 for x in b["test_points"] if str(x.get("origin") or "") == "impact"),
        # T4：文件级跨文件影响面单列（它属于影响面，但断言强度只有「区域渲染与交互」，
        # 与后端的「入口未回归」不是一回事，混在一起看会高估覆盖强度）
        "cases_file_impact": sum(
            1 for x in b["test_points"] if str(x.get("origin") or "") == "impact_file"
        ),
        "cases_runtime": sum(
            1 for x in b["test_points"] if str(x.get("origin") or "") == "runtime"
        ),
        "fp_before": int(counts.get("fp_before") or counts.get("functional_points") or 0),
        "fp_after": int(counts.get("fp_after") or 0),
        "fp_dropped_unchanged": int(counts.get("fp_dropped_unchanged") or 0),
        "fp_attributed": int(counts.get("fp_attributed") or 0),
        "route_inferred": int(counts.get("route_inferred") or 0),
        "file_impact_items": int(counts.get("file_impact_items") or 0),
        "dimension_kinds": len({str(x.get("dimension") or "") for x in b["test_points"]}),
        "unverified_count": sum(1 for x in b["test_points"] if x.get("unverified")),
        # T5：门禁 + 三 KPI + 三通道（数值都来自生成期快照 a["quality"]，不重算）
        "gate_blocked": int(counts.get("gate_blocked") or 0),
        "gate_best_effort": int(counts.get("gate_best_effort") or 0),
        "kpi_changed_func_pct": int(counts.get("kpi_changed_func_pct") or 0),
        "kpi_impact_entry_pct": int(counts.get("kpi_impact_entry_pct") or 0),
        "kpi_dimension_pct": int(counts.get("kpi_dimension_pct") or 0),
        "changed_func_covered": int(counts.get("changed_func_covered") or 0),
        "changed_func_total": int(counts.get("changed_func_total") or 0),
        "impact_entry_covered": int(counts.get("impact_entry_covered") or 0),
        "impact_entry_total": int(counts.get("impact_entry_total") or 0),
        "dimension_actual": int(counts.get("dimension_actual") or 0),
        "dimension_theory": int(counts.get("dimension_theory") or 0),
        "channel_body": int(counts.get("channel_body") or 0),
        "channel_impact": int(counts.get("channel_impact") or 0),
        "channel_runtime": int(counts.get("channel_runtime") or 0),
        "cases_created": int(a["case_stats"].get("created") or 0),
        "cases_reused": int(a["case_stats"].get("reused") or 0),
        "duration_s": _duration_seconds(a.get("created_at"), a.get("updated_at")),
        "traceability": a["traceability"],
        "channel_summary": a["channel_summary"],
    }

    return {
        "tag": tag,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "preset": PRESET,
        "a_server": a,
        "b_outputs": {
            "output_dir": b["output_dir"],
            "summary_generated_at": b["summary_generated_at"],
            "case_count": cases_b,
            "tp_count": len(b["test_points"]),
            "evidence": [
                {
                    "tc_no": x.get("tc_no"),
                    "tp_id": x.get("tp_id"),
                    "title": x.get("title"),
                    "dimension": next(
                        (
                            t.get("dimension")
                            for t in b["test_points"]
                            if t.get("tp_id") == x.get("tp_id")
                        ),
                        "",
                    ),
                    "origin": next(
                        (
                            t.get("origin")
                            for t in b["test_points"]
                            if t.get("tp_id") == x.get("tp_id")
                        ),
                        "",
                    ),
                    "tag": next(
                        (
                            t.get("tag")
                            for t in b["test_points"]
                            if t.get("tp_id") == x.get("tp_id")
                        ),
                        "",
                    ),
                    "source": (x.get("steps") or [{}])[0].get("source")
                    if isinstance(x.get("steps"), list) and x.get("steps")
                    else "",
                }
                for x in b["cases"]
            ],
        },
        "c_database": c,
        "checks": {
            "cases_a": cases_a,
            "cases_b": cases_b,
            "cases_c": cases_c,
            "consistent": consistent,
            "issues": []
            if consistent
            else [f"三口径用例数不一致：A={cases_a} B={cases_b} C={cases_c}"],
        },
        "metrics": metrics,
        "distribution": _distribution(b["test_points"], b["cases"]),
    }


def _write(snapshot: dict[str, Any], tag: str) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    json_path = OUT_DIR / f"{day}-{tag}.json"
    json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    m = snapshot["metrics"]
    a = snapshot["a_server"]
    checks = snapshot["checks"]
    dist = snapshot["distribution"]
    lines = [
        f"# 生成度量快照 · {snapshot['tag']}",
        "",
        f"- 采集时间：{snapshot['captured_at']}",
        f"- 任务：`{a.get('task_id')}`（project {a.get('project_id')}，state {a.get('state')}）",
        f"- 耗时：{m['duration_s']}s",
        "",
        "## 三口径一致性",
        "",
        "| 口径 | 用例数 |",
        "|---|---|",
        f"| A 服务端 counts | {checks['cases_a']} |",
        f"| B 产物 test_cases.json | {checks['cases_b']} |",
        f"| C 数据库 cases 表 | {checks['cases_c']} |",
        f"| 判定 | {'一致' if checks['consistent'] else '**不一致**'} |",
        "",
        "## 核心指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 用例总数 | {m['cases_total']} |",
        f"| 测试点 | {m['test_points']} |",
        f"| 本体用例（rule+semantic） | {m['cases_direct']} |",
        f"| 其中语义维度用例 | {m['cases_semantic']} |",
        f"| 影响面用例 | {m['cases_impact']} |",
        f"| 其中文件级影响面（前端） | {m['cases_file_impact']} |",
        f"| 运行时用例 | {m['cases_runtime']} |",
        f"| 功能点 原始 → 保留 | {m['fp_before']} → {m['fp_after']} |",
        f"| 归属补救功能点 | {m['fp_attributed']} |",
        f"| 路由反查页面 | {m['route_inferred']} |",
        f"| 维度种类 | {m['dimension_kinds']} |",
        f"| 未过门禁 | {m['unverified_count']} |",
        f"| 门禁拦下 / 需特殊环境 | {m['gate_blocked']} / {m['gate_best_effort']} |",
        f"| KPI 改动函数覆盖 | {m['kpi_changed_func_pct']}%（{m['changed_func_covered']}/{m['changed_func_total']}） |",
        f"| KPI 影响入口覆盖 | {m['kpi_impact_entry_pct']}%（{m['impact_entry_covered']}/{m['impact_entry_total']}） |",
        f"| KPI 维度覆盖 | {m['kpi_dimension_pct']}%（{m['dimension_actual']}/{m['dimension_theory']}） |",
        f"| 三通道（本体/影响面/运行时） | {m['channel_body']} / {m['channel_impact']} / {m['channel_runtime']} |",
        f"| 新建 / 复用 | {m['cases_created']} / {m['cases_reused']} |",
        "",
        "## 分布",
        "",
        f"- 维度：`{json.dumps(dist['dimension'], ensure_ascii=False)}`",
        f"- 来源：`{json.dumps(dist['origin'], ensure_ascii=False)}`",
        f"- 类别：`{json.dumps(dist['category'], ensure_ascii=False)}`",
        f"- 优先级：`{json.dumps(dist['priority'], ensure_ascii=False)}`",
        "",
        "## 用例清单",
        "",
        "| tc_no | 维度 | 来源 | 标签 | 标题 |",
        "|---|---|---|---|---|",
    ]
    for item in snapshot["b_outputs"]["evidence"]:
        lines.append(
            "| {tc} | {dim} | {org} | {tag} | {title} |".format(
                tc=item.get("tc_no"),
                dim=item.get("dimension"),
                org=item.get("origin"),
                tag=item.get("tag"),
                title=str(item.get("title") or "")[:60].replace("|", "\\|"),
            )
        )
    if a.get("errors"):
        lines += ["", "## 运行错误", ""] + [f"- {e}" for e in a["errors"]]
    md_path = OUT_DIR / f"{day}-{tag}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _diff(prev_path: Path, cur: dict[str, Any]) -> None:
    prev = json.loads(prev_path.read_text(encoding="utf-8"))
    keys = [
        "cases_total",
        "test_points",
        "cases_direct",
        "cases_semantic",
        "cases_impact",
        "cases_file_impact",
        "cases_runtime",
        "fp_before",
        "fp_after",
        "fp_attributed",
        "route_inferred",
        "file_impact_items",
        "dimension_kinds",
        "unverified_count",
        # T5：门禁与三 KPI（用例数不变，变的是「可见性」与「扣水货后的覆盖」）
        "gate_blocked",
        "gate_best_effort",
        "kpi_changed_func_pct",
        "kpi_impact_entry_pct",
        "kpi_dimension_pct",
        "changed_func_covered",
        "changed_func_total",
        "impact_entry_covered",
        "impact_entry_total",
        "dimension_actual",
        "dimension_theory",
        "channel_body",
        "channel_impact",
        "channel_runtime",
    ]
    print(f"\n对比 {prev.get('tag')} → {cur.get('tag')}")
    print(f"{'指标':<20}{'上一批':>10}{'本批':>10}{'Δ':>8}")
    for key in keys:
        old = prev["metrics"].get(key) or 0
        new = cur["metrics"].get(key) or 0
        print(f"{key:<20}{old:>10}{new:>10}{new - old:>+8}")
    old_dims = set(prev["distribution"]["dimension"])
    new_dims = set(cur["distribution"]["dimension"])
    added = sorted(new_dims - old_dims)
    if added:
        print(f"新增维度：{', '.join(added)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集 testgen 生成任务的三口径度量")
    parser.add_argument("--task-id", help="已有任务 id（不重新生成）")
    parser.add_argument("--run", action="store_true", help="按固定 preset 发起一次生成再采集")
    parser.add_argument("--from-task", help="--run 时复制哪个任务的请求做模板")
    parser.add_argument("--tag", default="snapshot", help="快照名，如 baseline / T1")
    parser.add_argument("--diff", help="与上一批 JSON 做对比")
    parser.add_argument("--timeout", type=int, default=600, help="--run 轮询超时秒数")
    args = parser.parse_args(argv)

    token = _load_token()
    if not token:
        print("未找到 AUTH_TOKEN（services/testgen/.env 或环境变量）", file=sys.stderr)
        return 2

    task_id = args.task_id
    if args.run:
        template = args.from_task or args.task_id
        if not template:
            print("--run 需要 --from-task 提供请求模板", file=sys.stderr)
            return 2
        print(f"按 preset 发起生成（模板 {template}）…")
        task_id = _submit(template, token)
        if not task_id:
            print("提交失败：未返回 task_id", file=sys.stderr)
            return 2
        print(f"task {task_id}，等待结束…")
        task = _wait_task(task_id, token, timeout=args.timeout)
    else:
        if not task_id:
            print("请给 --task-id 或 --run", file=sys.stderr)
            return 2
        task = _get_task(task_id, token)

    snapshot = _build(args.tag, task)
    json_path, md_path = _write(snapshot, args.tag)
    checks = snapshot["checks"]
    print(f"\n三口径：A={checks['cases_a']} B={checks['cases_b']} C={checks['cases_c']}")
    if not checks["consistent"]:
        print("警告：三口径不一致，本批数据不可用于对比")
        for issue in checks["issues"]:
            print(f"  - {issue}")
    print(f"快照：{json_path.relative_to(ROOT)}")
    print(f"摘要：{md_path.relative_to(ROOT)}")
    if args.diff:
        _diff(Path(args.diff), snapshot)
    return 0 if checks["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
