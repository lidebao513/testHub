"""F1 · 取码（pull）决策分支契约测试。

设计守则要验的：
- `PullStatus` 不再是悬空契约：取码在服务内实现，结果结构与 legacy 逐字段兼容；
- **不触发网络**就能验证的分支都覆盖（克隆/深链需要真实网络，留给集成）；
- URL 凭证一律掩码，绝不进结果 / 日志；
- 安全网：缺地址不克隆、非空非仓库不克隆、空 local_path 直接报错（不是静默 noop）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.enums import PullMode, PullStatus
from core.errors import FetchError
from engine import diff_tag, pull


# 真实仓库根（本服务自身，供 ref 校验类用例使用，不触发网络）
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_mask_url_strips_credentials():
    """凭证必须被掩码，结果与日志都不出现明文。"""
    raw = "https://alice:s3cret@github.com/org/repo.git?token=abc"
    masked = pull.mask_url(raw)
    assert "s3cret" not in masked
    assert "abc" not in masked
    assert "***" in masked
    assert masked.startswith("https://***@github.com")


def test_mask_url_empty_is_safe():
    assert pull.mask_url("") == ""


def test_empty_local_path_is_error_not_noop():
    """local_path 为空属「调用方写错」，应直接抛错而不是静默 noop。"""
    with pytest.raises(FetchError):
        pull.pull("https://x/y.git", "")


def test_missing_dir_without_url_is_clone_required(tmp_path):
    """目录不存在且没给地址 → 无法克隆（诚实失败，不是假装拉到了）。"""
    target = tmp_path / "missing" / "repo"
    result = pull.pull("", str(target))
    assert result.status == PullStatus.CLONE_REQUIRED_NO_URL.value
    assert result.success is False
    assert result.mode == PullMode.NOOP.value
    assert not target.exists()


def test_empty_nongit_dir_without_url_is_clone_required(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    result = pull.pull("", str(d))
    assert result.status == PullStatus.CLONE_REQUIRED_NO_URL.value


def test_nonempty_nongit_dir_without_url_is_target_not_empty(tmp_path):
    """非空且非仓库目录：git clone 写不进 → 如实报 target_not_empty。

    注：无 url 时 `pull` 优先报 `clone_required_no_url`（没地址根本无从克隆）；
    `target_not_empty` 分支在「给了地址但仍写不进非空目录」时触发——此处给一个
    占位地址即可命中（空目录检查在克隆之前短路，不触网）。
    """
    d = tmp_path / "occupied"
    d.mkdir()
    (d / "notes.txt").write_text("i am not a repo")
    result = pull.pull("https://example.com/org/repo.git", str(d))
    assert result.status == PullStatus.TARGET_NOT_EMPTY.value
    assert result.success is False


def test_classify_target_states(tmp_path):
    missing = tmp_path / "m"
    empty = tmp_path / "e"
    empty.mkdir()
    occupied = tmp_path / "o"
    occupied.mkdir()
    (occupied / "x.txt").write_text("z")
    assert pull.classify_target(missing) == pull.STATE_MISSING
    assert pull.classify_target(empty) == pull.STATE_NOT_REPO
    assert pull.classify_target(occupied) == pull.STATE_NOT_REPO


def test_broken_git_dir_is_escape_blocked(tmp_path):
    """残缺 .git（缺 HEAD/config）必须判逃逸，绝不能把 fetch/checkout 打到父仓库。

    背景：workspaces/research-agent 的 .git 被删得只剩 hooks/objects/refs（无 HEAD），
    git 判其无效后向上逃逸到 testhub_platform 父仓库；历史实现的逃逸判定把
    「顶层是目标的祖先」白名单放行了，导致 update 模式差点改写父项目。
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    _init_repo_with_commit(parent)
    child = parent / "ws"
    (child / ".git" / "objects").mkdir(parents=True)
    (child / ".git" / "refs").mkdir()
    (child / ".git" / "hooks").mkdir()
    # .git 缺 HEAD/config → git 判定无效 → 向上解析到 parent 仓库
    assert diff_tag.git_toplevel(str(child))  # 能解析出顶层（=parent）
    assert diff_tag.repo_escape_blocked(child) is True
    assert pull.classify_target(child) == pull.STATE_ESCAPE


def test_rev_of_head_is_resolvable():
    """真实仓库里 HEAD 必须能解析出 sha（与 legacy 同口径）。"""
    sha = pull._rev(str(REPO_ROOT), "HEAD")
    assert len(sha) >= 7


def _init_repo_with_commit(repo: Path) -> None:
    """tmp_path 里造一个带单次提交的真实本地仓库（不触网）。"""
    assert pull._run(["git", "init", "-q", str(repo)]).returncode == 0
    (repo / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    assert pull._run(["git", "-C", str(repo), "add", "."]).returncode == 0
    assert (
        pull._run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "init",
            ]
        ).returncode
        == 0
    )


def test_update_restores_empty_worktree(tmp_path):
    """工作树被清空（只剩 .git）时，update 模式必须自动恢复 HEAD 文件。

    背景：fetch 只更新 .git 不补工作树；空工作树会让代码分析扫 0 个文件 →
    生成 0 功能点 0 用例却全程报 success（静默失败）。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commit(repo)
    # 模拟工作树被清空：删除 .git 之外的所有文件
    (repo / "hello.py").unlink()
    result = pull.pull("", str(repo))
    assert (repo / "hello.py").exists(), "空工作树未被恢复"
    assert result.success is True
    assert any("工作树为空" in e for e in result.errors)


def test_update_keeps_nonempty_worktree_untouched(tmp_path):
    """工作树有文件时绝不 checkout --force（保护未提交改动）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_commit(repo)
    local = (repo / "hello.py").read_text(encoding="utf-8")
    (repo / "untracked.txt").write_text("user file", encoding="utf-8")
    result = pull.pull("", str(repo))
    assert (repo / "untracked.txt").exists(), "未提交文件不得被清除"
    assert (repo / "hello.py").read_text(encoding="utf-8") == local
    assert result.success is True
    assert not any("工作树为空" in e for e in result.errors)


def test_refs_available_partial_failure_is_false():
    """ref 逐个校验：任一不可解析即整体 False——否则增量会被误判全量。"""
    assert diff_tag.refs_available(str(REPO_ROOT), "HEAD") is True
    assert diff_tag.refs_available(str(REPO_ROOT), "HEAD", "no_such_ref_xyz") is False
    assert diff_tag.refs_available(str(REPO_ROOT), "no_such_ref_xyz") is False


def test_pull_result_fields_align_with_legacy():
    """PullResult 字段与 legacy 结果逐一对齐（行为契约，防回归）。"""
    r = pull.pull("", str(REPO_ROOT / "__must_not_exist__"))
    d = r.to_dict()
    for key in (
        "status",
        "success",
        "mode",
        "url",
        "local_path",
        "base",
        "target",
        "deepened",
        "fetch_status",
        "checked_out",
        "base_sha",
        "target_sha",
        "changed_count",
        "changed_files",
        "errors",
    ):
        assert key in d, f"PullResult 缺字段 {key}（与 legacy 失配）"
