"""引擎 · 模块四：差异打标（diff_tag）。

两条输入通道共用本模块：
  - 全量通道：不做 diff，所有测试点标 `全量`；
  - 增量通道：`git diff base..target` → 变更文件集 + hunk 行区间 → 命中即标 `更新`。

两种增量基准形态：
  - **双 ref**：`base..target`，两个 ref 都必须能解析为 commit（真·版本对比）；
  - **工作树**（`target=WORKTREE`）：`git diff base`，比较 base 与**当前工作区**
    （含未提交改动）——这正是「拿到一份代码、只想知道相对基线改了什么」的常用形态，
    无需先把改动提交出来。行号取自工作区，与扫描的源码天然对齐。

安全约束（沿用 legacy 的实战教训）：
  - 先做**仓库可用性校验**：若 `.git` 向上逃逸到祖先仓库（孤儿 `.git` 目录），
    一律阻断，避免 `git -C <target>` 误伤本项目仓库、或拿到错误的 diff；
  - git 调用一律走参数列表（不经 shell），并设超时。

诚实约束（F7）：
  本模块**只负责如实报错**：`base/target` 明确给出却不可解析时抛 `ValueError`，
  由调用方决定是否降级——历史上这里曾静默降级为全量，导致所有「更新」标签失真
  且无任何告警（失效的 `test-20260906/07` ref 就是这类）。
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from core.enums import Tag
from core.errors import WorkspaceEscapeBlocked


_GIT_TIMEOUT = 60
# @@ -old_start,old_len +new_start,new_len @@
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")

# 工作树哨兵：`--target WORKTREE` 表示「不与某个 commit 比，而与当前工作区比」。
# 这是唯一被 accept 的非 ref 取值——其余无法解析的 target 一律报错，不再静默降级。
WORKTREE_TARGET = "WORKTREE"

# 运行时（地址通道）功能点的来源标记前缀：`file_path = "runtime:<url>"`。
# 这类功能点没有版本概念（不是从某个 commit 的代码里提出来的），因此无法参与 diff——
# 必须**显式**处理，否则 `tag_of_rel` 会拿 "runtime:http://..." 去和变更文件集比对，
# 永远 miss → 全部落到「全量」。结果虽与「按设计应然」一致，却是一次**静默空转**：
# 使用者无法从产物里看出「这些功能点为什么没有被增量打标」。见 `tag_of_source`。
RUNTIME_SOURCE_PREFIX = "runtime:"


def is_worktree_target(target: str | None) -> bool:
    """`target` 是否为工作树哨兵（大小写不敏感）。"""
    return (target or "").strip().upper() == WORKTREE_TARGET


def is_runtime_source(rel: str) -> bool:
    """来源是否为运行时（地址通道）功能点（`runtime:<url>`）。

    F4：这类功能点无版本概念，必须显式识别而不是让 `tag_of_rel` 空转。
    """
    return (rel or "").strip().startswith(RUNTIME_SOURCE_PREFIX)


def run_git(
    repo: str | Path, args: list[str], *, timeout: int = _GIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """在指定仓库执行 git 命令（参数列表，不启用 shell）。"""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def git_toplevel(repo: str | Path) -> str | None:
    """返回 git 顶层目录；非仓库返回 None。"""
    try:
        cp = run_git(repo, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def repo_escape_blocked(repo: str | Path) -> bool:
    """判断目标目录的 git 顶层是否**越过了**目标目录本身（逃逸）。

    典型场景：目标目录里只有一个孤儿 `.git`，git 会向上找到父仓库，
    导致 diff 拿到的是父仓库的变更——必须阻断。
    """
    top = git_toplevel(repo)
    if not top:
        return False
    target = Path(repo).resolve()
    try:
        top_path = Path(top).resolve()
    except OSError:
        return True
    return top_path != target and top_path not in target.parents


# G1-2 修复：缩写 SHA（不完整 40 位 hex）会被 `git rev-parse` 做前缀模糊匹配，
# 误判为「可用」→ build_context 不报错 → 算出错误增量（更新标签失真）。
# 此类必须显式拒绝：只有完整 40 位 SHA 才走 rev-parse 校验；HEAD / 分支名 /
# 标签名 / HEAD~N / main~2 等相对表达不受影响。
_SHA_HEX = re.compile(r"^[0-9a-fA-F]{4,}$")
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def refs_available(repo: str | Path, *refs: str) -> bool:
    """给定的 base/target ref 是否**逐个**都能解析为 commit。

    注意：`git rev-parse --verify` 一次只接受**一个** revision，
    传多个会以 "Needed a single revision" 失败（rc=1）——必须逐个校验，
    否则任何增量运行都会被误判为「ref 不可解析」而静默降级为全量。

    G1-2：缩写 SHA（4–39 位 hex）一律拒绝——git 会对其前缀模糊匹配成功，
    若放任会让 `--base <误截SHA>` 静默产出错误增量。完整 40 位 SHA 仍正常校验。
    """
    if not refs:
        return False
    for ref in refs:
        if _SHA_HEX.match(ref) and not _FULL_SHA.match(ref):
            return False
        try:
            cp = run_git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        except (OSError, subprocess.SubprocessError):
            return False
        if cp.returncode != 0:
            return False
    return True


def _diff_range(base: str, target: str | None) -> str:
    """构造 git diff 的版本区间：`base..target`，或 `base`（与工作区比较）。"""
    return f"{base}..{target}" if target else base


def compute_changed_files(repo: str | Path, base: str, target: str | None = None) -> set[str]:
    """返回变更文件集合（正斜杠相对路径）；`target=None` 表示与工作区比较。"""
    try:
        cp = run_git(repo, ["diff", "--name-only", _diff_range(base, target)])
    except (OSError, subprocess.SubprocessError):
        return set()
    if cp.returncode != 0:
        return set()
    return {ln.strip().replace("\\", "/") for ln in cp.stdout.splitlines() if ln.strip()}


def compute_diff_hunks(
    repo: str | Path, base: str, target: str | None = None
) -> dict[str, list[tuple[int, int]]]:
    """返回 {相对路径: [(新文件起始行, 行数), ...]}；`target=None` 表示与工作区比较。"""
    try:
        cp = run_git(repo, ["diff", "-U0", _diff_range(base, target)])
    except (OSError, subprocess.SubprocessError):
        return {}
    if cp.returncode != 0:
        return {}

    hunks: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in cp.stdout.splitlines():
        m = _DIFF_FILE_RE.match(line)
        if m:
            current = m.group(1).replace("\\", "/")
            hunks.setdefault(current, [])
            continue
        if current is None:
            continue
        hm = _HUNK_RE.match(line)
        if hm:
            start = int(hm.group(1))
            length = int(hm.group(2)) if hm.group(2) is not None else 1
            hunks[current].append((start, length))
    return hunks


# ---------------------------------------------------------------- 打标
@dataclass
class DiffContext:
    """增量上下文（全量通道传空即退化为「全量」）。"""

    changed_files: set[str] = field(default_factory=set)
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    aligned: bool = False  # 是否已按 target ref 对齐行号

    @property
    def is_incremental(self) -> bool:
        return bool(self.changed_files)


def tag_of_rel(rel: str, ctx: DiffContext) -> str:
    """文件级打标：文件在变更集内即「更新」。"""
    if not ctx.is_incremental:
        return Tag.FULL.value
    norm = (rel or "").replace("\\", "/")
    if norm in ctx.changed_files:
        return Tag.UPDATE.value
    base = norm.rsplit("/", 1)[-1]
    for changed in ctx.changed_files:
        if changed.rsplit("/", 1)[-1] == base:
            return Tag.UPDATE.value
    return Tag.FULL.value


def tag_of_source(rel: str, ctx: DiffContext) -> str:
    """按「来源形态」打标：运行时来源显式走专用分支，不落进 `tag_of_rel` 的空转。

    F4：`runtime:<url>` 功能点是从**当前线上环境**发现的，不对应任何 commit，
    因此增量通道对它无从打标。此处**显式**返回「全量」——与「碰巧 miss」结果相同，
    但语义明确，且调用方（`pipeline.stage_tag`）能据此统计并写出备注。
    """
    if is_runtime_source(rel):
        return Tag.FULL.value
    return tag_of_rel(rel, ctx)


def context_from_files(files: Iterable[str]) -> DiffContext:
    """F2：由**显式变更文件清单**构造差异上下文（不跑 git diff）。

    用途：CI / 上游平台已经算好变更集，只需把清单交给服务——服务再跑一次
    `git diff` 既慢又可能因浅克隆而失真。典型入口：`--changed-files a.py,b.ts`
    或 HTTP 请求体的 `changed_files`。

    诚实边界：只有文件清单、没有 hunk 行号，因此只做**文件级**打标
    （命中文件的全部功能点都标「更新」），`aligned=False` 明示「未对齐到行号」，
    不得据此做符号级判定。
    """
    norm = {str(f).strip().replace("\\", "/") for f in files if str(f).strip()}
    return DiffContext(changed_files=norm, hunks={}, aligned=False)


def tag_of_symbol(rel: str, start_line: int, end_line: int, ctx: DiffContext) -> str:
    """符号级打标：符号行区间 ∩ hunk 区间命中即「更新」；无 hunk 数据则降级到文件级。"""
    if not ctx.is_incremental:
        return Tag.FULL.value
    norm = (rel or "").replace("\\", "/")
    spans = ctx.hunks.get(norm)
    if not spans:
        return tag_of_rel(norm, ctx)
    for start, length in spans:
        hunk_end = start + max(length, 1) - 1
        if start_line <= hunk_end and end_line >= start:
            return Tag.UPDATE.value
    return Tag.FULL.value


def build_context(repo: str | Path, base: str | None, target: str | None) -> DiffContext:
    """构造差异上下文（含安全校验）。

    - base/target 任一为空 → 全量通道（返回空上下文，调用方据此标『全量』）
    - 仓库逃逸 → 抛 WorkspaceEscapeBlocked
    - **target = WORKTREE** → 与当前工作区比较（只校验 base）
    - ref 不可解析 → 抛 ValueError（**调用方须显式处理，不得静默降级**）
    """
    if not base or not target:
        return DiffContext()
    if repo_escape_blocked(repo):
        raise WorkspaceEscapeBlocked(f"{repo} 的 git 顶层逃逸到祖先目录，已阻断")
    if is_worktree_target(target):
        if not refs_available(repo, base):
            raise ValueError(f"基线 ref 不可解析：{base}（工作树模式只校验基线）")
        return DiffContext(
            changed_files=compute_changed_files(repo, base, None),
            hunks=compute_diff_hunks(repo, base, None),
            aligned=True,  # 行号取自当前工作区，与被扫描的源码天然对齐
        )
    if not refs_available(repo, base, target):
        raise ValueError(f"ref 不可解析：{base} / {target}")
    return DiffContext(
        changed_files=compute_changed_files(repo, base, target),
        hunks=compute_diff_hunks(repo, base, target),
        aligned=True,
    )


def base_ref_of(repo: str | Path, branch: str = "HEAD") -> str | None:
    """取工作树模式下用于对比的基线 ref。"""
    try:
        cp = run_git(repo, ["rev-parse", "--verify", "--quiet", branch])
    except (OSError, subprocess.SubprocessError):
        return None
    return branch if cp.returncode == 0 else None


def is_repo(path: str | Path) -> bool:
    """目录本身是否为 git 仓库（`.git` 存在且未被逃逸）。"""
    p = Path(path)
    if not (p / ".git").exists():
        return False
    return not repo_escape_blocked(p)


def normalize_rel(rel: str) -> str:
    return os.path.normpath(rel).replace("\\", "/") if rel else ""
