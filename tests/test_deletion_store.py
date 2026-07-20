from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing

from telegram_comfyui_selfie.service import TelegramComfyUIService
from telegram_comfyui_selfie.state_runtime import LEGACY_STATE_IMPORT_MARKER
from tests.support import ServiceFixtureMixin, make_project_temp_dir


class DeletionStoreTestCase(ServiceFixtureMixin, unittest.TestCase):
    @staticmethod
    def _seed_character(service, session_id: str, character: str, suffix: str) -> None:
        service.memory.add_memory(
            session_id,
            "event",
            f"记忆-{suffix}",
            character=character,
        )
        message_ids = service.app_store.append_messages(
            session_id,
            character,
            [{"role": "user", "content": f"消息-{suffix}"}],
        )
        service.app_store.upsert_checkpoint(
            session_id,
            character,
            f"摘要-{suffix}",
            message_ids[-1],
        )
        service.app_store.upsert_diary(
            session_id,
            character,
            "2026-07-20",
            f"日记-{suffix}",
        )
        service.app_store.upsert_life_plan(
            session_id,
            character,
            {"today": {"date": "2026-07-20", "texture": f"生活-{suffix}"}},
        )
        service.app_store.upsert_character_history_summary(
            session_id,
            character,
            f"历史-{suffix}",
        )

    def test_character_bundle_deletes_one_role_and_commits_next_state(self):
        service = self.make_service()
        session_id = "telegram:delete-character-store"
        self._seed_character(service, session_id, "角色A", "A")
        self._seed_character(service, session_id, "角色B", "B")
        service.app_store.save_session_state(session_id, {"marker": "before"})

        deleted = service.app_store.delete_character_bundle(
            session_id,
            "角色A",
            {"marker": "after"},
        )

        self.assertGreaterEqual(sum(deleted.values()), 6)
        self.assertEqual(
            service.memory.list_memories(session_id, character="角色A", include_inactive=True),
            [],
        )
        self.assertEqual(service.app_store.list_messages(session_id, "角色A"), [])
        self.assertEqual(service.app_store.get_diary(session_id, "角色A", "2026-07-20"), None)
        self.assertEqual(service.app_store.get_life_plan(session_id, "角色A"), None)
        self.assertEqual(service.app_store.load_session_state(session_id), {"marker": "after"})
        self.assertEqual(
            [item["summary"] for item in service.memory.list_memories(session_id, character="角色B")],
            ["记忆-B"],
        )
        self.assertEqual(
            [item["content"] for item in service.app_store.list_messages(session_id, "角色B")],
            ["消息-B"],
        )

    def test_character_bundle_failure_rolls_back_every_table_and_state(self):
        service = self.make_service()
        session_id = "telegram:delete-character-rollback"
        self._seed_character(service, session_id, "角色A", "A")
        original_state = {"marker": "before"}
        service.app_store.save_session_state(session_id, original_state)
        with closing(sqlite3.connect(service.app_store.path)) as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_character_bundle_diary_delete
                BEFORE DELETE ON diaries
                WHEN OLD.session_id = 'telegram:delete-character-rollback'
                BEGIN
                    SELECT RAISE(ABORT, 'forced character delete rollback');
                END
                """
            )
            conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            service.app_store.delete_character_bundle(
                session_id,
                "角色A",
                {"marker": "after"},
            )

        self.assertEqual(service.app_store.load_session_state(session_id), original_state)
        self.assertEqual(
            [item["summary"] for item in service.memory.list_memories(session_id, character="角色A")],
            ["记忆-A"],
        )
        self.assertEqual(
            [item["content"] for item in service.app_store.list_messages(session_id, "角色A")],
            ["消息-A"],
        )
        self.assertEqual(
            service.app_store.get_diary(session_id, "角色A", "2026-07-20")["content"],
            "日记-A",
        )

    def test_session_bundle_cascades_scope_but_preserves_identity_by_default(self):
        service = self.make_service()
        target = "telegram:1001"
        other = "telegram:2002"
        self._seed_character(service, target, "角色A", "target")
        self._seed_character(service, other, "角色B", "other")
        service.app_store.save_session_state(target, {"target": True})
        service.app_store.save_session_state(other, {"other": True})
        service.app_store.record_llm_usage(session_id=target, total_tokens=10)
        service.app_store.set_web_password("1001", "secret")
        service.app_store.upsert_model_profile("1001", "private", {"model": "m"})
        service.app_store.update_user_model_settings("1001", chat_profile_id="private")
        service.app_store.stage_telegram_update(9, "1001", {"update_id": 9})

        deleted = service.app_store.delete_session_bundle(target)

        self.assertGreater(deleted["session_state"], 0)
        self.assertIsNone(service.app_store.load_session_state(target))
        self.assertEqual(
            service.memory.list_memories(target, character="角色A", include_inactive=True),
            [],
        )
        self.assertEqual(service.app_store.list_messages(target, "角色A"), [])
        self.assertEqual(service.app_store.list_pending_telegram_updates(), [])
        self.assertEqual(service.app_store.load_session_state(other), {"other": True})
        self.assertEqual(
            [item["summary"] for item in service.memory.list_memories(other, character="角色B")],
            ["记忆-other"],
        )
        self.assertTrue(service.app_store.verify_user_password("1001", "secret"))
        self.assertIn("private", service.app_store.list_model_profiles("1001"))
        self.assertEqual(
            service.app_store.get_user_model_settings("1001")["chat_profile_id"],
            "private",
        )

    def test_legacy_state_marker_prevents_deleted_last_session_from_resurrecting(self):
        root = make_project_temp_dir("legacy_state_delete_marker")
        config_path = root / "config.json"
        state_path = root / "state.json"
        session_id = "telegram:legacy-resurrection"
        state_path.write_text(
            json.dumps({
                "sessions": {session_id: {"custom_character": "旧角色"}},
                "city_place_catalogs": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        first = TelegramComfyUIService(config_path, state_path)
        self.assertIn(session_id, first.sessions)
        self.assertEqual(
            first.app_store.get_metadata(LEGACY_STATE_IMPORT_MARKER),
            "1",
        )
        first.app_store.delete_session_bundle(session_id)

        second = TelegramComfyUIService(config_path, state_path)

        self.assertNotIn(session_id, second.sessions)
        self.assertFalse(second.app_store.has_session_states())
        self.assertTrue(state_path.exists())


if __name__ == "__main__":
    unittest.main()
