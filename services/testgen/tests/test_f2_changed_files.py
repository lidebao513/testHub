"""F2 · 显式变更集（changed-files）与 F4 来源打标契约测试。

要解决的两类失真：
- F2：CI / 上游平台已算好变更集，服务不应再跑一次 `git diff`（慢、且浅克隆会失真）。
  `--changed-files a.py,b.ts` / HTTP 的 `changed_files` 应直接構造差异上下文；
- F4：`runtime:<url>` 功能点来自线上环境、不对应任何 commit，必须**显式**标全量，
  而不是让 `tag_of_rel` 拿它去和变更文件比对、永远 miss、静默空转（产物里看不出为何没打标）。

本文件不触发网络克隆：纯函数用例用内存上下文，ref 校验类用例复用真实仓库（本服务）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.enums import Tag
from engine import diff_tag


REPO_ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# F2：显式变更集
# ============================================================================
def test_context_from_files_normalizes_paths():
    ctx = diff_tag.context_from_files(["a.py", r"b\c.ts", "  d/e.py  "])
    assert ctx.is_incremental is True
    assert "a.py" in ctx.changed_files
    assert "b/c.ts" in ctx.changed_files  # 反斜杠统一为正斜杠
    assert "d/e.py" in ctx.changed_files
    assert ctx.aligned is False  # 没有 hunk 行号，显式标记「未对齐」


def test_context_from_files_empty_is_full():
    ctx = diff_tag.context_from_files([])
    assert ctx.is_incremental is False


def test_tag_of_rel_marks_changed_files():
    ctx = diff_tag.context_from_files(["svc/orders.py", "api/x.ts"])
    assert diff_tag.tag_of_rel("svc/orders.py", ctx) == Tag.UPDATE.value
    assert diff_tag.tag_of_rel("api/x.ts", ctx) == Tag.UPDATE.value
    assert diff_tag.tag_of_rel("other/main.py", ctx) == Tag.FULL.value


def test_tag_of_rel_matches_by_basename_when_dir_differs():
    """同文件、不同目录（如 mock server 与真实后端）仍能命中——避免漏标。"""
    ctx = diff_tag.context_from_files(["backend/orders.py"])
    assert diff_tag.tag_of_rel("frontend/mock-server/orders.py", ctx) == Tag.UPDATE.value


def test_tag_of_rel_full_when_not_incremental():
    ctx = diff_tag.DiffContext()  # 空 → 全量通道
    assert diff_tag.tag_of_rel("any.py", ctx) == Tag.FULL.value


# ============================================================================
# F4：运行时来源显式打标（不静默空转）
# ============================================================================
def test_is_runtime_source_detects_prefix():
    assert diff_tag.is_runtime_source("runtime:http://app.local/pc/tasks") is True
    assert diff_tag.is_runtime_source("app/api.py") is False


def test_tag_of_source_marks_runtime_as_full_explicitly():
    ctx = diff_tag.context_from_files(["app/api.py"])
    # 与「碰巧 miss」结果相同（都是全量），但 F4 走专用分支，语义明确、可统计
    assert diff_tag.tag_of_source("runtime:http://app.local/pc/tasks", ctx) == Tag.FULL.value
    # 非运行时来源仍走正常比对
    assert diff_tag.tag_of_source("app/api.py", ctx) == Tag.UPDATE.value


def test_worktree_target_sentinel():
    assert diff_tag.is_worktree_target("WORKTREE") is True
    assert diff_tag.is_worktree_target("worktree") is True  # 大小写不敏感
    assert diff_tag.is_worktree_target("main") is False
    assert diff_tag.is_worktree_target("") is False


# ============================================================================
# ref 校验（诚实报错，不静默降级）
# ============================================================================
def test_refs_available_on_real_repo():
    assert diff_tag.refs_available(str(REPO_ROOT), "HEAD") is True
    assert diff_tag.refs_available(str(REPO_ROOT), "HEAD", "no_such_ref") is False


def test_refs_available_rejects_abbreviated_sha(monkeypatch):
    """G1-2 回归：缩写 SHA 即便能被 git 模糊匹配，也必须被拒绝。

    用 monkeypatch 让 run_git 对任何 revision 都返回 rc=0（模拟 git 前缀模糊
    匹配成功），证明 refs_available 的缩写 SHA 拦截不依赖 git 自身行为。
    """

    class _CP:
        returncode = 0
        stdout = "0" * 40 + "\n"
        stderr = ""

    def fake_run_git(repo, args, **kwargs):
        return _CP()

    monkeypatch.setattr(diff_tag, "run_git", fake_run_git)
    repo = str(REPO_ROOT)
    # HEAD / 完整 40 位 SHA / 分支名：不应被缩写规则拦截
    assert diff_tag.refs_available(repo, "HEAD") is True
    assert diff_tag.refs_available(repo, "0" * 40) is True
    assert diff_tag.refs_available(repo, "main") is True
    # 缩写 SHA（7 位 / 8 位 hex）：即使 git 会模糊匹配成功，也必须拒绝
    assert diff_tag.refs_available(repo, "abc1234") is False
    assert diff_tag.refs_available(repo, "deadbeef") is False
    # 混合：一个合法 + 一个缩写 → 整体 False
    assert diff_tag.refs_available(repo, "HEAD", "abc1234") is False


def test_build_context_worktree_uses_working_tree():
    """WORKTREE 模式：与当前工作区比较，且只校验 base（不把哨兵当 ref 去解析）。"""
    if not shutil.which("git"):
        pytest.skip("git 不可用")
    repo = _make_repo()
    (repo / "a.py").write_text("x = 2\n")  # 改动工作树
    ctx = diff_tag.build_context(repo, "HEAD", diff_tag.WORKTREE_TARGET)
    assert ctx.is_incremental is True
    assert "a.py" in ctx.changed_files
    assert ctx.aligned is True


def test_build_context_unresolvable_base_raises():
    """基线 ref 不可解析：必须**显式报错**（ValueError），不得静默降级为全量。"""
    if not shutil.which("git"):
        pytest.skip("git 不可用")
    repo = _make_repo()
    with pytest.raises(ValueError):
        diff_tag.build_context(repo, "no_such_ref", diff_tag.WORKTREE_TARGET)


def test_compute_changed_files_vs_working_tree():
    """F2 端到端：构造一个真实小仓库，改一个文件，变更集应只含该文件。"""
    if not shutil.which("git"):
        pytest.skip("git 不可用")
    repo = _make_repo()
    (repo / "a.py").write_text("x = 99\n")  # 改 a.py
    changed = diff_tag.compute_changed_files(repo, "HEAD")  # 与工作区比
    assert changed == {"a.py"}
    assert "sub/b.py" not in changed  # 未改动的不应出现


def _make_repo() -> Path:
    """造一个最小 git 仓库（独立、不接远端），仅用于本地 diff 计算。"""
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="f2_repo_"))
    repo = base / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    (repo / "a.py").write_text("x = 1\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, env=env)
    return repo
