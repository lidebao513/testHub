"""browser-use 执行历史的字段兼容层。

``AgentHistoryList`` 的真实字段是 ``history``，不是 ``steps``。旧代码到处读
``history.steps``，结果恒为空：终态快照复用、失败步骤判定、一致性检查和
``steps_completed`` 落库全部静默失效。这里统一一个入口，兼容测试 fake 对象。
"""


def history_steps(history):
    """返回执行步骤列表，兼容 ``.history``（browser-use）与 ``.steps``（旧/fake）。"""
    if history is None:
        return []
    return list(
        getattr(history, 'history', None)
        or getattr(history, 'steps', None)
        or []
    )
