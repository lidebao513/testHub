"""G5（缺陷 ORM 直写）真实 Defect 表落库测试。

与 test_security_verdict_endpoint.py 的\"纯离线 mock create\"互补：本文件用
Django TestCase 真实建 Project/User → 调 create_security_defect_orm → 断言
真实 Defect 表字段无损落库，兑现 #237 完成标准「真实 Defect 表落库字段无误、
单测断言 payload 完整」。
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.defects.models import Defect
from apps.projects.models import Project
from apps.testgen_integration.defects import create_security_defect_orm
from apps.testgen_integration.flow import (
    PRIORITY_FOR_UNSAFE,
    SEVERITY_FOR_UNSAFE,
)


class TestG5DefectPersistence(TestCase):
    """G5 缺陷 payload 真实落库：字段完整、枚举合法、FK 正确。"""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="g5_reporter", password="x-Temp#123"
        )
        cls.project = Project.objects.create(name="G5项目", owner=cls.user)

    def _payload(self, **overrides):
        payload = {
            "title": "[安全] 越权访问 应拒却放通",
            "description": "testgen 安全判定：应拒访问被放通\n维度：越权\n证据：HTTP 200",
            "severity": SEVERITY_FOR_UNSAFE,
            "priority": PRIORITY_FOR_UNSAFE,
            "defect_type": "security",
            "source": "api_testing",
            "project_id": self.project.id,
            "reporter_id": self.user.id,
        }
        payload.update(overrides)
        return payload

    def test_full_payload_persists_to_real_defect_table(self):
        """完整 payload 真实落库：所有字段无损、枚举合法、code 自动生成。"""
        defect = create_security_defect_orm(self._payload())

        # 真实对象从 DB 回读，确保不是内存态假象
        saved = Defect.objects.get(pk=defect.pk)
        self.assertEqual(saved.title, "[安全] 越权访问 应拒却放通")
        self.assertIn("越权", saved.description)
        self.assertEqual(saved.severity, "critical")
        self.assertEqual(saved.priority, "p0")
        self.assertEqual(saved.defect_type, "security")
        self.assertEqual(saved.source, "api_testing")
        self.assertEqual(saved.project_id, self.project.id)
        self.assertEqual(saved.reporter_id, self.user.id)
        self.assertIsNone(saved.related_testcase_id)  # 可空 FK 缺省
        self.assertEqual(saved.status, "new")  # 模型默认
        self.assertTrue(saved.code.startswith("BUG-"))  # save() 自动生成编号

    def test_related_testcase_id_nullable(self):
        """related_testcase_id 缺省落库为 NULL，不抛错。"""
        defect = create_security_defect_orm(self._payload())
        self.assertIsNone(defect.related_testcase_id)

    def test_payload_enum_values_are_model_valid(self):
        """G5 使用的枚举值全部在 Defect 真实 choices 内（拒绝静默错值）。"""
        p = self._payload()
        self.assertIn(p["severity"], [c[0] for c in Defect.SEVERITY_CHOICES])
        self.assertIn(p["priority"], [c[0] for c in Defect.PRIORITY_CHOICES])
        self.assertIn(p["defect_type"], [c[0] for c in Defect.TYPE_CHOICES])
        self.assertIn(p["source"], [c[0] for c in Defect.SOURCE_CHOICES])
