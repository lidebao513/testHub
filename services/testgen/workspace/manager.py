"""工作区管理：目标目录的解析、隔离校验、只读加固与状态查询。

安全底线（沿用 legacy 的实战教训）：
- **逃逸即阻断**：目标目录若落在工作区根之外（含 `..` 穿越、符号链接指向外部），一律拒绝；
- 目标目录自身是 git 仓库但顶层逃逸到祖先仓库时，交由 `engine.diff_tag` 阻断 diff；
- 只读加固后**实测**可写性，结果写进状态，不做"声明式安全"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.config import get_settings
from core.errors import WorkspaceError
from core.log import get_logger, log_extra
from workspace import readonly


log = get_logger(__name__)


@dataclass
class WorkspaceStatus:
    name: str
    path: str
    exists: bool
    is_git: bool
    readonly: bool
    writable_probe: bool
    file_count: int = 0
    detail: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "is_git": self.is_git,
            "readonly": self.readonly,
            "writable_probe": self.writable_probe,
            "file_count": self.file_count,
            "detail": self.detail,
            "extra": self.extra,
        }


def _safe_name(name: str) -> str:
    """校验工作区名：禁止路径分隔符与穿越片段。"""
    if not name or name.strip() == "":
        raise WorkspaceError("工作区名不能为空")
    if any(sep in name for sep in ("/", "\\", "..", "\x00")):
        raise WorkspaceError(f"工作区名非法：{name!r}")
    return name.strip()


class WorkspaceManager:
    """工作区根目录下的隔离管理。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else get_settings().workspace_root
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 路径解析
    def resolve(self, name: str, *, create: bool = False) -> Path:
        """解析工作区路径，并强制其落在根目录内（否则抛 WorkspaceError）。"""
        safe = _safe_name(name)
        target = (self.root / safe).resolve()
        self.assert_inside(target)
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def assert_inside(self, path: str | Path) -> None:
        """断言路径位于工作区根内（防 `..` 与符号链接逃逸）。"""
        resolved = Path(path).resolve()
        root = self.root.resolve()
        if resolved != root and root not in resolved.parents:
            raise WorkspaceError(f"路径逃逸出工作区根：{resolved}（根 {root}）")

    # ------------------------------------------------------------ 生命周期
    def prepare(self, name: str) -> Path:
        """准备一个可写工作区（拉取/更新代码前调用）。"""
        target = self.resolve(name, create=True)
        readonly.release(target)
        return target

    def lock(self, name: str, *, verify: bool = True) -> dict[str, object]:
        """分析完成后置只读并实测。"""
        target = self.resolve(name)
        if not target.exists():
            raise WorkspaceError(f"工作区不存在：{target}")
        result = readonly.harden(target, verify=verify)
        log.info("工作区已置只读", extra=log_extra(workspace=name, result=result))
        return result

    def unlock(self, name: str) -> dict[str, object]:
        """需要更新代码时解除只读。"""
        target = self.resolve(name)
        return readonly.release(target)

    # ------------------------------------------------------------ 状态
    def status(self, name: str) -> WorkspaceStatus:
        target = self.resolve(name)
        if not target.exists():
            return WorkspaceStatus(
                name=name,
                path=str(target),
                exists=False,
                is_git=False,
                readonly=False,
                writable_probe=False,
                detail="尚未创建工作区",
            )
        probe = readonly.probe_writable(target)
        files = sum(1 for p in target.rglob("*") if p.is_file())
        return WorkspaceStatus(
            name=name,
            path=str(target),
            exists=True,
            is_git=(target / ".git").exists(),
            readonly=readonly.is_readonly(target),
            writable_probe=probe.writable,
            file_count=files,
            detail=probe.detail,
        )

    def list_all(self) -> list[WorkspaceStatus]:
        out: list[WorkspaceStatus] = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir():
                out.append(self.status(child.name))
        return out

    def register_external(self, name: str, local_path: str, *, lock: bool = False) -> Path:
        """接入一个「已在磁盘上」的外部目录（不复制、不迁移），仅做校验与可选加固。"""
        target = Path(local_path).resolve()
        if not target.is_dir():
            raise WorkspaceError(f"目录不存在：{target}")
        if lock:
            readonly.harden(target)
        log.info("已登记外部目录", extra=log_extra(workspace=name, path=str(target)))
        return target
