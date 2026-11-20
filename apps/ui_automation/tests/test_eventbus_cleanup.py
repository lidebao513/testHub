"""闭环收尾回归：asyncio 事件循环关闭前必须停掉 bubus 常驻事件总线。

真实故障：浏览器已 done，但 bubus.EventBus 的 `_run_loop` 与队列 `get()`
不响应 `asyncio.run()` 的强制 cancel，进程卡在 `_cancel_all_tasks()`，
闭环记录停在 running、报告永远写不出来。这里用 fake bus 守住清理动作。
"""

from __future__ import annotations

import asyncio

import pytest

from apps.ui_automation import ai_agent


class FakeBus:
    def __init__(self, fail=False):
        self._is_running = True
        self.stopped = False
        self.fail = fail

    async def stop(self, timeout=None):
        self.stopped = True
        if self.fail:
            raise RuntimeError('bus stop failed')


def _run_with_bus(coro, bus):
    async def main():
        if bus is not None:
            loop = asyncio.get_running_loop()
            loop._eventbus_instances = {bus}
        return await coro

    return asyncio.run(main())


def test_eventbus_stopped_when_coro_succeeds():
    bus = FakeBus()
    result = _run_with_bus(
        ai_agent._run_with_eventbus_cleanup(asyncio.sleep(0, result='done')), bus
    )
    assert result == 'done'
    assert bus.stopped is True


def test_eventbus_stopped_when_coro_raises():
    async def boom():
        raise ValueError('agent failed')

    bus = FakeBus()
    with pytest.raises(ValueError, match='agent failed'):
        _run_with_bus(ai_agent._run_with_eventbus_cleanup(boom()), bus)
    assert bus.stopped is True


def test_bus_stop_failure_does_not_break_result():
    bus = FakeBus(fail=True)
    result = _run_with_bus(
        ai_agent._run_with_eventbus_cleanup(asyncio.sleep(0, result='done')), bus
    )
    assert result == 'done'
    assert bus.stopped is True


def test_no_registered_bus_is_noop():
    result = _run_with_bus(
        ai_agent._run_with_eventbus_cleanup(asyncio.sleep(0, result='done')), None
    )
    assert result == 'done'


def test_all_sync_entries_use_cleanup():
    with open(ai_agent.__file__, encoding='utf-8') as fh:
        src = fh.read()
    assert src.count('return _run_sync(') == 3
    assert 'asyncio.run(agent.' not in src
