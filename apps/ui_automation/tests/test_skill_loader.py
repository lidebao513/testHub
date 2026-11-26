"""PW-1 Skill 规范层守护测试：加载 / 渲染 / fail-open / 上限 / 注入链路。"""

from __future__ import annotations

import hashlib

from apps.ui_automation.skill_loader import (
    BLOCK_END,
    BLOCK_START_PREFIX,
    MAX_BODY_CHARS,
    SkillDocument,
    load_skill,
    render_skill_block,
)

_SAMPLE = """version: 9

# 规范标题

## 探页面纪律
先看快照，禁止编造索引。

## 失败诊断纪律
不得删除断言。
"""


def _write(tmp_path, name="UI_TEST_SKILL.md", content=_SAMPLE):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_skill
# ---------------------------------------------------------------------------


def test_load_skill_parses_version_and_body(tmp_path):
    _write(tmp_path)
    doc = load_skill(base_dir=tmp_path)
    assert doc is not None
    assert doc.name == "UI_TEST_SKILL"
    assert doc.version == "9"
    assert "探页面纪律" in doc.body
    assert "version:" not in doc.body, "版本头是元数据，不应重复进入 prompt 正文"
    assert doc.truncated is False


def test_load_skill_digest_matches_body(tmp_path):
    _write(tmp_path)
    doc = load_skill(base_dir=tmp_path)
    assert doc is not None
    expected = hashlib.sha256(doc.body.encode("utf-8")).hexdigest()[:12]
    assert doc.digest == expected
    assert len(doc.digest) == 12


def test_load_skill_missing_file_is_fail_open(tmp_path):
    assert load_skill(base_dir=tmp_path) is None


def test_load_skill_empty_body_is_fail_open(tmp_path):
    _write(tmp_path, content="version: 1\n\n   \n")
    assert load_skill(base_dir=tmp_path) is None


def test_load_skill_without_version_header_uses_unknown(tmp_path):
    _write(tmp_path, content="# 无版本头\n\n正文内容。\n")
    doc = load_skill(base_dir=tmp_path)
    assert doc is not None
    assert doc.version == "unknown"
    assert "无版本头" in doc.body


def test_load_skill_truncates_oversized_body(tmp_path):
    _write(tmp_path, content="version: 1\n\n" + "长" * (MAX_BODY_CHARS + 500))
    doc = load_skill(base_dir=tmp_path)
    assert doc is not None
    assert doc.truncated is True
    assert len(doc.body) == MAX_BODY_CHARS


def test_load_skill_rejects_path_traversal_name(tmp_path):
    assert load_skill("../evil", base_dir=tmp_path) is None
    assert load_skill("sub/evil", base_dir=tmp_path) is None
    assert load_skill("", base_dir=tmp_path) is None


def test_load_skill_uses_default_directory():
    """真实规范文件必须可被默认加载（防止文件被误删/改名后静默 fail-open）。"""
    doc = load_skill()
    assert doc is not None, "默认 skills/UI_TEST_SKILL.md 必须存在且可加载"
    assert doc.version == "1"
    assert doc.truncated is False


# ---------------------------------------------------------------------------
# render_skill_block
# ---------------------------------------------------------------------------


def test_render_skill_block_contains_version_digest_and_end():
    doc = SkillDocument(name="X", version="3", body="纪律正文", digest="abc123")
    block = render_skill_block(doc)
    assert block.startswith(f"{BLOCK_START_PREFIX} v3 (sha256:abc123) ===")
    assert "纪律正文" in block
    assert block.rstrip().endswith(BLOCK_END)


def test_render_skill_block_none_returns_empty():
    assert render_skill_block(None) == ""


def test_rendered_default_block_is_within_body_budget():
    """渲染出的整块也应有界，避免 token 成本失控。"""
    doc = load_skill()
    assert doc is not None
    assert len(render_skill_block(doc)) < MAX_BODY_CHARS + 200
