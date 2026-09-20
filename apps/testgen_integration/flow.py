"""回流编排：testgen 用例 → testhub TestCase（幂等 upsert，逻辑与 Django 解耦）。

把「调用 testgen → 映射 → 写库」的编排收口在此模块，``upsert`` 回调由调用方注入，
使得回流逻辑可在无 Django 环境下单测：

- 真实环境：``upsert(testgen_id, payload)`` 写入 ``apps.testcases.models.TestCase``，
  以 ``tg:<testgen_id>`` 标签做幂等 update_or_create；
- 单测环境：内存 dict 模拟 ``upsert``，断言幂等与计数。

幂等键：``testgen_id``（= tc_no，稳定内容指纹）。相同指纹重复回流 → 更新而非新建，
不会翻倍用例。
"""
from __future__ import annotations

from typing import Any, Callable

from .mapping import case_spec_to_testcase, testgen_id_of

# upsert 回调返回值约定
CREATED = "created"
UPDATED = "updated"
SKIPPED = "skipped"


def sync_cases(
    cases: list[dict[str, Any]],
    *,
    project_id: int,
    author_id: int,
    upsert: Callable[[str, dict[str, Any]], str],
    suite_id: int | None = None,
) -> dict[str, int]:
    """把 testgen 用例列表映射并回流到 testhub。

    参数：
      cases        testgen 用例 dict 列表
      project_id   testhub 项目 id
      author_id    testhub 用户 id（TestCase.author 必填）
      upsert       注入的写库回调，返回 CREATED/UPDATED/SKIPPED
      suite_id     可选，仅作追溯写入 description

    返回：{"total","created","updated","skipped"}
    """
    created = updated = skipped = 0
    for cs in cases:
        tg_id = testgen_id_of(cs)
        if not tg_id:
            # 无稳定指纹，无法幂等，跳过避免重复污染
            skipped += 1
            continue
        payload = case_spec_to_testcase(
            cs, project_id=project_id, author_id=author_id, suite_id=suite_id
        )
        result = upsert(tg_id, payload)
        if result == CREATED:
            created += 1
        elif result == UPDATED:
            updated += 1
        else:
            skipped += 1
    return {
        "total": len(cases),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


# ============================================================================
# M2：安全语义裁判编排（security 用例执行后 → 调 testgen /verdict → 应拒却放通自动建缺陷）
# ============================================================================
SEVERITY_FOR_UNSAFE = "critical"  # 应拒却放通 = 高危
PRIORITY_FOR_UNSAFE = "p0"


def is_security_case(tags: list[str] | None) -> bool:
    """该用例是否带 security 标记（M1 回流时 `security` 维度会写入 tags）。"""
    return "security" in (tags or [])


def build_verdict_facts(case: dict[str, Any]) -> dict[str, Any]:
    """从一条「已执行的 security 用例观测」构造发给 testgen /verdict 的 facts。

    只搬运 category/dimension/observed 结果，**绝不**夹带账号密码等凭证明文。
    category/dimension 优先用 testgen 原生中文（case_type/dimension 列），
    缺失时由调用方以英文 tag 提供亦可（verdict 端点会做双语归一化）。
    """
    return {
        "category": case.get("case_type") or case.get("category") or "",
        "dimension": case.get("dimension") or "",
        "auth_mode": case.get("auth_mode") or "",
        "observed_status": case.get("observed_status"),
        "observed_body_has_sensitive": bool(case.get("observed_body_has_sensitive", False)),
        "expected_denied": case.get("expected_denied", False),
        "tenant_identity": case.get("tenant_identity") or "",
        "raw_evidence": case.get("raw_evidence") or {},
    }


def run_security_verdict(
    observations: list[dict[str, Any]],
    *,
    verdict_fn: "Callable[[dict[str, Any]], dict[str, Any]] | None" = None,
    create_defect: "Callable[[dict[str, Any]], Any] | None" = None,
    reporter_id: int | None = None,
) -> dict[str, Any]:
    """对 security 用例执行观测做安全语义裁判，应拒却放通时自动建缺陷。

    参数：
      observations   每条 = {case_id, title, tags, project_id, testcase_id?,
                            reporter_id?, case_type/dimension/observed_status/observed_body_has_sensitive/
                            expected_denied/tenant_identity/raw_evidence ...}
      verdict_fn      注入式调 testgen /verdict（默认 client.verdict）
      create_defect   注入式建缺陷（默认 client.create_security_defect；
                      真实环境更推荐直接 ``Defect.objects.create``，见 client.create_security_defect 说明）
      reporter_id     可选，统一注入执行触发人 id（Defect.reporter 为必填 FK；不传则取 obs.reporter_id）

    返回：{total, security_cases, unsafe, defects_created, skipped, details}
    """
    from . import client as tg_client  # 延迟导入，避免循环 + 便于单测注入

    vfn = verdict_fn or tg_client.verdict
    cfn = create_defect or tg_client.create_security_defect

    security = unsafe = defects_created = skipped = 0
    details: list[dict[str, Any]] = []
    for obs in observations:
        if not is_security_case(obs.get("tags")):
            skipped += 1  # 非 security 用例不调 /verdict（M2 验收：避免无谓开销）
            continue
        security += 1
        facts = build_verdict_facts(obs)
        try:
            result = vfn(facts)
        except Exception as exc:  # noqa: BLE001 - 单条裁判失败不应中断整批
            details.append({"case_id": obs.get("case_id"), "verdict": "error", "error": str(exc)})
            continue

        if result.get("verdict") == "unsafe":
            unsafe += 1
            payload = {
                "title": f"[安全] {obs.get('title', obs.get('case_id', '未知用例'))} 应拒却放通",
                "description": (
                    f"testgen 安全判定：{result.get('reason')}\n"
                    f"维度：{result.get('dimension')}\n证据：{result.get('evidence')}"
                ),
                "severity": SEVERITY_FOR_UNSAFE,
                "priority": PRIORITY_FOR_UNSAFE,
                "defect_type": "security",  # Defect.defect_type 有效枚举（真实模型无 source=testgen_verdict）
                # source 须为 Defect.SOURCE_CHOICES 有效值；testgen 经 testhub 自动化触发判定，
                # 暂映射到 api_testing。更优解：Defect 模型增加 'testgen' 来源枚举（需迁移），见文档 M2-R1。
                "source": "api_testing",
                "related_testcase_id": obs.get("testcase_id"),
                "project_id": obs.get("project_id"),
                # reporter 为 Defect 必填 FK；真实环境由执行钩子注入 reporter_id（当前观测缺省则取 obs.reporter_id）
                "reporter_id": reporter_id if reporter_id is not None else obs.get("reporter_id"),
            }
            try:
                cfn(payload)
                defects_created += 1
                details.append(
                    {"case_id": obs.get("case_id"), "verdict": "unsafe", "defect": "created"}
                )
            except Exception as exc:  # noqa: BLE001
                details.append(
                    {
                        "case_id": obs.get("case_id"),
                        "verdict": "unsafe",
                        "defect": "failed",
                        "error": str(exc),
                    }
                )
        else:
            details.append({"case_id": obs.get("case_id"), "verdict": result.get("verdict")})

    return {
        "total": len(observations),
        "security_cases": security,
        "unsafe": unsafe,
        "defects_created": defects_created,
        "skipped": skipped,
        "details": details,
    }
