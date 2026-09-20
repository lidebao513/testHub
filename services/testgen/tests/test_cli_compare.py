"""CLI `testgen compare` 子命令测试（P0-2）。

覆盖：
- ① --json-in 直接比对（绕过库）；
- ② --project/--case-id/--actual 从库取预期再比对；
- ③ --no-llm 强制规则-only；
- ④ 缺参 / 用例未找到 → 退出码 2。

comparator 一律 mock，无网络。红线：本命令不落库、不写文件、不触执行动作。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from engine.comparator import CompareVerdict


def _verdict(verdict: str = "pass", rule: str = "pass") -> CompareVerdict:
    return CompareVerdict(
        verdict=verdict,
        confidence=0.9,
        reason="ok",
        diff=[],
        rule_verdict=rule,
        model="qwen3.7-flash",
    )


def _run(argv: list[str]) -> tuple[int, str]:
    from cli import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main.main(argv)
    return rc, buf.getvalue()


def test_cli_compare_json_in(tmp_path: Path) -> None:
    """--json-in：直接读完整比对输入 JSON，输出 verdict。"""
    payload = {
        "case": {"id": "C1", "title": "t", "steps": [{"expect": "x"}]},
        "actual": {"status": "PASS"},
        "context": {},
    }
    f = tmp_path / "in.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    with patch("engine.comparator.compare_one", return_value=_verdict("pass", "pass")):
        rc, out = _run(["compare", "--json-in", str(f)])
    assert rc == 0
    assert json.loads(out)["verdict"]["verdict"] == "pass"


def test_cli_compare_from_db() -> None:
    """--project/--case-id/--actual：从库取预期，比对实际结果。"""
    row = {"id": "C1", "title": "t", "steps": [{"expect": "x"}]}
    with (
        patch("cli.main.store.list_cases", return_value=[row]),
        patch("cli.main.init_db"),
        patch("engine.comparator.compare_one", return_value=_verdict("fail", "fail")),
    ):
        rc, out = _run(
            ["compare", "--project", "7", "--case-id", "C1", "--actual", '{"status":"FAIL"}']
        )
    assert rc == 0
    assert json.loads(out)["verdict"]["verdict"] == "fail"


def test_cli_compare_case_not_found() -> None:
    """库里没有对应用例 → 退出码 2。"""
    with patch("cli.main.store.list_cases", return_value=[]), patch("cli.main.init_db"):
        rc, _out = _run(
            ["compare", "--project", "7", "--case-id", "NOPE", "--actual", '{"status":"PASS"}']
        )
    assert rc == 2


def test_cli_compare_missing_args() -> None:
    """既不给 --json-in 也不给 --project/--case-id → 退出码 2。"""
    rc, _out = _run(["compare"])
    assert rc == 2


def test_cli_compare_no_llm_forces_rule_only(tmp_path: Path) -> None:
    """--no-llm：comparator 被强制 enabled=False（规则-only），仍返回结论。"""
    payload = {"case": {"id": "C1"}, "actual": {"status": "PASS"}}
    f = tmp_path / "in.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    with patch("engine.comparator.compare_one") as m:
        m.return_value = _verdict("pass", "pass")
        rc, _out = _run(["compare", "--json-in", str(f), "--no-llm"])
    assert rc == 0
    m.assert_called_once()
    opts = m.call_args.args[1]
    assert opts.enabled is False
