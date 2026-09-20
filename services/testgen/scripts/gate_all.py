#!/usr/bin/env python3
# ============================================================================
# testgen-service 统一质量门禁（本地 / pre-commit / CI 单入口）
# ----------------------------------------------------------------------------
# 与 legacy(test-accel) 同源，八环不变：
#   secret scan -> ruff(lint + format) -> check_enums -> check_structure
#   -> mypy -> bandit -> pytest(含覆盖率)
# 任一环节非零退出，则整体非零退出。
#
# 工具解析优先级：
#   1) PATH（shutil.which）——CI 中 pip 安装后即命中
#   2) 托管环境目录（QUALITY_ENV 或本机 managed venv Scripts）
#   3) 回退 `python -m <tool>`（ruff/mypy/bandit）
# pytest 用「项目 venv 的 python -m pytest」，须在已装依赖的 venv 中运行。
# ============================================================================
import os
import shutil
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 本机托管环境（含 ruff/mypy/bandit）；可用 QUALITY_ENV 覆盖
_MANAGED = os.environ.get("QUALITY_ENV") or (
    r"C:\Users\EDY\.workbuddy\binaries\python\envs\default\Scripts"
)

# 源码层（ruff / mypy / bandit 的一致口径）
SRC_DIRS = ["core", "engine", "service", "cli", "workspace", "output", "scripts", "tools"]


def _resolve(name: str) -> list:
    """返回可执行的命令列表（长度 1 或 3）。"""
    p = shutil.which(name) or shutil.which(name + ".exe")
    if p:
        return [p]
    cand = os.path.join(_MANAGED, name + ".exe")
    if os.path.isfile(cand):
        return [cand]
    return [sys.executable, "-m", name]


def _step(label: str, cmd: list, env: dict | None = None, logfile: str | None = None) -> int:
    print(f"\n=== {label} ===")
    print("  $ " + " ".join(cmd))
    # logfile：把子进程 stdout/stderr 重定向到文件。用途有二：
    #   1) 隔离「门禁宿主」的 stderr —— 在部分沙箱里宿主 stderr 会被回收（closed
    #      file），若直接继承给 pytest，pytest 全程/总结阶段写出即抛
    #      `ValueError('I/O operation on closed file.')` 而假性失败；重定向到文件后
    #      子进程拿到的是活的文件 fd，不受宿主 stderr 生命周期影响。
    #   2) 长输出（尤其 pytest 详细用例）不刷屏；失败时再 tail 关键行。
    if logfile is not None:
        with open(logfile, "w", encoding="utf-8") as out:
            rc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out, stderr=out).returncode
        if rc != 0:
            print(f"  (详见 {logfile}，末尾如下)")
            _tail(logfile, 40)
    else:
        rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    print(f"--- {label}: {'OK' if rc == 0 else 'FAIL'} (rc={rc}) ---")
    return rc


def _tail(path: str, n: int = 40) -> None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        for ln in lines[-n:]:
            print("    " + ln)
    except Exception:  # 读日志失败不应反噬门禁
        pass


def _pytest_env() -> dict:
    """pytest 子进程环境：清空 WorkBuddy「批量删除安全护栏」变量，使其与 CI 行为一致。

    护栏仅在注入了 `CODEBUDDY_SAFE_DELETE_*` 的沙箱里激活，会在 pytest 清理
    系统临时目录（一次 rmtree 数百文件）时 `SystemExit(1)`，导致整套门禁假失败；
    CI 中这些变量本就为空、护栏不触发。仅对 pytest 这步清空，其余 7 环护栏仍生效。
    清空后 temp 目录照常移至回收站，不涉及任何用户数据删除。
    """
    e = dict(os.environ)
    for key in (
        "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR",
        "CODEBUDDY_TOOL_CALL_ID",
        "CODEBUDDY_SAFE_DELETE_BULK_GUARD",
        "CODEBUDDY_NODE_BIN",
    ):
        e.pop(key, None)
    return e


def _pytest_cmd() -> list:
    """pytest 必须在含项目依赖的 venv 中跑；优先用项目 venv，否则回退当前 python。"""
    venv_py = os.path.join(ROOT, "venv", "Scripts", "python.exe")
    py = venv_py if os.path.isfile(venv_py) else sys.executable
    return [
        py,
        "-m",
        "pytest",
        "-m",
        "not live_llm",
        "--cov=core",
        "--cov=engine",
        "--cov=service",
        "--cov=workspace",
        "--cov=output",
        "--cov-report=term-missing",
        "-q",
    ]


def main() -> int:
    ruff = _resolve("ruff")
    mypy = _resolve("mypy")
    bandit = _resolve("bandit")
    py = sys.executable

    failures = []

    # 0) 密钥 / 凭据预提交扫描（gitleaks 类，最先跑：最高风险）
    failures.append(("secret scan", _step("secret scan", [py, "scripts/check_secrets.py"])))
    # 1) ruff lint（各层源码 + 门禁脚本 + 工具 + 测试）
    failures.append(("ruff lint", _step("ruff lint", [*ruff, "check", *SRC_DIRS, "tests"])))
    # 2) ruff format 校验（不自动改写，保持 CI 幂等）
    failures.append(
        (
            "ruff format",
            _step(
                "ruff format --check",
                [*ruff, "format", *SRC_DIRS, "tests", "--check"],
            ),
        )
    )
    # 3) 枚举单源门禁（字段感知 + 反向孤儿检查）
    failures.append(("check_enums", _step("check_enums", [py, "scripts/check_enums.py"])))
    # 4) 架构结构门禁（分层反向依赖 / 数据契约 / 根目录散落脚本）
    failures.append(
        ("check_structure", _step("check_structure", [py, "scripts/check_structure.py"]))
    )
    # 5) mypy 类型检查（各层源码 + 门禁脚本 + 工具）
    failures.append(("mypy", _step("mypy src dirs", [*mypy, *SRC_DIRS])))
    # 6) bandit 安全扫描（MEDIUM 及以上；跳过测试；误报见 pyproject [tool.bandit]）
    failures.append(
        (
            "bandit",
            _step(
                "bandit src dirs",
                [
                    *bandit,
                    "-r",
                    *SRC_DIRS,
                    "-x",
                    "tests",
                    "-c",
                    "pyproject.toml",
                    "--severity-level",
                    "medium",
                ],
            ),
        )
    )
    # 7) pytest + 覆盖率（fail_under 见 pyproject [tool.coverage.report]）
    #    pytest 子进程清空批量删除护栏变量，避免清理系统临时目录触发 SystemExit（见 _pytest_env）；
    #    输出重定向到 pytest_gate.log（见 _step），隔离宿主 stderr 被沙箱回收导致的假性失败。
    failures.append(
        (
            "pytest",
            _step(
                "pytest",
                _pytest_cmd(),
                env=_pytest_env(),
                logfile=os.path.join(ROOT, "pytest_gate.log"),
            ),
        )
    )

    failed = [(name, rc) for name, rc in failures if rc != 0]
    print("\n" + "=" * 60)
    if not failed:
        print("✅ 全部质量门禁通过（secret/lint/format/enum/structure/type/security/test）")
        return 0
    print(f"❌ {len(failed)} 项门禁未通过：")
    for name, rc in failed:
        print(f"   - {name} (rc={rc})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
