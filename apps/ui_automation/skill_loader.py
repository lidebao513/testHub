"""PW-1：UI 自动化 Skill 规范层加载器（纯函数，不依赖 Django / browser-use）。

规范文件（默认 ``skills/UI_TEST_SKILL.md``）是 AI 执行纪律的单一来源，
每次执行前由 ``ai_base.run_task`` 注入到 prompt。设计要点：

- **fail-open**：文件缺失 / 空内容 / 读取异常一律返回 ``None`` 并告警，
  绝不让规范加载失败阻断用例执行；
- **可核验**：返回的 ``digest`` 是正文 sha256 前 12 位，渲染块带版本与摘要，
  便于在日志 / 测试里断言「加载了哪一版规范」；
- **有上限**：正文超过 ``MAX_BODY_CHARS`` 截断并告警，防止 token 成本失控；
- **无副作用**：只读文件，不写库、不改配置。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("django")

DEFAULT_SKILL_NAME = "UI_TEST_SKILL"
MAX_BODY_CHARS = 4000
BLOCK_START_PREFIX = "=== UI_TEST_SKILL"
BLOCK_END = "=== END UI_TEST_SKILL ==="
_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SkillDocument:
    """一份已解析的规范文档。"""

    name: str
    version: str
    body: str
    digest: str
    truncated: bool = False


def _default_base_dir() -> Path:
    return Path(__file__).resolve().parent / "skills"


def load_skill(
    name: str = DEFAULT_SKILL_NAME,
    *,
    base_dir: Path | str | None = None,
) -> SkillDocument | None:
    """加载并解析规范文件；任何异常都降级为 ``None``（不阻断执行）。

    ``name`` 仅作为文件名（自动补 ``.md``），不允许路径分隔符，
    防止调用方传入任意路径。``base_dir`` 主要用于测试注入临时目录。
    """
    if not name or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        logger.warning("Skill 名称非法，跳过加载: %r", name)
        return None

    directory = Path(base_dir) if base_dir is not None else _default_base_dir()
    path = directory / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        logger.warning("Skill 规范加载失败，跳过（fail-open）: %s (%s)", path, exc)
        return None

    match = _VERSION_RE.search(raw)
    version = match.group(1) if match else "unknown"
    # 版本头本身是元数据，不进入给 Agent 的正文；避免重复告知模型。
    body = raw[: match.start()] + raw[match.end() :] if match else raw
    body = body.strip()
    if not body:
        logger.warning("Skill 规范正文为空，跳过加载: %s", path)
        return None

    truncated = len(body) > MAX_BODY_CHARS
    if truncated:
        logger.warning(
            "Skill 规范正文超长（%d > %d），已截断: %s",
            len(body),
            MAX_BODY_CHARS,
            path,
        )
        body = body[:MAX_BODY_CHARS]

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return SkillDocument(name=name, version=version, body=body, digest=digest, truncated=truncated)


def render_skill_block(doc: SkillDocument | None) -> str:
    """渲染可核验的 prompt 块；``doc`` 为 ``None`` 时返回空串。"""
    if doc is None:
        return ""
    return (
        f"{BLOCK_START_PREFIX} v{doc.version} (sha256:{doc.digest}) ===\n{doc.body}\n{BLOCK_END}\n"
    )
