#!/usr/bin/env python3
"""免费大模型登记册校验脚本（可重复用于每次「阿里云免费额度截图」）。

读取 `services/testgen/.env` 的 `LLM_MODEL_REGISTRY`（格式：模型名|类型|到期|是否开启），
逐项判定并输出：

  1. **类型**是否可用于文本对话链路（image/video/realtime 等会被拒）；
  2. **到期**：支持 `YYYY-MM-DD`（当天 23:59:59 失效）与 `YYYY-MM-DD HH:MM`（精确时刻）；
  3. **临近到期闸**：剩余时长 < `LLM_EXPIRY_GUARD_MINUTES`（默认 60 分钟）即提前禁用，
     避免一次调用跨过到期时刻——跨期部分按量计费不报错、只会悄悄进账单；
  4. **额度状态**三态：`exhausted`（已耗尽，凭 403 AllocationQuota.FreeTierOnly 落盘标记）/
     `ok`（有过成功调用且未收到耗尽信号）/ `unknown`（从未调用过）。

用法：
    python scripts/llm_model_registry_check.py
    LLM_MODEL_REGISTRY="a|llm|2026-12-01|on" python scripts/llm_model_registry_check.py
    # 清除「免费额度已耗尽」标记（换新一批免费模型 / 平台补发额度后）：
    python scripts/llm_model_registry_check.py --clear-quota
    python scripts/llm_model_registry_check.py --clear-quota qwen3.7-flash-2026-07-15

依赖：services/testgen 的 venv（需要 python-dotenv 读 .env，缺省则只读当前环境变量）。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDE = ROOT / "services" / "testgen"
sys.path.insert(0, str(SIDE))

# 额度状态三态 → 展示文案
_QUOTA_LABEL = {"ok": "充足", "exhausted": "已耗尽", "unknown": "未知"}


def _load_env() -> None:
    """尽量加载 sidecar 的 .env（不覆盖已存在的环境变量）。"""
    if os.getenv("LLM_MODEL_REGISTRY"):
        return
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        print("提示：未安装 python-dotenv，跳过 .env 加载，仅使用当前环境变量。")
        return
    load_dotenv(SIDE / ".env")


def _split_csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _dwidth(text: str) -> int:
    """字符串显示宽度（中文/全角算 2 列），用于表格对齐。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def _remain_text(v: dict) -> str:
    """剩余时长：>=1 天显示天数；<1 天显示小时+分钟；已过期单独标注。"""
    if v.get("expired"):
        return "已过期"
    minutes = v.get("minutes_left")
    if minutes is None:
        return "长期"
    # 语音类「每月重置·长期有效」用 2099-12-31 表达，别显示成 26722 天
    if minutes >= 1440 * 3650:
        return "长期"
    if minutes >= 1440:
        return f"{minutes // 1440}天"
    if minutes >= 60:
        return f"{minutes // 60}h{minutes % 60}m"
    return f"{minutes}m"


def _print_table(verdicts: list[dict], chain: list[str]) -> None:
    # 模型名长度不一（如 qwen3.8-livetranslate-flash-realtime 有 32 字符），按最长者自适应列宽，
    # 否则名字会把「类型」列顶歪，读起来像是名字里带了单词 realtime。
    name_w = max(30, max((_dwidth(v["model"]) for v in verdicts), default=0) + 2)
    widths = (name_w, 11, 20, 9, 6, 6, 9, 6)
    head = ("模型", "类型", "到期时刻", "剩余", "开启", "文本", "额度", "链路")
    print("".join(_pad(h, w) for h, w in zip(head, widths, strict=False)) + "说明")
    print("-" * 120)
    for v in verdicts:
        text_flag = "✓" if v["usable_for_text"] else "✗"
        exp_txt = (v.get("expire_at") or v["expiry"]) + (
            "*" if v.get("expiry_assumed") else ""
        )
        cells = (
            v["model"],
            v["type"],
            exp_txt,
            _remain_text(v),
            "on" if v["enabled"] else "off",
            text_flag,
            _QUOTA_LABEL.get(
                v.get("quota_state", "unknown"), v.get("quota_state", "?")
            ),
            "是" if v["model"] in chain else "-",
        )
        print(
            "".join(_pad(c, w) for c, w in zip(cells, widths, strict=False))
            + v["reason"]
        )
    print("-" * 120)


def _main(argv: list[str]) -> int:
    _load_env()
    from core.llm_budget import (
        clear_quota_marks,
        evaluate_registry,
        expiry_guard_minutes,
        resolve_text_chain,
        sort_chain_by_expiry,
    )

    if "--clear-quota" in argv:
        idx = argv.index("--clear-quota")
        target = None
        if len(argv) > idx + 1 and not argv[idx + 1].startswith("-"):
            target = argv[idx + 1]
        removed = clear_quota_marks(target)
        print(f"已清除 {removed} 条「免费额度已耗尽」标记（{target or '全部'}）。")
        return 0

    verdicts = evaluate_registry()
    if not verdicts:
        print("未在 LLM_MODEL_REGISTRY 中找到任何模型。")
        print(
            "请把截图里的模型按 `模型名|类型|到期|是否开启` "
            "登记进 services/testgen/.env 的 LLM_MODEL_REGISTRY。"
        )
        return 1

    # 链路：显式配置的按到期时刻重排（与运行期一致），否则用自动推导的
    raw_chain = _split_csv(os.getenv("LLM_MODEL_CHAIN"))
    chain = sort_chain_by_expiry(raw_chain) if raw_chain else resolve_text_chain()
    by_name = {v["model"]: v for v in verdicts}
    guard = expiry_guard_minutes()

    print("=" * 120)
    print(f"免费大模型登记册校验   (现在 = {date.today().isoformat()})")
    snapshot = (os.getenv("LLM_MODEL_REGISTRY_SNAPSHOT") or "").strip()
    default_days = os.getenv("LLM_MODEL_DEFAULT_EXPIRY_DAYS") or "30"
    print(
        f"登记日 = {snapshot or '未设置'}    到期留空按默认 {default_days} 天推定"
        f"    临近到期闸 = 剩余 < {guard} 分钟即提前禁用"
    )
    print("=" * 120)
    _print_table(verdicts, chain)
    print(f"* = 截图未写到期（「长期」），按登记日 + {default_days} 天推定")
    print()

    # 计数
    usable = sum(1 for v in verdicts if v["usable_for_text"])
    non_text = sum(1 for v in verdicts if not v["text_capable"])
    exhausted = [v["model"] for v in verdicts if v.get("quota_state") == "exhausted"]
    unknown = [v["model"] for v in verdicts if v.get("quota_state") == "unknown"]
    ok_count = sum(1 for v in verdicts if v.get("quota_state") == "ok")
    print(
        f"文本可用：{usable} 个    "
        f"非文本对话（视觉/视频/语音/向量/实时）：{non_text} 个    "
        f"其余：过期/关闭/日期非法/额度耗尽"
    )
    print(
        f"额度状态：充足 {ok_count} 个 · 已耗尽 {len(exhausted)} 个 · 未知 {len(unknown)} 个"
    )
    print()

    # 实际生效的降级链（运行期就是这个顺序）
    print("实际生效的文本降级链（按到期时刻升序：用完即停，快到期的先消耗）：")
    print("  -> " + (" , ".join(chain) if chain else "（空）"))
    if raw_chain and raw_chain != chain:
        print(f"  （.env 里书写顺序为 {raw_chain}，运行期已按到期时刻重排）")
    idle = [
        v["model"] for v in verdicts if v["usable_for_text"] and v["model"] not in chain
    ]
    if idle:
        print(
            f"  （类型可调用但未进链路：{', '.join(idle)}——需要时再手动加入 LLM_MODEL_CHAIN）"
        )
    print()

    # 链路健康
    bad = [m for m in chain if m in by_name and not by_name[m]["usable_for_text"]]
    unknown_chain = [m for m in chain if m not in by_name]
    if bad:
        print("⚠ 链路告警：以下模型当前不可用，轮到它时会跳过（护栏已拦，不会花钱）：")
        for m in bad:
            print(f"   · {m}：{by_name[m]['reason']}")
    if unknown_chain:
        print("⚠ 链路告警：以下模型未登记在册（无法校验类型/到期），建议补登记：")
        for m in unknown_chain:
            print(f"   · {m}")
    if not bad and not unknown_chain:
        print("✓ 链路健康：链上模型全部已登记且当前可用。")
    print()

    # 临近到期闸（剩余 < 1 小时，已提前禁用）
    near = [v for v in verdicts if v.get("near_expiry")]
    if near:
        print(f"⏰ 临近到期闸已触发（剩余 < {guard} 分钟，已提前禁用，避免跨期扣费）：")
        for v in near:
            print(
                f"   · {v['model']}：{v.get('expire_at')} 失效，仅剩 {_remain_text(v)}"
            )
        print()

    # 额度耗尽清单（由运行时 403 自动标记）
    if exhausted:
        print(
            "⛔ 免费额度已耗尽（运行时收到 403 AllocationQuota.FreeTierOnly 自动标记）："
        )
        for name in exhausted:
            mark = by_name.get(name, {})
            print(f"   · {name}：{mark.get('reason', '')}")
        print("   平台补发额度 / 换成新快照后可清除标记：")
        print("   python scripts/llm_model_registry_check.py --clear-quota [模型名]")
        print()

    # 到期提醒：30 天内到期
    soon = [
        v
        for v in verdicts
        if v["usable_for_text"]
        and v.get("days_left") is not None
        and 0 <= v["days_left"] <= 30
    ]
    if soon:
        print("到期提醒（30 天内，按紧迫度）：")
        for v in sorted(soon, key=lambda x: x.get("expire_at") or ""):
            print(
                f"   · {v['model']}：{v.get('expire_at')} 失效，还剩 {_remain_text(v)}"
            )
        print()

    # 结论
    print("结论：")
    if usable:
        print(
            f"  · 可进文本链路的 {usable} 个模型按到期时刻升序依次调用，前一个不可用自动切下一个。"
        )
    if non_text:
        print(
            f"  · {non_text} 个非文本对话模型（视觉/视频/语音/向量/实时）不能用于文本对话，护栏会自动拒绝。"
        )
    print(
        "  · 额度状态由运行时 403 自动学习：命中 FreeTierOnly 即标记为「已耗尽」并跳过，"
        "无需手工维护。"
    )
    # 检查口径一致性提示：额度状态用的是哪个 quota 文件
    print(
        f"  · 额度标记文件：{os.getenv('LLM_QUOTA_STATE_PATH') or (SIDE / 'data' / 'llm_quota_state.json')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
