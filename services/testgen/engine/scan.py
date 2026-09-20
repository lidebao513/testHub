"""引擎 · 模块一：扫描（scan）。

职责单一：把仓库目录变成「可分析的文件集合」，并对每个文件**只读一次、只解析一次 AST**。
这是 legacy `code_analyzer.py` 中 `_SourceIndex` 想解决却被上帝模块淹没的那件事：
同文件被功能点提取、语义增强、测试点展开、hunk 打标反复读取，导致 O(n²) 级 IO。

对外只暴露两个东西：`SourceFile` 与 `Scanner`。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


# 非业务目录：与业务无关，直接跳过
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        "venv",
        ".venv",
        "env",
        "node_modules",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "site-packages",
        # 测试/工具产生的临时目录：**.pytest_tmp 必须排除**。
        # 它是本项目的 pytest `--basetemp`（见 pyproject `addopts`），里面装着**上一轮测试
        # 生成的夹具仓库副本**（含 .py 源码）。真机自测暴露：扫本服务自身时它被当成源码，
        # 产出大量「幽灵功能点」（如 `.pytest_tmp/<case>/sample_app/billing/api.py`），
        # 并让语义去重凭空收敛 351 条——数字全被污染而不自知。
        ".pytest_tmp",
        ".tmp",
        ".cache",
        ".tox",
        ".nox",
        "htmlcov",
        # 非业务审计/快照工具目录：research-agent 仓库根自带 `audit_safeguard/`，
        # 其内部 `snapshot/audit_safeguard/...` 是整个产品代码的**整仓副本**，
        # 若不排除会被重复扫描，产生约 176 个重复功能点 / ~213 条重复用例。
        # 该目录只含 gen_manifest/restore_audit 等审计工具，不是被测产品代码。
        "audit_safeguard",
    }
)

# 测试/脚手架文件名前缀：参与扫描但不作为「业务功能点」来源
NOISE_FILE_PREFIXES: tuple[str, ...] = ("test_", "conftest", "selftest_", "verify_")
# 非业务目录（整文件排除）：测试/文档/迁移 + 回归套件/脚手架/夹具/构建脚本。
# research-agent 实测：regression/_fx_test/scripts/fixtures 目录含大量测试套件与构建脚本，
# 其内部的「公开函数」被旧逻辑当成业务功能点（约 207 条），属明显噪声，应整文件排除。
# 注意：conf/schema 不在本集合——它们可能含真实 API 路由，留给 fp_extract 的
# 「业务函数级」噪声过滤（_file_is_business_noise）单独收窄，避免误伤接口。
NOISE_DIR_PARTS: tuple[str, ...] = (
    "tests",
    "test",
    "docs",
    "examples",
    "migrations",
    "regression",
    "_fx_test",
    "fixtures",
    "scripts",
    "selftest",
)

# ---------------------------------------------------------------- 扩展名（单一真值）
# 前端类扩展名：**扫描器与 fp_extract 必须共用同一份**，否则会出现
# 「文件没被扫到 → 前端功能点恒为 0」的静默丢层（真实仓库上曾丢掉 147 个 .tsx）。
FRONTEND_EXTS: tuple[str, ...] = (".js", ".ts", ".tsx", ".jsx", ".vue")
# 全量扫描扩展名
SOURCE_EXTS: tuple[str, ...] = (".py", *FRONTEND_EXTS, ".html")


@dataclass
class SourceFile:
    """一个被扫描到的源码文件（AST 惰性解析、只解析一次）。"""

    rel: str  # 相对仓库根路径，**统一正斜杠**
    abspath: Path
    text: str
    _tree: ast.Module | None = field(default=None, repr=False)
    _parsed: bool = field(default=False, repr=False)

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        return self.abspath.suffix.lower()

    @property
    def is_python(self) -> bool:
        return self.ext == ".py"

    @property
    def is_noise(self) -> bool:
        parts = self.rel.split("/")
        if any(p in NOISE_DIR_PARTS for p in parts[:-1]):
            return True
        return self.name.startswith(NOISE_FILE_PREFIXES)

    def tree(self) -> ast.Module | None:
        """解析 AST（失败返回 None，并缓存结果避免重复解析）。"""
        if self._parsed:
            return self._tree
        self._parsed = True
        try:
            self._tree = ast.parse(self.text)
        except (SyntaxError, ValueError):
            self._tree = None
        return self._tree


class Scanner:
    """目录扫描器：产出 `SourceFile`，并按相对路径建索引。"""

    def __init__(
        self,
        root: str | Path,
        *,
        exts: tuple[str, ...] = SOURCE_EXTS,
        exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
        max_bytes: int = 2_000_000,
    ) -> None:
        self.root = Path(root).resolve()
        self.exts = exts
        self.exclude_dirs = exclude_dirs
        self.max_bytes = max_bytes

    def iter_files(self) -> Iterator[SourceFile]:
        """按稳定顺序产出文件（排序保证跨运行结果一致）。

        **排除规则按「相对仓库根的目录名」判定**，不看绝对路径。
        为什么必须如此：早期实现用 `p.parts`（绝对路径）匹配，于是只要仓库本身位于某个
        叫 `build/ dist/ env/ .pytest_tmp/` 的目录下，整个仓库都会被判为「排除目录」
        → 扫描结果**静默变 0 文件**（与「漏 .tsx 导致前端整层不可见」同一类静默丢层）。
        """
        if not self.root.is_dir():
            return
        paths: list[Path] = []
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in self.exts:
                continue
            if any(seg in self.exclude_dirs for seg in self.rel_dir_parts(p)):
                continue
            try:
                if p.stat().st_size > self.max_bytes:
                    continue
            except OSError:
                continue
            paths.append(p)

        for p in sorted(paths, key=lambda x: x.as_posix()):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            yield SourceFile(rel=self.rel_of(p), abspath=p, text=text)

    def rel_dir_parts(self, path: Path) -> tuple[str, ...]:
        """路径**相对仓库根**的目录片段（不含文件名）；不在根内则退回绝对片段。"""
        try:
            return path.relative_to(self.root).parts[:-1]
        except ValueError:
            return path.parts[:-1]

    def rel_of(self, path: Path) -> str:
        """转成相对仓库根的正斜杠路径（与 git diff 路径形态一致）。"""
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            rel = Path(path.name)
        return rel.as_posix()

    def index(self) -> dict[str, SourceFile]:
        """一次性建索引：{相对路径: SourceFile}。"""
        return {sf.rel: sf for sf in self.iter_files()}

    def list_records(self) -> list[dict[str, object]]:
        """轻量清单（供 API 输出，不携带全文）。"""
        return [
            {
                "path": sf.rel,
                "name": sf.name,
                "ext": sf.ext,
                "noise": sf.is_noise,
                "bytes": len(sf.text.encode("utf-8")),
            }
            for sf in self.iter_files()
        ]
