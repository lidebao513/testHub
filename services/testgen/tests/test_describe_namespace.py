"""B-5 批次 1：describe 缓存命名空间守护测试。

本批只守护纯函数，不改任何调用方；批次 2 接入后由本测试保证读写两侧同源口径。
"""

from __future__ import annotations

import os

import pytest

from core.describe_namespace import describe_namespace


def _local_ns(path: str) -> str:
    return "local:" + os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def test_explicit_namespace_has_highest_priority() -> None:
    assert (
        describe_namespace(
            namespace="  custom:ns  ",
            runtime_url="https://x.example/p",
            repo_key="svc",
            project_id=7,
            repo_url="https://repo.example/a.git",
            local_path="/tmp/a",
        )
        == "custom:ns"
    )


def test_blank_explicit_namespace_falls_through() -> None:
    assert describe_namespace(namespace="   ", repo_url="https://repo.example/a.git") == (
        "https://repo.example/a.git"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP://Example.COM:80/path/", "runtime:http://example.com/path"),
        ("example.com:8080/a/", "runtime:http://example.com:8080/a"),
        ("https://user:pw@example.com:443/app/?q=1#frag", "runtime:https://example.com/app"),
    ],
)
def test_runtime_url_is_normalized(raw: str, expected: str) -> None:
    assert describe_namespace(runtime_url=raw) == expected


def test_invalid_runtime_url_degrades_without_raising() -> None:
    assert describe_namespace(runtime_url="  http://  ") == "runtime:http://"


def test_runtime_url_wins_over_all_other_sources() -> None:
    assert (
        describe_namespace(
            runtime_url="example.com/a",
            repo_key="svc",
            project_id=7,
            repo_url="https://repo.example/a.git",
            local_path="/tmp/a",
        )
        == "runtime:http://example.com/a"
    )


def test_repo_key_isolated_by_project() -> None:
    assert (
        describe_namespace(
            repo_key="  svc  ",
            project_id=42,
            repo_url="https://repo.example/a.git",
            local_path="/tmp/a",
        )
        == "multi:42:svc"
    )
    assert describe_namespace(repo_key="svc") == "multi:0:svc"
    assert describe_namespace(repo_key="svc", project_id=7) != describe_namespace(
        repo_key="svc", project_id=8
    )


def test_repo_url_kept_verbatim_for_history_cache() -> None:
    assert (
        describe_namespace(repo_url="  HTTPS://Example.COM/Repo.GIT  ")
        == "HTTPS://Example.COM/Repo.GIT"
    )
    assert describe_namespace(repo_url="https://r/x.git", local_path="/tmp/x") == (
        "https://r/x.git"
    )


def test_local_path_is_absolute_and_case_normalized() -> None:
    assert describe_namespace(local_path="  ./some/dir  ") == _local_ns("./some/dir")
    assert describe_namespace(local_path="~") == _local_ns("~")
    assert describe_namespace(local_path="/tmp/a") != describe_namespace(local_path="/tmp/b")


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"repo_url": "   "},
        {"local_path": ""},
        {"repo_key": "  "},
        {"runtime_url": ""},
    ],
)
def test_no_identity_means_disable_cache(kwargs: dict[str, str]) -> None:
    ns = describe_namespace(**kwargs)
    assert ns is None
    assert ns != ""  # 不得退化成历史孤儿命名空间 / 「清全部」哨兵
