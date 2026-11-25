"""#454：CLI `--target-runtime` 与 HTTP `runtime_scope=targeted` 行为一致。

CLI 缺省保持旧行为（全站巡检），显式开关后才按变更收窄运行时用例；
HTTP 入口默认 targeted。两条入口构造出的选项与靶向结果必须一致。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from cli.main import _apply_cli_target, _build_parser, main
from core.contracts import FunctionalPoint
from engine import pipeline, runtime_ui
from service.app import PipelineRequest, build_opts_from_request


_CHANGED = "frontend/app/src/api/backend.ts"
_URL = "https://a.example"
_API_SRC = """
export function listThreadsPage(channel, offset, limit = 20) {
  return apiJson(`/api/v1/threads?offset=${offset}&limit=${limit}`);
}
"""


def _cli_opts(extra: list[str]):
    args = _build_parser().parse_args(
        ["pipeline", "--url", _URL, "--changed-files", _CHANGED, *extra]
    )
    opts = pipeline.default_options(mode="full")
    _apply_cli_target(args, opts)
    return opts


def _http_opts(runtime_scope: str):
    req = PipelineRequest(test_url=_URL, changed_files=[_CHANGED], runtime_scope=runtime_scope)
    return build_opts_from_request(req)


def _result() -> pipeline.PipelineResult:
    result = pipeline.PipelineResult(mode="full")
    result.runtime_ui = runtime_ui.RuntimeUiResult(
        base_url="http://localhost:3000",
        pages=[runtime_ui.UiPage(url="http://localhost:3000/chat", path="/chat", title="会话")],
        api_endpoints=[runtime_ui.ApiEndpoint(method="GET", path="/api/v1/threads", status=200)],
    )
    result.functional_points = [
        FunctionalPoint(
            fp_id="api:threads",
            ftype="api",
            file_path="runtime:http://localhost:3000/",
            name="GET /api/v1/threads",
            title="会话列表",
            module="m",
        ),
        FunctionalPoint(
            fp_id="api:memories",
            ftype="api",
            file_path="runtime:http://localhost:3000/",
            name="GET /api/v1/memories",
            title="记忆列表",
            module="m",
        ),
    ]
    result.files = {_CHANGED: _API_SRC}
    return result


def test_cli_flag_sets_target_runtime_and_keeps_other_target_fields():
    opts = _cli_opts(["--target-runtime"])

    assert opts.target_runtime is True
    assert opts.changed_files == [_CHANGED]
    assert opts.target_req.base_url == _URL
    assert opts.target_req.enabled is True


def test_cli_without_flag_keeps_legacy_full_scan():
    assert _cli_opts([]).target_runtime is False


def test_cli_targeted_equals_http_default():
    cli_opts = _cli_opts(["--target-runtime"])
    http_opts = _http_opts("targeted")

    assert cli_opts.target_runtime is True
    assert (cli_opts.target_runtime, cli_opts.changed_files) == (
        http_opts.target_runtime,
        http_opts.changed_files,
    )


def test_cli_full_equals_http_explicit_full():
    cli_opts = _cli_opts([])
    http_opts = _http_opts("full")

    assert cli_opts.target_runtime is False
    assert http_opts.target_runtime is False


def test_targeting_result_is_identical_on_both_entries():
    cli_result, http_result = _result(), _result()

    pipeline.stage_target_runtime(_cli_opts(["--target-runtime"]), cli_result, None)
    pipeline.stage_target_runtime(_http_opts("targeted"), http_result, None)

    cli_names = sorted(fp.name for fp in cli_result.functional_points)
    http_names = sorted(fp.name for fp in http_result.functional_points)
    assert cli_names == http_names == ["GET /api/v1/threads"]


def test_cli_pipeline_passes_flag_to_runner():
    captured: dict[str, pipeline.PipelineOptions] = {}

    def fake_run(opts, progress=None):
        captured["opts"] = opts
        return pipeline.PipelineResult()

    out, err = io.StringIO(), io.StringIO()
    with (
        patch("cli.main.pipeline.run_pipeline_auto", side_effect=fake_run),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        rc = main(
            [
                "pipeline",
                "--url",
                _URL,
                "--changed-files",
                _CHANGED,
                "--target-runtime",
                "--no-persist",
                "--no-output",
            ]
        )

    assert rc == 0
    assert captured["opts"].target_runtime is True
    assert "result" in json.loads(out.getvalue())
