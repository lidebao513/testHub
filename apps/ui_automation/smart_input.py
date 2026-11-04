"""#184 统一智能输入框解析器（纯规则、零 LLM 依赖、fail-open）。

解析用户一段混合自由文本（可混写 URL/账号/密码/权限说明/动作/验证点）：
    http://demo.todomvc.com
    账号: admin
    密码: Test@123456
    权限: 只读测试，禁止删除数据
    打开页面后添加三条任务
    验证：剩 0 条 active

输出结构：
    {url, username, password, notes, actions, asserts, confidence, warnings}

解析规则（优先级从高到低）：
1. URL：第一个 http(s):// 开头行优先，否则全文正则；纯域名（demo.x.com[:port][/path]）兜底。
2. 键值对：账号/密码/权限等标签（中英兼容，``[:：=]`` 分隔），移植 aiSitePilot crawl_input.py。
3. 三段语义（#184 扩展）：
   - 「打开/访问/进入 X」且 X 像 URL/域名 → url（若尚未解析出）
   - 「验证/校验/检查/应显示/断言 Y」→ asserts 列表
   - 其余非 URL 非键值从句 → actions 列表

安全约定：本模块只做文本解析；明文密码仅在内存中流转，
接口回显走 ``mask_password``，持久化组装走 ``compose_task_description(include_credentials=False)``。
任何解析异常都降级为 raw + confidence=low，绝不抛错（fail-open）。
"""
from __future__ import annotations

import re
from typing import Any

# http(s)/ftp 链接（移植 aiSitePilot crawl_input.py）
URL_RE = re.compile(r'(?:https?://|ftp://)[^\s,，;；]+', re.IGNORECASE)

# 账号/密码/权限 标签键值对（行级）
KV_RE = re.compile(
    r'^\s*(?:'
    r'(?P<user>账号|用户名|用户|登录名|账号名|登录账号|username|user|account|login)'
    r'|(?P<passw>密码|口令|登录密码|password|pass|pwd)'
    r'|(?P<note>权限|范围|说明|备注|授权|note|notes|remark|desc)'
    r')\s*[:：=]\s*(?P<val>.+?)\s*$',
    re.IGNORECASE,
)

_TAG_USER = 'user'
_TAG_PASS = 'passw'
_TAG_NOTE = 'note'

# 纯域名兜底：demo.todomvc.com[:8080][/path]（不含 scheme）
BARE_DOMAIN_RE = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+'
    r'[a-zA-Z]{2,}(?::\d{1,5})?(?:/[^\s,，;；]*)?$'
)

# 「打开/访问/进入 X」语义（#184）：X 需像 URL/域名才归 url，否则归动作
OPEN_URL_RE = re.compile(
    r'^(?:请|帮我|帮忙)?\s*(?:打开|访问|进入|跳转到|转到|open|visit|goto|go to|navigate to)\s*'
    r'(?P<target>\S+?)(?:[，,。；;！!？?\s].*)?$',
    re.IGNORECASE,
)

# 「验证/校验/检查 Y」语义（#184）：整从句归 asserts
ASSERT_RE = re.compile(
    r'^(?:请|帮我)?\s*(?:最后)?\s*(?:验证|校验|检查|断言|应显示|确认|verify|check|assert|expect)\s*[:：]?\s*(?P<rest>.+)$',
    re.IGNORECASE,
)

# 从句切分：中文逗号/句号/分号 + 英文对应 + 换行
CLAUSE_SPLIT_RE = re.compile(r'[，,。；;！!？?\n]+')

# 动作从句开头可剥离的连接词（剥离后更干净，剥不干净也无害）
_LEADING_FILLERS = ('然后', '再', '接着', '之后', '并', '且', '先', '最后', 'and', 'then', 'also')

_URL_LINE_RE = re.compile(r'^(?:https?|ftp)://', re.IGNORECASE)


def _is_url_token(token: str) -> bool:
    """token 是否为 URL 或裸域名。"""
    token = (token or '').strip().strip('。．.')
    if not token:
        return False
    if _URL_LINE_RE.match(token):
        return True
    return bool(BARE_DOMAIN_RE.match(token))


def _strip_fillers(clause: str) -> str:
    text = (clause or '').strip()
    lowered = text.lower()
    for filler in _LEADING_FILLERS:
        if lowered.startswith(filler):
            return text[len(filler):].lstrip('：:，, ')
    return text


def parse_unified_input(text: str) -> dict[str, Any]:
    """解析整段混合文本 → 结构化任务。永不抛错（fail-open）。"""
    result: dict[str, Any] = {
        'url': '',
        'username': '',
        'password': '',
        'notes': [],
        'actions': [],
        'asserts': [],
        'confidence': 'low',
        'warnings': [],
        'raw': text or '',
    }
    try:
        source = text or ''
        lines = [ln.strip() for ln in source.splitlines() if ln.strip()]

        # 1) URL：优先取 http(s):// 开头行，否则全文正则
        for ln in lines:
            if _URL_LINE_RE.match(ln):
                result['url'] = ln.split()[0]
                break
        if not result['url']:
            m = URL_RE.search(source)
            if m:
                result['url'] = m.group(0)
        if not result['url']:
            # 裸域名兜底：行首第一个 token 像 URL/域名（如 demo.x.com:8080/login 添加任务）
            for ln in lines:
                first_token = ln.split()[0] if ln.split() else ''
                if _is_url_token(first_token):
                    result['url'] = first_token
                    break

        # 2) 行级键值对：账号/密码/权限
        for ln in lines:
            m = KV_RE.match(ln)
            if not m:
                continue
            val = m.group('val').strip()
            if m.group(_TAG_USER) and not result['username']:
                result['username'] = val
            elif m.group(_TAG_PASS) and not result['password']:
                result['password'] = val
            elif m.group(_TAG_NOTE) and val not in result['notes']:
                result['notes'].append(val)

        # 3) 从句级三段语义
        for ln in lines:
            if _URL_LINE_RE.match(ln) or KV_RE.match(ln):
                continue
            if result['url'] and (ln == result['url'] or ln.startswith(result['url'])):
                # 剥离 URL 前缀后继续处理该行剩余部分（如 "demo.x.com 添加任务"）
                ln = ln[len(result['url']):].strip()
                if not ln:
                    continue
            for clause in CLAUSE_SPLIT_RE.split(ln):
                clause = clause.strip()
                if not clause:
                    continue
                _classify_clause(clause, result)

        # 4) 置信度与告警
        if not result['url']:
            result['warnings'].append('未识别到目标 URL')
        if not result['actions'] and not result['asserts']:
            result['warnings'].append('未识别到执行动作或验证点')
        result['confidence'] = (
            'high' if result['url'] and (result['actions'] or result['asserts']) else 'low'
        )
        return result
    except Exception:  # noqa: BLE001 —— fail-open：解析异常降级为原文
        return {
            'url': '',
            'username': '',
            'password': '',
            'notes': [],
            'actions': [],
            'asserts': [],
            'confidence': 'low',
            'warnings': ['解析失败，已保留原文，请人工拆分'],
            'raw': text or '',
        }


def _classify_clause(clause: str, result: dict[str, Any]) -> None:
    """把单个从句归类到 url/actions/asserts（原地更新 result）。"""
    # a) 验证语义 → asserts
    m = ASSERT_RE.match(clause)
    if m:
        rest = m.group('rest').strip()
        if rest and rest not in result['asserts']:
            result['asserts'].append(rest)
        return

    # b) 打开/访问 + URL/域名 → url（仅当尚未解析出）
    m = OPEN_URL_RE.match(clause)
    if m and not result['url'] and _is_url_token(m.group('target')):
        result['url'] = m.group('target').strip('。．.')
        return

    # c) 其余 → 动作
    action = _strip_fillers(clause)
    if action and action not in result['actions']:
        result['actions'].append(action)


def mask_password(password: str) -> str:
    """密码打码（接口回显/日志用）：Tes******56 式。"""
    pwd = password or ''
    if len(pwd) >= 6:
        return f'{pwd[:3]}******{pwd[-2:]}'
    if pwd:
        return '******'
    return ''


def compose_task_description(parsed: dict[str, Any], include_credentials: bool = False) -> str:
    """把解析结果组装为自然语言任务描述。

    include_credentials=False（持久化用）：绝不包含明文密码。
    include_credentials=True（执行线程内传给 Agent 用）：包含账号密码，供登录。
    """
    parts: list[str] = []
    url = (parsed or {}).get('url') or ''
    if url:
        parts.append(f'打开 {url}')
    for action in (parsed or {}).get('actions') or []:
        if action:
            parts.append(action)
    asserts = [a for a in (parsed or {}).get('asserts') or [] if a]
    if asserts:
        parts.append(f'验证：{"；".join(asserts)}')
    username = (parsed or {}).get('username') or ''
    if username:
        parts.append(f'测试账号：{username}')
    if include_credentials:
        password = (parsed or {}).get('password') or ''
        if password:
            parts.append(f'登录密码：{password}')
    for note in (parsed or {}).get('notes') or []:
        if note:
            parts.append(note)
    return '。'.join(p.rstrip('。') for p in parts if p)


def compose_execution_input(parsed: dict[str, Any] | None, raw_text: str | None = None) -> dict[str, Any]:
    """run_adhoc 组装入口：parsed（前端修正后）优先，缺失字段可由 raw_text 服务端补解析。

    返回 {'parsed', 'persisted', 'agent'}：
    - parsed：最终采用的结构化结果
    - persisted：不含明文密码，落 AIExecutionRecord.task_description
    - agent：含凭据，仅传给执行线程内的 Agent
    """
    merged: dict[str, Any] = {k: (v.copy() if isinstance(v, list) else v) for k, v in parse_unified_input(raw_text or '').items()}
    for key in ('url', 'username', 'password'):
        val = ((parsed or {}).get(key) or '').strip()
        if val:
            merged[key] = val
    for key in ('notes', 'actions', 'asserts'):
        vals = [v for v in (parsed or {}).get(key) or [] if str(v).strip()]
        if vals:
            merged[key] = [str(v).strip() for v in vals]
    return {
        'parsed': merged,
        'persisted': compose_task_description(merged, include_credentials=False),
        'agent': compose_task_description(merged, include_credentials=True),
    }
