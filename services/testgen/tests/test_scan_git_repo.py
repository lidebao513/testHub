"""DEF-20261116-01 回归：通用 git 仓库快速扫描不再强制要求 Codeup PAT。

覆盖：
- ``scan_git_repo`` 浅克隆本地 git 仓库 → 扫描出文件与 Python 符号；
- ``base_ref``（分支名 / 完整 SHA）补取基线后正确标注 added/modified；
- 临时目录用完即清理、克隆失败如实抛错；
- ``_dispatch_scan`` 三类仓库分派口径（local / Codeup / 通用 git）。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.errors import ValidationError
from engine import remote_scan


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    (repo / "pkg").mkdir(parents=True)
    (repo / "README.md").write_text("# sample\n", encoding="utf-8")
    (repo / "pkg" / "mod.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ui@example.test")
    _git(repo, "config", "user.name", "UI Verify")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


# ---------------------------------------------------------------------------
# scan_git_repo：浅克隆 → 扫描
# ---------------------------------------------------------------------------
def test_scan_git_repo_lists_files_and_symbols(git_repo: Path) -> None:
    items, meta = remote_scan.scan_git_repo(git_repo.as_uri())
    by_path = {it["path"]: it for it in items}
    assert "pkg/mod.py" in by_path
    assert "hello" in by_path["pkg/mod.py"]["functions"]
    assert meta["source_mode"] == "git-shallow"
    assert meta["truncated"] is False


def test_scan_git_repo_marks_changes_with_branch_base(git_repo: Path) -> None:
    """基线是分支名：需显式 refspec 补取 origin/<base> 才能做出增量。"""
    _git(git_repo, "branch", "base")
    (git_repo / "pkg" / "mod.py").write_text("def hello():\n    return 2\n", encoding="utf-8")
    (git_repo / "pkg" / "added.py").write_text("def added():\n    pass\n", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-qm", "change")

    items, _ = remote_scan.scan_git_repo(git_repo.as_uri(), base_ref="base")
    status = {it["path"]: it["change_status"] for it in items}
    assert status["pkg/mod.py"] == remote_scan.CHANGE_MODIFIED
    assert status["pkg/added.py"] == remote_scan.CHANGE_ADDED


def test_scan_git_repo_cleans_temp_dir(git_repo: Path) -> None:
    before = set(Path(tempfile.gettempdir()).glob("tg-git-scan-*"))
    remote_scan.scan_git_repo(git_repo.as_uri())
    assert set(Path(tempfile.gettempdir()).glob("tg-git-scan-*")) == before


def test_scan_git_repo_raises_on_bad_url(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git 浅克隆失败"):
        remote_scan.scan_git_repo((tmp_path / "missing").as_uri())


# ---------------------------------------------------------------------------
# _dispatch_scan：三类仓库分派
# ---------------------------------------------------------------------------
def test_dispatch_scan_general_git_needs_no_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    import service.app as service_app

    calls: dict[str, Any] = {}

    def fake(
        url: str, *, ref: str = "", base_ref: str = ""
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        calls["url"] = url
        return ([{"path": "a.py", "name": "a.py", "functions": [], "classes": []}], {})

    monkeypatch.setattr(service_app, "scan_git_repo", fake)
    monkeypatch.setattr(service_app, "_resolve_code_source", lambda url, cs: {})
    items, source, _ = service_app._dispatch_scan(
        service_app.ScanRequest(repo_url="git@github.com:some/repo.git")
    )
    assert source == "remote"
    assert calls["url"] == "git@github.com:some/repo.git"
    assert len(items) == 1


def test_dispatch_scan_codeup_without_pat_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import service.app as service_app

    monkeypatch.setattr(service_app, "_resolve_code_source", lambda url, cs: {})
    with pytest.raises(ValidationError, match="access_key"):
        service_app._dispatch_scan(
            service_app.ScanRequest(repo_url="https://codeup.aliyun.com/ns/repo.git")
        )


def test_dispatch_scan_codeup_with_pat_keeps_api_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import service.app as service_app

    calls: dict[str, Any] = {}

    def fake(
        cs: dict[str, Any], url: str, **kw: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        calls["url"] = url
        return ([], {"source_mode": "codeup"})

    monkeypatch.setattr(service_app, "scan_repository", fake)
    monkeypatch.setattr(
        service_app,
        "_resolve_code_source",
        lambda url, cs: {"access_key": "fake-token"},
    )
    _, source, meta = service_app._dispatch_scan(
        service_app.ScanRequest(repo_url="https://codeup.aliyun.com/ns/repo.git")
    )
    assert source == "remote"
    assert meta["source_mode"] == "codeup"
    assert calls["url"] == "https://codeup.aliyun.com/ns/repo.git"
