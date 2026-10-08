"""代码仓库连接测试探针：判定类型（git / local）并做只读连通性检查。

安全约定：
- git 探活用 ``git ls-remote``（只读，不拉取、不落盘）；
- 禁止交互式凭据提示（``GIT_TERMINAL_PROMPT=0`` + SSH ``BatchMode=yes``），失败即返回原因；
- 凭据复用本机 git 凭据链（SSH key / credential helper），平台**不存凭据**。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from core.errors import ValidationError


GIT_TIMEOUT_SECONDS = 30


def is_git_url(url: str) -> bool:
    """按常见形态判定是否 git 仓库地址（git@ / ssh:// / http(s)://*.git / http(s)）。"""
    u = url.strip()
    if u.startswith(("git@", "ssh://")):
        return True
    if u.endswith(".git"):
        return True
    return u.startswith(("http://", "https://"))


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    # 关键：禁止凭据交互提示，否则无凭据仓库会把请求挂死到超时。
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"
    return env


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _translate_git_error(reason: str) -> str:
    low = reason.lower()
    if "permission denied (publickey)" in low:
        return "SSH 认证失败：本机未配置该仓库的 SSH key"
    if "authentication failed" in low or "403" in low:
        return "认证失败：仓库需要凭据（平台不存凭据，请先在本机 git 凭据链配置）"
    if "not found" in low or "does not appear" in low:
        return "仓库不存在或无权访问"
    if "could not resolve host" in low:
        return "域名解析失败：检查网络或仓库地址拼写"
    return reason


def _probe_git(url: str, ref: str, started: float) -> dict[str, Any]:
    """git 只读探活：``git ls-remote``（给出 ref 时顺带校验其存在）。"""
    git = shutil.which("git")
    if not git:
        return {
            "ok": False,
            "kind": "git",
            "message": "本机未安装 git，无法测试远程仓库",
            "duration_ms": _ms(started),
        }
    cmd = [git, "ls-remote", url]
    if ref:
        cmd.append(ref)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "kind": "git",
            "message": f"连接超时（>{GIT_TIMEOUT_SECONDS}s）：仓库不可达或需要凭据",
            "duration_ms": _ms(started),
        }
    if proc.returncode == 0 and proc.stdout.strip():
        heads = len(proc.stdout.strip().splitlines())
        msg = f"连接成功（读到 {heads} 个 ref）"
        if ref and ref not in proc.stdout:
            msg += f"；注意：未在输出中找到 ref「{ref}」"
        return {"ok": True, "kind": "git", "message": msg, "duration_ms": _ms(started)}
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    raw = err[-1] if err else f"exit={proc.returncode}"
    return {
        "ok": False,
        "kind": "git",
        "message": f"连接失败：{_translate_git_error(raw)[:200]}",
        "duration_ms": _ms(started),
    }


def _probe_local(target: str, started: float) -> dict[str, Any]:
    """本地目录探活：存在且非空即通过（顺带报告是否 git 工作区）。"""
    path = Path(target)
    if not path.exists():
        return {
            "ok": False,
            "kind": "local",
            "message": "本地目录不存在",
            "duration_ms": _ms(started),
        }
    if not path.is_dir():
        return {
            "ok": False,
            "kind": "local",
            "message": "地址存在但不是目录",
            "duration_ms": _ms(started),
        }
    count = sum(1 for _ in path.iterdir())
    if not count:
        return {
            "ok": False,
            "kind": "local",
            "message": "本地目录为空",
            "duration_ms": _ms(started),
        }
    has_git = (path / ".git").exists()
    return {
        "ok": True,
        "kind": "local",
        "message": f"目录可用（顶层 {count} 项{'，git 工作区' if has_git else ''}）",
        "duration_ms": _ms(started),
    }


def probe_repo(url: str, ref: str = "") -> dict[str, Any]:
    """只读探活一个仓库地址，返回 {ok, kind, message, duration_ms}。"""
    target = (url or "").strip()
    ref = (ref or "").strip()
    if not target:
        raise ValidationError("仓库地址为空")
    started = time.monotonic()
    if is_git_url(target):
        return _probe_git(target, ref, started)
    return _probe_local(target, started)
