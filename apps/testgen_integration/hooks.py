"""G4 执行后安全裁判钩子：用例（仅 security 标记）执行成功路径末尾触发 run_security_verdict。

设计原则：
- 与 Django / 具体执行引擎解耦：核心函数接受归一化输入（obj + passed），``verdict_fn`` /
  ``create_defect`` 均可注入，便于在离线环境单测；
- best-effort：任何异常都被吸收，**绝不**中断原用例执行流程（G4 是安全增强，不是主链路）；
- 复用 M2 已就绪的 ``flow.run_security_verdict`` + G5 的 ``create_security_defect_orm`` 直写，
  编排逻辑与 G3 端点完全一致，避免重复实现。

接线位置（见各执行引擎）：
- ``apps/ui_automation/views.py::TestCaseViewSet.run`` 单用例执行成功末尾；
- ``apps/api_testing/views.py::TestSuiteViewSet.execute`` 套件执行成功末尾。
"""

from __future__ import annotations

from typing import Any

from . import flow
from .client import verdict as _default_verdict_fn
from .defects import create_security_defect_orm as _default_create_defect


def _is_canonical_project(project: Any) -> bool:
    """该用例的 project 是否就是缺陷所需的规范 ``apps.projects.Project``。

    仅规范 ``apps.testcases.TestCase`` 的 ``project`` 指向 ``apps.projects.Project``；
    引擎 ``ui_automation.TestCase.project``→``UiProject``、``api_testing.ApiRequest``→
    ``ApiCollection``→``ApiProject``，三者与 ``Defect.project`` 指向的 ``apps.projects.Project``
    **不是同一张表**。故引擎用例无法直接解析出缺陷可用的 project_id。
    """
    if project is None:
        return False
    return getattr(project, "_meta", None) and project._meta.label == "projects.Project"


def resolve_canonical_project_id(obj: Any) -> int | None:
    """解析缺陷可用的规范 ``apps.projects.Project`` id。

    决策（见 整合实施_未完成任务追踪.md G4 缺口 / 二选一）：
    - 选 ②：**security 用例统一收敛到规范 ``apps.testcases.TestCase``**（其 project 已是
      ``apps.projects.Project``，G5 已验证可直落缺陷）；
    - 拒绝 ①：不在钩子里猜 ``UiProject/ApiProject → apps.projects.Project`` 映射——
      两表无任何关联字段，猜映射会把安全缺陷挂到错误项目（比不建更糟）。
    因此：只有规范 TestCase 能解析出 project_id；引擎用例返回 ``None``（后续由
    build_execution_observation 标 ``skipped_no_project``，verdict 仍照常跑）。
    """
    project = getattr(obj, "project", None)
    if _is_canonical_project(project):
        return project.id
    return None


def is_security_case_obj(obj: Any) -> bool:
    """该被执行对象是否带 security 标记。

    兼容两种标记来源：
      - ``tags`` 列表含 ``"security"``（testgen 回流写入 apps.testcases.TestCase.tags，
        以及本仓库 ui_automation.TestCase / api_testing.ApiRequest 新增的 tags 字段）；
      - ``test_type == "security"``（apps.testcases.TestCase 的枚举值）。
    """
    tags = getattr(obj, "tags", None)
    if isinstance(tags, list) and "security" in tags:
        return True
    return getattr(obj, "test_type", None) == "security"


def build_execution_observation(
    obj: Any,
    *,
    passed: bool,
    project_id: int | None = None,
    extra_facts: dict | None = None,
) -> dict[str, Any]:
    """从一条「已执行的用例」构造发给 flow.run_security_verdict 的 observation。

    只搬运不涉凭证的元数据；``observed_status`` 由执行结果推导（pass/fail）。
    安全语义 facts（expected_denied / dimension / case_type / tenant_identity 等）若存在
    于 ``extra_facts`` 则一并透传，否则由 /verdict 端点按缺省判定（安全默认：不误报）。
    """
    obs: dict[str, Any] = {
        "case_id": getattr(obj, "id", None),
        "title": getattr(obj, "name", None) or getattr(obj, "title", None),
        "tags": list(getattr(obj, "tags", []) or []),
        # 决策②：project_id 只允许来自规范 apps.projects.Project；
        # 引擎用例（UiProject/ApiProject）无关联，解析为 None → 走 skipped_no_project（不猜映射）。
        "project_id": project_id
        if project_id is not None
        else resolve_canonical_project_id(obj),
        "testcase_id": None,
        "observed_status": "pass" if passed else "fail",
    }
    if extra_facts:
        obs.update(extra_facts)
    return obs


def trigger_security_verdict_after_execution(
    items: list[Any],
    *,
    reporter: Any,
    default_project_id: int | None = None,
    verdict_fn: Any = None,
    create_defect: Any = None,
) -> dict[str, Any]:
    """在用例执行成功后批量触发安全裁判。

    参数：
      items              每项可为：
                         - ``(obj, passed: bool)`` 元组（推荐，obj 为被执行模型实例）；
                         - 已归一化的 observation dict（含 ``tags``）。
      reporter           触发人（User 实例），提供 ``reporter_id``；
      default_project_id 调用方已确知「规范 apps.projects.Project」id 时强制注入
                         （如规范 apps.testcases.TestCase 执行路径已知其 project）。
                         留空时由 ``resolve_canonical_project_id`` 推导：仅规范
                         TestCase 能解析，引擎用例（UiProject/ApiProject 无关联）解析为
                         None → 该用例 verdict 照常跑、缺陷建单走诚实的 ``skipped_no_project``
                         （决策②：拒绝在钩子里猜 UiProject/ApiProject→Project 的脆弱映射）。
      verdict_fn         注入式调 testgen /verdict（默认 client.verdict）
      create_defect      注入式建缺陷（默认 create_security_defect_orm，G5 直写）

    返回：flow.run_security_verdict 的汇总；异常时返回安全的降级汇总（best-effort）。
    """
    vfn = verdict_fn or _default_verdict_fn
    cfn = create_defect or _default_create_defect

    observations: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, dict):
            obs = dict(it)
            obs.setdefault("tags", [])
            observations.append(obs)
            continue
        obj, passed = it
        if not is_security_case_obj(obj):
            continue  # 非 security 用例不触发 /verdict（避免无谓开销）
        observations.append(
            build_execution_observation(
                obj, passed=passed, project_id=default_project_id
            )
        )

    if not observations:
        return {
            "total": len(items),
            "security_cases": 0,
            "unsafe": 0,
            "defects_created": 0,
            "skipped": len(items),
            "details": [],
            "hook": "no_security_cases",
        }

    reporter_id = getattr(reporter, "id", None)
    try:
        return flow.run_security_verdict(
            observations, verdict_fn=vfn, create_defect=cfn, reporter_id=reporter_id
        )
    except Exception:  # noqa: BLE001 - best-effort：钩子失败绝不中断原执行
        return {
            "total": len(items),
            "security_cases": len(observations),
            "unsafe": 0,
            "defects_created": 0,
            "skipped": 0,
            "details": [{"error": "g4_hook_failed"}],
            "hook": "exception",
        }
