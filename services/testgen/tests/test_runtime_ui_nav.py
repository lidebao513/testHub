"""runtime_ui 导航发现的 LLM-free 兜底逻辑单测（不依赖浏览器）。

覆盖：危险菜单项识别（避免点击退出登录）、广搜菜单文字包装（异常/非列表降级）。
"""

from engine.runtime_ui import _harvest_nav_labels, _is_danger_label


class _FakePage:
    def __init__(self, ret):
        self._ret = ret

    def evaluate(self, js):
        return self._ret


class _BoomPage:
    def evaluate(self, js):
        raise RuntimeError("no browser")


def test_is_danger_label_detects_logout_variants():
    assert _is_danger_label("退出登录")
    assert _is_danger_label("退出")
    assert _is_danger_label("注销")
    assert _is_danger_label("Logout")
    assert _is_danger_label("sign out")


def test_is_danger_label_accepts_normal_menus():
    assert not _is_danger_label("用户管理")
    assert not _is_danger_label("仪表盘")
    assert not _is_danger_label("")
    assert not _is_danger_label(None)


def test_harvest_nav_labels_ok():
    p = _FakePage(["仪表盘", "用户管理", "设置"])
    assert _harvest_nav_labels(p) == ["仪表盘", "用户管理", "设置"]


def test_harvest_nav_labels_non_list_degrades():
    assert _harvest_nav_labels(_FakePage(None)) == []
    assert _harvest_nav_labels(_FakePage("not-a-list")) == []


def test_harvest_nav_labels_raises_degrades():
    assert _harvest_nav_labels(_BoomPage()) == []
