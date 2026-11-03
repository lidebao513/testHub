"""引擎 · 分支阶段锚定与 manifest（C1，对应方案 A-L1 / B）。

把"同一分支在不同时间点的代码"定格为 StageSnapshot 并登记到 manifest，
支持把历史 commit 的完整代码树物化到独立目录（不干扰主工作区）。

复用：engine.diff_tag（resolve_ref / repo_escape_blocked / run_git）、
      engine.remote_fetch.materialize_remote（远程按 ref 取文件）。
设计铁律（沿用 diff_tag 诚实约束）：commit 不可解析一律抛 ValueError，不静默降级。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.errors import WorkspaceEscapeBlocked
from engine import diff_tag


_SCHEMA_VERSION = 1
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SHORT_SHA = re.compile(r"^[0-9a-fA-F]{4,39}$")
_SAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]")


def _slug(repo: str) -> str:
    """repo 标识归一化为安全目录名。"""
    s = (repo or "").rstrip("/").rsplit("/", 1)[-1] or "repo"
    s = re.sub(r"\.git$", "", s)
    return _SAFE_SLUG.sub("_", s) or "repo"


@dataclass
class StageSnapshot:
    """单个分支阶段快照（锚定某 commit）。"""

    repo: str = ""
    branch: str = ""
    commit: str = ""
    base_commit: str | None = None
    mode: str = "full"  # full | incremental
    created_at: str = ""
    stage_id: str = ""
    artifact_dir: str = ""
    source: str = "local"  # local | codeup
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StageSnapshot:
        fields = cls.__dataclass_fields__
        kwargs: dict[str, Any] = {}
        for name in fields:
            if name == "meta":
                kwargs[name] = d.get("meta") or {}
            else:
                val = d.get(name)
                if val in (None, ""):
                    val = "local" if name == "source" else ""
                kwargs[name] = val
        return cls(**kwargs)


class StageStore:
    """manifest 清单管理：analysis_root/<slug>/stages.json。"""

    def __init__(self, analysis_root: str | Path) -> None:
        self.root = Path(analysis_root)

    def _manifest_path(self, repo: str) -> Path:
        return self.root / _slug(repo) / "stages.json"

    def _load(self, repo: str) -> dict[str, Any]:
        p = self._manifest_path(repo)
        if not p.exists():
            return {"schema_version": _SCHEMA_VERSION, "repo": repo, "stages": []}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            backup = p.with_suffix(".json.bak")
            try:
                shutil.copy(p, backup)
            except OSError:
                pass
            return {"schema_version": _SCHEMA_VERSION, "repo": repo, "stages": []}

    def _save(self, repo: str, data: dict[str, Any]) -> None:
        p = self._manifest_path(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)  # 原子写

    def add(self, snap: StageSnapshot) -> StageSnapshot:
        data = self._load(snap.repo)
        if any(s.get("stage_id") == snap.stage_id for s in data["stages"]):
            raise ValueError(f"重复阶段快照：{snap.stage_id}")
        data["stages"].append(snap.to_dict())
        self._save(snap.repo, data)
        return snap

    def list_all(self, repo: str) -> list[StageSnapshot]:
        data = self._load(repo)
        return [StageSnapshot.from_dict(s) for s in data["stages"]]

    def list_by_branch(self, repo: str, branch: str) -> list[StageSnapshot]:
        data = self._load(repo)
        return [StageSnapshot.from_dict(s) for s in data["stages"] if s.get("branch") == branch]

    def get(self, repo: str, stage_id: str) -> StageSnapshot:
        data = self._load(repo)
        for s in data["stages"]:
            if s.get("stage_id") == stage_id:
                return StageSnapshot.from_dict(s)
        raise KeyError(f"阶段快照不存在：{stage_id}")

    def latest(self, repo: str, branch: str) -> StageSnapshot | None:
        stages = self.list_by_branch(repo, branch)
        return stages[-1] if stages else None

    def resolve(self, repo: str, ref: str) -> StageSnapshot:
        """stage_id 精确匹配 或 commit 前缀匹配到唯一快照。"""
        if not ref:
            raise KeyError("ref 为空")
        data = self._load(repo)
        matched = []
        for s in data["stages"]:
            sid = s.get("stage_id", "")
            commit = s.get("commit", "")
            if sid == ref or (ref and commit.startswith(ref)):
                matched.append(s)
        uniq = {s["stage_id"]: s for s in matched}
        if len(uniq) == 1:
            return StageSnapshot.from_dict(next(iter(uniq.values())))
        if not uniq:
            raise KeyError(f"无法解析阶段：{ref}")
        raise KeyError(f"阶段引用 {ref} 命中多个快照：{list(uniq)}")

    def artifact_path(self, snap: StageSnapshot, name: str) -> Path:
        return self.root / snap.artifact_dir / f"{name}.json"

    def save_artifact(self, snap: StageSnapshot, name: str, data: Any) -> Path:
        p = self.artifact_path(snap, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return p

    def load_artifact(self, snap: StageSnapshot, name: str) -> Any:
        p = self.artifact_path(snap, name)
        if not p.exists():
            raise FileNotFoundError(str(p))
        return json.loads(p.read_text(encoding="utf-8"))


def resolve_commit(repo: str | Path, ref: str, *, source: str = "local") -> str:
    """把 ref 归一化为完整 commit sha（本地）；远程回退原始 ref。

    缩写 SHA（4–39 位 hex）一律拒绝，防前缀模糊匹配出错（沿用 diff_tag 铁律）。
    """
    if not ref:
        raise ValueError("commit ref 为空")
    if _SHORT_SHA.match(ref) and not _FULL_SHA.match(ref):
        raise ValueError(f"缩写 commit 不可用于历史锚定（需完整 40 位 SHA）：{ref}")
    if source != "local":
        return ref
    if diff_tag.repo_escape_blocked(repo):
        raise WorkspaceEscapeBlocked(f"{repo} 的 git 顶层逃逸，已阻断")
    ref = diff_tag.resolve_ref(repo, ref)
    cp = diff_tag.run_git(repo, ["rev-parse", f"{ref}^{{commit}}"])
    if cp.returncode != 0:
        raise ValueError(f"commit 不可解析：{ref}")
    return cp.stdout.strip()


def materialize_commit(  # noqa: PLR0913 - 物化选项各自独立，拼成对象反而绕
    repo: str | Path,
    commit: str,
    dest_dir: str | Path,
    *,
    source: str = "local",
    code_source: dict | None = None,
    repo_url: str | None = None,
    materializer: Callable[..., list[str]] | None = None,
) -> list[str]:
    """把历史 commit 的完整代码树物化到 dest_dir，返回相对路径列表。

    不干扰主工作区。本地走 git archive；远程走 remote_fetch（按 ref 取文件）。
    materializer 可注入，便于单测无 git/网络。
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if materializer is not None:
        return materializer(repo, commit, dest)
    if source != "local":
        from engine.remote_fetch import materialize_remote

        if code_source is None or repo_url is None:
            raise ValueError("远程物化需要 code_source 与 repo_url")
        real_dir, files = materialize_remote(code_source, repo_url, str(dest), ref=commit)
        real = Path(real_dir)
        if real != dest and str(real).startswith(str(dest)):
            for item in real.iterdir():
                shutil.move(str(item), str(dest / item.name))
            d = real
            while d != dest and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        return files
    # 本地：git archive 解压到 dest（不 checkout 主工作区）
    if diff_tag.repo_escape_blocked(repo):
        raise WorkspaceEscapeBlocked(f"{repo} 的 git 顶层逃逸，已阻断")
    proc = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"git archive 失败（commit={commit}）：{proc.stderr.decode('utf-8', 'replace')[:300]}"
        )
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:*") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        tf.extractall(dest, filter="data")
    return sorted(m.name for m in members)


def create_snapshot(  # noqa: PLR0913 - 快照选项各自独立，与 materialize_commit 对称
    store: StageStore,
    repo: str,
    branch: str,
    *,
    commit: str | None = None,
    base_commit: str | None = None,
    source: str = "local",
    code_source: dict | None = None,
    repo_url: str | None = None,
    materializer: Callable[..., list[str]] | None = None,
) -> StageSnapshot:
    """记录一个分支阶段快照：解析 commit → 物化代码树 → 登记 manifest。"""
    if commit is None:
        if source == "local":
            cp = diff_tag.run_git(repo, ["rev-parse", "HEAD"])
            if cp.returncode != 0:
                raise ValueError("无法获取当前 HEAD")
            commit = cp.stdout.strip()
        else:
            raise ValueError("远程模式必须显式提供 commit")
    resolved = resolve_commit(repo, commit, source=source)
    short = resolved[:12]
    artifact_dir = f"{_slug(repo)}/{branch}@{short}"
    dest = store.root / artifact_dir
    files = materialize_commit(
        repo,
        resolved,
        dest,
        source=source,
        code_source=code_source,
        repo_url=repo_url,
        materializer=materializer,
    )
    seq = len(store.list_by_branch(repo, branch)) + 1
    snap = StageSnapshot(
        repo=repo,
        branch=branch,
        commit=resolved,
        base_commit=base_commit,
        mode="incremental" if base_commit else "full",
        created_at=datetime.now(UTC).isoformat(),
        stage_id=f"{branch}@{short}_{seq}",
        artifact_dir=artifact_dir,
        source=source,
        meta={"files": len(files)},
    )
    store.add(snap)
    return snap


__all__ = [
    "StageSnapshot",
    "StageStore",
    "create_snapshot",
    "materialize_commit",
    "resolve_commit",
]
