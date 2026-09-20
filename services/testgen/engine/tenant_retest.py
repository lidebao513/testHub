"""G-9 双身份越权复测专项通道（可选执行通道）。

背景
----
生成阶段（`engine/tp_expand`）已把跨租户越权用例标记为 `安全-越权读他人数据` /
`安全-越权写他人数据` 维度。但由于判定**必须**用「两个真实身份 + 他人 resource_id」，
生成期的执行器只能诚实 SKIPPED（不伪造通过、不误判失败）。

本模块提供**可选的专项复测通道**，把判定从「诚实跳过」升级为「真实结论」：
1. 用**属主身份**发请求，拿到一个真实的 `resource_id`（如发票 id、项目 id）；
2. 用**他人身份**重放该用例（读他人资源 / 写他人资源）；
3. 断言：他人身份应被拒（读 401/403/404 且不泄露属主数据；写 401/403/404），
   否则记为高危 FAIL（越权成功）。

双身份来源
----------
- 属主身份：调用方传入（`owner_user` / `owner_password`），或回退到 `RUNTIME_LOGIN_*`；
- 他人身份：**必须显式提供**（CLI `--other-user` / `--other-password`）——平台既不持有
  「他人」凭证、也无从推断；只给属主一方时，复测降级为诚实 SKIPPED，保留生成期结论。

红线
----
- 不发任何写操作到属主数据本身；他人重放只读 / 越权写（由用例 method 决定），均属「探测越权」；
- 凭证不入库、不日志（与 runtime_ui / executor 同一红线）；
- 复测走真实网络，失败即失败，绝不粉饰。
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from core.contracts import CaseSpec
from core.enums import Dimension, TPType


class RetestSession(Protocol):
    """复测会话协议（便于用桩做单测；CLI 用 requests 实现真实会话）。"""

    def request(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, json: Any = None
    ) -> tuple[int, Any]:
        """返回 (状态码, 响应体)。"""
        ...


def _case_dims(case: CaseSpec) -> list[str]:
    return [str(s.get("dimension") or "") for s in case.doc_steps]


def is_tenant_case(case: CaseSpec) -> bool:
    """是否属 G-9 跨租户越权用例。"""
    if case.case_type != TPType.SECURITY.value:
        return False
    return any(
        d in (Dimension.TENANT_READ.value, Dimension.TENANT_WRITE.value) for d in _case_dims(case)
    )


def tenant_cases_of(cases: list[CaseSpec]) -> list[CaseSpec]:
    """从用例集合中筛出 G-9 跨租户越权用例。"""
    return [c for c in cases if is_tenant_case(c)]


def _primary_step(case: CaseSpec) -> dict[str, Any]:
    for s in case.doc_steps:
        d = str(s.get("dimension") or "")
        if d in (Dimension.TENANT_READ.value, Dimension.TENANT_WRITE.value):
            return s
    return case.doc_steps[0] if case.doc_steps else {}


def _strip_param(path: str) -> str:
    """去掉路径尾部参数占位符，得到「列表接口」路径（/invoices/{id} → /invoices）。"""
    p = re.sub(r"\{[^}]*\}\Z", "", path)
    p = re.sub(r"<[^>]*>\Z", "", p)
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*\Z", "", p)
    return p.rstrip("/") or path


def _materialize(path: str, rid: str) -> str:
    """把路径里的参数占位符替换为真实 resource_id；无占位符则追加重 id。"""
    for pat in (r"\{[^}]*\}", r"<[^>]*>", r":[A-Za-z_][A-Za-z0-9_]*"):
        if re.search(pat, path):
            return re.sub(pat, rid, path, count=1)
    return f"{path.rstrip('/')}/{rid}"


def _extract_id(body: Any) -> str | None:
    """从属主 GET 响应里取一个 id（优先 data[].id / data.id / id / 顶层列表首项 id）。"""
    if isinstance(body, dict):
        if "id" in body:
            return str(body["id"])
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
            return str(data[0]["id"])
        if isinstance(data, dict) and "id" in data:
            return str(data["id"])
    if isinstance(body, list) and body and isinstance(body[0], dict) and "id" in body[0]:
        return str(body[0]["id"])
    return None


def _no_other_data(other_body: Any, owner_body: Any) -> bool:
    """读越权粗判：他人响应体不应包含属主数据的关键 id（避免误判为通过）。"""
    owner_id = _extract_id(owner_body)
    if not owner_id:
        return True  # 无属主 id 可比 → 不误判
    txt = (
        json.dumps(other_body, ensure_ascii=False)
        if not isinstance(other_body, str)
        else other_body
    )
    return owner_id not in txt


def retest_case(
    case: CaseSpec,
    owner: RetestSession,
    other: RetestSession | None,
    base_url: str,
) -> dict[str, Any]:
    """复测单条 G-9 越权用例，返回结构化结论（pass / fail / skipped）。

    不抛异常：任一环节不可复测（无 path / 无他人凭证 / 属主取不到 id）一律降级为
    skipped 并写明原因，绝不伪造 PASS。
    """
    step = _primary_step(case)
    path = str(step.get("path") or "").strip()
    if not path:
        return {
            "tc_no": case.tc_no,
            "title": case.title,
            "result": "skipped",
            "reason": "用例无 path，无法构造复测请求",
        }
    if other is None:
        return {
            "tc_no": case.tc_no,
            "title": case.title,
            "result": "skipped",
            "reason": "仅提供属主身份、无他人凭证：无法复测越权，保留生成期诚实 SKIPPED 结论",
        }

    # 1) 属主身份取真实 resource_id
    base = base_url.rstrip("/")
    st_owner, body_owner = owner.request("GET", base + _strip_param(path))
    rid = _extract_id(body_owner)
    if rid is None:
        return {
            "tc_no": case.tc_no,
            "title": case.title,
            "result": "skipped",
            "owner_status": st_owner,
            "reason": "属主身份未取得可复测的 resource_id（接口未返回 id），无法构造他人资源请求",
        }

    target = base + _materialize(path, rid)
    write = Dimension.TENANT_WRITE.value in _case_dims(case)
    if write:
        method = str(step.get("method") or "PUT").upper()
        st, _body = other.request(method, target, json={})
        passed = st in (401, 403, 404)
        return {
            "tc_no": case.tc_no,
            "title": case.title,
            "result": "pass" if passed else "fail",
            "status": st,
            "resource_id": rid,
            "reason": (
                "他人身份越权写被拒（401/403/404），隔离有效"
                if passed
                else "他人身份越权写未被拒绝 = 高危越权"
            ),
        }
    # 读越权
    st, body = other.request("GET", target)
    passed = st in (401, 403, 404) and _no_other_data(body, body_owner)
    return {
        "tc_no": case.tc_no,
        "title": case.title,
        "result": "pass" if passed else "fail",
        "status": st,
        "resource_id": rid,
        "reason": (
            "他人身份越权读被拒且不泄露属主数据，隔离有效"
            if passed
            else "他人身份可读到属主数据 = 越权泄露"
        ),
    }


def retest_all(
    cases: list[CaseSpec],
    owner: RetestSession,
    other: RetestSession | None,
    base_url: str,
) -> dict[str, Any]:
    """复测全部 G-9 用例，返回汇总（含逐条结论）。"""
    tenant = tenant_cases_of(cases)
    rows = [retest_case(c, owner, other, base_url) for c in tenant]
    counts = {"pass": 0, "fail": 0, "skipped": 0}
    for r in rows:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    return {
        "total_tenant_cases": len(tenant),
        "counts": counts,
        "results": rows,
    }
