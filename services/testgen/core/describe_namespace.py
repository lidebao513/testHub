"""describe 缓存命名空间的单一事实源（B-5）。

把「命名空间」从调用方随手传的字符串收口为一个纯函数：写入（``/describe``）、
命中判断（``_describe_cache_lookup``）、生成侧背景注入（``pipeline.load_file_context``）
与清理（``/describe/cache/clear``）必须调用本函数，避免同一文件在不同项目 / 不同仓
之间共用 ``""`` 命名空间而串味。

复用既有 ``file_descriptions.repo_url`` 列承载命名空间，不新增列、不升 schema；
返回 ``None`` 表示**禁用缓存**（不读不写），绝不退化成空字符串——空串是历史孤儿
命名空间，且在清缓存接口里带有「清全部」语义。
"""

from __future__ import annotations

import os

from core.errors import ValidationError
from core.url_identity import normalize_url


def _runtime_namespace(runtime: str) -> str:
    """运行时地址归一；无效地址保留原文，避免 describe 500。"""
    try:
        return "runtime:" + normalize_url(runtime)["normalized"]
    except ValidationError:
        return "runtime:" + runtime


def describe_namespace(  # noqa: PLR0913 - 各身份来源必须同时可传（方案契约）
    *,
    namespace: str = "",
    runtime_url: str = "",
    repo_key: str = "",
    project_id: int | None = None,
    repo_url: str = "",
    local_path: str = "",
) -> str | None:
    """计算 describe 缓存命名空间；``None`` = 禁用缓存。

    优先级：显式 namespace > 运行时地址 > 多仓键 > 仓库地址 > 本地目录 > 禁用。

    - 显式 namespace：调用方已算好的值，原样使用（仅去首尾空白）；
    - 运行时地址：``runtime:<归一 URL>``，与地址优先入口同口径；
    - 多仓键：``multi:<project_id>:<repo_key>``，按项目 + 仓隔离；
    - 仓库地址：沿用原值，保证远程仓历史缓存继续命中；
    - 本地目录：``local:<规范绝对路径>``，Windows 统一大小写与分隔符；
    - 全空：返回 ``None``，兑现「无身份信息不缓存」的既有承诺。
    """
    explicit = (namespace or "").strip()
    if explicit:
        return explicit

    runtime = (runtime_url or "").strip()
    if runtime:
        return _runtime_namespace(runtime)

    key = (repo_key or "").strip()
    if key:
        return f"multi:{project_id or 0}:{key}"

    remote = (repo_url or "").strip()
    if remote:
        return remote

    local = (local_path or "").strip()
    if local:
        expanded = os.path.expanduser(local)
        return "local:" + os.path.normcase(os.path.realpath(expanded))

    return None
