from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

from telegram_comfyui_selfie.scheduler_runtime import compose_organize_source
from tests.support import ServiceFixtureMixin


class ComposeOrganizeSourceTestCase(unittest.TestCase):
    """整理 source 组合纯函数：追加格式与 800 字符截断行为。"""

    def test_appends_entry_to_existing_source(self):
        result = compose_organize_source("chat", "日记提到已和好", date_str="2026-08-17")
        self.assertEqual(result, "chat；整理@2026-08-17: 日记提到已和好")

    def test_empty_existing_source_keeps_entry_only(self):
        result = compose_organize_source("", "依据 checkpoint", date_str="2026-08-17")
        self.assertEqual(result, "整理@2026-08-17: 依据 checkpoint")
        self.assertEqual(compose_organize_source(None, "依据 checkpoint", date_str="2026-08-17"), result)

    def test_overlong_source_keeps_head_and_latest_entry(self):
        existing = "旧来源" * 300  # 900 字符，已超上限
        reason = "最新整理依据" * 40
        result = compose_organize_source(existing, reason, date_str="2026-08-17")
        self.assertLessEqual(len(result), 800)
        # 头部保留原始来源开头，尾部保留最新整理记录
        self.assertTrue(result.startswith("旧来源"))
        self.assertTrue(result.endswith(compose_organize_source("", reason, date_str="2026-08-17")))


class IncrementalOrganizeReasonTestCase(ServiceFixtureMixin, unittest.TestCase):
    """增量整理证据引用：update 写 source、delete 记 ulog、无 reason 兼容、脏 op 隔离。"""

    def _mock_llm(self, svc, ops):
        parsed = {"ops": ops}
        svc._call_memory_json_llm = AsyncMock(return_value=(
            json.dumps(parsed, ensure_ascii=False),
            parsed,
            "chat",
            [],
        ))

    def test_prompt_requires_reason_for_update_and_delete(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            svc.memory.add_memory(sid, "event", "一起去看过电影", character=key)
            captured = {}

            async def fake_call(system, user, **kw):
                captured["system"] = system
                return json.dumps({"ops": []}, ensure_ascii=False)

            svc._call_llm = fake_call
            svc.has_llm_config = lambda purpose, session_id="": True
            editable = svc.memory.list_memories(sid, character=key, limit=10)
            await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            system = captured.get("system") or ""
            self.assertIn('"reason"', system)
            self.assertIn("update or delete op must include", system)

        asyncio.run(run())

    def test_update_op_reason_appended_to_source(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            mid = svc.memory.add_memory(sid, "relationship", "用户在冷战", character=key, source="chat")
            today = time.strftime("%Y-%m-%d")
            self._mock_llm(svc, [{
                "op": "update", "id": mid, "summary": "用户已和好",
                "reason": "日记[2026-08-16]提到两人和好",
            }])
            editable = svc.memory.list_memories(sid, character=key, limit=10)

            result = await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            self.assertEqual(result["status"], "ok")
            row = svc.memory.list_memories(sid, character=key, limit=10)[0]
            self.assertEqual(row["summary"], "用户已和好")
            self.assertEqual(row["source"], f"chat；整理@{today}: 日记[2026-08-16]提到两人和好")

        asyncio.run(run())

    def test_update_op_source_truncates_at_800_keeping_head_and_tail(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            existing_source = "旧来源" * 260  # 780 字符
            reason = "最新整理依据" * 40
            mid = svc.memory.add_memory(sid, "event", "需要整理的事件", character=key, source=existing_source)
            today = time.strftime("%Y-%m-%d")
            self._mock_llm(svc, [{"op": "update", "id": mid, "importance": 4, "reason": reason}])
            editable = svc.memory.list_memories(sid, character=key, limit=10)

            result = await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            self.assertEqual(result["status"], "ok")
            source = svc.memory.list_memories(sid, character=key, limit=10)[0]["source"]
            self.assertLessEqual(len(source), 800)
            self.assertTrue(source.startswith("旧来源"))
            self.assertTrue(source.endswith(f"整理@{today}: {reason[:300]}"))

        asyncio.run(run())

    def test_update_op_without_reason_keeps_source(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            mid = svc.memory.add_memory(sid, "preference", "喜欢草莓", character=key, importance=2, source="chat")
            self._mock_llm(svc, [{"op": "update", "id": mid, "importance": 5}])
            editable = svc.memory.list_memories(sid, character=key, limit=10)

            result = await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            self.assertEqual(result["status"], "ok")
            row = svc.memory.list_memories(sid, character=key, limit=10)[0]
            self.assertEqual(row["importance"], 5)
            self.assertEqual(row["source"], "chat")

        asyncio.run(run())

    def test_delete_op_reason_recorded_in_detail(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            mid = svc.memory.add_memory(sid, "event", "过时的事件", character=key)
            self._mock_llm(svc, [{"op": "delete", "id": mid, "reason": "事件已在日记[2026-08-10]完结"}])
            editable = svc.memory.list_memories(sid, character=key, limit=10)

            result = await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            self.assertEqual(result["status"], "ok")
            self.assertEqual(svc.memory.count_active(sid, character=key), 0)
            self.assertEqual(result["details"][0].get("reason"), "事件已在日记[2026-08-10]完结")

        asyncio.run(run())

    def test_dirty_op_does_not_abort_remaining_ops(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            mid = svc.memory.add_memory(sid, "event", "将被正常删除的事件", character=key)
            self._mock_llm(svc, [
                {"op": "update", "id": "abc-非数字", "summary": "脏数据"},
                {"op": "delete", "id": mid, "reason": "已过时"},
            ])
            editable = svc.memory.list_memories(sid, character=key, limit=10)

            result = await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            # 脏 op 只计入 failed，后续 delete 仍正常执行，dream 不再被单条脏数据中止
            self.assertEqual(result["status"], "partial_failed")
            self.assertEqual(result["applied"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(svc.memory.count_active(sid, character=key), 0)
            self.assertIn("error", result["details"][0])

        asyncio.run(run())

    def test_add_op_with_reason_records_source_note(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            svc.memory.add_memory(sid, "event", "已有记忆", character=key)
            today = time.strftime("%Y-%m-%d")
            self._mock_llm(svc, [{"op": "add", "kind": "preference", "summary": "喜欢爵士乐", "reason": "用户在当前窗口提到"}])
            editable = svc.memory.list_memories(sid, character=key, limit=10)

            result = await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            self.assertEqual(result["status"], "ok")
            rows = {m["summary"]: m for m in svc.memory.list_memories(sid, character=key, limit=10)}
            self.assertEqual(rows["喜欢爵士乐"]["source"], f"dream；整理@{today}: 用户在当前窗口提到")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
