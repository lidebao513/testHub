"""只读工作区：落盘即置只读，并**实测**能否写入（不靠"假设生效"）。

为什么要"实测"：
- POSIX 下 `chmod -R a-w` 对目录生效即不可新建文件；
- Windows 下 `os.chmod` 只影响文件的只读属性，**目录不生效**，
  ACL 才是硬手段。因此本模块提供 `probe_writable()` 做真实写入探测，
  把"是否真的只读"变成可验证的事实，而不是一句声明。

生产建议（见 ARCHITECTURE §只读加固）：
- 容器内以 `:ro` 挂载代码区 + 非 root 用户运行；
- 输出目录与只读区**物理分离**。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from core.log import get_logger, log_extra


log = get_logger(__name__)

DIR_MODE_READONLY = 0o555
DIR_MODE_WRITABLE = 0o755
FILE_MODE_READONLY = 0o444
FILE_MODE_WRITABLE = 0o644


@dataclass
class ProbeResult:
    """写入探测结果。"""

    writable: bool
    path: str
    detail: str = ""


def _iter_entries(root: Path) -> list[Path]:
    return [root, *sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)]


def make_readonly(root: str | Path) -> int:
    """把目录树置为只读（自底向上，先文件后目录）。返回处理条目数。"""
    base = Path(root)
    if not base.exists():
        return 0
    count = 0
    for entry in _iter_entries(base):
        try:
            if entry.is_dir():
                os.chmod(entry, DIR_MODE_READONLY)
            else:
                os.chmod(entry, FILE_MODE_READONLY)
            count += 1
        except OSError as exc:
            log.warning("置只读失败", extra=log_extra(path=str(entry), err=str(exc)))
    return count


def make_writable(root: str | Path) -> int:
    """解除只读（自顶向下，先目录后文件）。返回处理条目数。"""
    base = Path(root)
    if not base.exists():
        return 0
    count = 0
    for entry in sorted(base.rglob("*"), key=lambda p: len(p.parts)):
        try:
            if entry.is_dir():
                os.chmod(entry, DIR_MODE_WRITABLE)
            else:
                os.chmod(entry, FILE_MODE_WRITABLE)
            count += 1
        except OSError as exc:
            log.warning("解除只读失败", extra=log_extra(path=str(entry), err=str(exc)))
    try:
        os.chmod(base, DIR_MODE_WRITABLE)
        count += 1
    except OSError:
        pass
    return count


def is_readonly(root: str | Path) -> bool:
    """目录权限位是否已去掉写位（仅反映权限位，不代表 Windows 下真实可写性）。"""
    base = Path(root)
    if not base.exists():
        return False
    mode = stat.S_IMODE(base.stat().st_mode)
    return not (mode & stat.S_IWUSR)


def probe_writable(root: str | Path, *, filename: str = ".write_probe") -> ProbeResult:
    """真实写入探测：尝试创建并删除一个探针文件。"""
    base = Path(root)
    if not base.is_dir():
        return ProbeResult(writable=False, path=str(base), detail="目录不存在")
    probe = base / filename
    try:
        probe.write_text("probe", encoding="utf-8")
    except OSError as exc:
        return ProbeResult(writable=False, path=str(probe), detail=f"{type(exc).__name__}")
    try:
        probe.unlink()
    except OSError:
        pass
    return ProbeResult(writable=True, path=str(probe), detail="写入成功（说明仍可写）")


def harden(root: str | Path, *, verify: bool = True) -> dict[str, object]:
    """置只读 + （可选）验证。返回结构化结果，便于写进验收报告。"""
    base = Path(root)
    changed = make_readonly(base)
    result: dict[str, object] = {
        "path": str(base),
        "entries_chmod": changed,
        "mode_bit_readonly": is_readonly(base),
    }
    if verify:
        probe = probe_writable(base)
        result["writable_probe"] = probe.writable
        result["probe_detail"] = probe.detail
        result["enforced"] = not probe.writable
    return result


def release(root: str | Path) -> dict[str, object]:
    """解除只读（用于需要更新代码时）。"""
    base = Path(root)
    changed = make_writable(base)
    probe = probe_writable(base)
    return {"path": str(base), "entries_chmod": changed, "writable": probe.writable}
