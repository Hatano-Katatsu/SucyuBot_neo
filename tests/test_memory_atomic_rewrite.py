from __future__ import annotations

import asyncio
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import AsyncMock

from tests.support import ServiceFixtureMixin


class AtomicMemoryRewriteTestCase(ServiceFixtureMixin, unittest.TestCase):
    @staticmethod
    def _active_ids(service, session_id: str, character: str) -> set[int]:
        return {
            int(item["id"])
            for item in service.memory.list_memories(session_id, character=character, limit=100)
        }

    def test_valid_candidate_with_multiple_invalid_candidates_keeps_old_set(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:memory-invalid-candidates"
            character = "角色A"
            for index in range(2):
                service.memory.add_memory(
                    session_id,
                    "event",
                    f"旧记忆 {index}",
                    character=character,
                    source="chat",
                )
            editable = service.memory.list_memories(session_id, character=character, limit=20)
            before_ids = self._active_ids(service, session_id, character)
            candidates = [
                {"kind": "event", "summary": "看似有效的新记忆", "importance": 4, "tags": ["新"]},
                {"kind": "event", "summary": "", "importance": 3, "tags": []},
                "不是对象",
                {"kind": "event", "importance": 3, "tags": []},
            ]
            service._call_memory_json_llm = AsyncMock(
                return_value=("raw", {"memories": candidates}, "chat", [])
            )

            result = await service._summarize_all_memories(
                session_id, character, editable, target_n=4
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["deactivated"], 0)
            self.assertEqual(self._active_ids(service, session_id, character), before_ids)
            all_rows = service.memory.list_memories(
                session_id, character=character, limit=100, include_inactive=True
            )
            self.assertFalse(any(row["summary"] == "看似有效的新记忆" for row in all_rows))

        asyncio.run(run())

    def test_failed_full_rewrite_does_not_run_follow_up_profile_merge(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:memory-failed-follow-up"
            character = "角色A"
            service.config["long_memory_context_limit"] = "1"
            for summary in ("用户喜欢夜跑", "用户留着短发"):
                service.memory.add_memory(
                    session_id,
                    "user_profile",
                    summary,
                    character=character,
                    source="chat",
                )
            service.memory.add_memory(
                session_id,
                "event",
                "用于触发全量重写的第三条记忆",
                character=character,
                source="chat",
            )
            before = service.memory.list_memories(
                session_id, character=character, limit=100, include_inactive=True
            )
            service._call_memory_json_llm = AsyncMock(return_value=(
                "raw",
                {"memories": [
                    {"kind": "event", "summary": "有效项", "importance": 3, "tags": []},
                    {"kind": "event", "summary": "", "importance": 3, "tags": []},
                ]},
                "chat",
                [],
            ))
            service.has_llm_config = lambda purpose, session_id="": purpose == "chat"

            result = await service._organize_memories_after_dream(session_id, character)

            self.assertEqual(result["status"], "failed")
            after = service.memory.list_memories(
                session_id, character=character, limit=100, include_inactive=True
            )
            self.assertEqual(
                [(row["id"], row["status"], row["summary"]) for row in after],
                [(row["id"], row["status"], row["summary"]) for row in before],
            )

        asyncio.run(run())

    def test_middle_insert_failure_rolls_back_new_and_old_sets(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:memory-write-failure"
            character = "角色A"
            for index in range(2):
                service.memory.add_memory(
                    session_id,
                    "event",
                    f"旧记忆 {index}",
                    character=character,
                    source="chat",
                )
            editable = service.memory.list_memories(session_id, character=character, limit=20)
            before_ids = self._active_ids(service, session_id, character)
            with closing(sqlite3.connect(service.memory.path)) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER fail_second_rewrite_insert
                    BEFORE INSERT ON memories
                    WHEN NEW.source = 'dream-summarize' AND NEW.summary = '第二条触发失败'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced atomic rewrite failure');
                    END
                    """
                )
                conn.commit()
            candidates = [
                {"kind": "event", "summary": "第一条本应回滚", "importance": 4, "tags": ["新"]},
                {"kind": "event", "summary": "第二条触发失败", "importance": 4, "tags": ["新"]},
            ]
            service._call_memory_json_llm = AsyncMock(
                return_value=("raw", {"memories": candidates}, "chat", [])
            )

            result = await service._summarize_all_memories(
                session_id, character, editable, target_n=2
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["deactivated"], 0)
            self.assertEqual(self._active_ids(service, session_id, character), before_ids)
            all_summaries = {
                row["summary"]
                for row in service.memory.list_memories(
                    session_id, character=character, limit=100, include_inactive=True
                )
            }
            self.assertNotIn("第一条本应回滚", all_summaries)
            self.assertNotIn("第二条触发失败", all_summaries)

        asyncio.run(run())

    def test_success_replaces_only_included_non_manual_memories(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:memory-atomic-success"
            character = "角色A"
            included_ids = {
                int(service.memory.add_memory(
                    session_id, "event", f"待重写旧记忆 {index}",
                    character=character, source="chat",
                ))
                for index in range(2)
            }
            omitted_id = int(service.memory.add_memory(
                session_id, "event", "预算外旧记忆必须保留",
                character=character, source="chat",
            ))
            manual_id = int(service.memory.add_memory(
                session_id, "manual", "手动记忆必须保留",
                character=character, source="manual",
            ))
            editable = [
                row
                for row in service.memory.list_memories(session_id, character=character, limit=20)
                if row["kind"] != "manual"
            ]
            service._format_memory_summarize_input = lambda _editable: (
                "included subset",
                set(included_ids),
                1,
            )
            candidates = [
                {"kind": "user_profile", "summary": "用户偏爱安静环境", "importance": 5, "tags": ["偏好"]},
                {"kind": "event", "summary": "角色答应周末见面", "importance": 4, "tags": ["约定"]},
            ]
            service._call_memory_json_llm = AsyncMock(
                return_value=("raw", {"memories": candidates}, "chat", [])
            )

            result = await service._summarize_all_memories(
                session_id, character, editable, target_n=2
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["added"], 2)
            self.assertEqual(result["deactivated"], 2)
            active = service.memory.list_memories(session_id, character=character, limit=100)
            active_ids = {int(row["id"]) for row in active}
            active_summaries = {row["summary"] for row in active}
            self.assertTrue(included_ids.isdisjoint(active_ids))
            self.assertIn(omitted_id, active_ids)
            self.assertIn(manual_id, active_ids)
            self.assertIn("用户偏爱安静环境", active_summaries)
            self.assertIn("角色答应周末见面", active_summaries)

            all_rows = service.memory.list_memories(
                session_id, character=character, limit=100, include_inactive=True
            )
            status_by_id = {int(row["id"]): row["status"] for row in all_rows}
            self.assertTrue(all(status_by_id[memory_id] == "deleted" for memory_id in included_ids))
            self.assertEqual(status_by_id[omitted_id], "active")
            self.assertEqual(status_by_id[manual_id], "active")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
