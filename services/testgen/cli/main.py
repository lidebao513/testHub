"""命令行入口（CLI）。

    python -m cli.main pipeline --path <dir> [--mode full|incremental] [--base REF --target REF]
    python -m cli.main pipeline --repo-url <仓库地址> [--path <目录>] [--deepen N]
    python -m cli.main pipeline --changed-files a.py,b.ts [--mode incremental]
    python -m cli.main pipeline --url <地址> --user <账号> --password <密码> [--otp <动态口令>]
    python -m cli.main pipeline --auto-input "<一段混排文本>"
    python -m cli.main parse-input "<一段混排文本>"
    python -m cli.main pull     --repo-url <仓库地址> [--path <目录>] [--base REF --target REF]
    python -m cli.main analyze  --path <dir>
    python -m cli.main runs     --project <项目ID> [--batch <批次号>]
    python -m cli.main report   --project <项目ID> [--batch <批次号>] [--out <目录>]
    python -m cli.main serve    [--host H] [--port P]

设计约定：
- CLI 是「最外层」，可依赖全部内层（core / engine / workspace / output）；
- 结构化输出走 stdout 的 JSON，人可读摘要走 stderr，便于脚本化调用；
- **统一智能输入框**：`--auto-input` 收一段混排文本（地址+账号+密码+动态口令+路径），
  由 `core.auto_input` 解析后填入选项；覆盖优先级为
  **显式参数 > 智能输入框 > 环境变量**；
- 凭证可走 `--user/--password/--otp`，但**更推荐环境变量**（命令行会留在 shell 历史里）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import store
from core.auto_input import AutoInputResult, parse_auto_input
from core.config import get_settings
from core.contracts import CaseSpec
from core.db import init_db
from core.enums import (
    ALL_TP_TYPES,
    DEFAULT_SCOPE,
    DEFAULT_SCOPE_LIST,
    MODE_CHOICES,
    MODE_FULL,
)
from core.errors import AppError
from core.log import setup_logging
from engine import diff_tag, pipeline, pull
from engine import report as report_engine
from engine.dialogue import DialogueAgent
from engine.scan import Scanner
from output.writer import OutputWriter


def _add_pull_parser(sub: Any) -> None:
    """`pull` 子命令（F1）：只取码、不跑生成链路。"""
    pl = sub.add_parser("pull", help="取码：把仓库准备到本地并算出变更集（F1）")
    pl.add_argument("--repo-url", default="", help="仓库地址（缺省读环境变量 REPO_URL）")
    pl.add_argument(
        "--path",
        default="",
        help="本地目标目录；缺省落到工作区根/仓库名（受管目录，便于只读加固）",
    )
    pl.add_argument("--base", default="", help="基线 ref（给定则一并算变更集）")
    pl.add_argument(
        "--target",
        default="",
        help=f"目标 ref；给 {diff_tag.WORKTREE_TARGET} 表示与当前工作区比较",
    )
    pl.add_argument("--deepen", type=int, default=0, help="浅克隆加深的提交数（0=不加深）")


def _add_pipeline_partners(sub: Any) -> None:
    """pipeline 之外的轻量子命令（解析输入 / 分析 / 留痕 / 报告 / 服务）。"""
    pi = sub.add_parser("parse-input", help="只解析统一智能输入框文本（不跑流水线）")
    pi.add_argument("text", nargs="*", help="混排文本；@文件 从文件读")
    pi.add_argument("--pretty", action="store_true", help="美化 JSON 输出")

    ana = sub.add_parser("analyze", help="只做扫描 + 功能点提取")
    ana.add_argument("--path", required=True)

    runs = sub.add_parser("runs", help="查询执行留痕（批次 / 逐条结论）")
    runs.add_argument("--project", type=int, required=True, help="项目 ID")
    runs.add_argument("--batch", default="", help="批次号；给出则额外输出该批次的逐条结论")
    runs.add_argument("--limit", type=int, default=50, help="批次 / 记录条数上限")

    rep = sub.add_parser("report", help="生成测试报告（摘要 / 覆盖率 / 趋势 / 追溯 / 证据）")
    rep.add_argument("--project", type=int, required=True, help="项目 ID")
    rep.add_argument("--batch", default="", help="执行批次号；缺省取最近一次")
    rep.add_argument("--out", default="", help="报告输出目录（缺省 outputs/<项目ID>/）")
    rep.add_argument("--no-write", action="store_true", help="只计算不落盘（用于快速核对结论）")

    srv = sub.add_parser("serve", help="启动 HTTP 服务")
    srv.add_argument("--host", default=None)
    srv.add_argument("--port", type=int, default=None)


def _add_chat_parser(sub: Any) -> None:
    """`chat` 子命令（P1-2）：对话式生成——模板优先 + 自由文本兜底。"""
    ch = sub.add_parser("chat", help="对话式生成：先给模板，填完即执行（或给自由文本）")
    ch.add_argument("--show-template", action="store_true", help="打印可填写的模板文本后退出")
    ch.add_argument("--template", default="", help="填写好的模板文件路径（key=value 格式）")
    ch.add_argument("--text", default="", help="自由文本兜底（统一智能输入框式混排文本）")
    ch.add_argument(
        "--json", action="store_true", help="只输出解析后的计划（JSON，凭证掩码），不执行"
    )
    ch.add_argument("--no-output", action="store_true", help="不写出产物文件（只计算并输出摘要）")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="testgen", description="测试用例生成服务 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pipe = sub.add_parser("pipeline", help="代码/地址 → 功能点 → 测试点 → 用例")
    pipe.add_argument("--path", default="", help="被测代码目录（可选；与 --url 至少给一个）")
    pipe.add_argument(
        "--repo-url",
        default="",
        help="仓库地址（F1）：先取码到 --path（缺省落到工作区根/仓库名），再走后续阶段",
    )
    pipe.add_argument("--deepen", type=int, default=0, help="浅克隆加深的提交数（0=不加深）")
    pipe.add_argument(
        "--changed-files",
        default="",
        help="显式变更文件清单（F2，逗号分隔；@文件 逐行读）；优先于本地 git diff",
    )
    pipe.add_argument("--name", default="", help="项目名（缺省用目录名或被测主机名）")
    pipe.add_argument("--mode", default=None, choices=list(MODE_CHOICES))
    pipe.add_argument("--base", default=None, help="增量模式基线 ref")
    pipe.add_argument(
        "--target",
        default=None,
        help=f"增量模式目标 ref；给 {diff_tag.WORKTREE_TARGET} 表示与当前工作区比较（无需提交）",
    )
    pipe.add_argument(
        "--scopes",
        default=None,
        help=f"行为维度范围，逗号分隔，可选 {ALL_TP_TYPES}（默认 {'+'.join(DEFAULT_SCOPE_LIST)}）",
    )
    pipe.add_argument("--prd", default="", help="PRD / OpenAPI 文件路径（启用 PRD 通道）")
    pipe.add_argument("--no-business", action="store_true", help="不提取业务函数")
    pipe.add_argument("--no-pages", action="store_true", help="不提取前端路由")
    pipe.add_argument("--llm", action="store_true", help="启用 LLM 增强通道")
    pipe.add_argument("--no-persist", action="store_true", help="不落库（只出报告）")
    pipe.add_argument("--no-output", action="store_true", help="不写出产物文件")
    # —— 地址通道（A1）——
    pipe.add_argument("--url", default="", help="被测环境地址（走运行时 UI 发现）")
    pipe.add_argument("--login-url", default="", help="登录页地址（缺省自动判定）")
    pipe.add_argument("--user", default="", help="登录账号（更推荐 RUNTIME_LOGIN_USER）")
    pipe.add_argument("--password", default="", help="登录密码（更推荐 RUNTIME_LOGIN_PASSWORD）")
    pipe.add_argument("--otp", default="", help="动态口令（更推荐 RUNTIME_LOGIN_OTP）")
    pipe.add_argument("--route", action="append", default=[], help="显式路由，可重复")
    pipe.add_argument(
        "--runtime-ui",
        action="store_true",
        help="启用运行时 UI 发现（等价于 RUNTIME_UI_ENABLED=on）",
    )
    pipe.add_argument(
        "--auto-input",
        default="",
        help="统一智能输入框：一段混排文本（地址+账号+密码+动态口令+路径）；@文件 从文件读",
    )
    pipe.add_argument("--execute", action="store_true", help="执行生成的用例（接口层 + UI 层）")
    pipe.add_argument(
        "--exec-url",
        default="",
        help="执行器被测服务地址（只跑接口层时用它，无需打开浏览器通道）",
    )
    pipe.add_argument(
        "--allow-write",
        action="store_true",
        help="放行写操作（POST/PUT/PATCH/DELETE）；默认只跑只读请求，防污染被测环境",
    )
    pipe.add_argument(
        "--ui-click",
        action="store_true",
        help="UI 层执行真实点击（F13）；默认只断言页面可达与元素可见，防污染被测环境",
    )
    pipe.add_argument(
        "--expert-off",
        action="store_true",
        help="关闭测试专家系统（URL 通道 PageExpert）；默认开启（D3），此开关用于无损降级对照",
    )

    _add_pull_parser(sub)
    _add_pipeline_partners(sub)
    _add_execute_parser(sub)
    _add_tenant_retest_parser(sub)
    _add_compare_parser(sub)
    _add_chat_parser(sub)
    return p


def _add_execute_parser(sub: Any) -> None:
    """`execute` 子命令（G-1）：对已有项目用例执行验证（不重新生成）。"""
    ex = sub.add_parser("execute", help="对已有项目用例执行验证（不重新生成）")
    ex.add_argument("--project", type=int, required=True, help="项目 ID（用例须已落库）")
    ex.add_argument("--exec-url", default="", help="执行器被测服务地址（只跑接口层时用它）")
    ex.add_argument("--url", default="", help="被测环境地址（运行时 UI 发现 + 接口 base_url）")
    ex.add_argument("--login-url", default="", help="登录页地址（缺省自动判定）")
    ex.add_argument("--user", default="", help="登录账号（更推荐 RUNTIME_LOGIN_USER）")
    ex.add_argument("--password", default="", help="登录密码（更推荐 RUNTIME_LOGIN_PASSWORD）")
    ex.add_argument("--otp", default="", help="动态口令（更推荐 RUNTIME_LOGIN_OTP）")
    ex.add_argument(
        "--runtime-ui",
        action="store_true",
        help="启用运行时 UI 发现通道（需 Playwright；不开启则只跑接口层）",
    )
    ex.add_argument(
        "--allow-write",
        action="store_true",
        help="放行写操作（POST/PUT/PATCH/DELETE）；默认只读，防污染被测环境",
    )
    ex.add_argument(
        "--ui-click",
        action="store_true",
        help="UI 层执行真实点击（F13）；默认只断言页面可达与元素可见",
    )


def _add_tenant_retest_parser(sub: Any) -> None:
    """`tenant-retest` 子命令（G-9 双身份越权复测专项通道）。"""
    tr = sub.add_parser(
        "tenant-retest", help="G-9 双身份越权复测：用属主+他人真实凭证重放跨租户用例"
    )
    tr.add_argument("--project", type=int, required=True, help="项目 ID（用例须已落库）")
    tr.add_argument(
        "--url", default="", help="被测环境地址（接口 base_url；缺省读 RUNTIME_UI_BASE_URL）"
    )
    tr.add_argument("--owner-user", default="", help="属主身份账号（缺省读 RUNTIME_LOGIN_USER）")
    tr.add_argument(
        "--owner-password", default="", help="属主身份密码（缺省读 RUNTIME_LOGIN_PASSWORD）"
    )
    tr.add_argument(
        "--other-user",
        default="",
        help="他人身份账号（必填才能真复测；只给属主则降级为诚实 skipped）",
    )
    tr.add_argument("--other-password", default="", help="他人身份密码")
    tr.add_argument(
        "--out",
        default="",
        help="结论落盘目录（可选）；不填则只打印 JSON 到 stdout",
    )


def _add_compare_parser(sub: Any) -> None:
    """`compare` 子命令（P0-2）：语义比对「预期(case) + 实际(actual)」→ 比对结论。"""
    cp = sub.add_parser("compare", help="语义比对：预期 + 实际 → 比对结论（规则为主·LLM 增强）")
    cp.add_argument(
        "--project",
        type=int,
        default=0,
        help="项目 ID（从库取 case；与 --json-in 二选一）",
    )
    cp.add_argument(
        "--case-id",
        default="",
        help="用例 ID（从库取预期；--project 给定时必填）",
    )
    cp.add_argument(
        "--actual",
        default="",
        help="实际执行结果 JSON（ExecutionResult.to_dict()）；@文件 从文件读",
    )
    cp.add_argument(
        "--json-in",
        default="",
        help="直接读完整比对输入 JSON 文件：{case, actual, context}（绕过库查询）",
    )
    cp.add_argument(
        "--no-llm",
        action="store_true",
        help="强制规则-only 判定，不触 LLM（无网络 / 无凭据场景）",
    )


def _parse_scopes(raw: str) -> set[str]:
    items = {s.strip() for s in raw.split(",") if s.strip()}
    invalid = items - set(ALL_TP_TYPES)
    if invalid:
        raise AppError(f"非法范围：{sorted(invalid)}，允许 {ALL_TP_TYPES}")
    return items or set(DEFAULT_SCOPE)


def _parse_list(raw: str) -> list[str]:
    """解析清单类参数（`--changed-files`）：`@文件` 读文件，否则按逗号/空白/分号切分。

    路径统一转正斜杠——与 `git diff --name-only` 的输出形态对齐，否则
    Windows 下 `a\\b.py` 永远匹配不上变更集（只能靠 basename 兜底，同名文件会误配）。
    """
    text = (raw or "").strip()
    if not text:
        return []
    body = Path(text[1:]).expanduser().read_text(encoding="utf-8") if text.startswith("@") else text
    parts = re.split(r"[\s,;]+", body)
    return [p.strip().replace("\\", "/") for p in parts if p.strip()]


def _read_text(raw: str) -> str:
    """解析输入文本：`@文件` 从文件读取，否则原样返回（空则返回空串）。"""
    text = (raw or "").strip()
    if text.startswith("@"):
        return Path(text[1:]).expanduser().read_text(encoding="utf-8")
    return raw or ""


def _read_auto_input(raw: str) -> AutoInputResult | None:
    text = _read_text(raw)
    if not text.strip():
        return None
    return parse_auto_input(text)


def _report_auto_input(parsed: AutoInputResult, adopted: list[str]) -> None:
    """把解析结果摘要写到 stderr（**只输出掩码视图**，绝不回显明文凭证）。"""
    info = parsed.redacted()
    print(
        f"[auto-input] 识别字段={info['recognized']} 已采纳={adopted} 未识别={info['unknown']}",
        file=sys.stderr,
    )
    if info["has_credentials"]:
        print("[auto-input] 已获得账号+密码，将走登录态发现", file=sys.stderr)
    else:
        print("[auto-input] 未凑齐账号+密码，运行时发现将按匿名访问", file=sys.stderr)


# 显式 CLI 参数 → 选项属性（表驱动：新增参数只加一行，避免长 if 分支链）
_OPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("path", "local_path"),
    ("repo_url", "repo_url"),
    ("deepen", "pull_deepen"),
    ("name", "project_name"),
    ("mode", "mode"),
    ("base", "base"),
    ("target", "target"),
    ("prd", "prd_source"),
)

# 地址通道参数 → TargetRequest 属性
_TARGET_FIELDS: tuple[tuple[str, str], ...] = (
    ("url", "base_url"),
    ("login_url", "login_url"),
    ("user", "login_user"),
    ("password", "login_password"),
    ("otp", "login_otp"),
)


def _apply_cli_target(args: argparse.Namespace, opts: pipeline.PipelineOptions) -> None:
    """把显式 CLI 参数覆盖到选项上（优先级最高）。"""
    for arg_name, attr in _OPTION_FIELDS:
        if getattr(args, arg_name, None):
            setattr(opts, attr, getattr(args, arg_name))
    if args.scopes:
        opts.scopes = _parse_scopes(args.scopes)
    # F2：显式变更文件清单（优先于后续取码得到的变更集）
    files = _parse_list(getattr(args, "changed_files", ""))
    if files:
        opts.changed_files = files
    req = opts.target_req
    for arg_name, attr in _TARGET_FIELDS:
        if getattr(args, arg_name, ""):
            setattr(req, attr, getattr(args, arg_name))
    if args.route:
        req.routes = [*args.route, *req.routes]
    if args.runtime_ui or args.url or args.login_url:
        req.enabled = True
    if args.exec_url:
        req.exec_url = args.exec_url
    req.execute = bool(req.execute or args.execute or args.exec_url)
    req.allow_write = bool(req.allow_write or args.allow_write)
    req.ui_click = bool(req.ui_click or getattr(args, "ui_click", False))


def _cmd_pipeline(args: argparse.Namespace) -> int:
    opts = pipeline.default_options(mode=MODE_FULL)
    parsed = _read_auto_input(args.auto_input)
    if parsed is not None:
        adopted = pipeline.apply_auto_input(opts, parsed)
        _report_auto_input(parsed, adopted)
    _apply_cli_target(args, opts)

    opts.include_business = not args.no_business
    opts.extract_pages = not args.no_pages
    opts.persist = not args.no_persist
    opts.llm.enabled = args.llm and get_settings().llm_enhance
    # D3：测试专家系统默认开；--expert-off 用于无损降级对照（验收第五条）。
    opts.expert_mode = not args.expert_off

    if opts.persist:
        init_db()

    def on_progress(stage: str, info: dict[str, Any]) -> None:
        print(f"[pipeline] {stage} {info}", file=sys.stderr)

    result = pipeline.run_pipeline(opts, progress=on_progress)

    payload: dict[str, Any] = {"result": result.to_dict()}
    if result.project_id and not args.no_output:
        payload["outputs"] = OutputWriter().write_all(
            result.project_id, result.test_points, result.cases, result.to_dict()
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_parse_input(args: argparse.Namespace) -> int:
    text = _read_text(" ".join(args.text))
    parsed = parse_auto_input(text)
    payload = {
        "ok": True,
        "parsed": parsed.redacted(),
        "recognized": parsed.recognized_fields(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


def _cmd_pull(args: argparse.Namespace) -> int:
    """取码（F1）：把仓库准备到本地，并（可选）算出 `base..target` 变更集。

    与 `pipeline --repo-url` 的区别：本命令**只取码**、不跑后续阶段——
    供外部技能 / CI 在跑流水线前先确认「代码拿到了、变更集对不对」。
    结果永远返回 JSON（含 `success`），失败时退出码 2 便于脚本判断。
    """
    url = args.repo_url or _env_repo_url()
    local_path = args.path or str(get_settings().workspace_root / pipeline.repo_dir_name(url))
    result = pull.pull(url, local_path, base=args.base, target=args.target, deepen=args.deepen)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if not result.success:
        reason = result.errors[0] if result.errors else result.status
        print(f"[error] 取码失败：{reason}", file=sys.stderr)
        return 2
    print(result.summary_line(), file=sys.stderr)
    return 0


def _env_repo_url() -> str:
    """从环境变量读仓库地址（F1）；命令行仍是最常用入口，故仅在缺省时兜底。"""
    import os

    return (os.environ.get("REPO_URL") or "").strip()


def _cmd_analyze(args: argparse.Namespace) -> int:
    from engine import fp_extract

    files = Scanner(args.path).index()
    extracted = fp_extract.extract_functional_points(files)
    print(
        json.dumps(
            {
                "path": args.path,
                "files": len(files),
                "counts": extracted.counts,
                "errors": extracted.errors[:20],
                "functional_points": [fp.to_dict() for fp in extracted.functional_points],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """查询执行留痕（F12）：批次列表 + 可选单批次逐条结论。"""
    batches = store.list_run_batches(args.project, limit=args.limit)
    payload: dict[str, Any] = {
        "project_id": args.project,
        "batches": batches,
        "latest": batches[0] if batches else None,
    }
    if args.batch:
        payload["batch"] = store.get_run_batch(args.batch)
        payload["runs"] = store.list_runs(args.project, batch_id=args.batch, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """生成测试报告（F15）：落盘 REPORT.md / REPORT.html / report.json，并打印结论摘要。"""
    built = report_engine.generate(
        args.project, batch_id=args.batch, out_dir=args.out or None, write=not args.no_write
    )
    body = built["report"]
    summary = body["exec_summary"]
    coverage = body["coverage"]
    payload: dict[str, Any] = {
        "project_id": args.project,
        "batch_id": (body.get("batch") or {}).get("batch_id"),
        "headline": summary["headline"],
        "metrics": summary["metrics"],
        "coverage": {
            "fp_rate": coverage["fp_rate"],
            "tp_rate": coverage["tp_rate"],
            "uncovered_tp_count": coverage["uncovered_tp_count"],
        },
        "trend": {"count": body["trend"]["count"], "direction": body["trend"]["direction"]},
        "flaky": {"count": body["flaky"]["count"]},
        "outputs": built["outputs"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    """对已有项目用例执行验证（G-1）：不重新生成，直接真跑接口层 / UI 层。"""
    init_db()
    opts = pipeline.default_options(mode=MODE_FULL)
    req = opts.target_req
    if args.exec_url:
        req.exec_url = args.exec_url
    if args.url:
        req.base_url = args.url
        req.enabled = True
    if args.login_url:
        req.login_url = args.login_url
    if args.user:
        req.login_user = args.user
    if args.password:
        req.login_password = args.password
    if args.otp:
        req.login_otp = args.otp
    if args.runtime_ui:
        req.enabled = True
    req.allow_write = bool(args.allow_write)
    req.ui_click = bool(args.ui_click)

    try:
        summary = pipeline.run_execution(args.project, opts)
    except AppError as exc:
        print(f"[error] {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    payload = {
        "project_id": args.project,
        "batch_id": summary.get("batch_id"),
        "state": summary.get("state"),
        "total": summary.get("total"),
        "executed": summary.get("executed"),
        "pass": summary.get("pass"),
        "fail": summary.get("fail"),
        "error": summary.get("error"),
        "skipped": summary.get("skipped"),
        "runs_written": summary.get("runs_written"),
        "cases_backfilled": summary.get("cases_backfilled"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "service.app:app",
        host=args.host or s.host,
        port=args.port or s.port,
        reload=False,
    )
    return 0


# G-9 双身份越权复测：CLI 用例字典 → CaseSpec（只取 CaseSpec 拥有的字段）
_CASE_FIELDS = (
    "tc_no",
    "title",
    "ctype",
    "steps",
    "module",
    "case_type",
    "priority",
    "precondition",
    "doc_steps",
    "tp_id",
    "fp_contract_id",
    "fp_row_id",
    "test_type",
    "status",
    "version",
    "coverage_role",
)


def _row_to_case(row: dict[str, Any]) -> CaseSpec:
    return CaseSpec(**{k: row[k] for k in _CASE_FIELDS if k in row})


class _HttpSession:
    """G-9 复测真实会话：用 requests 实现 RetestSession 协议（凭证不留存）。"""

    def __init__(self, base_url: str, user: str, password: str) -> None:
        import requests

        self._base = base_url.rstrip("/")
        self._s = requests.Session()
        self._user, self._pwd = user, password
        self._token: str | None = None

    def request(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, json: Any = None
    ) -> tuple[int, Any]:
        full = url if url.startswith("http") else self._base + url
        # 简易登录：优先 Bearer（环境变量已含 token 场景），否则表单/基本登录不在此处——交由调用方注入
        hdrs = dict(headers or {})
        if self._token:
            hdrs.setdefault("Authorization", f"Bearer {self._token}")
        resp = self._s.request(method, full, headers=hdrs, json=json, timeout=20)
        try:
            body: Any = resp.json()
        except ValueError:  # 非 JSON 响应体（HTML 错误页等）→ 退回文本，避免崩溃
            body = resp.text
        return resp.status_code, body


def _cmd_tenant_retest(args: argparse.Namespace) -> int:
    """G-9 双身份越权复测专项通道：从库里取用例 → 双身份重放 → 输出结论。

    红线：只给属主、无他人凭证时，复测降级为诚实 skipped（保留生成期结论），
    绝不伪造 PASS。失败（越权成功）即高危 FAIL 并明确标注。
    """
    from core import store
    from engine import tenant_retest

    init_db()
    settings = get_settings()
    base_url = (args.url or settings.runtime_base_url or "").strip()
    if not base_url:
        print("[error] 缺少被测地址：--url 或 RUNTIME_UI_BASE_URL 至少给一个", file=sys.stderr)
        return 2
    owner_user = (args.owner_user or settings.runtime_login_user or "").strip()
    owner_pwd = args.owner_password or settings.runtime_login_password or ""
    other_user = args.other_user.strip()
    other_pwd = args.other_password

    rows = store.list_cases(args.project)
    if not rows:
        print(f"[error] 项目 {args.project} 无用例", file=sys.stderr)
        return 2
    cases = [_row_to_case(r) for r in rows]

    owner = _HttpSession(base_url, owner_user, owner_pwd) if owner_user else None
    other = _HttpSession(base_url, other_user, other_pwd) if other_user else None
    if owner is None:
        print("[error] 缺少属主身份：--owner-user 或 RUNTIME_LOGIN_USER", file=sys.stderr)
        return 2

    summary = tenant_retest.retest_all(cases, owner, other, base_url)
    if args.out:
        from pathlib import Path

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "tenant_retest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[ok] 已写出 {out_dir / 'tenant_retest.json'}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    """语义比对（P0-2）：从库取 case 或读 JSON 文件，比对实际执行结果。

    红线：不落库、不写文件、不触执行动作；凭证脱敏由 comparator 保证。
    """
    from engine.comparator import ComparatorOptions, CompareInput, compare_one

    context: dict[str, Any] = {}
    if args.json_in:
        data = json.loads(Path(args.json_in).expanduser().read_text(encoding="utf-8"))
        case = data.get("case") or {}
        actual = data.get("actual") or {}
        context = data.get("context") or {}
    else:
        if not args.project or not args.case_id:
            print(
                "[error] 缺少参数：需 --json-in，或同时给 --project 与 --case-id",
                file=sys.stderr,
            )
            return 2
        if not args.actual:
            print("[error] 缺少 --actual（实际执行结果 JSON）", file=sys.stderr)
            return 2
        init_db()
        rows = store.list_cases(args.project)
        matched = [r for r in rows if str(r.get("id")) == str(args.case_id)]
        if not matched:
            print(
                f"[error] 项目 {args.project} 未找到用例 {args.case_id}",
                file=sys.stderr,
            )
            return 2
        case = matched[0]
        actual = json.loads(_read_text(args.actual))

    opts = ComparatorOptions.from_settings()
    if args.no_llm:
        opts = ComparatorOptions(enabled=False)
    inp = CompareInput(case=case, actual=actual, context=context)
    verdict = compare_one(inp, opts)
    print(json.dumps({"verdict": verdict.to_dict()}, ensure_ascii=False, indent=2))
    return 0


# 子命令 → 处理函数（表驱动：新增子命令只加一行，避免 main() 里堆 return 分支）
_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "pipeline": _cmd_pipeline,
    "parse-input": _cmd_parse_input,
    "pull": _cmd_pull,
    "analyze": _cmd_analyze,
    "runs": _cmd_runs,
    "report": _cmd_report,
    "execute": _cmd_execute,
    "serve": _cmd_serve,
    "tenant-retest": _cmd_tenant_retest,
    "compare": _cmd_compare,
}


def _build_opts_from_dialogue_params(params: dict[str, Any]) -> pipeline.PipelineOptions:
    """把对话计划（PipelineRequest 形状 dict）转成运行选项（与 HTTP 层共用同一份契约）。

    纯字典驱动，不依赖 service.app，保持 CLI 独立于 HTTP 层。
    """
    opts = pipeline.default_options(
        params.get("local_path") or "", mode=params.get("mode") or MODE_FULL
    )
    if params.get("auto_input", "").strip():
        pipeline.apply_auto_input(opts, parse_auto_input(params["auto_input"]))
    # 显式字段覆盖（优先级高于 auto_input）
    if params.get("repo_url"):
        opts.repo_url = params["repo_url"]
    if params.get("project_name"):
        opts.project_name = params["project_name"]
    if params.get("base"):
        opts.base = params["base"]
    if params.get("target"):
        opts.target = params["target"]
    if params.get("scopes"):
        opts.scopes = set(params["scopes"])
    req = opts.target_req
    for attr, key in (
        ("base_url", "test_url"),
        ("login_url", "login_url"),
        ("login_user", "login_user"),
        ("login_password", "login_password"),
        ("login_otp", "login_otp"),
    ):
        if params.get(key):
            setattr(req, attr, params[key])
    if params.get("test_url") or params.get("login_url"):
        req.enabled = True
    opts.include_business = bool(params.get("include_business", True))
    opts.extract_pages = bool(params.get("extract_pages", True))
    opts.persist = bool(params.get("persist", True))
    opts.llm.enabled = bool(params.get("llm_enhance") and get_settings().llm_enhance)
    return opts


def _cmd_chat(args: argparse.Namespace) -> int:
    """对话式生成（P1-2）：模板优先 + 自由文本兜底，填完即执行。"""
    agent = DialogueAgent()

    if args.show_template:
        print(agent.start().render_template_text())
        return 0

    if args.text:
        plan = agent.handle_text(args.text)
    elif args.template:
        raw = Path(args.template).expanduser().read_text(encoding="utf-8")
        filled = agent.start().parse_filled_text(raw)
        plan = agent.handle_template(filled)
    else:
        # 默认：打印模板并给出下一步提示（不直接跑，避免空参数误触）
        print(agent.start().render_template_text())
        print(
            '\n# 填好后运行：testgen chat --template <文件>   或   testgen chat --text "..."',
            file=sys.stderr,
        )
        return 0

    if not plan.ok:
        print("[error] 校验失败：", file=sys.stderr)
        for e in plan.errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    # 凭证脱敏后打印摘要（绝不回显明文）
    print(
        f"[chat] intent={plan.intent} 已采纳参数（凭证已掩码）：{json.dumps(plan.redacted, ensure_ascii=False)}",
        file=sys.stderr,
    )

    if args.json:
        print(json.dumps(plan.redacted, ensure_ascii=False, indent=2))
        return 0

    opts = _build_opts_from_dialogue_params(plan.params)
    if opts.persist:
        init_db()
    result = pipeline.run_pipeline(opts)
    payload: dict[str, Any] = {"result": result.to_dict()}
    if result.project_id and not args.no_output:
        payload["outputs"] = OutputWriter().write_all(
            result.project_id, result.test_points, result.cases, result.to_dict()
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


# chat 命令在 _cmd_chat 定义后再登记，避免名字前向引用
_COMMANDS["chat"] = _cmd_chat


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = _build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.cmd](args)
    except AppError as exc:
        print(f"[error] {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
