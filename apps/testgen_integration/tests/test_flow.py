"""flow 纯单测：幂等 upsert 编排（注入式 upsert，不依赖 Django）。

验证：相同 tc_no 重复回流 → 更新而非新建（不翻倍）；无指纹用例被跳过。
"""
from __future__ import annotations

import unittest

from apps.testgen_integration import flow


class _MemoryUpsert:
    """内存模拟 testhub TestCase 写库，按 (project_id, tg_id) 幂等。"""

    def __init__(self) -> None:
        self._store: dict[tuple[int, str], dict] = {}

    def __call__(self, testgen_id: str, payload: dict) -> str:
        key = (payload["project_id"], testgen_id)
        if key in self._store:
            self._store[key] = payload
            return flow.UPDATED
        self._store[key] = payload
        return flow.CREATED

    def count(self) -> int:
        return len(self._store)


def _case(tc_no: str, case_type: str = "正常", priority: str = "P2") -> dict:
    return {"tc_no": tc_no, "title": f"用例 {tc_no}", "case_type": case_type, "priority": priority}


class TestSyncCasesIdempotent(unittest.TestCase):
    def test_created_then_updated_is_idempotent(self):
        upsert = _MemoryUpsert()
        cases = [_case("TP-1"), _case("TP-2"), _case("TP-3")]

        r1 = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        self.assertEqual(r1["created"], 3)
        self.assertEqual(r1["updated"], 0)
        self.assertEqual(upsert.count(), 3)

        # 再次回流相同用例 → 全部更新，不新增
        r2 = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        self.assertEqual(r2["created"], 0)
        self.assertEqual(r2["updated"], 3)
        self.assertEqual(upsert.count(), 3)  # 仍为 3 条，未翻倍

    def test_no_fingerprint_skipped(self):
        upsert = _MemoryUpsert()
        cases = [_case("TP-1"), {"title": "无指纹", "case_type": "正常"}]
        r = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        self.assertEqual(r["created"], 1)
        self.assertEqual(r["skipped"], 1)

    def test_tp_id_fallback_as_fingerprint(self):
        upsert = _MemoryUpsert()
        cases = [{"tp_id": "TP-FALLBACK", "title": "仅 tp_id", "case_type": "安全", "priority": "P1"}]
        r = flow.sync_cases(cases, project_id=1, author_id=1, upsert=upsert)
        self.assertEqual(r["created"], 1)
        self.assertEqual(upsert.count(), 1)

    def test_different_projects_keep_separate(self):
        upsert = _MemoryUpsert()
        flow.sync_cases([_case("TP-1")], project_id=1, author_id=1, upsert=upsert)
        flow.sync_cases([_case("TP-1")], project_id=2, author_id=1, upsert=upsert)
        self.assertEqual(upsert.count(), 2)  # 不同项目各有 1 条


if __name__ == "__main__":
    unittest.main()
