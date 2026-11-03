"""#181 三层断言引擎：AI 生成断言 / 规则断言 / 会话级断言。

背景：此前"任务是否成功"完全由 AI 自称（mark_task_complete）决定，执行器只看
planned_tasks 里有没有 failed 状态——AI 声称完成即通过，没有独立证据。
本模块在执行结果之上叠加独立的质量信号：

- L1 AI 生成断言：执行前由 LLM 为每个子任务生成可验证的完成判据
  （url_contains / page_contains），执行后逐一核对。生成失败静默降级为空。
- L2 规则断言：纯规则从任务描述提取硬判据（目标 URL、应显示的文案），
  不依赖模型，任何情况下都可用。
- L3 会话级断言：#177 首步快照 vs 终态快照对比（从未离开空白页、
  URL/标题完全未变化、终态采集失败）。

**质量信号，非阻断**：断言结果只追加到执行日志与 assertion_report 字段，
不改变 passed/failed 判定——它是"AI 自称完成"之外的独立证据，
由人（或后续回流逻辑）决定如何消费。

设计约束（与项目惯例一致）：
- 所有采集/评估路径 fail-open：任何异常都降级为 unknown/error 条目，绝不影响主执行。
- 纯函数为主，方便单测；唯一 async 入口是 L1 生成（有界超时）。
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 断言类型常量
URL_HOST = "url_host"
URL_CONTAINS = "url_contains"
PAGE_CONTAINS = "page_contains"
NO_NAVIGATION = "no_navigation"
SESSION_UNCHANGED = "session_unchanged"
SESSION_ERROR = "session_error"

# 结果三态 + error
PASSED = "passed"
FAILED = "failed"
UNKNOWN = "unknown"
ERROR = "error"

_L1_TIMEOUT = 25.0
_MAX_PAGE_TEXT = 20000
_MAX_VALUE_LEN = 120

# L2 规则：任务描述里的动词 + 目标文案（中文引号/冒号后的短文案）
_KEYWORD_PATTERNS = [
    re.compile(
        r'(?:应|需要|须|必须|然后)(?:显示|出现|提示|看到|包含)["\u201c\u2018]?([^，。；、"\u201d\u2019]{2,40})'
    ),
    re.compile(r'(?:显示|提示|出现)(?:成功|失败)?["\u201c]([^"\u201d]{2,40})["\u201d]'),
    re.compile(r'["\u201c]([^"\u201d]{2,40})["\u201d](?:的?(?:提示|文案|信息|字样))'),
]
_URL_PATTERN = re.compile(r'https?://[^\s\uff0c\u3002\uff1b\u3001\uff09\u201d"\']+')


@dataclass
class AssertionItem:
    """单条断言及其评估结果。"""

    level: str  # 'ai' / 'rule' / 'session'
    type: str
    value: str
    status: str = UNKNOWN  # passed / failed / unknown / error
    detail: str = ""
    task_id: int | None = None  # L1 断言关联的子任务

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "type": self.type,
            "value": self.value,
            "status": self.status,
            "detail": self.detail,
            "task_id": self.task_id,
        }


@dataclass
class AssertionReport:
    """三层断言汇总。quality_signal 是给执行记录消费方看的一眼结论。"""

    items: list = field(default_factory=list)

    @property
    def quality_signal(self) -> str:
        statuses = {i.status for i in self.items}
        if FAILED in statuses or ERROR in statuses:
            return "fail"
        if any(i.status in (UNKNOWN,) for i in self.items):
            return "warn"
        if self.items:
            return "pass"
        return "none"

    @property
    def counts(self) -> dict:
        out = {}
        for i in self.items:
            out[i.status] = out.get(i.status, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "quality_signal": self.quality_signal,
            "counts": self.counts,
            "items": [i.to_dict() for i in self.items],
        }


# ---------------------------------------------------------------------------
# L2 规则断言：从任务描述提取
# ---------------------------------------------------------------------------


def extract_rule_assertions(
    task_description: str, planned_tasks: list | None = None
) -> list:
    """从任务描述（+子任务描述）用纯规则提取硬判据。永不抛异常。"""
    items = []
    try:
        texts = [task_description or ""]
        for t in planned_tasks or []:
            texts.append(str(t.get("description") or ""))
        blob = "\n".join(texts)

        # URL 判据：取第一个明确出现的 URL（host 断言）
        m = _URL_PATTERN.search(blob)
        if m:
            url = m.group(0).rstrip(".,;:)")
            host = urlparse(url).hostname
            if host:
                items.append(AssertionItem(level="rule", type=URL_HOST, value=host))

        # 关键词判据：动词模式命中的文案（去重，最多 3 条，防误报淹没信号）
        seen = set()
        for pat in _KEYWORD_PATTERNS:
            for m in pat.finditer(blob):
                value = m.group(1).strip().strip('"\u201c\u201d\u2018\u2019')
                if value and len(value) >= 2 and value not in seen:
                    seen.add(value)
                    items.append(
                        AssertionItem(
                            level="rule",
                            type=PAGE_CONTAINS,
                            value=value[:_MAX_VALUE_LEN],
                        )
                    )
            if len(seen) >= 3:
                break
    except Exception as e:  # 规则提取绝不影响主流程
        logger.warning(f"规则断言提取失败（忽略）: {e}")
    return items


# ---------------------------------------------------------------------------
# 终态采集与文本提取
# ---------------------------------------------------------------------------


def _node_text(node) -> str:
    """从 EnhancedDOMTreeNode 提取展示文本（value + 常用 attributes）。"""
    parts = []
    try:
        if node.node_value:
            parts.append(str(node.node_value))
        attrs = getattr(node, "attributes", None) or {}
        for key in ("value", "title", "placeholder", "aria-label", "alt"):
            v = attrs.get(key)
            if v:
                parts.append(str(v))
    except Exception:
        pass
    return " ".join(parts)


def page_text_from_state(state) -> str:
    """从 BrowserStateSummary 提取页面可见文本（供 page_contains 评估）。失败返回空串。"""
    try:
        dom_state = getattr(state, "dom_state", None)
        selector_map = getattr(dom_state, "selector_map", None) or {}
        chunks = []
        for node in selector_map.values():
            text = _node_text(node)
            if text.strip():
                chunks.append(text.strip())
            if sum(len(c) for c in chunks) > _MAX_PAGE_TEXT:
                break
        return "\n".join(chunks)[:_MAX_PAGE_TEXT]
    except Exception:
        return ""


def _is_blank_url(url: str) -> bool:
    normalized = (url or "").strip().lower()
    return not normalized or normalized == "about:blank"


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


def evaluate_url_assertions(items: list, final_url: str) -> None:
    """就地评估 url_host / url_contains 类断言。final_url 缺失 → unknown。"""
    try:
        host = urlparse(final_url or "").hostname or ""
        for item in items:
            if item.type == URL_HOST:
                if not host:
                    item.status = UNKNOWN
                    item.detail = "终态 URL 不可得"
                elif host == item.value or host.endswith("." + item.value):
                    item.status = PASSED
                    item.detail = f"final_url={final_url}"
                else:
                    item.status = FAILED
                    item.detail = f"预期 host={item.value}，实际 final_url={final_url}"
            elif item.type == URL_CONTAINS:
                if not final_url:
                    item.status = UNKNOWN
                    item.detail = "终态 URL 不可得"
                elif item.value in final_url:
                    item.status = PASSED
                    item.detail = f"final_url={final_url}"
                else:
                    item.status = FAILED
                    item.detail = (
                        f'预期 URL 包含"{item.value}"，实际 final_url={final_url}'
                    )
    except Exception as e:
        logger.warning(f"URL 断言评估失败（忽略）: {e}")


def evaluate_page_assertions(items: list, final_text: str) -> None:
    """就地评估 page_contains 断言。无文本 → unknown。"""
    try:
        for item in items:
            if item.type != PAGE_CONTAINS:
                continue
            if not final_text:
                item.status = UNKNOWN
                item.detail = "终态页面文本不可得"
            elif item.value in final_text:
                item.status = PASSED
            else:
                item.status = FAILED
                item.detail = f'终态页面文本中未找到"{item.value}"'
    except Exception as e:
        logger.warning(f"页面断言评估失败（忽略）: {e}")


def evaluate_session_assertions(
    pre_state: dict | None, final_state: dict | None, had_actions: bool = True
) -> list:
    """L3：首步快照 vs 终态快照。pre/final 形如 {'url': ..., 'title': ...}。"""
    items = []
    try:
        if final_state is None:
            items.append(
                AssertionItem(
                    level="session",
                    type=SESSION_ERROR,
                    value="terminal_state",
                    status=ERROR,
                    detail="终态快照采集失败",
                )
            )
            return items

        pre_url = str((pre_state or {}).get("url") or "")
        final_url = str(final_state.get("url") or "")

        if _is_blank_url(final_url) and had_actions:
            items.append(
                AssertionItem(
                    level="session",
                    type=NO_NAVIGATION,
                    value="about:blank",
                    status=FAILED,
                    detail="任务执行了动作，但终态仍在空白页（浏览器可能未成功启动或导航全部失败）",
                )
            )
        elif (
            not _is_blank_url(pre_url)
            and pre_url == final_url
            and str((pre_state or {}).get("title") or "")
            == str(final_state.get("title") or "")
            and had_actions
        ):
            items.append(
                AssertionItem(
                    level="session",
                    type=SESSION_UNCHANGED,
                    value=final_url,
                    status=UNKNOWN,
                    detail="终态 URL 与标题和首步快照完全一致（单页操作属正常，若任务含导航则可疑）",
                )
            )
        else:
            items.append(
                AssertionItem(
                    level="session",
                    type="session_alive",
                    value=final_url or "about:blank",
                    status=PASSED,
                    detail="会话终态正常",
                )
            )
    except Exception as e:
        logger.warning(f"会话断言评估失败（忽略）: {e}")
        items.append(
            AssertionItem(
                level="session",
                type=SESSION_ERROR,
                value="evaluate",
                status=ERROR,
                detail=str(e),
            )
        )
    return items


# ---------------------------------------------------------------------------
# L1 AI 生成断言
# ---------------------------------------------------------------------------

_L1_PROMPT = """你是测试断言生成器。根据测试任务与其子任务，为每个子任务生成一条可自动核验的完成判据。
只输出 JSON 数组，不要输出任何其他文字。每条形如：
{{"task_id": 1, "type": "url_contains|page_contains", "value": "..."}}
规则：
- type=url_contains：子任务完成后浏览器 URL 应包含的片段（如路径、页面标识）。
- type=page_contains：完成后页面上应出现的明确文案（优先任务里写明的成功提示/关键词）。
- 无法给出可靠判据的子任务直接跳过，宁缺毋滥。
- value 必须是字面量，不要变量或正则。

任务描述：
{task}

子任务：
{tasks}
"""


async def generate_ai_assertions(
    llm, task_description: str, planned_tasks: list | None, timeout: float = _L1_TIMEOUT
) -> list:
    """L1：一次轻量 LLM 调用生成断言。任何失败都返回空列表（非阻断）。"""
    if llm is None or not planned_tasks:
        return []
    try:
        tasks_text = "\n".join(
            f"{t.get('id')}. {t.get('description')}"
            for t in planned_tasks
            if t.get("id")
        )
        prompt = _L1_PROMPT.format(
            task=(task_description or "")[:2000], tasks=tasks_text
        )

        async def _call():
            resp = await llm.ainvoke(prompt)
            content = getattr(resp, "content", resp)
            if isinstance(content, list):  # 某些 provider 返回消息块列表
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            return str(content)

        raw = await asyncio.wait_for(_call(), timeout=timeout)
        return parse_ai_assertions(raw)
    except Exception as e:
        logger.warning(f"AI 断言生成失败（跳过，不影响执行）: {e}")
        return []


def parse_ai_assertions(raw: str) -> list:
    """解析 L1 输出 JSON。只接受白名单类型与短 value，脏数据一律丢弃。"""
    items = []
    try:
        text = (raw or "").strip()
        # 容忍模型包一层 ```json ... ```
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        data = json.loads(text[start : end + 1])
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            typ = str(entry.get("type") or "")
            value = str(entry.get("value") or "").strip()
            if typ not in (URL_CONTAINS, PAGE_CONTAINS) or not value:
                continue
            try:
                task_id = int(entry.get("task_id"))
            except (TypeError, ValueError):
                task_id = None
            items.append(
                AssertionItem(
                    level="ai", type=typ, value=value[:_MAX_VALUE_LEN], task_id=task_id
                )
            )
    except Exception as e:
        logger.warning(f"AI 断言解析失败（丢弃）: {e}")
    return items


# ---------------------------------------------------------------------------
# 汇总与渲染
# ---------------------------------------------------------------------------


def build_assertion_report(
    task_description: str,
    planned_tasks: list,
    pre_state: dict | None,
    final_state: dict | None,
    final_text: str,
    ai_items: list | None = None,
    had_actions: bool = True,
) -> AssertionReport:
    """汇总三层断言并评估，返回 AssertionReport。永不抛异常。"""
    report = AssertionReport()
    try:
        report.items.extend(ai_items or [])
        report.items.extend(extract_rule_assertions(task_description, planned_tasks))
        report.items.extend(
            evaluate_session_assertions(pre_state, final_state, had_actions)
        )

        evaluate_url_assertions(
            [i for i in report.items if i.type in (URL_HOST, URL_CONTAINS)],
            str((final_state or {}).get("url") or ""),
        )
        evaluate_page_assertions(
            [i for i in report.items if i.type == PAGE_CONTAINS], final_text or ""
        )
    except Exception as e:
        logger.warning(f"断言汇总失败（忽略）: {e}")
    return report


def render_assertion_block(report: AssertionReport) -> str:
    """渲染日志块（追加到执行日志，给人看）。"""
    try:
        lines = [f"[断言] 质量信号: {report.quality_signal}  明细: {report.counts}"]
        for i in report.items:
            tag = {"ai": "AI断言", "rule": "规则", "session": "会话"}.get(
                i.level, i.level
            )
            line = f'  [{tag}] {i.type}: "{i.value[:60]}" → {i.status}'
            if i.detail:
                line += f"（{i.detail[:120]}）"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""
