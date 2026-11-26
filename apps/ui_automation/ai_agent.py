import logging
import asyncio
from .ai_base import BaseBrowserAgent

logger = logging.getLogger('django')


async def _stop_event_buses():
    """关闭 browser-use/bubus 注册在当前事件循环上的常驻 EventBus。

    bubus 的 EventBus._start() 会把 ``_run_loop`` 和队列 ``get()`` 两个任务挂到
    当前 loop。``asyncio.run()`` 退出时只调 ``Task.cancel()``，这两个任务不响应
    （queue getter 仍 pending），``_cancel_all_tasks()`` 的 gather 永不返回 →
    浏览器明明跑完，闭环进程却卡死。

    这里走 bubus 官方 ``stop()``：queue.shutdown() 让 get() 抛 QueueShutDown、
    run_loop 正常 break，之后 asyncio.run() 才能顺利收尾。后续 dispatch 会按需
    自动重启 EventBus，不影响同一个 agent 再跑一条用例。
    """
    loop = asyncio.get_running_loop()
    buses = list(getattr(loop, '_eventbus_instances', ()) or ())
    for bus in buses:
        try:
            if getattr(bus, '_is_running', False):
                await bus.stop(timeout=0)
        except Exception as exc:  # noqa: BLE001 - 清理失败不能把已执行完的用例改成失败
            logger.warning(f'EventBus 关闭失败（不阻断执行）: {exc}')


async def _run_with_eventbus_cleanup(coro):
    try:
        return await coro
    finally:
        await _stop_event_buses()


def _run_sync(coro):
    return asyncio.run(_run_with_eventbus_cleanup(coro))


class BrowserAgent(BaseBrowserAgent):
    """
    Standard Browser Agent for Text Mode.
    Inherits all base functionality without applying dangerous visual patches.
    """
    def __init__(self, execution_mode='text', enable_gif=True, case_name=None):
        self.enable_gif = enable_gif
        self.case_name = case_name or "Adhoc Task"
        super().__init__(execution_mode='text')

# ============================================================================
# EXPORTED FUNCTIONS (FACTORY)
# ============================================================================

def get_agent_class(execution_mode='text'):
    # 始终返回文本模式实现
    return BrowserAgent

def run_ai_task_sync(task_description: str, planned_tasks=None, callback=None, should_stop=None, execution_mode='text'):
    agent = BrowserAgent(execution_mode='text')
    return _run_sync(agent.run_task(task_description, planned_tasks, callback, should_stop))
    
def analyze_task_sync(task_description: str, execution_mode='text'):
    agent = BrowserAgent(execution_mode='text')
    return _run_sync(agent.analyze_task(task_description))

def run_full_process_sync(task_description: str, analysis_callback=None, step_callback=None, should_stop=None, execution_mode='text', enable_gif=True, case_name=None, agent_holder=None, session_alias=None, persist_session_state=False):
    logger.info(f"DEBUG: Entering run_full_process_sync with execution_mode=text, enable_gif={enable_gif}")

    agent = BrowserAgent(execution_mode='text', enable_gif=enable_gif, case_name=case_name)
    # #178：把 agent 交回调用方，便于执行结束后读取 token/成本/路由归因。
    # 用 holder 而不是改返回值，是为了不影响其它既有调用方（返回值仍是 history）。
    if agent_holder is not None:
        agent_holder['agent'] = agent

    logger.info(f"DEBUG: Agent created successfully ({type(agent).__name__}), starting event loop")
    return _run_sync(agent.run_full_process(task_description, analysis_callback, step_callback, should_stop, session_alias=session_alias, persist_session_state=persist_session_state))
