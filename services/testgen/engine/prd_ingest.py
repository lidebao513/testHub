"""引擎 · 模块八（P2）：PRD 通道（需求文档 → 结构化需求 → 测试点候选）。

设计定位：在「代码静态分析」之外，提供第二条来源通道——把 PRD / 接口文档 /
OpenAPI 规格解析为结构化需求，与代码提取出的功能点做对齐，弥补「代码里看不出
业务意图」的盲区（见 qa-test-points 技能 §十一）。

已实现（2026-09-14）：
- `ingest_prd` 真实解析 **Markdown**（按二/三级标题切分需求）与 **OpenAPI**
  （YAML/JSON，按 `paths` 逐个 operation 展开）；格式可显式指定或按扩展名推断。
- `requirements_to_test_points` 把需求端点对齐到代码功能点，派生出「业务规则」维度的
  测试点（`origin=prd`、`review_status=pending` 待人工复核）；未对齐的需求记入 `PrdDoc.notes`。

依赖方向严格向下（只 import core），不感知 service / cli。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.contracts import TestPoint, tp_id_of, verify_layer_of_ftype
from core.enums import (
    HTTP_METHODS,
    PRD_FORMAT_AUTO,
    PRD_FORMAT_CHOICES,
    PRD_FORMAT_MARKDOWN,
    PRD_FORMAT_OPENAPI,
    Dimension,
    ReviewStatus,
    Tag,
    TPType,
)
from core.errors import ConfigError, EngineError


# 需求编号：`R-` + md5(来源|标题) 前 6 位——绑内容指纹而非位置，同一份文档重复跑编号不变。
_RID_PREFIX = "R-"
_RID_HASH_LEN = 6

# Markdown：二级 / 三级标题视为「一条需求」（一级标题是文档标题）
_MD_REQ_LEVELS: tuple[int, ...] = (2, 3)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# 端点抽取：优先 `METHOD /path`，其次裸 `/path`
_METHODS_ALT = "|".join(HTTP_METHODS)
_ENDPOINT_RE = re.compile(rf"\b({_METHODS_ALT})\s+(/[A-Za-z0-9_{{}}/._\-]*)")
_PATH_ONLY_RE = re.compile(r"(?<![\w/])(/[A-Za-z0-9_][A-Za-z0-9_{{}}/._\-]*)")

# Markdown 标签行：`标签：甲、乙` / `tags: a, b`
_TAG_LINE_RE = re.compile(r"^\s*(?:标签|tags?)\s*[:：]\s*(.+)$", re.IGNORECASE)
_TAG_SPLIT_RE = re.compile(r"[、,，;；\s]+")


@dataclass
class PrdRequirement:
    """一条结构化需求。"""

    rid: str
    title: str
    description: str = ""
    source: str = ""  # 来源文件 / 章节
    endpoints: list[str] = field(default_factory=list)  # 关联接口（`METHOD /path` 或裸 path）
    tags: list[str] = field(default_factory=list)
    matched_fp_id: str = ""  # 对齐到的功能点 id（空 = 未对齐，待人工确认）


@dataclass
class PrdDoc:
    """一份 PRD 的解析结果。"""

    source: str
    fmt: str
    requirements: list[PrdRequirement] = field(default_factory=list)
    raw: str = ""
    notes: list[str] = field(default_factory=list)  # 解析 / 对齐过程中的提示

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fmt": self.fmt,
            "requirements": [asdict(r) for r in self.requirements],
            "raw": self.raw,
            "notes": self.notes,
        }


@dataclass
class PrdIngestOptions:
    """PRD 解析选项。"""

    fmt: str = PRD_FORMAT_AUTO
    encoding: str = "utf-8"


def _resolve_format(source: str, fmt: str) -> str:
    """按显式 fmt 或文件扩展名推断 PRD 格式；非法值抛 ConfigError。"""
    if fmt != PRD_FORMAT_AUTO:
        if fmt not in PRD_FORMAT_CHOICES:
            raise ConfigError(f"PRD 格式非法：{fmt!r}，允许 {PRD_FORMAT_CHOICES}")
        return fmt
    lowered = source.lower()
    if lowered.endswith((".json", ".yaml", ".yml")):
        return PRD_FORMAT_OPENAPI
    return PRD_FORMAT_MARKDOWN


def _rid_of(source: str, title: str) -> str:
    """需求稳定编号：内容指纹（同 contracts 的哈希策略，跨运行不变）。"""
    key = f"{source}|{title}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:_RID_HASH_LEN]  # nosec B324  # 非安全用途：仅生成稳定 ID 指纹
    return _RID_PREFIX + digest


def _read_text(source: str, encoding: str) -> str:
    """读取 PRD 文件；不存在 / 不可读 / 编码不符一律抛 EngineError（带上下文，不吞异常）。"""
    path = Path(source)
    if not path.is_file():
        raise EngineError(f"PRD 文件不存在：{source}")
    try:
        return path.read_text(encoding=encoding)
    except UnicodeDecodeError as exc:
        raise EngineError(f"PRD 文件编码不是 {encoding}：{source}") from exc
    except OSError as exc:
        raise EngineError(f"PRD 文件读取失败：{source}（{exc}）") from exc


def _endpoints_in(text: str) -> list[str]:
    """抽取文本中的接口引用（去重，保持出现顺序）：先 `METHOD /path`，再裸 `/path`。"""
    out: list[str] = []
    seen: set[str] = set()
    for match in _ENDPOINT_RE.finditer(text):
        token = f"{match.group(1).upper()} {match.group(2)}"
        if token not in seen:
            seen.add(token)
            out.append(token)
    for match in _PATH_ONLY_RE.finditer(text):
        token = match.group(1)
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _tags_in(text: str) -> list[str]:
    """从 `标签：…` / `tags: …` 行解析标签（逗号 / 顿号 / 分号 / 空白分隔）。"""
    tags: list[str] = []
    for line in text.splitlines():
        matched = _TAG_LINE_RE.match(line)
        if not matched:
            continue
        for part in _TAG_SPLIT_RE.split(matched.group(1)):
            cleaned = part.strip()
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
    return tags


def _parse_markdown(text: str, source: str) -> list[PrdRequirement]:
    """Markdown → 需求列表：二/三级标题切分，标题下正文作为描述。"""
    reqs: list[PrdRequirement] = []

    def emit(title: str | None, body: list[str]) -> None:
        if not title or not body:
            return
        joined = "\n".join(body).strip()
        reqs.append(
            PrdRequirement(
                rid=_rid_of(source, title),
                title=title,
                description=joined,
                source=source,
                endpoints=_endpoints_in(joined),
                tags=_tags_in(joined),
            )
        )

    title: str | None = None
    body: list[str] = []
    for raw in text.splitlines():
        matched = _MD_HEADING_RE.match(raw)
        level = len(matched.group(1)) if matched else 0
        if matched and level in _MD_REQ_LEVELS:
            emit(title, body)
            title, body = matched.group(2), []
            continue
        if matched and 0 < level < min(_MD_REQ_LEVELS):
            # 一级标题（文档标题）：不单列需求，仅收尾上一节。
            emit(title, body)
            title, body = None, []
            continue
        body.append(raw)
    emit(title, body)
    return reqs


def _parse_openapi(text: str, source: str) -> list[PrdRequirement]:
    """OpenAPI（YAML 或 JSON）→ 需求列表：每个 `path` 下的 operation 即一条需求。"""
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"OpenAPI 解析失败（YAML/JSON 语法错误）：{source}") from exc
    if not isinstance(spec, dict):
        raise ConfigError(f"OpenAPI 根节点必须是对象：{source}")

    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        raise ConfigError(f"OpenAPI 的 paths 必须是对象：{source}")

    reqs: list[PrdRequirement] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            op = item.get(method.lower())
            if not isinstance(op, dict):
                continue
            endpoint = f"{method} {path}"
            summary = str(op.get("summary") or op.get("operationId") or "").strip()
            reqs.append(
                PrdRequirement(
                    rid=_rid_of(source, endpoint),
                    title=summary or endpoint,
                    description=str(op.get("description") or "").strip(),
                    source=source,
                    endpoints=[endpoint],
                    tags=[str(t) for t in (op.get("tags") or [])],
                )
            )
    return reqs


def ingest_prd(source: str, options: PrdIngestOptions | None = None) -> PrdDoc:
    """解析 PRD 文档为结构化需求（Markdown 或 OpenAPI，真实实现）。"""
    opts = options or PrdIngestOptions()
    fmt = _resolve_format(source, opts.fmt)
    text = _read_text(source, opts.encoding)
    reqs = (
        _parse_openapi(text, source) if fmt == PRD_FORMAT_OPENAPI else _parse_markdown(text, source)
    )
    doc = PrdDoc(source=source, fmt=fmt, requirements=reqs, raw=text)
    if not reqs:
        doc.notes.append(
            f"未从 {source} 解析出任何需求（格式 {fmt}）；请检查文档结构，"
            f"或用 fmt 显式指定格式（markdown / openapi）"
        )
    return doc


def _fp_lookup(fps: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """建立功能点索引：按完整机器键（`METHOD /path`）与按 path 各一份。"""
    by_name: dict[str, Any] = {}
    by_path: dict[str, Any] = {}
    for fp in fps:
        name = str(getattr(fp, "name", "") or "")
        if not name:
            continue
        by_name.setdefault(name, fp)
        if " " in name:
            by_path.setdefault(name.split(" ", 1)[1], fp)
    return by_name, by_path


def _name_parts(fp: Any) -> tuple[str, str]:
    """功能点机器键 → (方法/标记, 定位)；`POST /x` → ("POST", "/x")。"""
    name = str(getattr(fp, "name", "") or "")
    if " " in name:
        head, tail = name.split(" ", 1)
        return head, tail
    return "", name


def _match_fp(endpoints: list[str], by_name: dict[str, Any], by_path: dict[str, Any]) -> Any | None:
    """把需求的端点列表对齐到某个功能点；对齐不上返回 None。"""
    for token in endpoints:
        cleaned = token.strip()
        if not cleaned:
            continue
        if cleaned in by_name:
            return by_name[cleaned]
        _, path = _name_parts_for_token(cleaned)
        if path in by_path:
            return by_path[path]
    return None


def _name_parts_for_token(token: str) -> tuple[str, str]:
    """端点 token → (方法, path)；`POST /x` → ("POST", "/x")，裸 path 原样返回。"""
    if " " in token:
        head, tail = token.split(" ", 1)
        if head.isupper():
            return head, tail
    return "", token


def requirements_to_test_points(prd: PrdDoc, fps: list[Any]) -> list[TestPoint]:
    """把结构化需求对齐到功能点，产出「业务规则」维度的补充测试点（真实实现）。

    规则：
    - 需求端点能对齐到某个功能点 → 派生一条 `正常 / 业务规则` 测试点（`origin=prd`，
      `review_status=pending` 需人工复核，`evidence` 记录需求编号）；
    - 对齐不上（代码里没有对应能力）→ 不臆造测试点，记入 `prd.notes` 待人工确认。
    """
    by_name, by_path = _fp_lookup(fps)
    out: list[TestPoint] = []
    unmatched: list[str] = []
    for idx, req in enumerate(prd.requirements):
        fp = _match_fp(req.endpoints, by_name, by_path)
        if fp is None:
            unmatched.append(req.rid)
            continue
        fp_id = str(getattr(fp, "fp_id", "") or "")
        req.matched_fp_id = fp_id
        method, area = _name_parts(fp)
        out.append(
            TestPoint(
                tp_id=tp_id_of(
                    fp_id,
                    TPType.NORMAL.value,
                    area=area,
                    method=method,
                    dimension=Dimension.BIZ_RULE.value,
                    ordinal=idx,  # 需求在文档中的固定序位，保证 tp_id 稳定且唯一
                ),
                fp_contract_id=fp_id,
                category=TPType.NORMAL.value,
                module=str(getattr(fp, "module", "") or ""),
                title=f"[{TPType.NORMAL.value}] {req.title}",
                semantic=f"按需求文档验证业务规则：{req.title}",
                source=req.source or prd.source,
                method=method,
                area=area,
                expect="功能行为与需求文档描述一致，业务规则正确生效；无遗漏或与文档相悖的表现",
                dimension=Dimension.BIZ_RULE.value,
                tag=Tag.FULL.value,
                review_status=ReviewStatus.PENDING.value,
                verify_layer=verify_layer_of_ftype(str(getattr(fp, "ftype", "") or "")),
                evidence=[f"prd:{req.rid}"],
                confidence=1.0,
                origin="prd",
                unverified=True,  # 需求驱动，需人工复核后才可执行
            )
        )
    if unmatched:
        prd.notes.append(
            f"{len(unmatched)} 条需求未对齐到代码功能点（待人工确认）：{unmatched[:5]}"
        )
    return out
