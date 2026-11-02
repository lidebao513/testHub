"""一次性授权码交换的纯逻辑（与 Django / Redis 解耦，便于单元测试）。

背景（M4）：原 `exchange_token_view` 把 `request.user.id`（int）直接 `setex` 进 Redis，
而 redis-py 在 `decode_responses=False`（默认）时 `get` 返回 bytes。
`int(b'5')` 会抛 `TypeError`，但原代码只捕获 `User.DoesNotExist`，导致 redeem 阶段 500。

这里把「写值 / 读值」统一收敛为字符串，并提供纯函数供单测，避免 bytes/int 混用。
"""

from __future__ import annotations


def encode_exchange_user_id(user_id: int) -> str:
    """Redis 中存储的交换码值（统一为字符串，杜绝 int/bytes 混用）。"""
    return str(int(user_id))


def decode_exchange_user_id(raw: bytes | str | None) -> int:
    """从 Redis 读回的交换码值 → 用户 id。

    redis-py 在 decode_responses=False 时返回 bytes，True 时返回 str。
    统一处理两种情形；缺失或非法时抛 ``ValueError``，由调用方转 400。
    """
    if raw is None:
        raise ValueError("exchange code missing")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return int(raw)
