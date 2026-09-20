"""结构化 JSON 日志（携带请求 ID）。

约定：
- 生产输出**单行 JSON**，便于采集与检索；开发可切纯文本；
- 每条日志自动携带 `request_id`（由中间件写入 ContextVar）；
- **禁止记录**密码、令牌、Cookie 等敏感字段（由 `_REDACT_KEYS` 兜底遮蔽）。
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime
from typing import Any


REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")

_REDACT_KEYS = {
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "authorization",
    "api_key",
    "cookie",
}


# LogRecord 的保留属性：同名 key 塞进 `extra=` 会抛
# `KeyError: Attempt to overwrite '<k>' in LogRecord`，因此统一加 `field_` 前缀规避。
_RESERVED_EXTRA_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)


def set_request_id(rid: str) -> None:
    REQUEST_ID.set(rid or "-")


def get_request_id() -> str:
    return REQUEST_ID.get()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _REDACT_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """把日志记录渲染为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": get_request_id(),
            "message": record.getMessage(),
        }
        extras = getattr(record, "extra_fields", None)
        if isinstance(extras, dict):
            payload["extra"] = _redact(extras)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """开发态可读输出。"""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} "
            f"[{record.levelname}] [{get_request_id()}] {record.name}: {record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class _SafeStreamHandler(logging.StreamHandler):
    """吞掉底层写出异常（stderr 被沙箱/管道关闭、BrokenPipe 等）。

    默认 StreamHandler 在流已关闭时 `emit` 会抛 `ValueError('I/O operation on
    closed file.')`，若发生在请求/子命令主路径上会直接拖垮整个进程。这里把
    emit 包成「失败即静默丢弃」，保证日志永远不能成为业务失败的原因。
    """

    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        try:
            super().emit(record)
        except Exception:  # noqa: BLE001 -- 写出失败时绝不抛，仅丢弃该条日志
            pass


def _resolve_stderr() -> Any:
    """返回可用的 stderr 流。

    若 `sys.stderr` 本身或其底层 buffer 已关闭（沙箱回收、管道断开等），退回
    `os.devnull`，避免后续任何日志/print 写出触发 `ValueError('I/O operation on
    closed file.')`。正常环境下返回原始 stderr 的 UTF-8 包装流，行为不变。

    返回类型标注为 `Any`：不同 typeshed 版本对 `sys.stderr` 的推断为
    `TextIO | Any`，与 `io.TextIOBase` 严格互推不稳定；此处仅为日志写出兜底，
    调用点 `StreamHandler` 接受任意 IO 对象，无需更精确注解。
    """
    stream = sys.stderr
    if getattr(stream, "closed", False):
        return open(os.devnull, "w", encoding="utf-8")
    raw = getattr(stream, "buffer", None)
    if raw is not None and getattr(raw, "closed", False):
        return open(os.devnull, "w", encoding="utf-8")
    if raw is not None:
        try:
            return io.TextIOWrapper(raw, encoding="utf-8", errors="replace", write_through=True)
        except Exception:  # noqa: BLE001 -- TextIOWrapper 包装失败时退回原始 stderr
            pass
    return stream


def setup_logging(level: str | None = None) -> None:
    """初始化根 logger（幂等）。`LOG_FORMAT=json|text` 控制格式。

    **日志一律写 stderr**：stdout 专供结构化结果（CLI 的 JSON / 服务无输出），
    避免日志行污染下游按行解析 stdout 的消费方。stderr 不可用时（见
    `_resolve_stderr`）自动降级到 devnull，绝不因日志写入失败而中断业务。
    """
    lvl = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    fmt = (os.environ.get("LOG_FORMAT") or "json").lower()

    stream = _resolve_stderr()
    handler = _SafeStreamHandler(stream)
    handler.setFormatter(TextFormatter() if fmt == "text" else JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(lvl)


def get_logger(name: str) -> logging.Logger:
    """获取 logger；附带 `log_extra()` 便于结构化字段。"""
    return logging.getLogger(name)


def log_extra(**fields: Any) -> dict[str, Any]:
    """构造 `logger.info(..., extra=log_extra(a=1))` 的 extra 参数。

    字段会以 `extra_fields` 传给 formatter 渲染为 JSON 的 `extra`；
    与 LogRecord 保留属性同名的字段自动加 `field_` 前缀，避免 logging 抛 KeyError。
    日志调用点应统一走本函数（直接写 `extra={...}` 会绕过脱敏与渲染）。
    """
    safe = {(f"field_{k}" if k in _RESERVED_EXTRA_KEYS else k): v for k, v in fields.items()}
    return {"extra_fields": safe}
