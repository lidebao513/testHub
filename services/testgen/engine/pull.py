"""引擎 · 模块十三（F1）：取码（pull）——把「给个仓库地址」变成服务内的能力。

背景（为什么要做）
------------------
在此之前，全链路的第一步（「拿到被测代码」）**不在服务内**：必须由外部技能
（`qa-code-pull`）把代码先放到 `--path`，服务只认一个已存在的目录。
而 `core.enums.PullStatus` 早已定义、docstring 却声称「与 `scripts/pull_code.py`
逐值一致、由 `tests/test_pull_code.py` 守护」——那两个文件在服务内**都不存在**：
枚举在、实现与守护测试都不在，是典型的**悬空契约**（换人接手必踩坑）。

本模块把取码内聚进服务，并产出与 legacy **逐字段兼容**的结果结构
（见 `PullResult.to_dict`），使 `PullStatus` 从悬空契约变成真实契约。

安全网（沿用 legacy 的实战教训，**不可省**）
------------------------------------------
1. **目录逃逸阻断**：目标目录里若只有孤儿 `.git`，`git -C <dir>` 会向上找到父仓库
   → 误改父项目、拿到错误 diff。命中即 `repo_escape_blocked` 且**不执行任何写操作**。
2. **非空非仓库不克隆**：`git clone` 写不进非空目录；命中即 `target_not_empty`。
3. **无地址不操作**：需要克隆却没给 URL → `clone_required_no_url`。
4. **在线探测失败即离线**：远端不可达时**用本地已有代码**（`ok_offline`）而不是报错——
   「没网就跑不了」会让本地重现问题变得不可能。
5. **URL 一律掩码**：`https://user:token@host/...` 里的凭证绝不进结果 / 日志（只留 `***`）。

有意不做（与 legacy 的差异，如实记录）
------------------------------------
- **不做 chmod 置只读**：Windows 下 chmod 非硬约束（本项目已在 `workspace/readonly.py`
  记录同一结论：`enforced=False`）。取码只做 git 原生动作（clone / fetch / checkout），
  不改写任何业务文件；只读加固交给工作区层与容器 `:ro`。
- **不做浅克隆 `--deepen` 的自动反复尝试**：`deepen>0` 时执行一次 `--deepen`，失败只记
  `fetch_status=failed` 并继续——反复重试会把一次取码拖到数分钟且收益不确定。

依赖方向严格向下（只 import core 与同层 diff_tag）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.enums import FetchStatus, PullMode, PullStatus
from core.errors import FetchError
from core.log import get_logger, log_extra
from engine import diff_tag


log = get_logger(__name__)

_GIT_TIMEOUT = 60
_NET_TIMEOUT = 300
# 协议降级：本机透明代理会破坏 git HTTPS 协商（legacy 同口径）
_GIT_PROTOCOL_PREFIX: tuple[str, ...] = ("-c", "protocol.version=0")

# 目录三态
STATE_MISSING = "missing"
STATE_REPO = "repo"
STATE_ESCAPE = "escape"
STATE_NOT_REPO = "not_repo"

# `https://user:pass@host/path` → 掩码凭证；同时吃掉 query 里的 token
_CREDENTIAL_RE = re.compile(r"//[^/@\s]+@")


def mask_url(url: str) -> str:
    """掩码 URL 中的凭证（**结果与日志里绝不出现明文**）。"""
    if not url:
        return ""
    masked = _CREDENTIAL_RE.sub("//***@", url)
    return masked.split("?", 1)[0] if "@" in url else masked


@dataclass
class PullResult:
    """取码结果（字段与 legacy `pull_code.py` 的结果**逐一对应**）。"""

    status: str = PullStatus.OK.value
    success: bool = True
    mode: str = PullMode.NOOP.value
    url: str = ""  # 已掩码
    local_path: str = ""
    base: str = ""
    target: str = ""
    deepened: bool = False
    fetch_status: str = FetchStatus.NONE.value
    checked_out: str = ""
    base_sha: str = ""
    target_sha: str = ""
    changed_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return len(self.changed_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "success": self.success,
            "mode": self.mode,
            "url": self.url,
            "local_path": self.local_path,
            "base": self.base,
            "target": self.target,
            "deepened": self.deepened,
            "fetch_status": self.fetch_status,
            "checked_out": self.checked_out,
            "base_sha": self.base_sha,
            "target_sha": self.target_sha,
            "changed_count": self.changed_count,
            "changed_files": list(self.changed_files),
            "errors": list(self.errors),
        }

    def summary_line(self) -> str:
        """一行可读摘要（写运行备注；**不含凭证**）。"""
        base = f"取码[{self.mode}] {self.status}"
        detail = f"本地 {self.local_path or '-'}"
        if self.changed_count:
            detail += f"，变更 {self.changed_count} 个文件"
        if self.errors:
            detail += f"；说明：{self.errors[0][:100]}"
        return f"{base}：{detail}"


# ============================================================================
# 目录判定与小工具
# ============================================================================
def classify_target(path: str | Path) -> str:
    """目标目录三态（外加逃逸态）：missing / repo / escape / not_repo。

    判定只认目标目录**自身**是否有 `.git`：
    - 有 `.git`：再查是否「孤儿 .git 向上逃逸到父仓库」→ 逃逸态或可用仓库；
    - 无 `.git`：无论是否碰巧位于某个父仓库内部，目标目录自身**都不是 git 仓库**，
      一律判 `not_repo`。绝不可靠 `git rev-parse --show-toplevel` 把「父仓库内的子目录」
      当成可用仓库——否则 `pull` 会去 `git -C` 父仓库、误改父项目（目录逃逸）。
    """
    p = Path(path)
    if not p.exists():
        return STATE_MISSING
    if not p.is_dir():
        return STATE_NOT_REPO
    if (p / ".git").exists():
        return STATE_ESCAPE if diff_tag.repo_escape_blocked(p) else STATE_REPO
    return STATE_NOT_REPO


def is_empty_dir(path: str | Path) -> bool:
    try:
        return not any(Path(path).iterdir())
    except OSError:
        return False


def _run(args: list[str], *, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """执行 git 命令（参数列表，不经 shell）。"""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _git(repo: str | Path, args: list[str], *, timeout: int = _GIT_TIMEOUT, net: bool = False):
    """在指定仓库执行 git；`net=True` 时加协议降级前缀并放宽超时。"""
    prefix = list(_GIT_PROTOCOL_PREFIX) if net else []
    return _run(["git", "-C", str(repo), *prefix, *args], timeout=timeout)


def _rev(repo: str | Path, ref: str) -> str:
    cp = _git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _fail(result: PullResult, status: str, message: str) -> PullResult:
    result.status = status
    result.success = False
    result.errors.append(message)
    return result


# ============================================================================
# 主流程
# ============================================================================
def pull(
    url: str = "",
    local_path: str = "",
    *,
    base: str = "",
    target: str = "",
    deepen: int = 0,
) -> PullResult:
    """取码：把远端仓库准备到 `local_path`，并（可选）算出 `base..target` 变更集。

    返回结果**永不抛异常**（除参数非法外）——取码失败是常见情况，应由调用方按
    `status` 决策；异常只保留给「调用方写错了」（如 local_path 为空）。
    """
    if not local_path.strip():
        raise FetchError("取码必须提供本地目录 local_path")
    result = PullResult(
        url=mask_url(url), local_path=str(Path(local_path)), base=base, target=target
    )
    state = classify_target(local_path)

    if state == STATE_ESCAPE:
        return _fail(
            result,
            PullStatus.REPO_ESCAPE_BLOCKED.value,
            f"目录 {local_path} 位于祖先 git 仓库内部（git 顶层逃逸），已阻断以防误改父项目；"
            "请在该目录内 git init 或删除后重新克隆为独立仓库",
        )
    if state == STATE_MISSING:
        return _clone_or_fail(result, url, local_path)
    if state == STATE_NOT_REPO:
        if not url:
            return _fail(
                result,
                PullStatus.CLONE_REQUIRED_NO_URL.value,
                f"目录 {local_path} 已存在但不是 git 仓库，且未提供仓库地址，无法克隆",
            )
        if not is_empty_dir(local_path):
            return _fail(
                result,
                PullStatus.TARGET_NOT_EMPTY.value,
                f"目录 {local_path} 已存在且非空，git clone 无法写入；请清空该目录或换路径",
            )
        return _clone_or_fail(result, url, local_path)

    # state == repo：远端更新（失败降级为离线）
    result.mode = PullMode.UPDATE.value
    _update_from_remote(result, local_path, deepen=deepen)
    return _finish_refs(result, local_path, base=base, target=target)


def _clone_or_fail(result: PullResult, url: str, local_path: str) -> PullResult:
    """克隆（目录不存在或为空时）；未提供地址 → `clone_required_no_url`。"""
    if not url:
        return _fail(
            result,
            PullStatus.CLONE_REQUIRED_NO_URL.value,
            f"本地目录 {local_path} 不存在或为空，且未提供仓库地址（REPO_URL），无法克隆",
        )
    result.mode = PullMode.CLONE.value
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    cp = _run(
        ["git", "clone", *(_GIT_PROTOCOL_PREFIX if _looks_remote(url) else ()), url, local_path],
        timeout=_NET_TIMEOUT,
    )
    if cp.returncode != 0:
        result.errors.append(f"clone 失败：{(cp.stderr or cp.stdout).strip()[:200]}")
        if _looks_remote(url):
            # 远端不可达时给出「离线可用」的明确结论，而不是笼统失败
            return _fail(
                result,
                PullStatus.CLONE_FAILED.value,
                "无法从远端克隆（网络不可达或凭据无效）；如本地已有代码请直接指定 --path",
            )
        return _fail(result, PullStatus.CLONE_FAILED.value, "本地克隆失败，请核对仓库地址")
    result.fetch_status = FetchStatus.OK.value
    return _finish_refs(result, local_path, base=result.base, target=result.target)


def _looks_remote(url: str) -> bool:
    return bool(url) and ("://" in url or url.startswith("git@") or url.startswith("ssh:"))


def _update_from_remote(result: PullResult, local_path: str, *, deepen: int) -> None:
    """远端更新（best-effort）：失败即降级为 `ok_offline`，用本地已有代码继续。"""
    origin = _git(local_path, ["remote"]).stdout.split()
    if not origin:
        result.mode = PullMode.OFFLINE.value
        result.fetch_status = FetchStatus.SKIPPED.value
        result.errors.append("本地仓库没有 remote：跳过远端更新，直接使用本地已有代码")
        return
    cp = _git(local_path, ["fetch", "--all", "--tags", "--prune"], timeout=_NET_TIMEOUT, net=True)
    if cp.returncode != 0:
        result.mode = PullMode.OFFLINE.value
        result.fetch_status = FetchStatus.FAILED.value
        result.status = PullStatus.OK_OFFLINE.value
        result.errors.append(
            f"远端更新失败（{FetchStatus.FAILED.value}），已降级为使用本地代码："
            f"{(cp.stderr or cp.stdout).strip()[:160]}"
        )
        return
    result.fetch_status = FetchStatus.OK.value
    if deepen > 0 and _is_shallow(local_path):
        dp = _git(
            local_path, ["fetch", "--deepen", str(int(deepen))], timeout=_NET_TIMEOUT, net=True
        )
        result.deepened = dp.returncode == 0
        if not result.deepened:
            result.errors.append("浅克隆加深失败（已继续使用现有历史）")


def _is_shallow(repo: str | Path) -> bool:
    return _git(repo, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"


def _finish_refs(
    result: PullResult,
    local_path: str,
    *,
    base: str,
    target: str,
) -> PullResult:
    """校验 base/target 并算出变更集（**ref 不可解析即 `refs_unavailable`**，不静默降级）。

    `target=WORKTREE` 是合法取值（与当前工作区比），此时**只校验 base**——
    把 WORKTREE 当成一个 ref 去 `rev-parse` 必然失败，会让工作树模式永远报
    「ref 不可解析」（与 F7 修复的是同一类静默/误报陷阱）。
    """
    worktree = diff_tag.is_worktree_target(target)
    refs = [r for r in (base, target) if r and not diff_tag.is_worktree_target(r)]
    if refs and not diff_tag.refs_available(local_path, *refs):
        return _fail(
            result,
            PullStatus.REFS_UNAVAILABLE.value,
            f"ref 不可解析：{base!r} / {target!r}；请核对 ref（git rev-parse --verify <ref>）",
        )
    result.base_sha = _rev(local_path, base) if base else ""
    result.target_sha = "" if worktree else (_rev(local_path, target) if target else "")
    _maybe_checkout(result, local_path, target)
    result.changed_files = sorted(_changed_files_of(local_path, base=base, target=target))
    log.info(
        "取码完成",
        extra=log_extra(
            mode=result.mode,
            status=result.status,
            changed=result.changed_count,
            url=result.url,
        ),
    )
    return result


def _changed_files_of(local_path: str, *, base: str, target: str) -> set[str]:
    """变更文件集：`base..target` / `base`（工作区）——两种形态在这里收口。"""
    if not base:
        return set()
    if not target or diff_tag.is_worktree_target(target):
        return diff_tag.compute_changed_files(local_path, base, None)
    return diff_tag.compute_changed_files(local_path, base, target)


def _maybe_checkout(result: PullResult, local_path: str, target: str) -> None:
    """把工作树切到 target（**仅当其实 ref 且与当前 HEAD 不同**）。

    为什么需要：流水线扫描的是**工作区**文件。若只 fetch 不 checkout，
    diff 算的是 `base..target`，扫的却还是旧 HEAD → 「变更集正确、代码版本错误」，
    是比报错更难发现的一类不一致。工作树模式（WORKTREE）**不 checkout**：
    它的语义就是「保留当前工作区与本地的未提交改动」，切换会把用户改动冲掉。
    """
    if not target or diff_tag.is_worktree_target(target):
        return
    want = _rev(local_path, target)
    if not want or want == _rev(local_path, "HEAD"):
        return
    cp = _git(local_path, ["checkout", "--force", target])
    if cp.returncode == 0:
        result.checked_out = target
    else:
        result.errors.append(f"工作树切到 {target} 失败：{(cp.stderr or cp.stdout).strip()[:160]}")


def check_git_available() -> bool:
    """git 是否可用（启动/能力探测用）。"""
    return shutil.which("git") is not None
