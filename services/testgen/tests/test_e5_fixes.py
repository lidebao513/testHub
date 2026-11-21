"""E5（P2/P3）修复契约测试：C1/C2 配置可观测、B5 鉴权、B7 选择器、
B9 配置校验+OTP 安全、B11 元素等待、B12 批次号。

纯函数 / 源码字符串断言，无真实浏览器与网络。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.config import load_settings
from core.enums import AuthMode
from core.errors import ConfigError
from engine import auth_scan, pipeline, runtime_ui, ui_executor
from engine.runtime_ui import RuntimeUiOptions, _fetch_otp
from engine.scan import SourceFile


# ---------------------------------------------------------------- C1/C2
def test_public_dict_exposes_mobile_enabled() -> None:
    """C1：面板能读到移动端发现开关。"""
    assert "runtime_ui_mobile_enabled" in load_settings(env={}).public_dict()


def test_single_max_elements_constant() -> None:
    """C2：单页元素上限只有一处定义（不再 200/150 静默覆盖）。"""
    src = Path(runtime_ui.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"^MAX_ELEMENTS_PER_PAGE\s*=", src, re.M)) == 1


# ---------------------------------------------------------------- B12 批次号
def test_batch_id_has_project_prefix_and_long_random() -> None:
    """B12：批次号带项目前缀 + 12 位随机，跨项目不再撞号。"""
    bid1 = pipeline._new_batch_id(project_id=30)
    bid2 = pipeline._new_batch_id(project_id=31)
    assert "p30-" in bid1 and "p31-" in bid2
    assert re.search(r"-[0-9a-f]{12}$", bid1)
    assert bid1 != bid2


def test_batch_id_without_project_still_valid() -> None:
    """默认（无 project_id）仍生成合法批次号（兼容既有调用/测试）。"""
    assert re.match(r"^RUN-", pipeline._new_batch_id())


# ---------------------------------------------------------------- B9 配置校验
@pytest.mark.parametrize("channel", ["firefox", "safari", "chrome-canary"])
def test_invalid_playwright_channel_rejected(channel: str) -> None:
    """B9：浏览器通道白名单，非法值启动期报错。"""
    with pytest.raises(ConfigError) as exc:
        load_settings(env={"PLAYWRIGHT_CHANNEL": channel})
    assert "PLAYWRIGHT_CHANNEL" in str(exc.value)


@pytest.mark.parametrize("channel", ["", "msedge", "chrome"])
def test_valid_playwright_channel_accepted(channel: str) -> None:
    assert load_settings(env={"PLAYWRIGHT_CHANNEL": channel}).playwright_channel == channel


@pytest.mark.parametrize(
    "env",
    [
        {"RUNTIME_BASE_URL": "localhost:3900"},
        {"RUNTIME_LOGIN_URL": "ftp://x"},
    ],
)
def test_base_url_requires_scheme(env: dict[str, str]) -> None:
    """B9：非 http(s) 地址启动期拒绝（运行期才炸太晚）。"""
    with pytest.raises(ConfigError):
        load_settings(env=env)


@pytest.mark.parametrize(
    "env",
    [
        {"DESCRIBE_BUDGET_SECONDS": "0"},
        {"EXPERT_MAX_PAGES": "-1"},
        {"EXPERT_TIME_BUDGET_SEC": "0"},
    ],
)
def test_expert_and_describe_budget_validated(env: dict[str, str]) -> None:
    """B9：此前未校验的数值配置现在快速失败。"""
    with pytest.raises(ConfigError):
        load_settings(env=env)


# ---------------------------------------------------------------- B9 OTP shell=False
def test_otp_refresh_uses_shell_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """B9：OTP 刷新命令 shell=False + shlex.split（.env 污染无法任意执行）。"""
    captured: dict[str, object] = {}

    class _Done:
        stdout = "123456\n"

    import engine.runtime_ui as ru

    def fake_run(argv: object, **kwargs: object) -> _Done:
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        return _Done()

    monkeypatch.setattr(ru.subprocess, "run", fake_run)
    opts = RuntimeUiOptions(login_otp="", login_otp_refresh_cmd="echo 123456")
    assert _fetch_otp(opts, refresh=True) == "123456"
    assert captured["argv"] == ["echo", "123456"]
    assert captured["shell"] is False


def test_otp_refresh_rejects_shell_metacharacters(monkeypatch: pytest.MonkeyPatch) -> None:
    """B9：含管道/重定向/命令替换的复杂命令直接拒绝执行。"""
    called: list[object] = []
    import engine.runtime_ui as ru

    monkeypatch.setattr(ru.subprocess, "run", lambda *a, **k: called.append(a))
    opts = RuntimeUiOptions(login_otp="static", login_otp_refresh_cmd="echo x; rm -rf /tmp/x")
    # 拒绝执行 → 返回空串（由调用方的「刷新失败回退静态 OTP」链路兜底）
    assert _fetch_otp(opts, refresh=True) == ""
    assert called == [], "含 shell 元字符的命令不得执行"


# ---------------------------------------------------------------- B7 选择器
def test_css_path_filters_hash_classes() -> None:
    """B7：CSS-in-JS 哈希 class 过滤函数存在于两个选择器提取模板。"""
    src = Path(runtime_ui.__file__).read_text(encoding="utf-8")
    # 两处 cssPath 都用了 stableClass 过滤
    assert src.count("const stableClass =") == 2
    assert src.count(".filter(stableClass)") == 2


def test_css_path_escapes_tailwind_special_chars() -> None:
    """Tailwind arbitrary-value 类名（如 min-h-[100dvh]）必须 CSS.escape 转义。

    历史缺陷：直接把 class 拼进选择器，Playwright 把 `[100dvh]` 当成属性选择器语法
    抛 Error → 元素断言异常 → 6 条用例假 FAIL。三处 cssPath 都要转义。
    """
    src = Path(runtime_ui.__file__).read_text(encoding="utf-8")
    # 三处 cssPath 的 class 拼接都经过 CSS.escape（2 处元素发现 + 1 处卡片导航触发）
    assert src.count("CSS.escape(c)") == 3, (
        "三处 class→选择器拼接都必须用 CSS.escape 转义 Tailwind 特殊字符类名"
    )


def test_panel_discovery_has_time_budget() -> None:
    """面板/菜单发现必须有时间预算 + 单次点击短超时，防大量导航项拖到像卡死。

    历史现场（2026-11-20，福享 Agent 带正确 OTP）：登录后发现 60 个菜单标签，
    每个点击最坏等 30s → runtime_ui 阶段进度 2 分钟不动，被误判卡死。
    """
    assert runtime_ui.PANEL_DISCOVERY_BUDGET_MS > 0
    assert 0 < runtime_ui.PANEL_CLICK_TIMEOUT_MS < 30000
    src = Path(runtime_ui.__file__).read_text(encoding="utf-8")
    assert "deadline = time.monotonic()" in src, "面板发现缺少总时间预算"
    assert "budget_exhausted" in src, "预算耗尽需可观测"
    # 菜单发现与面板发现都用短超时（各至少 1 处传 click_timeout_ms）
    assert src.count("click_timeout_ms=PANEL_CLICK_TIMEOUT_MS") == 3


# ---------------------------------------------------------------- B11 元素等待
def test_element_check_waits_when_supported() -> None:
    """B11：locator 支持 wait_for 时优先等待可见（SPA 慢页面不假 FAIL）。"""
    waited: list[object] = []

    class _Loc:
        def wait_for(self, **kwargs: object) -> None:
            waited.append(kwargs)

        def count(self) -> int:
            return 1

        @property
        def first(self) -> _Loc:
            return self

        def is_visible(self) -> bool:
            return True

    class _Page:
        def locator(self, selector: str) -> _Loc:
            return _Loc()

    ok, _ = ui_executor._check_element_visible(_Page(), "#btn", timeout_ms=3000)
    assert ok is True
    assert waited and waited[0]["state"] == "visible"
    assert waited[0]["timeout"] == 3000


def test_element_check_falls_back_without_wait_for() -> None:
    """B11：桩/旧版 locator 无 wait_for 时回退快照判定（零回归）。"""

    class _Loc:
        def count(self) -> int:
            return 1

        @property
        def first(self) -> _Loc:
            return self

        def is_visible(self) -> bool:
            return True

    class _Page:
        def locator(self, selector: str) -> _Loc:
            return _Loc()

    assert ui_executor._check_element_visible(_Page(), "#btn", timeout_ms=3000)[0] is True


# ---------------------------------------------------------------- B5 鉴权
def test_norm_strips_dot_slash() -> None:
    """B5：路径归一化去 ./ 前缀，mode_for 精确匹配得上。"""
    assert auth_scan._norm("./app/api.py") == "app/api.py"
    assert auth_scan._norm("app\\api.py") == "app/api.py"


def test_comments_not_counted_as_auth_signal() -> None:
    """B5：注释里的 Authorization/Bearer 不算鉴权接线。"""
    text = "# Authorization: Bearer xxx\n// Bearer token\ncode = 1\n"
    assert auth_scan._strip_comments(text).strip() == "code = 1"
    sf = SourceFile(rel="app/api.py", abspath=Path("app/api.py"), text=text)
    profile = auth_scan.scan_auth({"app/api.py": sf})
    assert profile.mode == AuthMode.ABSENT.value
