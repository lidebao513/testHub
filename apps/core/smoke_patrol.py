"""#182 站点冒烟巡检：配置加载、单项结论、汇总与报告渲染（纯逻辑，供命令与单测复用）。

设计（docs/browser_use_enhancement_prompts_2026-11-03.md §4 P1-1）：
- 巡检项 = 自然语言任务列表，存 data/smoke_tasks.json；
- 每项用现有 browser-use Agent 执行（复用 #180 会话隔离、#181 三层断言）；
- 汇总为巡检报告（JSON + Markdown），人只看一眼结论。

fail-open 约定：配置缺失/单项执行异常都不拖垮整轮巡检，错误进报告。
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = 'data/smoke_tasks.json'
DEFAULT_OUT_DIR = 'data/smoke_patrol_reports'

# 单项结论
ITEM_PASSED = 'passed'
ITEM_FAILED = 'failed'
ITEM_ERROR = 'error'


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_smoke_config(path) -> list:
    """读取巡检配置（[{name, task, enabled}]）。缺失/损坏返回空列表并告警，不抛异常。"""
    try:
        p = Path(path)
        if not p.exists():
            logger.warning('巡检配置不存在: %s（跳过巡检）', p)
            return []
        data = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(data, list):
            logger.warning('巡检配置须为 JSON 数组: %s（跳过巡检）', p)
            return []
        items = []
        for i, entry in enumerate(data):
            if not isinstance(entry, dict):
                continue
            task = str(entry.get('task') or '').strip()
            if not task:
                continue  # 没有任务描述的条目无意义，直接跳过
            items.append({
                'name': str(entry.get('name') or f'巡检项{i + 1}'),
                'task': task,
                'enabled': entry.get('enabled', True) is not False,
            })
        return [it for it in items if it['enabled']]
    except Exception as e:
        logger.warning('巡检配置读取失败（跳过巡检）: %s', e)
        return []


# ---------------------------------------------------------------------------
# 单项结论与汇总
# ---------------------------------------------------------------------------

def evaluate_patrol_item(name: str, task: str, *, status: str, duration: float,
                         assertion_report: dict | None, log_tail: str) -> dict:
    """把一次执行的结果归一为巡检项结论。

    status 口径与执行器一致（passed/failed/stopped）；执行抛异常时传 error。
    断言质量信号（#181）作为附加证据：fail 会把结论压成 failed（可见但不推翻
    执行器判定的语义保持一致——信号与状态都指向问题时才标失败）。
    """
    item = {
        'name': name,
        'task': task[:200],
        'status': status,
        'duration': round(float(duration or 0), 1),
        'assertion_signal': (assertion_report or {}).get('quality_signal', 'none'),
        'assertion_counts': (assertion_report or {}).get('counts', {}),
        'log_tail': (log_tail or '')[-800:],
    }
    if status == ITEM_ERROR or status == 'stopped':
        item['conclusion'] = ITEM_ERROR if status == ITEM_ERROR else 'stopped'
    elif status == 'passed' and item['assertion_signal'] == 'fail':
        item['conclusion'] = ITEM_FAILED
        item['note'] = '执行器判过，但三层断言质量信号为 fail（见明细）'
    else:
        item['conclusion'] = ITEM_PASSED if status == 'passed' else ITEM_FAILED
    return item


def summarize(results: list) -> dict:
    """整轮巡检汇总。"""
    counts = {ITEM_PASSED: 0, ITEM_FAILED: 0, 'error': 0, 'stopped': 0}
    for r in results:
        c = r.get('conclusion', ITEM_ERROR)
        counts[c] = counts.get(c, 0) + 1
    total = len(results)
    if total == 0:
        signal = 'none'
    elif counts[ITEM_FAILED] + counts['error'] > 0:
        signal = 'fail'
    else:
        signal = 'pass'
    return {
        'total': total,
        'passed': counts[ITEM_PASSED],
        'failed': counts[ITEM_FAILED],
        'error': counts['error'],
        'stopped': counts.get('stopped', 0),
        'signal': signal,
    }


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def render_markdown_report(results: list, summary: dict, started_at: datetime, duration: float) -> str:
    """渲染 Markdown 巡检报告。"""
    lines = [
        f'# 站点冒烟巡检报告 {started_at.strftime("%Y-%m-%d %H:%M:%S")}',
        '',
        (
            f'- 结论：**{summary["signal"]}**（通过 {summary["passed"]} / 失败 {summary["failed"]} / '
            f'异常 {summary["error"]} / 停止 {summary["stopped"]}，共 {summary["total"]} 项）'
        ),
        f'- 整轮耗时：{duration:.1f}s',
        '',
        '| 巡检项 | 结论 | 执行状态 | 断言信号 | 耗时(s) |',
        '|---|---|---|---|---|',
    ]
    for r in results:
        mark = {'passed': '✅', 'failed': '❌'}.get(r['conclusion'], '⚠️')
        lines.append(
            f"| {mark} {r['name']} | {r['conclusion']} | {r['status']} | "
            f"{r['assertion_signal']} {r['assertion_counts']} | {r['duration']} |"
        )

    lines.append('')
    for r in results:
        lines.append(f"## {r['name']} — {r['conclusion']}")
        lines.append('')
        lines.append(f"- 任务：{r['task']}")
        if r.get('note'):
            lines.append(f"- 备注：{r['note']}")
        lines.append(f"- 执行状态：{r['status']}，耗时 {r['duration']}s，断言信号：{r['assertion_signal']}")
        tail = (r.get('log_tail') or '').strip()
        if tail:
            lines.append('- 日志尾部：')
            lines.append('  ```')
            for log_line in tail.splitlines()[-15:]:
                lines.append(f'  {log_line}')
            lines.append('  ```')
        lines.append('')
    return '\n'.join(lines)


def write_reports(results: list, summary: dict, started_at: datetime, duration: float,
                  out_dir) -> dict:
    """落盘 JSON + Markdown 两份报告，返回路径。失败不抛（记日志返回空路径）。"""
    paths = {'json': '', 'md': ''}
    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = started_at.strftime('%Y%m%d_%H%M%S')
        json_path = out / f'smoke_patrol_{stamp}.json'
        md_path = out / f'smoke_patrol_{stamp}.md'
        json_path.write_text(
            json.dumps({'started_at': started_at.isoformat(), 'duration': round(duration, 1),
                        'summary': summary, 'items': results}, ensure_ascii=False, indent=2),
            encoding='utf-8')
        md_path.write_text(
            render_markdown_report(results, summary, started_at, duration), encoding='utf-8')
        paths = {'json': str(json_path), 'md': str(md_path)}
    except Exception as e:
        logger.warning('巡检报告写盘失败: %s', e)
    return paths
