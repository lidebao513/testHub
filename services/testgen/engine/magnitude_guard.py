"""G-13 · 量级稳定性回归守护（P2-10）。

为什么需要
----------
真实仓库（福享 Agent）曾观测到**量级**：功能点 520、运行时元素 2138。
元素级分解（#221）+ 深度发现（#223）+ 去重/上限（#224）重构后，这套计数**可能在
无人察觉的情况下无声膨胀**——一旦比例或绝对值偏离基线过多，下游测试点/用例数会
同步失真却没人报警。本模块把「基线 + 阈值」固化成纯函数，供合并环节调用。

设计原则（对照 F10a「配了以为生效」的静默陷阱）
------------------------------------------------
- **只警告、不阻断**：量级异常属于「需关注」而非「必须失败」，故 `merge_functional_points`
  在异常时只 `log.warning`（写入运行备注/日志），**绝不 raise、绝不改变合并结果**。
- **硬上限（ok=False）与软漂移（warn=True）区分**：硬上限是绝对值失控（铁定有问题），
  软漂移是比例偏离基线（可能合理也可能隐患）；二者都告警，但语义清晰便于后续接门禁。
- **基线常量带溯源注释**：520 FP / 2138 元素来自哪次真实运行，改基线时必须同步更新注释。

依赖方向严格向下：只依赖标准库 + `core.log`，不反向 import 上层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.log import get_logger


log = get_logger(__name__)


# 基线常量（**必须带溯源**，改基线时同步更新此注释）
# 来源：福享 Agent（47.97.154.50:8090）真实运行观测到的量级
#   - 功能点 FP = 520
#   - 运行时元素 = 2138
#   → 基线比例 ≈ 4.11 元素 / FP
_BASELINE_FP = 520
_BASELINE_ELEMENTS = 2138
_BASELINE_RATIO = _BASELINE_ELEMENTS / _BASELINE_FP  # ≈ 4.11

# 比例漂移容忍倍率：实际比例超出 [基线/倍率, 基线×倍率] 即告警。
# 3.0 意为「允许 ±3 倍波动」——元素级分解本就可能大幅改变比例，过严会误报。
_RATIO_TOLERANCE = 3.0

# 绝对硬上限（任一超即判定失控）：约为基线的 10 倍，给重构留足空间又不至于无限膨胀。
_MAX_FP = 5000
_MAX_ELEMENTS = 60000


@dataclass
class MagnitudeVerdict:
    """量级校验结论。

    - `ok=False`：绝对值失控（硬失败），必须排查；
    - `ok=True` 但 `warn=True`：比例相对基线漂移，需关注但不阻断；
    - 二者皆 `True`：量级在健康区间。
    """

    fp_count: int
    element_count: int
    ratio: float
    baseline_ratio: float
    ok: bool
    warn: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fp_count": self.fp_count,
            "element_count": self.element_count,
            "ratio": round(self.ratio, 3),
            "baseline_ratio": round(self.baseline_ratio, 3),
            "ok": self.ok,
            "warn": self.warn,
            "note": self.note,
        }


def check_magnitude(fp_count: int, element_count: int) -> MagnitudeVerdict:
    """校验功能点 / 元素量级是否健康。

    基线与阈值由模块常量固化（见文件头），不对外暴露可变参数以避免签名膨胀
    （PLR0913）。返回结构化结论，不直接打日志。
    """
    if fp_count <= 0:
        return MagnitudeVerdict(
            fp_count=fp_count,
            element_count=element_count,
            ratio=0.0,
            baseline_ratio=_BASELINE_RATIO,
            ok=True,
            warn=False,
            note="无功能点（fp_count<=0），跳过量级校验",
        )

    ratio = element_count / fp_count
    baseline_ratio = _BASELINE_RATIO

    # 硬上限：绝对值失控
    if fp_count > _MAX_FP or element_count > _MAX_ELEMENTS:
        return MagnitudeVerdict(
            fp_count=fp_count,
            element_count=element_count,
            ratio=ratio,
            baseline_ratio=baseline_ratio,
            ok=False,
            warn=True,
            note=(
                f"量级绝对值超出硬上限（fp={fp_count}>{_MAX_FP} 或 "
                f"elements={element_count}>{_MAX_ELEMENTS}）：疑似无声膨胀，必须排查"
            ),
        )

    # 软漂移：比例偏离基线
    low, high = baseline_ratio / _RATIO_TOLERANCE, baseline_ratio * _RATIO_TOLERANCE
    if ratio > high or ratio < low:
        return MagnitudeVerdict(
            fp_count=fp_count,
            element_count=element_count,
            ratio=ratio,
            baseline_ratio=baseline_ratio,
            ok=True,
            warn=True,
            note=(
                f"元素/FP 比例 {ratio:.2f} 偏离基线 {baseline_ratio:.2f} "
                f"（允许区间 [{low:.2f}, {high:.2f}]）：重构后量级可能变化，需确认是否预期"
            ),
        )

    return MagnitudeVerdict(
        fp_count=fp_count,
        element_count=element_count,
        ratio=ratio,
        baseline_ratio=baseline_ratio,
        ok=True,
        warn=False,
        note="量级在健康区间",
    )


def warn_if_anomalous(fp_count: int, element_count: int) -> MagnitudeVerdict:
    """合并环节调用的便捷封装：校验并在异常时 `log.warning`（**不阻断**）。

    返回结论供调用方（如写入运行备注）使用；无论是否异常都不抛异常、不改变合并结果。
    """
    verdict = check_magnitude(fp_count, element_count)
    if not verdict.ok:
        log.error("G-13 量级失控：%s", verdict.note)  # 硬失败用 error 级，便于门禁/告警捕获
    elif verdict.warn:
        log.warning("G-13 量级漂移：%s", verdict.note)
    return verdict
