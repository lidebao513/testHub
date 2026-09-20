"""日志模块测试：保留字段不炸、敏感字段脱敏、结构化字段可渲染。"""

import json
import logging

from core.log import JsonFormatter, log_extra, set_request_id


def _record(**extra_fields):
    rec = logging.LogRecord("t", logging.INFO, "f.py", 1, "msg", (), None)
    rec.extra_fields = extra_fields
    return rec


def test_log_extra_avoids_reserved_logrecord_keys():
    """与 LogRecord 保留属性同名的字段必须被改名，否则 logging 抛 KeyError。"""
    fields = log_extra(name="ws-1", message="hello", path="p/a")
    assert fields["extra_fields"]["field_name"] == "ws-1"
    assert fields["extra_fields"]["field_message"] == "hello"
    assert fields["extra_fields"]["path"] == "p/a"

    # 关键：真的走一遍 logging 的记录构造（保留键冲突会在这里炸）
    logger = logging.getLogger("test_log_reserved")
    logger.setLevel(logging.INFO)
    logger.info("hello", extra=fields)


def test_json_formatter_redacts_secrets():
    set_request_id("rid-1")
    payload = json.loads(JsonFormatter().format(_record(token="abc", password="p", ok=1)))
    assert payload["request_id"] == "rid-1"
    assert payload["level"] == "INFO"
    assert payload["extra"]["token"] == "***"
    assert payload["extra"]["password"] == "***"
    assert payload["extra"]["ok"] == 1


def test_json_formatter_is_single_line():
    """结构化日志必须单行，便于采集（多行会破坏按行解析）。"""
    rendered = JsonFormatter().format(_record(a=1))
    assert "\n" not in rendered
    assert json.loads(rendered)["message"] == "msg"
