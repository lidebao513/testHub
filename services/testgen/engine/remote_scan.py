"""引擎 · 快速扫描（scan）：把「目标」变成可勾选的「功能列表」。

两条来源，统一产出 ``ScanItem``（一个源文件 = 一个可选项模块，附解析出的函数/类）：
- 远程（C+E）：经 ``codeup_client.CodeupClient`` 只读取码（不克隆）；
- 本地：直接扫 ``local_path``（复用 ``engine.scan.Scanner``）。

设计要点
--------
- 「快速扫描」只列**文件级**可选项（模块），并为 Python 文件附顶层 def/class 名，
  让用户直观看到模块里有什么；真正取内容留到「生成」阶段按需拉取，避免大仓扫描过慢。
- 过滤规则与 ``engine.scan`` 单一真值保持一致（扩展名 / 噪声目录 / 噪声文件前缀），
  避免出现「前端功能点恒为 0」「扫到测试夹具」等静默丢层/幽灵点。
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

from engine import diff_tag, scan as _scan
from engine.codeup_client import CodeupClient, CodeupError, parse_repo_to_repo_id


# 复用 scan.py 的单一真值
SOURCE_EXTS = _scan.SOURCE_EXTS
DEFAULT_EXCLUDE_DIRS = _scan.DEFAULT_EXCLUDE_DIRS
NOISE_DIR_PARTS = _scan.NOISE_DIR_PARTS
NOISE_FILE_PREFIXES = _scan.NOISE_FILE_PREFIXES

# 变更状态（基线对比结果；未提供基线时一律 unchanged）
CHANGE_ADDED = "added"  # 新增（基线中不存在该文件）
CHANGE_MODIFIED = "modified"  # 修改（基线中存在且有差异/重命名）
CHANGE_UNCHANGED = "unchanged"  # 未变更（或未提供基线）

_MAX_FILES = 400  # 未变更文件的填充上限（变更文件不受此限，另行全量入选），保证「快」
_MAX_CHANGED_FILES = 1000  # 变更文件（新增/修改）优先全量入选的上限
_MAX_SYMBOL_FILES = (
    50  # 仅前 N 个文件取内容解析符号（其余列文件名即可），避免为全仓逐文件拉内容导致扫描过慢
)
_MAX_BYTES = 2_000_000


def _is_source(path: str) -> bool:
    return Path(path).suffix.lower() in SOURCE_EXTS


def _is_noise(path: str) -> bool:
    parts = path.split("/")
    if any(seg in DEFAULT_EXCLUDE_DIRS for seg in parts[:-1]):
        return True
    if any(seg in NOISE_DIR_PARTS for seg in parts[:-1]):
        return True
    name = parts[-1]
    return name.startswith(NOISE_FILE_PREFIXES)


def extract_symbols(text: str, path: str) -> tuple[list[str], list[str]]:
    """解析源文件顶层符号（函数 / 类）。Python 走 AST；其余语言仅列文件、不解析符号。"""
    ext = Path(path).suffix.lower()
    if ext == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return [], []
        funcs: list[str] = []
        classes: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.append(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
        return funcs, classes
    # 前端等：仅列文件，符号解析交给后续阶段
    return [], []


def client_from_code_source(
    code_source: dict[str, Any],
    *,
    domain: str = "",
    verify_ssl: bool = True,
) -> CodeupClient:
    """从前端传入的 code_source 构造只读客户端。

    仅支持 PAT（x-yunxiao-token）。AK/SK 走 RPC 签名，本适配器暂未接入，
    遇到 ak_sk 明确报错而不是静默失败。
    """
    credential_type = (code_source.get("credential_type") or "pat").strip()
    org_id = (code_source.get("org_id") or "").strip()
    access_key = (code_source.get("access_key") or "").strip()
    if not access_key:
        raise CodeupError(None, "远程只读取码：凭证（access_key）为空")
    if credential_type == "ak_sk":
        raise CodeupError(
            None,
            "AK/SK 远程取码尚未接入 RPC 签名，请改用「个人访问令牌 PAT」模式",
        )
    return CodeupClient(token=access_key, org_id=org_id, domain=domain, verify_ssl=verify_ssl)


def _change_map_from_diffs(diffs: list[dict[str, Any]]) -> dict[str, str]:
    """把 GetCompare 的 diffs 列表解析为 ``{文件路径: 变更状态}`` 映射。

    - ``newFile=true`` → added（新增）；
    - ``deletedFile=true`` → 跳过（文件已不在目标树中，扫描列表里不会出现）；
    - 其余（含 renamed，取 ``newPath``）→ modified；
    - 路径取 ``newPath``，缺失时回退 ``oldPath``。
    """
    out: dict[str, str] = {}
    for d in diffs:
        if not isinstance(d, dict):
            continue
        path = str(d.get("newPath") or d.get("oldPath") or "").strip()
        if not path:
            continue
        if d.get("deletedFile"):
            continue
        if d.get("newFile"):
            out[path] = CHANGE_ADDED
        else:
            out[path] = CHANGE_MODIFIED
    return out


def _local_git_change_map(local_path: str, base_ref: str) -> dict[str, str]:
    """本地目录的变更状态（best-effort）：``git diff --name-status <base>`` + 未跟踪文件。

    - 非 git 仓库 / git 不可用 / base_ref 为空 → 返回 {}（全部按 unchanged）；
    - 状态字母映射：A→added；M/R/T 及其他→modified；未跟踪文件→added。
    """
    if not base_ref:
        return {}
    try:
        # G8 补漏（2026-10-14）：短名 ref（如 `oa-20260909`）在只有远端跟踪分支的克隆里
        # 会让 `git diff` 报 bad revision → except 静默返回 {} → 全部误判「未变更」。
        # 与 pull/diff_tag 同口径：先做 origin/ 前缀归一化。
        base_ref = diff_tag.resolve_ref(local_path, base_ref)
        p = subprocess.run(
            ["git", "-C", local_path, "diff", "--relative", "--name-status", base_ref, "--", "."],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except Exception:  # noqa: BLE001 - best-effort：非 git 目录/无 git/超时一律静默降级
        return {}
    out: dict[str, str] = {}
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0].strip(), parts[-1].strip()
        if not path:
            continue
        out[path] = CHANGE_ADDED if status.startswith("A") else CHANGE_MODIFIED
    try:
        p2 = subprocess.run(
            ["git", "-C", local_path, "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        for line in (p2.stdout or "").splitlines():
            path = line.strip()
            if path:
                out.setdefault(path, CHANGE_ADDED)
    except Exception:  # noqa: BLE001
        pass
    return out


def _change_map_bidirectional(
    client: CodeupClient, repo_id: str, base_ref: str, ref: str
) -> dict[str, str]:
    """双向 GetCompare 对比基线与目标，合并为 ``{文件路径: 变更状态}``。

    背景：只做正向（base → target）时，若目标分支**落后**于基线（如从 master
    切出的旧分支），正向 diffs 为空 → 全部标「未变」，用户以为对比失效。
    实际两个分支有 8 个文件差异（在反向 diffs 里）。因此：

    - 正向（base → target）：newFile→added（目标新增）、修改→modified、
      deletedFile 跳过（文件已不在目标树）；
    - 反向（target → base）：newFile 跳过（基线新增的文件不在目标树）；
      deletedFile→added（文件在目标树存在而基线没有，即相对基线新增）；
      其余→modified（两分支有差异）。

    任一方向 compare 失败（权限/参数）均 best-effort 降级，不阻塞扫描。
    """
    change_map: dict[str, str] = {}
    try:
        change_map.update(
            _change_map_from_diffs(client.compare(repo_id, from_ref=base_ref, to_ref=ref))
        )
    except CodeupError:
        pass
    try:
        for d in client.compare(repo_id, from_ref=ref, to_ref=base_ref):
            if not isinstance(d, dict) or d.get("newFile"):
                continue  # 基线新增的文件不在目标树里，扫描列表不会出现
            path = str(d.get("newPath") or d.get("oldPath") or "").strip()
            if not path:
                continue
            status = CHANGE_ADDED if d.get("deletedFile") else CHANGE_MODIFIED
            change_map.setdefault(path, status)
    except CodeupError:
        pass
    return change_map


def _select_and_package(
    candidates: list[tuple[Any, str, str]],
    change_map: dict[str, str],
    *,
    path_of: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """变更文件优先入选 + 打包 ScanItem（candidates: (node, path, name)）。

    背景：旧实现按树序截断 ``_MAX_FILES``，当目标分支的变更文件不在树序前 400 个时，
    扫描结果会「400 个文件全是未变」——变更文件被静默截掉。现改为：
    变更文件（新增/修改）优先全量入选（上限 ``_MAX_CHANGED_FILES``），
    未变更文件填充剩余名额至 ``_MAX_FILES``。

    返回 ``(items, meta)``，meta 含 ``total_matched``（过滤后源文件总数）与
    ``truncated``（是否发生截断），供前端如实展示。
    """
    changed: list[tuple[Any, str, str]] = []
    unchanged: list[tuple[Any, str, str]] = []
    for cand in candidates:
        (changed if cand[1] in change_map else unchanged).append(cand)
    changed = changed[:_MAX_CHANGED_FILES]
    selected = changed + unchanged[: max(0, _MAX_FILES - len(changed))]
    items: list[dict[str, Any]] = []
    for idx, (node, path, name) in enumerate(selected):
        funcs: list[str] = []
        classes: list[str] = []
        content = ""
        # 仅前 _MAX_SYMBOL_FILES 个文件取内容解析符号，避免为全仓逐文件拉内容导致扫描过慢；
        # 超出部分仅列文件名，仍可在前端勾选（生成阶段按需取内容）。
        if idx < _MAX_SYMBOL_FILES:
            return_content = path_of(node, path) if path_of else ""
            content = return_content
            if len(content.encode("utf-8", "replace")) <= _MAX_BYTES:
                funcs, classes = extract_symbols(content, path)
        items.append(
            {
                "path": path,
                "name": name,
                "ext": node.ext if hasattr(node, "ext") else Path(path).suffix.lower(),
                "functions": funcs,
                "classes": classes,
                "bytes": len(content.encode("utf-8", "replace")),
                "change_status": change_map.get(path, CHANGE_UNCHANGED),
            }
        )
    meta = {
        "total_matched": len(candidates),
        "truncated": len(candidates) > len(selected),
    }
    return items, meta


def scan_repository(  # noqa: PLR0913 - 各参数均为独立可选项，聚合对象反而降低可读性
    code_source: dict[str, Any],
    repo_url: str,
    *,
    ref: str = "",
    base_ref: str = "",
    domain: str = "",
    verify_ssl: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """远程快速扫描：列文件树 → 过滤 → 取内容 → 解析符号，返回 ``(items, meta)``。

    ``base_ref``（可选）：基线分支/commit。提供后经 GetCompare 对比
    ``base_ref → ref``，为每个文件标注 ``change_status``（added/modified/unchanged）；
    变更文件优先入选（详见 ``_select_and_package``），meta 如实回报截断情况。
    """
    client = client_from_code_source(code_source, domain=domain, verify_ssl=verify_ssl)
    repo_id = parse_repo_to_repo_id(repo_url)
    tree = client.list_tree(repo_id, ref=ref, recursive=True)
    change_map: dict[str, str] = {}
    if base_ref:
        change_map = _change_map_bidirectional(client, repo_id, base_ref, ref)
    candidates: list[tuple[Any, str, str]] = []
    for node in tree:
        if node.type != "blob" or not _is_source(node.path) or _is_noise(node.path):
            continue
        candidates.append((node, node.path, node.name))

    def _fetch_blob(_node: Any, path: str) -> str:
        try:
            return client.get_blob(repo_id, path, ref=ref)
        except CodeupError:
            # 单文件取不到不影响整体（如超大/LFS），仅列文件名
            return ""

    return _select_and_package(candidates, change_map, path_of=_fetch_blob)


def scan_local(
    local_path: str, *, base_ref: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """本地快速扫描：复用 Scanner，产出与远程一致的 ``(items, meta)``。

    ``base_ref``（可选）：本地目录为 git 仓库时，按 ``git diff <base>``（best-effort）
    标注变更状态；非 git 目录或对比失败一律按 unchanged。变更文件优先入选。
    """
    scanner = _scan.Scanner(local_path)
    change_map = _local_git_change_map(local_path, base_ref)
    candidates: list[tuple[Any, str, str]] = []
    for sf in scanner.iter_files():
        if sf.is_noise:
            continue
        candidates.append((sf, sf.rel, sf.name))

    def _read_text(sf: Any, _path: str) -> str:
        return sf.text

    return _select_and_package(candidates, change_map, path_of=_read_text)
