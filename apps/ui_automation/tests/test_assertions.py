"""#181 三层断言引擎的单元测试。

覆盖：L2 规则提取、L1 输出解析（白名单/容错）、URL 与页面断言评估边界、
L3 会话级对比（空白页/未变化/正常/终态缺失）、quality_signal 汇总、
日志渲染、AI 生成降级（异常/超时）、ai_base 侧开关语义。
"""

import asyncio
import types

import pytest

from apps.ui_automation import assertions as A
from apps.ui_automation import ai_base


# ---------------------------------------------------------------------------
# L2 规则断言提取
# ---------------------------------------------------------------------------


class TestExtractRuleAssertions:
    def test_url_host_extracted(self):
        items = A.extract_rule_assertions("打开 https://admin.example.com/login 并登录")
        hosts = [i for i in items if i.type == A.URL_HOST]
        assert len(hosts) == 1
        assert hosts[0].value == "admin.example.com"

    def test_keyword_should_show(self):
        items = A.extract_rule_assertions('提交表单后应显示"保存成功"')
        pages = [i for i in items if i.type == A.PAGE_CONTAINS]
        assert any(i.value == "保存成功" for i in pages)

    def test_keyword_quoted_label(self):
        items = A.extract_rule_assertions('页面出现"登录成功"提示')
        pages = [i for i in items if i.type == A.PAGE_CONTAINS]
        assert any(i.value == "登录成功" for i in pages)

    def test_no_url_no_keyword(self):
        assert A.extract_rule_assertions("随便看看这个网站") == []

    def test_duplicate_keywords_deduped(self):
        items = A.extract_rule_assertions(
            '应显示"保存成功"，然后确认出现"保存成功"提示'
        )
        values = [i.value for i in items if i.type == A.PAGE_CONTAINS]
        assert values.count("保存成功") == 1

    def test_never_raises_on_garbage(self):
        assert A.extract_rule_assertions("") == []
        assert A.extract_rule_assertions(None, [{"description": None}]) == []

    def test_planned_tasks_included(self):
        items = A.extract_rule_assertions(
            "主任务", [{"id": 1, "description": '在 https://a.test.com 上应显示"欢迎"'}]
        )
        assert any(i.type == A.URL_HOST and i.value == "a.test.com" for i in items)
        assert any(i.type == A.PAGE_CONTAINS and i.value == "欢迎" for i in items)


# ---------------------------------------------------------------------------
# L1 AI 断言解析
# ---------------------------------------------------------------------------


class TestParseAiAssertions:
    def test_plain_json(self):
        raw = '[{"task_id": 1, "type": "page_contains", "value": "保存成功"}]'
        items = A.parse_ai_assertions(raw)
        assert len(items) == 1
        assert items[0].level == "ai"
        assert items[0].task_id == 1

    def test_json_in_code_fence(self):
        raw = '```json\n[{"task_id": 2, "type": "url_contains", "value": "/dashboard"}]\n```'
        items = A.parse_ai_assertions(raw)
        assert len(items) == 1
        assert items[0].type == A.URL_CONTAINS

    def test_non_whitelisted_type_dropped(self):
        raw = '[{"task_id": 1, "type": "element_exists", "value": "#btn"}]'
        assert A.parse_ai_assertions(raw) == []

    def test_empty_value_dropped(self):
        raw = '[{"task_id": 1, "type": "page_contains", "value": ""}]'
        assert A.parse_ai_assertions(raw) == []

    def test_garbage_returns_empty(self):
        assert A.parse_ai_assertions("我不是 JSON") == []
        assert A.parse_ai_assertions("") == []
        assert A.parse_ai_assertions(None) == []

    def test_bad_task_id_becomes_none(self):
        raw = '[{"task_id": "x", "type": "page_contains", "value": "OK"}]'
        items = A.parse_ai_assertions(raw)
        assert items[0].task_id is None


class TestGenerateAiAssertions:
    def test_normal_generation(self):
        class FakeLLM:
            async def ainvoke(self, prompt):
                return types.SimpleNamespace(
                    content='[{"task_id": 1, "type": "page_contains", "value": "登录成功"}]'
                )

        items = asyncio.run(
            A.generate_ai_assertions(
                FakeLLM(), "登录", [{"id": 1, "description": "输入密码"}]
            )
        )
        assert len(items) == 1

    def test_llm_none_returns_empty(self):
        assert asyncio.run(A.generate_ai_assertions(None, "x", [{"id": 1}])) == []

    def test_no_planned_tasks_returns_empty(self):
        class FakeLLM:
            async def ainvoke(self, prompt):  # pragma: no cover - 不应被调用
                raise AssertionError("不应调用 LLM")

        assert asyncio.run(A.generate_ai_assertions(FakeLLM(), "x", [])) == []

    def test_llm_exception_degrades_silently(self):
        class FakeLLM:
            async def ainvoke(self, prompt):
                raise RuntimeError("boom")

        assert asyncio.run(A.generate_ai_assertions(FakeLLM(), "x", [{"id": 1}])) == []

    def test_timeout_degrades_silently(self):
        class FakeLLM:
            async def ainvoke(self, prompt):
                await asyncio.sleep(10)

        assert (
            asyncio.run(
                A.generate_ai_assertions(FakeLLM(), "x", [{"id": 1}], timeout=0.1)
            )
            == []
        )

    def test_content_block_list_handled(self):
        class FakeLLM:
            async def ainvoke(self, prompt):
                return types.SimpleNamespace(
                    content=[
                        {
                            "text": '[{"task_id": 1, "type": "page_contains", "value": "OK"}]'
                        }
                    ]
                )

        items = asyncio.run(A.generate_ai_assertions(FakeLLM(), "x", [{"id": 1}]))
        assert len(items) == 1


# ---------------------------------------------------------------------------
# 评估：URL / 页面 / 会话
# ---------------------------------------------------------------------------


def _rule(typ, value):
    return A.AssertionItem(level="rule", type=typ, value=value)


class TestEvaluateUrlAssertions:
    def test_host_passed(self):
        item = _rule(A.URL_HOST, "example.com")
        A.evaluate_url_assertions([item], "https://api.example.com/login")
        assert item.status == A.PASSED

    def test_host_subdomain_passes(self):
        item = _rule(A.URL_HOST, "example.com")
        A.evaluate_url_assertions([item], "https://www.example.com/")
        assert item.status == A.PASSED

    def test_host_failed(self):
        item = _rule(A.URL_HOST, "example.com")
        A.evaluate_url_assertions([item], "https://other.org/")
        assert item.status == A.FAILED

    def test_no_final_url_unknown(self):
        item = _rule(A.URL_HOST, "example.com")
        A.evaluate_url_assertions([item], "")
        assert item.status == A.UNKNOWN

    def test_url_contains(self):
        item = _rule(A.URL_CONTAINS, "/dashboard")
        A.evaluate_url_assertions([item], "https://x.com/dashboard?tab=1")
        assert item.status == A.PASSED
        item2 = _rule(A.URL_CONTAINS, "/settings")
        A.evaluate_url_assertions([item2], "https://x.com/dashboard")
        assert item2.status == A.FAILED


class TestEvaluatePageAssertions:
    def test_contains_passed_and_failed(self):
        passed = _rule(A.PAGE_CONTAINS, "保存成功")
        failed = _rule(A.PAGE_CONTAINS, "余额不足")
        A.evaluate_page_assertions([passed, failed], "操作完成，保存成功 已写入")
        assert passed.status == A.PASSED
        assert failed.status == A.FAILED

    def test_no_text_unknown(self):
        item = _rule(A.PAGE_CONTAINS, "保存成功")
        A.evaluate_page_assertions([item], "")
        assert item.status == A.UNKNOWN


class TestEvaluateSessionAssertions:
    def test_blank_terminal_after_actions_fails(self):
        items = A.evaluate_session_assertions(
            {"url": "about:blank", "title": ""},
            {"url": "about:blank", "title": ""},
            had_actions=True,
        )
        assert any(i.type == A.NO_NAVIGATION and i.status == A.FAILED for i in items)

    def test_unchanged_url_warns_unknown(self):
        items = A.evaluate_session_assertions(
            {"url": "https://x.com/list", "title": "列表"},
            {"url": "https://x.com/list", "title": "列表"},
            had_actions=True,
        )
        assert any(
            i.type == A.SESSION_UNCHANGED and i.status == A.UNKNOWN for i in items
        )

    def test_normal_navigation_passes(self):
        items = A.evaluate_session_assertions(
            {"url": "https://x.com/list", "title": "列表"},
            {"url": "https://x.com/detail/1", "title": "详情"},
            had_actions=True,
        )
        assert any(i.type == "session_alive" and i.status == A.PASSED for i in items)

    def test_missing_terminal_state_is_error(self):
        items = A.evaluate_session_assertions({"url": "https://x.com"}, None)
        assert any(i.type == A.SESSION_ERROR and i.status == A.ERROR for i in items)

    def test_no_actions_relaxes_blank_check(self):
        items = A.evaluate_session_assertions(
            {"url": "about:blank", "title": ""},
            {"url": "about:blank", "title": ""},
            had_actions=False,
        )
        assert not any(i.type == A.NO_NAVIGATION for i in items)


# ---------------------------------------------------------------------------
# 汇总、信号与渲染
# ---------------------------------------------------------------------------


class TestReportAndSignal:
    def test_signal_fail_on_failed(self):
        report = A.AssertionReport(items=[_rule(A.PAGE_CONTAINS, "不存在")])
        A.evaluate_page_assertions(report.items, "页面文本")
        assert report.quality_signal == "fail"

    def test_signal_warn_on_unknown(self):
        report = A.AssertionReport(items=[_rule(A.URL_HOST, "x.com")])
        A.evaluate_url_assertions(report.items, "")
        assert report.quality_signal == "warn"

    def test_signal_pass_when_all_passed(self):
        report = A.AssertionReport(
            items=[
                _rule(A.URL_HOST, "x.com"),
                A.AssertionItem(
                    level="session", type="session_alive", value="u", status=A.PASSED
                ),
            ]
        )
        A.evaluate_url_assertions(report.items, "https://x.com/")
        assert report.quality_signal == "pass"

    def test_signal_none_when_empty(self):
        assert A.AssertionReport().quality_signal == "none"

    def test_build_report_end_to_end(self):
        pre = {"url": "about:blank", "title": ""}
        final = {"url": "https://x.com/list", "title": "列表"}
        ai = A.parse_ai_assertions(
            '[{"task_id": 1, "type": "page_contains", "value": "登录成功"}]'
        )
        report = A.build_assertion_report(
            '打开 https://x.com 后应显示"登录成功"',
            [],
            pre,
            final,
            "欢迎，登录成功",
            ai,
        )
        d = report.to_dict()
        assert d["quality_signal"] == "pass"
        assert any(i["level"] == "ai" for i in d["items"])
        assert any(i["level"] == "rule" for i in d["items"])
        assert any(i["level"] == "session" for i in d["items"])

    def test_build_report_never_raises(self):
        report = A.build_assertion_report(None, None, None, None, None, None)
        assert isinstance(report, A.AssertionReport)

    def test_render_block(self):
        item = _rule(A.PAGE_CONTAINS, "保存成功")
        A.evaluate_page_assertions([item], "")
        report = A.AssertionReport(items=[item])
        block = A.render_assertion_block(report)
        assert block.startswith("[断言]")
        assert "规则" in block
        assert A.UNKNOWN in block


# ---------------------------------------------------------------------------
# ai_base 侧接线
# ---------------------------------------------------------------------------


class TestAiBaseWiring:
    def test_switch_default_on(self, monkeypatch):
        monkeypatch.delenv("UI_ASSERTIONS", raising=False)
        assert ai_base._ui_assertions_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
    def test_switch_explicit_off(self, monkeypatch, raw):
        monkeypatch.setenv("UI_ASSERTIONS", raw)
        assert ai_base._ui_assertions_enabled() is False

    def test_agent_wiring_exists(self):
        # run_task 末尾把报告写入 agent._assertion_report，views.apply_assertion_report 消费
        assert hasattr(ai_base.BaseBrowserAgent, "_capture_final_state")
        src = __import__("inspect").getsource(ai_base.BaseBrowserAgent.run_task)
        assert "_assertion_report" in src
        assert "build_assertion_report" in src
