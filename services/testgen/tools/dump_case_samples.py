"""导出各执行层／各维度的真实用例样本为 Markdown，供人工核对。

用法：
    venv/Scripts/python.exe tools/dump_case_samples.py [输出路径]
默认输出到仓库根 `用例样本展示.md`。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "testgen.db"
DEFAULT_OUT = ROOT.parent.parent / "用例样本展示.md"
PID = 1

L = "json_extract(c.steps, '$[0].layer')"
K = "json_extract(c.steps, '$[0].kind')"
A = "json_extract(c.steps, '$[0].action')"
M = "json_extract(c.steps, '$[0].method')"
R = "json_extract(c.steps, '$[0].coverage_role')"

# dimension 在测试点表上，需联表
FROM_JOIN = (
    "FROM cases c LEFT JOIN test_points t "
    "ON t.project_id = c.project_id AND t.tp_id = c.tp_id "
    f"WHERE c.project_id={PID} AND c.status != 'obsolete' AND ({{}})"
)

# (小标题, 说明, WHERE 条件, 取样条数)
GROUPS: list[tuple[str, str, str, int]] = [
    (
        "A. 接口层 · HTTP 接口",
        '代码里的路由装饰器（`@router.get("/x")` 等）→ 用 HTTP 请求直接打接口。',
        f"{L}='接口' AND {M} IN ('GET','POST','PUT','PATCH','DELETE') AND c.case_type='正常'",
        3,
    ),
    (
        "B. 接口层 · 后端业务函数",
        "不是 HTTP 接口，是被调用到的业务函数／工具脚本，归在接口层用函数级断言验证。",
        f"{L}='接口' AND {M}='FUNC' AND c.case_type='正常'",
        2,
    ),
    (
        "C. UI 层 · 页面可达",
        "前端路由（React `<Route path=\"/pc/chat\">` / `path: '/x'`）→ 打开页面看能否渲染。",
        f"{L}='UI' AND {M}='PAGE' AND c.case_type='正常'",
        3,
    ),
    (
        "D. UI 层 · 交互组件",
        "带交互钩子的前端组件（onClick / onChange / onSubmit / <button> 等）→ 点击输入看响应。",
        f"{L}='UI' AND {M}='UI' AND c.case_type='正常'",
        2,
    ),
    (
        "E. 安全 · 鉴权缺失",
        "未携带或携带无效凭证访问接口，应被拒绝。",
        "case_type='安全' AND t.dimension='安全-鉴权缺失'",
        2,
    ),
    (
        "F. 安全 · 越权访问",
        "带路径参数的写操作（PUT/PATCH/DELETE），用非属主身份访问。",
        "case_type='安全' AND t.dimension='安全-越权'",
        2,
    ),
    (
        "G. 异常",
        "仅对有路径参数的接口展开：资源不存在／状态非法。",
        "case_type='异常'",
        2,
    ),
    (
        "H. 边界",
        "参数缺失／非法：所有 API 路由各一条，写操作再补一条凑对。",
        "case_type='边界'",
        2,
    ),
    (
        "I. 增量命中：更新标签",
        "本次 diff（base..target）改动到的文件所涉测试点，标为「更新」。",
        "test_type='更新'",
        2,
    ),
    (
        "J. 接口层 · 补充用例（对应能力已有 UI 覆盖）",
        "UI 优先策略下，对应模块已有 UI 覆盖的接口层用例被标记为 `supplement`："
        "UI 是优先验证层，接口仅作补充。",
        f"{L}='接口' AND {R}='supplement'",
        2,
    ),
]


def fetch(conn: sqlite3.Connection, where: str, limit: int) -> list[dict]:
    sql = f"SELECT c.* {FROM_JOIN.format(where)} LIMIT {limit}"
    return [dict(r) for r in conn.execute(sql)]


def count(conn: sqlite3.Connection, where: str) -> int:
    sql = f"SELECT COUNT(*) {FROM_JOIN.format(where)}"
    return int(conn.execute(sql).fetchone()[0])


def render_case(d: dict) -> list[str]:
    steps = json.loads(d["steps"])
    doc = json.loads(d["doc_steps"]) if d.get("doc_steps") else []
    out = [
        f"#### `{d['tc_no']}` {d['title']}",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| 编号 | `{d['tc_no']}` |",
        f"| 模块 | {d['module']} |",
        f"| 类型（维度） | {d['case_type']} |",
        f"| 优先级 | {d['priority']} |",
        f"| 测试类型 | {d['test_type']} |",
        f"| 覆盖角色 | {steps[0].get('coverage_role')} |",
        f"| 期望执行层 | {steps[0].get('layer')}（{steps[0].get('action')}） |",
        f"| 来源功能点 | `{d['fp_contract_id']}` → `{d['tp_id']}` |",
        f"| 用例 ctype | {d['ctype']} |",
        "",
        f"**前置条件**：{d['precondition']}",
        "",
        "**步骤**",
        "",
    ]
    for s in doc:
        out.append(f"{s['seq']}. [{s['type']}] {s['desc']}")
        if s.get("expect"):
            out.append(f"   - 预期：{s['expect']}")
    out += ["", "**机器步（执行器只读第 1 步）**", "", "```json"]
    out.append(json.dumps(steps[0], ensure_ascii=False, indent=2))
    out += ["```", ""]
    return out


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    total = count(conn, "1=1")
    lines = [
        "# 测试用例样本展示（真实产出）",
        "",
        f"> 数据来源：`work/testgen-service/data/testgen.db`　项目：research-agent（id={PID}）",
        f"> 全库用例总数：**{total}**　契约版本：1.0",
        "",
        "本文件由 `work/testgen-service/tools/dump_case_samples.py` 直接从数据库导出，",
        "未经手工修改，用于核对「代码 → 功能点 → 测试点 → 用例」链路的真实产出。",
        "",
        "## 执行层与维度总览",
        "",
        "| 执行层 | 用例类型 | 条数 |",
        "|---|---|---|",
    ]
    for r in conn.execute(
        f"SELECT {L} l, c.ctype, COUNT(*) n FROM cases c WHERE c.project_id={PID} "
        f"AND c.status != 'obsolete' GROUP BY l, c.ctype ORDER BY n DESC"
    ):
        lines.append(f"| {r['l']} | {r['ctype']} | {r['n']} |")
    lines += ["", "| 维度 | 子维度 | 条数 |", "|---|---|---|"]
    for r in conn.execute(
        "SELECT category, dimension, COUNT(*) n FROM test_points "
        f"WHERE project_id={PID} GROUP BY category, dimension ORDER BY n DESC"
    ):
        lines.append(f"| {r['category']} | {r['dimension']} | {r['n']} |")
    lines += ["", "| 覆盖角色 | 条数 | 含义 |", "|---|---|---|"]
    for r in conn.execute(
        f"SELECT {R} role, COUNT(*) n FROM cases c WHERE c.project_id={PID} "
        f"AND c.status != 'obsolete' GROUP BY role ORDER BY n DESC"
    ):
        meaning = {
            "primary": "UI 优先／接口为该能力唯一覆盖方式",
            "supplement": "接口层用例，对应能力已有 UI 覆盖（接口仅补充）",
            "": "（未标注，旧数据）",
        }.get(r["role"], "")
        lines.append(f"| {r['role'] or '（空）'} | {r['n']} | {meaning} |")
    lines.append("")

    for title, desc, where, limit in GROUPS:
        n = count(conn, where)
        lines += [f"## {title}（共 {n} 条，下列 {min(n, limit)} 条）", "", f"> {desc}", ""]
        rows = fetch(conn, where, limit)
        if not rows:
            lines += ["（无数据）", ""]
            continue
        for d in rows:
            lines += render_case(d)

    lines += [
        "## 产物文件清单",
        "",
        "| 文件 | 内容 |",
        "|---|---|",
        "| `outputs/1/test_cases.json` | 全部用例（机器可读，含机器步） |",
        "| `outputs/1/TEST_CASES.md` | 全部用例（人类可读清单） |",
        "| `outputs/1/test_points.json` | 全部测试点（含证据引用） |",
        "| `outputs/1/summary.json` | 本次运行汇总（计数／标签／追溯体检） |",
        "",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {out_path}")
    print(f"lines  : {len(lines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
