#!/usr/bin/env python3
# ============================================================================
# 架构结构门禁（check_structure）
# 校验「项目整体改动前」立下的架构铁律，防优化/生成跑偏：
#   1) 分层依赖方向：modules 不得反向 import service/main；core 不依赖上层
#   2) 测试点数据契约：产物 JSON 每条必须含 verify_layer
#   3) 根目录散落脚本清单（warning，不阻断；迁移目标见 ARCHITECTURE §5）
# 退出码 0 = 无 error；非 0 = 存在违反（供 pre-commit / CI 拦截）
# ============================================================================
import json
import os
import re
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ERRORS: list = []
WARNINGS: list = []

# ---- 1) 分层依赖方向 ----
# 规则（依赖只能向下，不能向上）：
#   core/*      禁止 import engine/service/cli/workspace/output（core 是最底层，只放契约与基础设施）
#   engine/*    禁止 import service/cli（引擎不感知 HTTP 与命令行）
#   workspace/* 禁止 import service/cli/engine
#   output/*    禁止 import service/cli/engine
#   service/*   禁止 import cli（service 可用 core/engine/workspace/output）
#   cli/*       顶层，可用全部
IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))")

LAYER_RULES = {
    "core": {"forbid": ["engine", "service", "cli", "workspace", "output"]},
    "engine": {"forbid": ["service", "cli"]},
    "workspace": {"forbid": ["service", "cli", "engine"]},
    "output": {"forbid": ["service", "cli", "engine"]},
    "service": {"forbid": ["cli"]},
}


def check_layer_imports():
    for layer, rule in LAYER_RULES.items():
        layer_dir = os.path.join(ROOT, layer)
        if not os.path.isdir(layer_dir):
            continue
        for dp, _, files in os.walk(layer_dir):
            if "venv" in dp or "__pycache__" in dp:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    with open(fp, encoding="utf-8", errors="ignore") as f:
                        src = f.read()
                except Exception:
                    continue
                for m in IMPORT_RE.finditer(src):
                    mod = (m.group(1) or m.group(2) or "").replace(" ", "")
                    if not mod:
                        continue
                    for f in rule["forbid"]:
                        if mod == f or mod.startswith(f + "."):
                            ERRORS.append(
                                f"[分层依赖] {os.path.relpath(fp, ROOT)} 禁止 import {mod}"
                                f"（{layer} 层不得依赖上层 {f}）"
                            )


# ---- 2) 测试点数据契约 ----
def check_data_contract():
    cand_dirs = [
        os.path.join(ROOT, "outputs"),
        os.path.join(ROOT, "data"),
    ]
    found = False
    for cd in cand_dirs:
        if not os.path.isdir(cd):
            continue
        for fn in os.listdir(cd):
            if not (fn.startswith("test_points") and fn.endswith(".json")):
                continue
            found = True
            fp = os.path.join(cd, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                ERRORS.append(f"[数据契约] {fn} 解析失败: {e}")
                continue
            pts = data if isinstance(data, list) else data.get("test_points", data.get("cases", []))
            missing = [i for i, r in enumerate(pts) if r.get("verify_layer") not in ("接口", "UI")]
            if missing:
                ERRORS.append(
                    f"[数据契约] {fn} 有 {len(missing)} 条测试点缺少 verify_layer"
                    f"（示例索引 {missing[:5]}）"
                )
    if not found:
        WARNINGS.append("[数据契约] 未找到 test_points*.json，跳过校验（首次生成后自动生效）")


# ---- 3) 根目录散落脚本清单（warning）----
# 新服务要求：项目根目录不得散落脚本，一切入口走 cli/ 或 scripts/。
SCATTER_PREFIX = ("gen_", "selftest_", "run_", "verify_", "validate_", "make_")
WHITELIST_ROOT_PY: set = set()


def check_root_scatter():
    for fn in os.listdir(ROOT):
        if not fn.endswith(".py"):
            continue
        if fn not in WHITELIST_ROOT_PY:
            WARNINGS.append(
                f"[历史债务] 根目录散落脚本 {fn} 应迁入 cli/ 或 scripts/ 或 tests/"
                f"（见 ARCHITECTURE.md §2）"
            )


def main():
    print("=== 架构结构门禁 check_structure ===")
    check_layer_imports()
    check_data_contract()
    check_root_scatter()

    if ERRORS:
        print(f"\n❌ 发现 {len(ERRORS)} 处硬违规（必须修复）：")
        for e in ERRORS:
            print("  -", e)
    if WARNINGS:
        print(f"\n⚠️  {len(WARNINGS)} 条提示（不阻断）：")
        for w in WARNINGS:
            print("  -", w)
    if not ERRORS and not WARNINGS:
        print("✅ 结构校验通过，无违规。")

    print(f"\n结果：error={len(ERRORS)} warning={len(WARNINGS)}")
    sys.exit(1 if ERRORS else 0)


if __name__ == "__main__":
    main()
