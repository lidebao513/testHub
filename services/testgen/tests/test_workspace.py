"""工作区测试：路径逃逸阻断、只读加固与**实测**可写性、状态查询。"""

import pytest

from core.errors import WorkspaceError
from workspace import readonly
from workspace.manager import WorkspaceManager, _safe_name


@pytest.fixture()
def wm(tmp_path):
    return WorkspaceManager(tmp_path / "ws")


def test_safe_name_rejects_traversal():
    for bad in ("../evil", "a/b", "a\\b", "..", ""):
        with pytest.raises(WorkspaceError):
            _safe_name(bad)


def test_resolve_inside_root(wm):
    path = wm.resolve("demo", create=True)
    assert path.exists()
    assert str(wm.root) in str(path)


def test_assert_inside_rejects_outside(wm):
    with pytest.raises(WorkspaceError):
        wm.assert_inside(wm.root.parent / "elsewhere")


def test_prepare_then_lock_cycle(wm):
    path = wm.prepare("demo")
    (path / "files").mkdir()
    (path / "files" / "a.py").write_text("x = 1\n", encoding="utf-8")

    result = wm.lock("demo", verify=True)
    assert result["path"]
    assert result["mode_bit_readonly"] is True
    assert "enforced" in result and "probe_detail" in result

    wm.unlock("demo")
    assert readonly.probe_writable(path).writable is True


def test_status_reports_state(wm):
    wm.prepare("demo")
    status = wm.status("demo")
    assert status.exists is True
    assert status.is_git is False
    payload = status.to_dict()
    assert set(payload) >= {"name", "path", "exists", "readonly", "writable_probe"}


def test_status_for_missing_workspace(wm):
    status = wm.status("nope")
    assert status.exists is False
    assert status.detail


def test_list_all_returns_created_workspaces(wm):
    wm.prepare("one")
    wm.prepare("two")
    names = {s.name for s in wm.list_all()}
    assert {"one", "two"} <= names


def test_readonly_probe_is_honest(tmp_path):
    """只读加固的验收口径：不假设生效，实测为准。"""
    target = tmp_path / "code"
    target.mkdir()
    (target / "x.py").write_text("1\n", encoding="utf-8")
    result = readonly.harden(target, verify=True)
    assert result["writable_probe"] is (not result["enforced"])
    readonly.release(target)
    assert readonly.probe_writable(target).writable is True


def test_register_external_dir(wm, tmp_path):
    ext = tmp_path / "external"
    ext.mkdir()
    assert wm.register_external("ext", str(ext)) == ext.resolve()
    with pytest.raises(WorkspaceError):
        wm.register_external("missing", str(tmp_path / "not-there"))
