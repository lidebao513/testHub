"""对话模板（Dialogue Template）· 薄适配层。

为什么需要
----------
用户期望「开箱即填」：给一张带示例的模板，填完关键信息就能直接触发生成。本模块
只负责**把模板翻译回 `PipelineRequest` 形状的参数字典**，不碰任何能力层逻辑——
校验/落库/执行仍走既有 `service.app.build_opts_from_request` / `pipeline.run_pipeline`。

设计要点
--------
- **模板优先 + 自由文本兜底**：CLI/HTTP 都先给模板，填完即执行；自由文本走
  `engine.dialogue.agent.handle_text`（复用 `core.auto_input` 规则解析）。
- **条件显隐**：选了 url 才问 test_url/账号/密码/口令；选了 code 才问
  repo_url/local_path；mode=incremental 才问 base/target。
- **核心三问**：来源方式(code/url/code+url) · 全量vs新增(full/incremental) ·
  行为维度(正常/异常/安全/边界/性能，默认系统 DEFAULT_SCOPE)。
- **凭证红线**：产物 `redacted()` 一律掩码密码/口令，明文只在内层选项对象内存活。
- **纯模块、无网络、无 LLM**：可单测、可复现。

字段取值以 `core.enums` 为唯一真值（ALL_TP_TYPES / DEFAULT_SCOPE_LIST /
MODE_CHOICES），新增维度只改 enums，本模板自动跟随。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.enums import ALL_TP_TYPES, DEFAULT_SCOPE_LIST, MODE_CHOICES


# ============================================================================
# 可见性规则（可序列化，供 HTTP 前端条件渲染）
# ============================================================================
@dataclass
class VisibilityRule:
    """字段可见性判定（轻量、可 JSON 化）。

    op:
    - ``contains``：filled[field] 为字符串且包含 value（用于 source_kind=code+url）
    - ``equals``：filled[field] == value（用于 mode==incremental）
    - ``truthy``：filled[field] 非空（用于「填了某值才追问」）
    """

    field: str
    op: str
    value: str = ""

    def test(self, filled: dict[str, Any]) -> bool:
        v = filled.get(self.field)
        if self.op == "contains":
            return isinstance(v, str) and self.value in v
        if self.op == "equals":
            return v == self.value
        if self.op == "truthy":
            return bool(v)
        return True

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "op": self.op, "value": self.value}


# ============================================================================
# 模板字段
# ============================================================================
@dataclass
class TemplateField:
    name: str
    label: str
    kind: str  # select | multiselect | text | bool
    required: bool = False
    options: list[str] = field(default_factory=list)
    default: Any = ""
    help: str = ""
    visible_when: VisibilityRule | None = None
    group: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "options": list(self.options),
            "default": self.default,
            "help": self.help,
            "group": self.group,
            "visible_when": self.visible_when.to_dict() if self.visible_when else None,
        }


# ============================================================================
# 解析结果（模板校验后的产物）
# ============================================================================
@dataclass
class DialoguePlan:
    """模板/文本解析后的执行计划。

    - ``params``：PipelineRequest 形状的参数字典（含明文凭证，仅供内层内存使用）；
    - ``redacted``：可安全打印/返回的掩码视图；
    - ``intent``：template / text / invalid / unknown；
    - ``errors``：非空即校验失败，``ok`` 为 False。
    """

    params: dict[str, Any]
    redacted: dict[str, Any]
    intent: str
    errors: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ============================================================================
# 模板定义
# ============================================================================
def default_dialogue_template() -> DialogueTemplate:
    """构造默认对话模板（覆盖「生成」这一步，不涉及执行）。"""
    fields = [
        TemplateField(
            name="source_kind",
            label="来源方式（必填）",
            kind="select",
            required=True,
            options=["code", "url", "code+url"],
            default="code+url",
            help="code=代码目录/仓库；url=被测环境地址；code+url=两者都要",
            group="来源",
        ),
        TemplateField(
            name="mode",
            label="生成模式（必填）",
            kind="select",
            required=True,
            options=list(MODE_CHOICES),
            default="full",
            help="full=全量扫描；incremental=仅 base..target 变更部分（标「更新」）",
            group="生成",
        ),
        TemplateField(
            name="scopes",
            label="行为维度（可多选）",
            kind="multiselect",
            options=list(ALL_TP_TYPES),
            default=list(DEFAULT_SCOPE_LIST),
            help="默认覆盖系统 DEFAULT_SCOPE；性能默认不纳入",
            group="生成",
        ),
        # —— 选了 url 才显隐 ——
        TemplateField(
            name="test_url",
            label="被测环境地址",
            kind="text",
            required=True,
            help="走运行时 UI 发现的目标地址（含协议头）",
            visible_when=VisibilityRule("source_kind", "contains", "url"),
            group="地址通道",
        ),
        TemplateField(
            name="login_user",
            label="登录账号",
            kind="text",
            help="更推荐用环境变量注入；此处仅本次运行使用，不落库",
            visible_when=VisibilityRule("source_kind", "contains", "url"),
            group="地址通道",
        ),
        TemplateField(
            name="login_password",
            label="登录密码",
            kind="text",
            help="掩码处理，绝不回显/落库；推荐环境变量",
            visible_when=VisibilityRule("source_kind", "contains", "url"),
            group="地址通道",
        ),
        TemplateField(
            name="login_otp",
            label="动态口令",
            kind="text",
            help="二次验证动态码；掩码处理",
            visible_when=VisibilityRule("source_kind", "contains", "url"),
            group="地址通道",
        ),
        # —— 选了 code 才显隐 ——
        TemplateField(
            name="repo_url",
            label="仓库地址",
            kind="text",
            help="Git 仓库地址（F1：先取码再走后续阶段）",
            visible_when=VisibilityRule("source_kind", "contains", "code"),
            group="代码通道",
        ),
        TemplateField(
            name="local_path",
            label="本地代码目录",
            kind="text",
            help="已存在的本地代码目录（与 repo_url 至少给一个）",
            visible_when=VisibilityRule("source_kind", "contains", "code"),
            group="代码通道",
        ),
        # —— mode=incremental 才显隐 ——
        TemplateField(
            name="base",
            label="增量基线 ref",
            kind="text",
            required=True,
            help="增量模式基线 ref（如某次提交哈希 / 分支）",
            visible_when=VisibilityRule("mode", "equals", "incremental"),
            group="增量",
        ),
        TemplateField(
            name="target",
            label="增量目标 ref",
            kind="text",
            required=True,
            help="增量模式目标 ref；给 worktree 表示与当前工作区比较",
            visible_when=VisibilityRule("mode", "equals", "incremental"),
            group="增量",
        ),
        # —— 可选 ——
        TemplateField(
            name="project_name",
            label="项目名",
            kind="text",
            help="缺省用目录名或被测主机名",
            group="可选",
        ),
        TemplateField(
            name="llm_enhance",
            label="启用 LLM 增强",
            kind="bool",
            default=False,
            help="是否在生成环节启用 LLM 增强通道（受配置开关约束）",
            group="可选",
        ),
        TemplateField(
            name="auto_input",
            label="统一智能输入框（自由文本兜底）",
            kind="text",
            help="一段混排文本（地址+账号+密码+口令+路径），解析后合并进参数",
            group="可选",
        ),
    ]
    return DialogueTemplate(fields=fields)


# ============================================================================
# 模板主体
# ============================================================================
class DialogueTemplate:
    """声明式对话模板：渲染 / 条件显隐 / 校验 / 解析。"""

    def __init__(self, fields: list[TemplateField]) -> None:
        self.fields = fields
        self._by_name = {f.name: f for f in fields}

    # ---- 显隐 ----
    def visible_fields(self, filled: dict[str, Any]) -> list[str]:
        """返回当前 filled 状态下应当可见的字段名列表。"""
        return [
            f.name for f in self.fields if f.visible_when is None or f.visible_when.test(filled)
        ]

    def field(self, name: str) -> TemplateField | None:
        return self._by_name.get(name)

    # ---- 校验 + 解析 ----
    def _normalize_scopes(self, raw: dict[str, Any], errors: list[str]) -> list[str]:
        scopes = raw.get("scopes") or []
        if isinstance(scopes, str):
            scopes = [s.strip() for s in scopes.replace("，", ",").split(",") if s.strip()]
        invalid = set(scopes) - set(ALL_TP_TYPES)
        if invalid:
            errors.append(f"非法行为维度：{sorted(invalid)}，允许 {list(ALL_TP_TYPES)}")
        if not scopes:
            scopes = list(DEFAULT_SCOPE_LIST)
        return list(scopes)

    def validate_and_parse(self, filled: dict[str, Any]) -> DialoguePlan:
        """校验已填值，成功则产出 PipelineRequest 形状的参数 dict。"""
        errors: list[str] = []
        raw = filled or {}

        source_kind = (raw.get("source_kind") or "").strip()
        if source_kind not in ("code", "url", "code+url"):
            errors.append("source_kind 必填，取值为 code / url / code+url")

        mode = (raw.get("mode") or "full").strip() or "full"
        if mode not in MODE_CHOICES:
            errors.append(f"mode 非法：{mode!r}，允许 {list(MODE_CHOICES)}")

        scopes = self._normalize_scopes(raw, errors)

        test_url = (raw.get("test_url") or "").strip()
        login_user = (raw.get("login_user") or "").strip()
        login_password = raw.get("login_password") or ""
        login_otp = raw.get("login_otp") or ""

        repo_url = (raw.get("repo_url") or "").strip()
        local_path = (raw.get("local_path") or "").strip()

        base = (raw.get("base") or "").strip() or None
        target = (raw.get("target") or "").strip() or None
        if "url" in source_kind and not test_url:
            errors.append("来源含 url 时 test_url 必填")
        if "code" in source_kind and not repo_url and not local_path:
            errors.append("来源含 code 时 repo_url / local_path 至少给一个")
        if mode == "incremental":
            if not base:
                errors.append("mode=incremental 时 base 必填")
            if not target:
                errors.append("mode=incremental 时 target 必填")

        project_name = (raw.get("project_name") or "").strip()
        llm_enhance = bool(raw.get("llm_enhance", False))
        auto_input = (raw.get("auto_input") or "").strip()

        if errors:
            return DialoguePlan(params={}, redacted={}, intent="invalid", errors=errors)

        params: dict[str, Any] = {
            "local_path": local_path,
            "repo_url": repo_url,
            "project_name": project_name,
            "mode": mode,
            "base": base,
            "target": target,
            "scopes": scopes,
            "test_url": test_url,
            "login_user": login_user,
            "login_password": login_password,
            "login_otp": login_otp,
            "llm_enhance": llm_enhance,
            "auto_input": auto_input,
            # 生成-only 红线：对话入口只生成用例，不触发执行
            "execute": False,
            "exec_url": "",
            "persist": True,
            "include_business": True,
            "extract_pages": True,
        }
        redacted = dict(params)
        redacted["login_password"] = "***" if login_password else ""
        redacted["login_otp"] = "***" if login_otp else ""
        return DialoguePlan(params=params, redacted=redacted, intent="template", errors=[])

    # ---- 渲染：文本模板（CLI 可读、可填） ----
    def render_template_text(self) -> str:
        lines: list[str] = []
        lines.append("# 测试用例生成 · 对话模板")
        lines.append("# 填写后运行：testgen chat --template <本文件>")
        lines.append("# 说明：source_kind=code/url/code+url；mode=full/incremental；")
        lines.append("#       scopes 空格分隔，可多选（默认 正常 异常 安全 边界）")
        lines.append("")
        last_group = None
        for f in self.fields:
            if f.group != last_group:
                lines.append(f"# ===== {f.group} =====")
                last_group = f.group
            lines.append(f"# {f.label}" + ("（必填）" if f.required else ""))
            if f.help:
                lines.append(f"#   {f.help}")
            if f.kind == "select":
                opts = " / ".join(f.options)
                lines.append(f"#   可选：{opts}")
                lines.append(f"{f.name}={f.default}")
            elif f.kind == "multiselect":
                opts = " / ".join(f.options)
                lines.append(f"#   可选：{opts}")
                lines.append(
                    f"{f.name}={' '.join(f.default) if isinstance(f.default, list) else f.default}"
                )
            elif f.kind == "bool":
                lines.append(f"{f.name}={'true' if f.default else 'false'}")
            else:
                lines.append(f"{f.name}=")
            lines.append("")
        return "\n".join(lines)

    # ---- 渲染：JSON schema（HTTP GET /api/v1/dialogue/template） ----
    def render_template_json(self) -> dict[str, Any]:
        return {
            "source_kinds": ["code", "url", "code+url"],
            "modes": list(MODE_CHOICES),
            "all_scopes": list(ALL_TP_TYPES),
            "default_scopes": list(DEFAULT_SCOPE_LIST),
            "fields": [f.to_dict() for f in self.fields],
        }

    # ---- 解析：文本模板 → filled dict（CLI --template 文件） ----
    def parse_filled_text(self, text: str) -> dict[str, Any]:
        """解析 `key=value` 形式的填写文本（# 开头为注释）。"""
        filled: dict[str, Any] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            f = self._by_name.get(key)
            if f is None:
                continue
            if f.kind == "multiselect":
                filled[key] = [s for s in val.replace("，", " ").split() if s]
            elif f.kind == "bool":
                filled[key] = val.lower() in ("true", "1", "是", "yes", "y")
            else:
                filled[key] = val
        return filled
