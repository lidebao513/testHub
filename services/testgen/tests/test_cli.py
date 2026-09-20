"""CLI 契约测试。

约定（见 cli/main.py 模块说明）：**结构化结果走 stdout，日志与进度走 stderr**。
历史上日志曾误写 stdout，导致下游按行 `json.loads` 直接崩（"Extra data"）；
本文件把这条契约钉死，防止回归。
"""

from __future__ import annotations

import json

from cli.main import main


def test_cli_stdout_is_pure_json(fresh_db, sample_repo, capsys):
    """stdout 必须能被整体解析为一个 JSON 文档（日志不得混入）。"""
    rc = main(
        [
            "pipeline",
            "--path",
            str(sample_repo),
            "--name",
            "cli-sample",
            "--scopes",
            "正常",
            "--no-output",
        ]
    )
    assert rc == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # 混入日志行 → JSONDecodeError

    assert payload["result"]["counts"]["files"] > 0
    assert payload["result"]["counts"]["functional_points"] > 0
    # 进度/日志必须落在 stderr
    assert captured.err.strip() != ""
    assert "pipeline" in captured.err


def test_cli_rejects_invalid_scope(fresh_db, sample_repo, capsys):
    """非法范围应快速失败（退出码 2），且不产生 stdout 结果。"""
    rc = main(["pipeline", "--path", str(sample_repo), "--scopes", "不存在的范围"])
    assert rc == 2
    assert capsys.readouterr().out.strip() == ""
