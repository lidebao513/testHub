"""卡片式导航只读探针（批次 D 校准工具）。

用途：在新 SPA 上**只读**抓取「卡片导航候选」，用于校准 auto 判定口径
（什么情况该自动开/关）与危险词表 / 白名单容器，不点击、零副作用、不写库。

用法（本机，服务已在 :3900 等端口运行）：

    services/testgen/.venv/Scripts/python.exe scripts/probe_card_nav.py \\
        --url http://127.0.0.1:3900 --out docs/metrics/card-nav-probe.json

需要登录态时，沿用运行时的环境变量（不落脚本、不进产物）：

    RUNTIME_LOGIN_USER / RUNTIME_LOGIN_PASSWORD / RUNTIME_AUTH_TOKEN / PLAYWRIGHT_CHANNEL

输出 JSON 字段：
    url / candidates / danger_filtered / labels / selectors / nav_labels / home_links / note

「更多 SPA 实测校准」流程：
    1. 对多个 SPA 跑本探针，收集每站的 candidates / nav_labels / home_links；
    2. 对照 docs/testgen_ui_element_cases_2026-11-19.md §3.10 的四来源判定口径；
    3. 若发现「有来源却判开」或「无来源却判关」的真实场景，再调 _card_nav_decision
       或 _CARD_DANGER_HINTS / _CARD_SCOPE_SELECTOR，并补单测回归。

只读保证：本脚本只调用候选提取 JS（`_build_card_candidate_js`）与链接 / 导航文本抓取，
**不调用** `_discover_card_routes` / 任何 `.click()`，因此不会触发卡片动作。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# 允许从仓库根直接跑：把 services/testgen 加进 sys.path（引擎包在那里）
_TESTGEN = Path(__file__).resolve().parents[1] / "services" / "testgen"
sys.path.insert(0, str(_TESTGEN))

from engine import runtime_ui
from engine.runtime_ui import (
    _crawl_links,
    _harvest_nav_labels,
    _nav_labels,
)


def probe(base_url: str, *, channel: str = "", timeout: int = 30) -> dict[str, Any]:
    """只读抓一个站点的卡片候选与四来源证据，返回可落盘的字典。"""
    from playwright.sync_api import sync_playwright

    report: dict[str, Any] = {
        "url": base_url,
        "candidates": 0,
        "danger_filtered": 0,
        "labels": [],
        "selectors": [],
        "nav_labels": [],
        "home_links": [],
        "note": "",
    }
    opts = runtime_ui.RuntimeUiOptions(
        base_url=base_url,
        timeout=timeout,
        channel=channel or os.environ.get("PLAYWRIGHT_CHANNEL", ""),
        auth_token=os.environ.get("RUNTIME_AUTH_TOKEN", ""),
        login_user=os.environ.get("RUNTIME_LOGIN_USER", ""),
        login_password=os.environ.get("RUNTIME_LOGIN_PASSWORD", ""),
    )
    with sync_playwright() as pw:
        browser = runtime_ui.launch_browser(pw, opts)
        context = browser.new_context(ignore_https_errors=True)
        try:
            carrier = runtime_ui.RuntimeUiResult(base_url=base_url)
            page, _logged_in = runtime_ui.open_authenticated_page(context, opts, carrier)
            raw = page.evaluate(runtime_ui._build_card_candidate_js())
            candidates = [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
            safe = []
            for cand in candidates:
                label = str(cand.get("label") or "")
                if runtime_ui._is_card_danger_label(label):
                    report["danger_filtered"] += 1
                    continue
                safe.append(cand)
            report["candidates"] = len(safe)
            report["labels"] = [str(c.get("label") or "") for c in safe]
            report["selectors"] = [str(c.get("selector") or "") for c in safe]
            report["nav_labels"] = list(_nav_labels(page)) + list(_harvest_nav_labels(page))
            report["home_links"] = list(_crawl_links(page))
            if carrier.notes:
                report["note"] = " | ".join(carrier.notes[:5])
        finally:
            runtime_ui.close_quietly(context, browser)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="卡片导航只读探针（不点击、零副作用）")
    parser.add_argument("--url", required=True, help="被测站点地址（如 http://127.0.0.1:3900）")
    parser.add_argument("--out", default="", help="输出 JSON 路径；空则打印到 stdout")
    parser.add_argument("--channel", default="", help="Playwright channel（msedge/chrome）")
    parser.add_argument("--timeout", type=int, default=30, help="超时秒数")
    args = parser.parse_args()

    report = probe(args.url, channel=args.channel, timeout=args.timeout)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {out}")
        print(
            f"候选 {report['candidates']}、危险过滤 {report['danger_filtered']}、"
            f"导航文本 {len(report['nav_labels'])}、首页链接 {len(report['home_links'])}"
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
