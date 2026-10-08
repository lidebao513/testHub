"""G4 执行后安全裁判钩子离线自测。

完全解耦 Django / 执行引擎：注入 verdict_fn + create_defect，断言钩子的
过滤 / 触发 / 优雅失败 / 绝不中断 行为。在 backend.test_settings（sqlite）下 pytest 运行。
"""

from __future__ import annotations

from apps.testgen_integration import hooks


class FakeProject:
    """最小 project 桩：仅提供 _meta.label 与 id，用于模拟规范/引擎两种 project。"""

    def __init__(self, label, pid):
        self._meta = type("M", (), {"label": label})()
        self.id = pid


class FakeCase:
    """最小被执行对象桩：带 tags / id / name / project（规范或引擎）/ project_id。"""

    def __init__(self, cid, name, tags, project=None, project_id=None, test_type=None):
        self.id = cid
        self.name = name
        self.tags = tags
        self.project = project
        self.project_id = project_id
        self.test_type = test_type


class FakeUser:
    def __init__(self, uid=42):
        self.id = uid


def _unsafe_verdict(facts):
    return {
        "verdict": "unsafe",
        "reason": "应拒却放通",
        "dimension": facts.get("dimension", "authz"),
        "evidence": "mock",
    }


def _safe_verdict(facts):
    return {"verdict": "safe"}


# ---------------------------------------------------------------------------
# 单元：标记识别与 observation 构造
# ---------------------------------------------------------------------------
def test_is_security_case_obj_by_tag():
    assert hooks.is_security_case_obj(FakeCase(1, "a", ["security"])) is True
    assert hooks.is_security_case_obj(FakeCase(2, "b", ["normal", "smoke"])) is False
    assert hooks.is_security_case_obj(FakeCase(3, "c", [])) is False


def test_is_security_case_obj_by_test_type():
    assert (
        hooks.is_security_case_obj(FakeCase(4, "d", [], test_type="security")) is True
    )
    assert (
        hooks.is_security_case_obj(FakeCase(5, "e", [], test_type="functional"))
        is False
    )


def test_build_execution_observation_derives_status():
    obs = hooks.build_execution_observation(
        FakeCase(7, "g", ["security"], project=FakeProject("projects.Project", 11)),
        passed=True,
    )
    assert obs["observed_status"] == "pass"
    assert obs["project_id"] == 11
    assert obs["tags"] == ["security"]
    assert obs["title"] == "g"

    obs2 = hooks.build_execution_observation(
        FakeCase(8, "h", ["security"], project=FakeProject("projects.Project", 99)),
        passed=False,
    )
    assert obs2["observed_status"] == "fail"
    assert obs2["project_id"] == 99


# ---------------------------------------------------------------------------
# 集成：钩子编排
# ---------------------------------------------------------------------------
def test_non_security_cases_skipped():
    verdict_calls = []
    defect_calls = []

    def vfn(facts):
        verdict_calls.append(facts)
        return _unsafe_verdict(facts)

    def cfn(payload):
        defect_calls.append(payload)

    result = hooks.trigger_security_verdict_after_execution(
        [(FakeCase(1, "a", ["smoke"]), True), (FakeCase(2, "b", ["normal"]), True)],
        reporter=FakeUser(),
        verdict_fn=vfn,
        create_defect=cfn,
    )
    assert result["security_cases"] == 0
    assert result["skipped"] == 2
    assert result["defects_created"] == 0
    assert verdict_calls == []  # 非 security 不调 /verdict
    assert defect_calls == []


def test_security_case_triggers_verdict_and_defect():
    defect_calls = []

    result = hooks.trigger_security_verdict_after_execution(
        [
            (
                FakeCase(
                    2, "sec-b", ["security"], project=FakeProject("projects.Project", 5)
                ),
                True,
            )
        ],
        reporter=FakeUser(42),
        verdict_fn=_unsafe_verdict,
        create_defect=lambda p: defect_calls.append(p),
    )
    assert result["security_cases"] == 1
    assert result["unsafe"] == 1
    assert result["defects_created"] == 1
    assert len(defect_calls) == 1
    payload = defect_calls[0]
    # 对齐 G5 直写 payload 契约
    assert payload["project_id"] == 5
    assert payload["reporter_id"] == 42
    assert payload["defect_type"] == "security"
    assert payload["source"] == "api_testing"
    assert payload["severity"] == "critical"
    assert payload["priority"] == "p0"
    assert payload["title"].startswith("[安全]")


def test_security_case_safe_verdict_no_defect():
    defect_calls = []

    result = hooks.trigger_security_verdict_after_execution(
        [
            (
                FakeCase(
                    3, "sec-c", ["security"], project=FakeProject("projects.Project", 5)
                ),
                True,
            )
        ],
        reporter=FakeUser(),
        verdict_fn=_safe_verdict,
        create_defect=lambda p: defect_calls.append(p),
    )
    assert result["security_cases"] == 1
    assert result["unsafe"] == 0
    assert result["defects_created"] == 0
    assert defect_calls == []


def test_missing_project_id_defect_skip_no_project():
    """project_id 缺失（引擎用例 UiProject/ApiProject 无规范 project 关联）→
    决策②：verdict 仍跑、缺陷建单诚实 skipped_no_project（不猜映射、不误建、不崩）。
    """
    result = hooks.trigger_security_verdict_after_execution(
        [(FakeCase(4, "sec-d", ["security"]), True)],  # 无 project_id
        reporter=FakeUser(),
        verdict_fn=_unsafe_verdict,
        create_defect=lambda p: (_ for _ in ()).throw(ValueError("missing project_id")),
    )
    assert result["security_cases"] == 1
    assert result["unsafe"] == 1
    assert result["defects_created"] == 0
    assert result["details"][0]["defect"] == "skipped_no_project"
    assert (
        result.get("hook") != "exception"
    )  # 走的是 flow 内部诚实跳过，不是钩子外层兜底


def test_engine_case_resolves_no_canonical_project():
    """引擎用例（.project 指向 UiProject/ApiProject）→ resolve_canonical_project_id 返回 None，
    绝不直接拿引擎 project_id 去建缺陷（拒绝①脆弱映射）。
    """
    from apps.testgen_integration.hooks import resolve_canonical_project_id

    # 引擎用例：.project 是 UiProject（label 非 projects.Project）
    engine = FakeCase(
        1, "e", ["security"], project=FakeProject("ui_automation.UiProject", 88)
    )
    assert resolve_canonical_project_id(engine) is None

    # 规范用例：.project 是 apps.projects.Project
    canonical = FakeCase(
        2, "c", ["security"], project=FakeProject("projects.Project", 77)
    )
    assert resolve_canonical_project_id(canonical) == 77


def test_verdict_fn_raises_is_absorbed():
    """verdict_fn 崩溃 → flow 内部记 error，钩子整体不崩。"""
    result = hooks.trigger_security_verdict_after_execution(
        [
            (
                FakeCase(
                    5, "sec-e", ["security"], project=FakeProject("projects.Project", 5)
                ),
                True,
            )
        ],
        reporter=FakeUser(),
        verdict_fn=lambda facts: (_ for _ in ()).throw(RuntimeError("boom")),
        create_defect=lambda p: None,
    )
    assert result["security_cases"] == 1
    assert result["unsafe"] == 0
    assert result["defects_created"] == 0
    assert result["details"][0]["verdict"] == "error"


def test_dict_items_accepted():
    defect_calls = []
    result = hooks.trigger_security_verdict_after_execution(
        [
            {
                "case_id": 9,
                "title": "D",
                "tags": ["security"],
                "project_id": 7,
                "observed_status": "pass",
            }
        ],
        reporter=FakeUser(),
        verdict_fn=_unsafe_verdict,
        create_defect=lambda p: defect_calls.append(p),
    )
    assert result["security_cases"] == 1
    assert result["defects_created"] == 1
    assert defect_calls[0]["project_id"] == 7


def test_empty_items_no_security():
    result = hooks.trigger_security_verdict_after_execution(
        [],
        reporter=FakeUser(),
        verdict_fn=_unsafe_verdict,
        create_defect=lambda p: None,
    )
    assert result["hook"] == "no_security_cases"
    assert result["security_cases"] == 0
