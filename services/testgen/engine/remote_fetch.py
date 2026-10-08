"""引擎 · 远程取码（生成阶段）：把 Codeup 上的「按需片段」物化到受管 workspace。

与 ``engine.pull``（整仓克隆）的根本区别
----------------------------------------
- 不克隆 `.git`、不拉历史；只把「要分析的源文件」按相对路径写到临时目录；
- 文件来源是 Codeup OpenAPI（只读），不是 git 协议；
- 满足合规「不落地完整仓库、代码所有权不转移」。

调用方拿到 ``(dest_dir, files)`` 后，把 ``dest_dir`` 当 local_path、``files`` 当
changed_files 交给现有本地分析链路即可，无需改动流水线其它部分。
"""

from __future__ import annotations

import os
from typing import Any

from engine.codeup_client import CodeupClient, CodeupError, parse_repo_to_repo_id
from engine.remote_scan import (
    _MAX_BYTES,
    _is_noise,
    _is_source,
    client_from_code_source,
)


def _safe_repo_name(repo_url: str) -> str:
    repo_id = parse_repo_to_repo_id(repo_url)
    # repo_id 形如 org%2Fgroup%2Frepo → 还原并取末段
    raw = repo_id.replace("%2F", "/").replace("%2f", "/")
    return raw.rsplit("/", 1)[-1] or "repo"


def materialize_remote(  # noqa: PLR0913 - 参数语义明确的 keyword 入参（7 个）
    code_source: dict[str, Any],
    repo_url: str,
    dest_root: str,
    *,
    paths: list[str] | None = None,
    ref: str = "",
    domain: str = "",
    verify_ssl: bool = True,
) -> tuple[str, list[str]]:
    """把远程源文件物化到 ``dest_root/<repo>``，返回 (目录, 相对路径列表)。

    - ``paths`` 给定（用户从扫描结果里勾选）→ 只取这些；
    - 否则扫描文件树，过滤源文件/噪声后全取（受 ``remote_scan._MAX_FILES`` 约束）。
    """
    client: CodeupClient = client_from_code_source(
        code_source, domain=domain, verify_ssl=verify_ssl
    )
    repo_id = parse_repo_to_repo_id(repo_url)
    dest_dir = os.path.join(dest_root, "remote", _safe_repo_name(repo_url))
    os.makedirs(dest_dir, exist_ok=True)

    if paths:
        targets = [p.strip().replace("\\", "/") for p in paths if p.strip()]
    else:
        tree = client.list_tree(repo_id, ref=ref, recursive=True)
        targets = [
            n.path
            for n in tree
            if n.type == "blob" and _is_source(n.path) and not _is_noise(n.path)
        ]

    written: list[str] = []
    for rel in targets:
        try:
            content = client.get_blob(repo_id, rel, ref=ref)
        except CodeupError:
            continue
        if len(content.encode("utf-8", "replace")) > _MAX_BYTES:
            continue
        out = os.path.join(dest_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(rel)

    return dest_dir, sorted(written)
