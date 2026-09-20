"""mapping 纯单测：testgen 用例 → testhub TestCase payload 字段对齐真实模型。

覆盖：维度(case_type)→test_type、优先级(P0-P3)→priority、标签(来源/维度/幂等指纹)、
预期结果推导（DB 行无 expect 列）、步骤文本渲染与截断(≤1000)、追溯 description。
"""
from __future__ import annotations

import json
import unittest

from apps.testgen_integration import mapping


def _db_case(**over) -> dict:
    """构造一条模拟 testgen DB 行（cases 表字段；doc_steps 为 Json 文本，无 expect 列）。"""
    base = {
        "tc_no": "TP-00000001",
        "title": "示例用例",
        "case_type": "正常",
        "priority": "P2",
        "precondition": "前置条件文本",
        "doc_steps": json.dumps(
            [
                {"kind": "前置", "action": "环境就绪"},
                {"kind": "断言", "action": "发送请求", "expect": "返回 2xx"},
            ]
        ),
        "ctype": "api",
        "tp_id": "TP-00000001",
        "fp_contract_id": "FP-aaaaaaaa",
        "module": "demo",
        "test_type": "全量",
    }
    base.update(over)
    # 若只传了 tc_no 未传 tp_id，则令 tp_id 跟随 tc_no（贴近 testgen 真实数据形态）
    if "tp_id" not in over and "tc_no" in over:
        base["tp_id"] = over["tc_no"]
    return base


class TestMapCaseTypeToTestType(unittest.TestCase):
    def test_security_maps_to_security(self):
        self.assertEqual(
            mapping.map_case_type_to_test_type(_db_case(case_type="安全", ctype="api")),
            "security",
        )

    def test_performance_maps_to_performance(self):
        self.assertEqual(
            mapping.map_case_type_to_test_type(_db_case(case_type="性能")), "performance"
        )

    def test_normal_api_maps_to_api(self):
        self.assertEqual(
            mapping.map_case_type_to_test_type(_db_case(case_type="正常", ctype="api")),
            "api",
        )

    def test_normal_e2e_ui_maps_to_ui(self):
        ui_case = _db_case(
            case_type="正常",
            ctype="e2e",
            doc_steps=json.dumps([{"kind": "页面", "action": "打开 /", "expect": "渲染"}]),
        )
        self.assertEqual(mapping.map_case_type_to_test_type(ui_case), "ui")

    def test_boundary_defaults_to_functional(self):
        self.assertEqual(
            mapping.map_case_type_to_test_type(_db_case(case_type="边界", ctype="e2e")),
            "functional",
        )

    def test_abnormal_defaults_to_functional(self):
        self.assertEqual(
            mapping.map_case_type_to_test_type(_db_case(case_type="异常", ctype="e2e")),
            "functional",
        )


class TestMapPriority(unittest.TestCase):
    def test_p0_to_critical(self):
        self.assertEqual(mapping.map_priority("P0"), "critical")

    def test_p1_to_high(self):
        self.assertEqual(mapping.map_priority("P1"), "high")

    def test_p2_to_medium(self):
        self.assertEqual(mapping.map_priority("P2"), "medium")

    def test_p3_to_low(self):
        self.assertEqual(mapping.map_priority("P3"), "low")

    def test_unknown_defaults_medium(self):
        self.assertEqual(mapping.map_priority(""), "medium")
        self.assertEqual(mapping.map_priority("XYZ"), "medium")


class TestBuildTags(unittest.TestCase):
    def test_source_and_idempotency_tag(self):
        tags = mapping.build_tags(_db_case(tc_no="TP-ABC123"))
        self.assertIn("testgen", tags)
        self.assertIn("tg:TP-ABC123", tags)

    def test_dimension_tag(self):
        self.assertIn("security", mapping.build_tags(_db_case(case_type="安全")))
        self.assertIn("boundary", mapping.build_tags(_db_case(case_type="边界")))
        self.assertIn("abnormal", mapping.build_tags(_db_case(case_type="异常")))
        self.assertIn("normal", mapping.build_tags(_db_case(case_type="正常")))


class TestCaseSpecToTestcase(unittest.TestCase):
    def test_security_case_payload(self):
        cs = _db_case(case_type="安全", priority="P1", tc_no="TP-939f15fd")
        payload = mapping.case_spec_to_testcase(cs, project_id=7, author_id=3)
        self.assertEqual(payload["project_id"], 7)
        self.assertEqual(payload["author_id"], 3)
        self.assertEqual(payload["test_type"], "security")
        self.assertEqual(payload["priority"], "high")
        self.assertIn("testgen", payload["tags"])
        self.assertIn("tg:TP-939f15fd", payload["tags"])
        # DB 行无 expect 列，应从 doc_steps 末步「断言」推导 → 返回 2xx
        self.assertIn("返回 2xx", payload["expected_result"])
        self.assertIn("source=testgen", payload["description"])
        self.assertIn("tp_id=TP-939f15fd", payload["description"])
        # 模型字段名正确（testhub 无 case_type/external_id/source 字段）
        for forbidden in ("case_type", "external_id", "source"):
            self.assertNotIn(forbidden, payload)

    def test_expected_derived_without_expect_column(self):
        # DB 行无 expect 列：应从 doc_steps 末步「断言」推导
        cs = _db_case(
            doc_steps=json.dumps(
                [
                    {"kind": "前置", "action": "x"},
                    {"kind": "断言", "action": "发送", "expect": "返回 2xx 且字段完整"},
                ]
            )
        )
        payload = mapping.case_spec_to_testcase(cs, project_id=1, author_id=1)
        self.assertIn("返回 2xx 且字段完整", payload["expected_result"])

    def test_steps_truncated_to_1000(self):
        big = json.dumps([{"kind": "步骤", "action": "x" * 2000, "expect": "y"}])
        cs = _db_case(doc_steps=big)
        payload = mapping.case_spec_to_testcase(cs, project_id=1, author_id=1)
        self.assertLessEqual(len(payload["steps"]), 1000)

    def test_full_text_preserved_in_description(self):
        big = json.dumps([{"kind": "步骤", "action": "细节" * 500, "expect": "ok"}])
        cs = _db_case(doc_steps=big)
        payload = mapping.case_spec_to_testcase(cs, project_id=1, author_id=1, suite_id=42)
        # description 是 TextField（无限长），保留全文
        self.assertIn("细节" * 500, payload["description"])
        self.assertIn("suite_id=42", payload["description"])

    def test_accepts_casespec_dict_with_expect_key(self):
        # CaseSpec.to_dict() 直接含 expect 字段
        cs = {
            "tc_no": "TP-CASE1",
            "title": "直接用例",
            "case_type": "边界",
            "priority": "P1",
            "precondition": "",
            "doc_steps": [{"kind": "断言", "action": "a", "expect": "400/422"}],
            "expect": "参数缺失返回 400/422",
            "ctype": "",
            "tp_id": "TP-CASE1",
        }
        payload = mapping.case_spec_to_testcase(cs, project_id=1, author_id=1)
        self.assertEqual(payload["test_type"], "functional")
        self.assertEqual(payload["priority"], "high")
        self.assertIn("boundary", payload["tags"])
        self.assertEqual(payload["expected_result"], "参数缺失返回 400/422")


if __name__ == "__main__":
    unittest.main()
