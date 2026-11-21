"""runtime_ui 导航发现的 LLM-free 兜底逻辑单测（不依赖浏览器）。

覆盖：危险菜单项识别（避免点击退出登录）、广搜菜单文字包装（异常/非列表降级）。
"""

from engine import runtime_ui
from engine.runtime_ui import (
    RuntimeUiOptions,
    RuntimeUiResult,
    _harvest_nav_labels,
    _is_danger_label,
)


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


def test_discover_menu_routes_filters_danger_labels(monkeypatch):
    """E2/A3 回归：发现路径必须过滤危险菜单项，不能只靠 `_is_danger_label` 纯函数存在。"""
    seen: dict[str, list[str]] = {}

    def fake_nav_labels(_page):
        return ["仪表盘", "退出登录", "用户管理", "Logout", "设置"]

    def fake_probe(probe):
        seen["labels"] = list(probe.labels)
        return ["/dashboard", "/users"]

    monkeypatch.setattr(runtime_ui, "_nav_labels", fake_nav_labels)
    monkeypatch.setattr(runtime_ui, "_wait_nav_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime_ui, "_safe_url", lambda _p: "http://t/home")
    monkeypatch.setattr(runtime_ui, "_route_of", lambda _p: "/home")
    monkeypatch.setattr(runtime_ui, "_probe_menu_routes", fake_probe)
    monkeypatch.setattr(runtime_ui, "_return_to", lambda *_a, **_k: True)

    opts = RuntimeUiOptions(base_url="http://t", max_pages=10, discover_by_menu=True)
    result = RuntimeUiResult()
    routes = runtime_ui._discover_menu_routes(object(), object(), opts, result)

    assert routes == ["/dashboard", "/users"]
    assert seen["labels"] == ["仪表盘", "用户管理", "设置"], (
        f"危险项未从发现路径过滤：{seen['labels']}"
    )


def test_discover_menu_routes_extra_labels_also_filtered(monkeypatch):
    """E2/A3：专家反推并入的 extra_labels 同样要走危险过滤。"""
    seen: dict[str, list[str]] = {}

    def fake_probe(probe):
        seen["labels"] = list(probe.labels)
        return []

    monkeypatch.setattr(runtime_ui, "_nav_labels", lambda _p: ["仪表盘"])
    monkeypatch.setattr(runtime_ui, "_wait_nav_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime_ui, "_safe_url", lambda _p: "http://t/home")
    monkeypatch.setattr(runtime_ui, "_route_of", lambda _p: "/home")
    monkeypatch.setattr(runtime_ui, "_probe_menu_routes", fake_probe)
    monkeypatch.setattr(runtime_ui, "_return_to", lambda *_a, **_k: True)

    opts = RuntimeUiOptions(base_url="http://t", max_pages=10, discover_by_menu=True)
    runtime_ui._discover_menu_routes(
        object(), object(), opts, RuntimeUiResult(), extra_labels=["注销", "报表"]
    )

    assert seen["labels"] == ["仪表盘", "报表"], f"extra_labels 危险项未过滤：{seen['labels']}"
