"""地址优先入口的轻量取码桥（只读、不落盘、不展开代码功能点）。

背景（docs/url_first_entry_full_design_2026-11-23.md 第 3.1 节缺口 3）：
``stage_target_runtime`` 把运行时发现出的页面/接口收窄到本次变更相关时，需要源码文本做
字面量匹配（接口 ``/api/...`` 只在源码里出现）。纯地址通道 ``result.files`` 为空，
接口目标一条都映射不到。本模块只取**变更文件的文本**注入 ``opts.target_sources``，
让靶向算法既能靠文件名推断页面、也能靠字面量命中接口——但绝不 clone 整仓、不写工作区、
不跑 ``fp_extract``（不产出任何代码通道功能点）。

失败一律降级为「空 sources + note」，绝不阻断生成主链路：取码凭证失效、仓库不可达、
文件超限时宁可退回「靠文件名推断」，也不能让地址通道整批失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


log = logging.getLogger(__name__)

# 预算护栏：桥只为靶向供文本，不是全量取码。超限跳过并记录，避免大仓拖垮地址通道。
DEFAULT_MAX_FILES = 60
DEFAULT_MAX_BYTES_PER_FILE = 200_000


@dataclass
class BridgeResult:
    """取码桥输出。sources/hunks 直接注入 PipelineOptions。"""

    sources: dict[str, str] = field(default_factory=dict)
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    fetched: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "skipped": self.skipped,
            "files": sorted(self.sources),
            "notes": list(self.notes),
        }


def _resolve_token(repo_url: str, code_source: dict[str, Any]) -> tuple[str, str]:
    """返回 (access_key, org_id)。优先请求内的 code_source，其次按 repo_url 从仓库配置带出。"""
    cs = code_source or {}
    token = str(cs.get("access_key") or "").strip()
    org_id = str(cs.get("org_id") or "").strip()
    if token and org_id:
        return token, org_id
    if not repo_url:
        return token, org_id
    try:
        from core import store as _store

        creds = _store.find_repo_creds_by_url(repo_url) or {}
        if not token:
            token = str(creds.get("access_key") or creds.get("codeup_access_key") or "").strip()
        if not org_id:
            org_id = str(creds.get("org_id") or creds.get("codeup_org_id") or "").strip()
    except Exception:  # noqa: BLE001 - 凭证带出是增强，失败即空
        log.warning("轻量取码桥：仓库配置读取失败，按空凭证处理")
    return token, org_id


@dataclass
class BridgeConfig:
    """取码桥的一次调用配置（收敛参数，避免长参数列表）。"""

    repo_url: str
    changed_files: list[str]
    code_source: dict[str, Any]
    base: str = ""
    target: str = ""
    max_files: int = DEFAULT_MAX_FILES
    max_bytes: int = DEFAULT_MAX_BYTES_PER_FILE


def _fetch_hunks(client: Any, rid: str, cfg: BridgeConfig) -> dict[str, list[tuple[int, int]]]:
    """有 base/target 时取 compare 的 hunk；失败退化为全文匹配（精度增强，不阻断）。"""
    if not (cfg.base and cfg.target):
        return {}
    try:
        from engine import incremental_narrow

        diffs = client.compare(rid, from_ref=cfg.base, to_ref=cfg.target)
        return dict(incremental_narrow.hunks_from_compare_diffs(diffs))
    except Exception:  # noqa: BLE001 - hunk 是精度增强，失败退化为全文匹配
        log.warning(
            "轻量取码桥：compare 取 hunk 失败，改用全文匹配", extra={"repo_url": cfg.repo_url}
        )
        return {}


def _fetch_one(client: Any, rid: str, path: str, ref: str, cfg: BridgeConfig) -> str | None:
    """取单个文件文本；返回 None 表示该文件应跳过（空/超限/二进制/失败）。"""
    try:
        text = client.get_blob(rid, path, ref=ref)
    except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其余
        log.warning(
            "轻量取码桥：取文件失败 %s（%s）",
            path,
            type(exc).__name__,
            extra={"repo_url": cfg.repo_url},
        )
        return None
    if not text or "\x00" in text:
        return None  # 空文件或二进制不当文本供给
    if len(text.encode("utf-8", "replace")) > cfg.max_bytes:
        return None
    return text


def fetch_changed_sources(  # noqa: PLR0913 - 对外稳定 API：2 必填 + 5 keyword-only 可选参数
    repo_url: str,
    changed_files: list[str],
    *,
    code_source: dict[str, Any] | None = None,
    base: str = "",
    target: str = "",
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES_PER_FILE,
) -> BridgeResult:
    """取变更文件的文本与可选 hunk，供 ``stage_target_runtime`` 做靶向匹配。

    - ``repo_url`` 为空 / 凭证为空 / 变更文件为空：直接返回空结果 + note，不抛异常。
    - ``base``+``target`` 齐全时额外取 compare diff 的 hunk；失败只影响精度不影响文本。
    - 文本超 ``max_bytes``、文件数超 ``max_files`` 的跳过并计入 ``skipped``。
    """
    cfg = BridgeConfig(
        repo_url=repo_url,
        changed_files=changed_files,
        code_source=code_source or {},
        base=base,
        target=target,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    out = BridgeResult()
    paths = [str(p).strip().replace("\\", "/") for p in (cfg.changed_files or []) if str(p).strip()]
    if not cfg.repo_url or not paths:
        out.notes.append("轻量取码桥：未提供仓库地址或变更文件，靶向按文件名推断")
        return out
    token, org_id = _resolve_token(cfg.repo_url, cfg.code_source)
    if not token:
        out.notes.append("轻量取码桥：未找到有效取码凭证，靶向按文件名推断（接口可能无法命中）")
        return out

    try:
        from engine import codeup_client, remote_scan

        rid = codeup_client.parse_repo_to_repo_id(cfg.repo_url)
        client = remote_scan.client_from_code_source(
            {"credential_type": "pat", "access_key": token, "org_id": org_id},
            verify_ssl=False,
        )
    except Exception as exc:  # noqa: BLE001 - 客户端构造失败即降级
        out.notes.append(f"轻量取码桥：取码客户端不可用（{type(exc).__name__}），靶向按文件名推断")
        return out

    out.hunks = _fetch_hunks(client, rid, cfg)
    ref = cfg.target or cfg.base or ""
    for path in paths:
        if len(out.sources) >= cfg.max_files:
            out.skipped += 1
            continue
        text = _fetch_one(client, rid, path, ref, cfg)
        if text is None:
            out.skipped += 1
            continue
        out.sources[path] = text
        out.fetched += 1

    if out.fetched:
        out.notes.append(
            f"轻量取码桥：取到 {out.fetched} 个变更文件文本供靶向匹配"
            + (f"（跳过 {out.skipped} 个）" if out.skipped else "")
            + "；未展开任何代码功能点"
        )
    elif out.skipped:
        out.notes.append(f"轻量取码桥：{out.skipped} 个变更文件均未取到文本，靶向按文件名推断")
    return out
