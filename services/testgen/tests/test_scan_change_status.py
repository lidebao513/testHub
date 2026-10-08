"""快速扫描变更状态单测（engine/remote_scan + codeup_client.compare）。

不触网：
- ``_change_map_from_diffs``：newFile→added、普通修改→modified、deleted 跳过、
  renamed→modified（取 newPath）；
- ``scan_repository``：monkeypatch 假 client，验证逐文件 change_status 标注
  （added/modified/unchanged），且不传 base_ref 时绝不调用 compare；
- ``scan_local``：非 git 目录 + base_ref → best-effort 静默降级为全 unchanged；
- ``_local_git_change_map``：base_ref 为空 → {}；
- ``_looks_like_sha``：commit SHA 形态识别。
"""

from __future__ import annotations

from engine import remote_scan
from engine.codeup_client import _looks_like_sha


# ---------------------------------------------------------------------------
# _change_map_from_diffs：GetCompare diffs → {path: 变更状态}
# ---------------------------------------------------------------------------
def test_change_map_added_modified_deleted_renamed():
    diffs = [
        {"newPath": "a/b_new.py", "oldPath": "a/b_new.py", "newFile": True, "deletedFile": False},
        {"newPath": "a/b_mod.py", "oldPath": "a/b_mod.py", "newFile": False, "deletedFile": False},
        {"newPath": "a/b_del.py", "oldPath": "a/b_del.py", "newFile": False, "deletedFile": True},
        {
            "newPath": "a/b_ren.py",
            "oldPath": "a/b_old.py",
            "newFile": False,
            "deletedFile": False,
            "renamedFile": True,
        },
        "not-a-dict",  # 非法元素应被跳过
    ]
    m = remote_scan._change_map_from_diffs(diffs)
    assert m == {
        "a/b_new.py": remote_scan.CHANGE_ADDED,
        "a/b_mod.py": remote_scan.CHANGE_MODIFIED,
        "a/b_ren.py": remote_scan.CHANGE_MODIFIED,
    }


# ---------------------------------------------------------------------------
# scan_repository：逐文件标注变更状态（假 client，不触网）
# ---------------------------------------------------------------------------
class _FakeNode:
    def __init__(self, path: str, name: str, type_: str = "blob") -> None:
        self.path = path
        self.name = name
        self.type = type_


class _FakeClient:
    def __init__(self, tree, diffs) -> None:
        self._tree = tree
        self._diffs = diffs
        self.compare_calls = 0

    def list_tree(self, repo_id, *, ref="", recursive=True):
        return self._tree

    def get_blob(self, repo_id, file_path, *, ref=""):
        return ""

    def compare(self, repo_id, *, from_ref, to_ref):
        self.compare_calls += 1
        assert from_ref == "master" and to_ref == "dev"
        return self._diffs


def _patch_client(monkeypatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(remote_scan, "client_from_code_source", lambda cs, **kw: fake)
    monkeypatch.setattr(remote_scan, "parse_repo_to_repo_id", lambda url: "123")


def test_scan_repository_annotates_change_status(monkeypatch):
    tree = [
        _FakeNode("pkg/new_mod.py", "new_mod.py"),
        _FakeNode("pkg/mod.py", "mod.py"),
        _FakeNode("pkg/same.py", "same.py"),
    ]
    diffs = [
        {
            "newPath": "pkg/new_mod.py",
            "oldPath": "pkg/new_mod.py",
            "newFile": True,
            "deletedFile": False,
        },
        {"newPath": "pkg/mod.py", "oldPath": "pkg/mod.py", "newFile": False, "deletedFile": False},
    ]
    fake = _FakeClient(tree, diffs)
    _patch_client(monkeypatch, fake)
    items = remote_scan.scan_repository({}, "https://x/repo.git", ref="dev", base_ref="master")
    assert fake.compare_calls == 1
    st = {it["path"]: it["change_status"] for it in items}
    assert st == {
        "pkg/new_mod.py": remote_scan.CHANGE_ADDED,
        "pkg/mod.py": remote_scan.CHANGE_MODIFIED,
        "pkg/same.py": remote_scan.CHANGE_UNCHANGED,
    }


def test_scan_repository_no_base_skips_compare(monkeypatch):
    tree = [_FakeNode("pkg/a.py", "a.py")]
    fake = _FakeClient(tree, [])
    _patch_client(monkeypatch, fake)
    items = remote_scan.scan_repository({}, "https://x/repo.git", ref="", base_ref="")
    assert fake.compare_calls == 0  # 未提供基线绝不调用 compare
    assert all(it["change_status"] == remote_scan.CHANGE_UNCHANGED for it in items)


# ---------------------------------------------------------------------------
# scan_local / _local_git_change_map：best-effort 静默降级
# ---------------------------------------------------------------------------
def test_local_change_map_empty_base(tmp_path):
    assert remote_scan._local_git_change_map(str(tmp_path), "") == {}


def test_scan_local_non_git_dir_all_unchanged(tmp_path):
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    items = remote_scan.scan_local(str(tmp_path), base_ref="master")
    assert [it["path"] for it in items] == ["mod.py"]
    assert items[0]["change_status"] == remote_scan.CHANGE_UNCHANGED


# ---------------------------------------------------------------------------
# _looks_like_sha：commit SHA 形态识别（compare 的 sourceType 推断依据）
# ---------------------------------------------------------------------------
def test_looks_like_sha():
    assert _looks_like_sha("6da8c14b5a9102998148b7ea35f96507d5304f74")
    assert _looks_like_sha("bed720e")
    assert not _looks_like_sha("master")
    assert not _looks_like_sha("dev_branch")
    assert not _looks_like_sha("v1.2.0")
    assert not _looks_like_sha("")
    assert not _looks_like_sha("abc12")  # <7 位
