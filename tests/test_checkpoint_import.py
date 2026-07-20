from __future__ import annotations

import copy
import unittest

from telegram_comfyui_selfie import session_schema
from tests.support import ServiceFixtureMixin


class CharacterCheckpointImportTestCase(ServiceFixtureMixin, unittest.TestCase):
    def _set_character(self, svc, session_id: str, name: str = "目标角色") -> dict:
        state = svc._get_session_state(session_id)
        session_schema.set_character_value(state, "custom_character", name)
        session_schema.set_character_value(state, "custom_scheduled_persona", "旧人格")
        session_schema.set_character_value(state, "custom_positive_prefix", "旧外观")
        session_schema.set_outfit(state, "old clothes")
        svc._snapshot_character(state)
        svc._save_session_state(session_id, state)
        return state

    @staticmethod
    def _payload(
        *,
        background: dict | None = None,
        state: dict | None = None,
        life_plan: dict | None = None,
        memories: list[dict] | None = None,
        messages: list[dict] | None = None,
    ) -> dict:
        payload = {
            "schema": "sucyubot.character_checkpoint.v1",
            "version": 1,
            "checkpoint_date": "2026-07-20",
            "character_key": "目标角色",
            "character_card": {
                "character": "目标角色",
                "persona": "导入人格",
                "appearance": "导入外观",
                "outfit": "blue dress",
            },
            "state": state or {},
            "background": background or {},
            "memories": memories or [],
            "chat_messages": messages or [],
        }
        if life_plan is not None:
            payload["life_plan"] = life_plan
        return payload

    def test_full_import_maps_source_boundary_to_existing_target_latest_id(self):
        svc = self.make_service()
        sid = "telegram:checkpoint-portable"
        self._set_character(svc, sid)
        target_ids = svc.app_store.append_messages(sid, "目标角色", [
            {"role": "user", "content": "目标库消息一"},
            {"role": "assistant", "content": "目标库消息二"},
        ])
        payload = self._payload(background={
            "sqlite_checkpoint": {"summary": "来源摘要", "source_until_id": 987654},
            "character_history_summary": "来源历史",
        })

        result = svc.import_character_checkpoint(sid, payload, mode="full")

        checkpoint = svc.app_store.get_checkpoint(sid, "目标角色")
        self.assertEqual(checkpoint["summary"], "来源摘要")
        self.assertEqual(int(checkpoint["source_until_id"]), target_ids[-1])
        self.assertEqual(result["mapped_source_until_id"], target_ids[-1])
        rows = svc.app_store.list_messages(sid, "目标角色")
        self.assertEqual([row["content"] for row in rows], ["目标库消息一", "目标库消息二"])
        self.assertTrue(all(int(row["checkpointed"]) == 1 for row in rows))
        state = svc._get_session_state(sid)
        self.assertEqual(session_schema.get_checkpoint_message_id(state), target_ids[-1])

    def test_explicit_chat_restore_replaces_messages_and_maps_all_source_ids(self):
        svc = self.make_service()
        sid = "telegram:checkpoint-replay"
        self._set_character(svc, sid)
        old_ids = svc.app_store.append_messages(sid, "目标角色", [
            {"role": "user", "content": "目标旧消息"},
        ])
        payload = self._payload(
            background={
                "sqlite_checkpoint": {"summary": "恢复摘要", "source_until_id": 120},
                "diaries": [{
                    "diary_date": "2026-07-19",
                    "content": "恢复日记",
                    "from_message_id": 100,
                    "to_message_id": 150,
                }],
            },
            messages=[
                {"id": 100, "role": "user", "content": "来源一", "created_at": 10},
                {"id": 120, "role": "assistant", "content": "来源二", "created_at": 20},
                {"id": 150, "role": "user", "content": "来源三", "created_at": 30},
            ],
        )

        result = svc.import_character_checkpoint(
            sid,
            payload,
            mode="full",
            restore_chat_messages=True,
        )

        rows = svc.app_store.list_messages(sid, "目标角色")
        restored_ids = [int(row["id"]) for row in rows]
        self.assertEqual([row["content"] for row in rows], ["来源一", "来源二", "来源三"])
        self.assertNotIn(old_ids[0], restored_ids)
        self.assertEqual(result["chat_messages_restored"], 3)
        self.assertEqual(result["mapped_source_until_id"], restored_ids[1])
        self.assertEqual(
            int(svc.app_store.get_checkpoint(sid, "目标角色")["source_until_id"]),
            restored_ids[1],
        )
        self.assertEqual([int(row["checkpointed"]) for row in rows], [1, 1, 0])
        diary = svc.app_store.get_diary(sid, "目标角色", "2026-07-19")
        self.assertEqual(int(diary["from_message_id"]), restored_ids[0])
        self.assertEqual(int(diary["to_message_id"]), restored_ids[2])

    def test_full_import_without_optional_sections_clears_old_role_data(self):
        svc = self.make_service()
        sid = "telegram:checkpoint-replace-empty"
        state = self._set_character(svc, sid)
        session_schema.get_character_contexts(state)["目标角色"] = {
            "chat_history": [{"role": "user", "content": "旧冻结对话"}],
        }
        svc._save_session_state(sid, state)
        message_ids = svc.app_store.append_messages(sid, "目标角色", [
            {"role": "user", "content": "保留的目标聊天"},
        ])
        svc.app_store.upsert_checkpoint(sid, "目标角色", "旧摘要", message_ids[-1])
        svc.app_store.upsert_character_history_summary(sid, "目标角色", "旧历史")
        svc.app_store.upsert_diary(sid, "目标角色", "2026-07-18", "旧日记")
        svc._save_life_plan_payload(sid, "目标角色", {
            "today": {"date": "2026-07-20", "events": [], "texture": "旧生活线"},
        })
        svc.memory.add_memory(sid, "event", "旧记忆", character="目标角色")
        payload = self._payload(
            background={},
            life_plan=None,
            memories=[],
            state={},
        )
        # 缺失字段也要体现 full replace，而不是沿用旧角色卡值。
        payload["character_card"] = {"character": "目标角色", "persona": "全新人格"}

        result = svc.import_character_checkpoint(sid, payload, mode="full")

        self.assertFalse(result["checkpoint_replaced"])
        self.assertEqual(svc.app_store.get_checkpoint(sid, "目标角色")["summary"], "")
        self.assertEqual(
            svc.app_store.get_context_meta(sid, "目标角色")["character_history_summary"],
            "",
        )
        self.assertEqual(svc.app_store.recent_diaries(sid, "目标角色", limit=10), [])
        self.assertIsNone(svc.app_store.get_life_plan(sid, "目标角色"))
        self.assertEqual(svc.memory.list_memories(sid, character="目标角色", limit=10), [])
        rows = svc.app_store.list_messages(sid, "目标角色")
        self.assertEqual([row["content"] for row in rows], ["保留的目标聊天"])
        self.assertEqual(int(rows[0]["checkpointed"]), 0)
        imported_state = svc._get_session_state(sid)
        self.assertEqual(session_schema.get_character_value(imported_state, "custom_scheduled_persona"), "全新人格")
        self.assertEqual(session_schema.get_character_value(imported_state, "custom_positive_prefix"), "")
        self.assertEqual(session_schema.get_outfit(imported_state), "")
        self.assertNotIn("目标角色", session_schema.get_character_contexts(imported_state))

    def test_explicit_empty_checkpoint_and_history_replace_old_values(self):
        svc = self.make_service()
        sid = "telegram:checkpoint-replace-explicit-empty"
        self._set_character(svc, sid)
        ids = svc.app_store.append_messages(sid, "目标角色", [
            {"role": "user", "content": "目标已有聊天"},
        ])
        svc.app_store.upsert_checkpoint(sid, "目标角色", "旧摘要", ids[-1])
        svc.app_store.upsert_character_history_summary(sid, "目标角色", "旧历史")
        svc._save_life_plan_payload(sid, "目标角色", {
            "today": {"date": "2026-07-20", "events": [], "texture": "旧生活线"},
        })
        payload = self._payload(
            background={
                "sqlite_checkpoint": {"summary": "", "source_until_id": 0},
                "character_history_summary": "",
                "diaries": [],
            },
            life_plan={},
        )

        result = svc.import_character_checkpoint(sid, payload, mode="full")

        checkpoint = svc.app_store.get_checkpoint(sid, "目标角色")
        self.assertTrue(result["checkpoint_replaced"])
        self.assertEqual(checkpoint["summary"], "")
        self.assertEqual(int(checkpoint["source_until_id"]), ids[-1])
        self.assertEqual(
            svc.app_store.get_context_meta(sid, "目标角色")["character_history_summary"],
            "",
        )
        self.assertIsNone(svc.app_store.get_life_plan(sid, "目标角色"))
        rows = svc.app_store.list_messages(sid, "目标角色")
        self.assertEqual(int(rows[0]["checkpointed"]), 1)

    def test_full_import_rolls_back_database_and_runtime_state_on_failure(self):
        svc = self.make_service()
        sid = "telegram:checkpoint-rollback"
        state = self._set_character(svc, sid)
        ids = svc.app_store.append_messages(sid, "目标角色", [
            {"role": "user", "content": "原消息"},
        ])
        svc.app_store.upsert_checkpoint(sid, "目标角色", "原摘要", ids[-1])
        svc.app_store.upsert_character_history_summary(sid, "目标角色", "原历史")
        svc.app_store.upsert_diary(sid, "目标角色", "2026-07-18", "原日记")
        svc._save_life_plan_payload(sid, "目标角色", {
            "today": {"date": "2026-07-20", "events": [], "texture": "原生活线"},
        })
        svc.memory.add_memory(sid, "event", "原记忆", character="目标角色")
        before_state = copy.deepcopy(svc._get_session_state(sid))
        payload = self._payload(
            background={
                "sqlite_checkpoint": {"summary": "新摘要", "source_until_id": 999},
                "character_history_summary": "新历史",
                "diaries": [{"diary_date": "2026-07-20", "content": "新日记"}],
            },
            life_plan={
                "payload": {"today": {"date": "2026-07-20", "events": [], "texture": "新生活线"}},
            },
            memories=[{"kind": "event", "summary": "新记忆"}],
        )
        original_replace = svc._replace_full_checkpoint_rows

        def fail_after_database_writes(conn, **kwargs):
            original_replace(conn, **kwargs)
            raise RuntimeError("模拟事务末尾失败")

        svc._replace_full_checkpoint_rows = fail_after_database_writes
        with self.assertRaisesRegex(RuntimeError, "模拟事务末尾失败"):
            svc.import_character_checkpoint(sid, payload, mode="full")

        self.assertEqual(svc._get_session_state(sid), before_state)
        self.assertEqual(svc.app_store.load_session_state(sid), before_state)
        self.assertEqual(svc.app_store.get_checkpoint(sid, "目标角色")["summary"], "原摘要")
        self.assertEqual(
            svc.app_store.get_context_meta(sid, "目标角色")["character_history_summary"],
            "原历史",
        )
        self.assertEqual(
            svc.app_store.get_diary(sid, "目标角色", "2026-07-18")["content"],
            "原日记",
        )
        self.assertEqual(
            svc.app_store.get_life_plan(sid, "目标角色")["payload"]["today"]["texture"],
            "原生活线",
        )
        memories = svc.memory.list_memories(sid, character="目标角色", limit=10)
        self.assertEqual([item["summary"] for item in memories], ["原记忆"])
        rows = svc.app_store.list_messages(sid, "目标角色")
        self.assertEqual([row["content"] for row in rows], ["原消息"])
        self.assertEqual(int(rows[0]["checkpointed"]), 1)


if __name__ == "__main__":
    unittest.main()
