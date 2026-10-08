"""引擎 · 模块（C+E 远程只读）：Codeup OpenAPI 适配器。

职责单一：用「个人访问令牌（PAT）」经 Codeup OpenAPI **只读**访问仓库内容，
不克隆、不落地完整仓库——只按需拉取文件树 / 指定文件内容，供「快速扫描」与
「按需取码生成」使用。

合规要点（与项目红线一致）：
- 只调 GET（files/tree、files/blob），绝不写操作；
- 令牌（x-yunxiao-token）绝不进入任何结果 / 日志（调用方负责，本模块不打印令牌）；
- 取回的片段写入受管 workspace 临时目录，不污染用户代码。

接口形态（中心版 / 标准版，domain 默认 openapi-rdc.aliyuncs.com）：
  GET /oapi/v1/codeup/organizations/{orgId}/repositories/{repoId}/files/tree?ref=&type=RECURSIVE
  GET /oapi/v1/codeup/organizations/{orgId}/repositories/{repoId}/files/blob?filePath=&ref=
鉴权：请求头 x-yunxiao-token: pt-xxxx
repositoryId：数字 ID，或「URL 编码后的完整路径」（如 org%2Fgroup%2Frepo）。
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


# 标准版 / 中心版 网关；专属版可改环境变量覆盖
DEFAULT_DOMAIN = "https://openapi-rdc.aliyuncs.com"

# 免校验（仅用于内网/测试 mock 服务器；生产走真实 TLS）
_SSL_CTX = ssl.create_default_context()


@dataclass
class FileNode:
    """仓库文件树中的一个节点。"""

    path: str
    name: str
    type: str  # tree | blob
    is_lfs: bool = False
    mode: str = ""


@dataclass
class ScanItem:
    """扫描产出的「可选项功能」（一个源文件 = 一个模块，附解析出的符号）。"""

    path: str
    name: str
    ext: str
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    bytes: int = 0


class CodeupError(Exception):
    """Codeup API 调用失败（含状态码与远端错误信息）。"""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


def parse_repo_to_repo_id(repo_url: str) -> str:
    """把 `https://codeup.aliyun.com/{ns}/{repo}.git` 转成 URL 编码的 repositoryId。

    返回形如 `ns%2Frepo`（去掉 .git 后缀、保留子组路径）。
    """
    url = (repo_url or "").strip()
    if not url:
        raise CodeupError(None, "repo_url 为空，无法解析仓库标识")
    # 去掉协议与主机，取路径
    path = url.split("//", 1)[-1] if "//" in url else url
    path = path.split("/", 1)[-1] if "/" in path else path
    # 去掉尾部 .git
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not path:
        raise CodeupError(None, f"无法从 repo_url 解析出仓库路径：{repo_url}")
    return urllib.parse.quote(path, safe="")


class CodeupClient:
    """Codeup 只读客户端（仅 stdlib，无第三方依赖）。"""

    def __init__(
        self,
        token: str,
        *,
        org_id: str = "",
        domain: str = "",
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        self.token = token
        self.org_id = (org_id or "").strip()
        self.domain = (domain or DEFAULT_DOMAIN).rstrip("/")
        self.timeout = timeout
        self._ctx = _SSL_CTX if verify_ssl else _insecure_ctx()
        self._branch_cache: dict[str, str] = {}  # repo_id -> 默认分支（避免每次取内容都查）

    def _resolve_ref(self, repo_id: str, ref: str) -> str:
        """把空 ref 解析为仓库默认分支（Codeup 取文件内容接口**拒绝空 ref**）。

        - 非空 ref 原样返回；
        - 空 ref → 查仓库信息拿 ``defaultBranch``（缓存到 ``_branch_cache``，
          同一 client 实例内只查一次）；查询失败兜底 ``master``。
        """
        if ref:
            return ref
        if repo_id not in self._branch_cache:
            try:
                info = self._get(self._repo_path(repo_id), {})
                self._branch_cache[repo_id] = (
                    (info.get("defaultBranch") or "master") if isinstance(info, dict) else "master"
                )
            except CodeupError:
                self._branch_cache[repo_id] = "master"
        return self._branch_cache[repo_id]

    # ------------------------------------------------------------------ 内部
    def _get(self, api_path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.domain + api_path
        if params:
            q = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items())
            url += "?" + q
        req = urllib.request.Request(
            url,
            headers={
                "x-yunxiao-token": self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            raise CodeupError(e.code, _extract_err_msg(body)) from e
        except Exception as e:
            raise CodeupError(None, f"Codeup 网络调用失败：{type(e).__name__} {e}") from e
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 某些网关对错误返回 HTML；原样抛出便于上层识别
            raise CodeupError(None, f"Codeup 返回非 JSON：{raw[:200]}") from None

    def _repo_path(self, repo_id: str) -> str:
        if not self.org_id:
            # Region 版（专属版）不需要 org；但中心版必须
            return f"/oapi/v1/codeup/repositories/{repo_id}"
        return f"/oapi/v1/codeup/organizations/{self.org_id}/repositories/{repo_id}"

    # ------------------------------------------------------------------ 公开
    def list_tree(self, repo_id: str, *, ref: str = "", recursive: bool = True) -> list[FileNode]:
        """列出仓库文件树。recursive=True 取全部文件（用于快速扫描）。"""
        ref = self._resolve_ref(repo_id, ref)
        # 取文件树也显式带 ref（与 get_blob 同一分支口径，避免「树是默认分支、内容是空 ref」错位）
        params: dict[str, Any] = {"type": "RECURSIVE" if recursive else "DIRECT", "ref": ref}
        data = self._get(self._repo_path(repo_id) + "/files/tree", params)
        nodes = _as_list(data)
        out: list[FileNode] = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            p = (n.get("path") or n.get("name") or "").strip()
            if not p:
                continue
            out.append(
                FileNode(
                    path=p,
                    name=n.get("name") or p.rsplit("/", 1)[-1],
                    type=(n.get("type") or "blob"),
                    is_lfs=bool(n.get("isLFS")),
                    mode=n.get("mode") or "",
                )
            )
        return out

    def compare(self, repo_id: str, *, from_ref: str, to_ref: str) -> list[dict[str, Any]]:
        """比较两个 ref 的文件差异（GetCompare，``.../compares?from=&to=``）。

        - ``from`` 为基线（旧），``to`` 为目标（新）：diffs 描述「从 from 到 to 引入的变更」；
        - 每个 diff 含 ``newFile / deletedFile / renamedFile / newPath / oldPath``；
        - ref 形似 commit SHA（7~40 位十六进制）则按 commit 解析，否则按 branch 解析
          （Codeup：分支/标签同名时必须显式传类型，commit 可不传）；
        - 返回 diffs 列表（仅含 dict 元素），空差异返回 []。
        """
        frm = self._resolve_ref(repo_id, from_ref)
        to = self._resolve_ref(repo_id, to_ref)
        params: dict[str, Any] = {"from": frm, "to": to}
        if not _looks_like_sha(frm):
            params["sourceType"] = "branch"
        if not _looks_like_sha(to):
            params["targetType"] = "branch"
        data = self._get(self._repo_path(repo_id) + "/compares", params)
        diffs = data.get("diffs") if isinstance(data, dict) else None
        return [d for d in _as_list(diffs) if isinstance(d, dict)]

    def get_blob(self, repo_id: str, file_path: str, *, ref: str = "") -> str:
        """取单个文件内容（Codeup GetFileBlobs）。

        正确形态：``GET /.../repositories/{repoId}/files/{URL编码的文件路径}?ref=``
        —— 文件路径是**路径段**（``{rest:.*}``），不是 ``filePath`` 查询参数，且
        路径里每个 ``/`` 都要 URL 编码；响应顶层 ``content`` 为 base64，需解码。

        ⚠️ 关键：Codeup 的 GetFileBlobs **强制要求** ``ref`` 查询参数（且不能为空 ref，
        空 ref 报 ``LastCommitForPath: empty Revision``）。因此：空 ref 自动解析为仓库
        默认分支（``_resolve_ref``，查仓库信息取 ``defaultBranch``，缓存），非空原样带上。
        """
        ref = self._resolve_ref(repo_id, ref)
        enc_path = urllib.parse.quote(file_path, safe="")
        params: dict[str, Any] = {"ref": ref}
        data = self._get(self._repo_path(repo_id) + "/files/" + enc_path, params)
        if isinstance(data, dict):
            content = data.get("content")
            if content is not None:
                try:
                    return base64.b64decode(content).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    return str(content)
        return ""

    def list_branches(
        self, repo_id: str, *, sort: str = "updated_desc", per_page: int = 50
    ) -> list[dict[str, Any]]:
        """列仓库分支（ListBranches，中心版 ``.../branches``）。

        每个分支带 ``commit``（最近一次提交：标题 / shortId / authoredDate 等），
        供前端「目标/基线 ref」下拉展示「分支名 + 最近提交时间」。仅 GET（只读）。
        """
        params: dict[str, Any] = {"page": 1, "perPage": per_page, "sort": sort}
        data = self._get(self._repo_path(repo_id) + "/branches", params)
        out: list[dict[str, Any]] = []
        for b in _as_list(data):
            if not isinstance(b, dict):
                continue
            commit = b.get("commit")
            if not isinstance(commit, dict):
                commit = {}
            out.append(
                {
                    "name": b.get("name", ""),
                    "default_branch": bool(b.get("defaultBranch")),
                    "protected": bool(b.get("protected")),
                    "commit": {
                        "short_id": commit.get("shortId", ""),
                        "title": commit.get("title") or commit.get("message", ""),
                        "message": commit.get("message", ""),
                        "author_name": commit.get("authorName", ""),
                        "authored_date": commit.get("authoredDate", ""),
                        "committed_date": commit.get("committedDate", ""),
                    },
                }
            )
        return out

    def list_commits(
        self, repo_id: str, *, ref_name: str, page: int = 1, per_page: int = 20
    ) -> list[dict[str, Any]]:
        """查指定分支/ref 的最近提交（ListCommits，``.../commits?refName=``）。

        供前端选定分支后「查看该分支提交内容」作为对比数据。仅 GET（只读）。
        """
        params: dict[str, Any] = {"refName": ref_name, "page": page, "perPage": per_page}
        data = self._get(self._repo_path(repo_id) + "/commits", params)
        out: list[dict[str, Any]] = []
        for c in _as_list(data):
            if not isinstance(c, dict):
                continue
            out.append(
                {
                    "id": c.get("id", ""),
                    "short_id": c.get("shortId", ""),
                    "title": c.get("title") or c.get("message", ""),
                    "message": c.get("message", ""),
                    "author_name": c.get("authorName", ""),
                    "authored_date": c.get("authoredDate", ""),
                    "committed_date": c.get("committedDate", ""),
                }
            )
        return out


def _looks_like_sha(ref: str) -> bool:
    """判断 ref 是否形如 git commit SHA（7~40 位十六进制）。"""
    r = (ref or "").strip()
    return 7 <= len(r) <= 40 and all(c in "0123456789abcdefABCDEF" for c in r)


def _insecure_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _extract_err_msg(body: str) -> str:
    try:
        d = json.loads(body)
        return d.get("errorMessage") or d.get("message") or body[:200]
    except Exception:  # noqa: BLE001
        return body[:200]


def _as_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("result", "data", "items", "list"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _extract_blob(data: Any) -> tuple[str, str]:
    """从 blob 响应取出 (content, encoding)。兼容多种包装形态。"""
    if isinstance(data, dict):
        # 常见：{result:{content, encoding}} 或 {content, encoding}
        inner = data.get("result") if isinstance(data.get("result"), dict) else data
        content = inner.get("content") if isinstance(inner, dict) else data.get("content")
        encoding = (
            inner.get("encoding") if isinstance(inner, dict) else data.get("encoding")
        ) or "text"
        if content is not None:
            return str(content), str(encoding)
    if isinstance(data, str):
        return data, "text"
    return "", "text"
