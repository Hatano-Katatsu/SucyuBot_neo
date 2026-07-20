from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.character_artifacts import avatar_file_path
from telegram_comfyui_selfie.deletion_runtime import DeletionForbiddenError
from telegram_comfyui_selfie.webui import (
    api_delete_character,
    api_delete_session,
    api_sessions,
)
from tests.support import ServiceFixtureMixin


class DeletionRuntimeTestCase(ServiceFixtureMixin, unittest.TestCase):
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

    @staticmethod
    def _request(app, method: str, path: str, match_info=None, payload=None):
        request = make_mocked_request(
            method,
            path,
            app=app,
            match_info=match_info or {},
            headers={"Content-Type": "application/json"},
        )
        request["web_auth"] = {
            "role": "admin",
            "user_id": "admin",
            "token": "test",
        }
        if payload is not None:
            request._read_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return request

    def test_character_delete_cascades_files_and_resets_only_active_role(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:delete-runtime-character"
            state = service._get_session_state(session_id)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.set_character_value(state, "custom_bot_name", "角色A")
            session_schema.set_outfit(state, "red dress")
            session_schema.get_saved_characters(state).update({
                "角色A": {"character": "角色A"},
                "角色B": {"character": "角色B"},
            })
            session_schema.get_character_contexts(state).update({
                "角色A": {"dynamic_appearance": "A"},
                "角色B": {"dynamic_appearance": "B"},
            })
            service._save_session_state(session_id, state)
            self._seed_character(service, session_id, "角色A", "A")
            self._seed_character(service, session_id, "角色B", "B")

            checkpoint_dir = service._character_checkpoint_dir(session_id, "角色A")
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            (checkpoint_dir / "2026-07-20.json").write_text("{}", encoding="utf-8")
            (checkpoint_dir / "pending.tmp").write_text("tmp", encoding="utf-8")
            avatar_path = avatar_file_path(service, session_id, "角色A")
            avatar_path.parent.mkdir(parents=True, exist_ok=True)
            avatar_path.write_bytes(b"avatar")

            result = await service.delete_character(session_id, "角色A")

            self.assertEqual(result["active_id"], "")
            self.assertNotIn("角色A", result["characters"])
            self.assertIn("角色B", result["characters"])
            self.assertNotIn(
                "角色A",
                session_schema.get_character_contexts(service.sessions[session_id]),
            )
            self.assertIn(
                "角色B",
                session_schema.get_character_contexts(service.sessions[session_id]),
            )
            self.assertEqual(session_schema.get_outfit(service.sessions[session_id]), "")
            self.assertFalse(checkpoint_dir.exists())
            self.assertFalse(avatar_path.exists())
            self.assertEqual(
                service.memory.list_memories(
                    session_id,
                    character="角色A",
                    include_inactive=True,
                ),
                [],
            )
            self.assertEqual(service.app_store.list_messages(session_id, "角色A"), [])
            self.assertEqual(
                [item["summary"] for item in service.memory.list_memories(
                    session_id,
                    character="角色B",
                )],
                ["记忆-B"],
            )
            service._snapshot_character(service.sessions[session_id])
            self.assertNotIn(
                "角色A",
                session_schema.get_saved_characters(service.sessions[session_id]),
            )

        asyncio.run(run())

    def test_character_delete_failure_restores_files_database_and_live_state(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:delete-runtime-rollback"
            state = service._get_session_state(session_id)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.get_saved_characters(state)["角色A"] = {"character": "角色A"}
            service._save_session_state(session_id, state)
            original = copy.deepcopy(state)
            self._seed_character(service, session_id, "角色A", "A")
            checkpoint_dir = service._character_checkpoint_dir(session_id, "角色A")
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            checkpoint_file = checkpoint_dir / "2026-07-20.json"
            checkpoint_file.write_text("{}", encoding="utf-8")
            avatar_path = avatar_file_path(service, session_id, "角色A")
            avatar_path.parent.mkdir(parents=True, exist_ok=True)
            avatar_path.write_bytes(b"avatar")
            with closing(sqlite3.connect(service.app_store.path)) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER fail_runtime_character_delete
                    BEFORE DELETE ON diaries
                    WHEN OLD.session_id = 'telegram:delete-runtime-rollback'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced runtime rollback');
                    END
                    """
                )
                conn.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                await service.delete_character(session_id, "角色A")

            self.assertEqual(service.sessions[session_id], original)
            self.assertTrue(checkpoint_file.exists())
            self.assertEqual(avatar_path.read_bytes(), b"avatar")
            self.assertEqual(
                [item["summary"] for item in service.memory.list_memories(
                    session_id,
                    character="角色A",
                )],
                ["记忆-A"],
            )
            self.assertIsNotNone(service.app_store.load_session_state(session_id))

        asyncio.run(run())

    def test_default_card_is_protected_but_same_name_custom_card_can_be_deleted(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:delete-default-name"
            default_id = service._default_character_payload()["id"]
            state = service._get_session_state(session_id)
            service._save_session_state(session_id, state)
            with self.assertRaises(DeletionForbiddenError):
                await service.delete_character(session_id, default_id)

            session_schema.set_character_value(state, "custom_character", default_id)
            session_schema.set_character_value(state, "custom_bot_name", "同名自定义")
            session_schema.get_saved_characters(state)[default_id] = {
                "character": default_id,
                "is_default": False,
            }
            service._save_session_state(session_id, state)
            self._seed_character(service, session_id, default_id, "custom-default-name")

            result = await service.delete_character(session_id, default_id)

            self.assertEqual(result["active_id"], "")
            self.assertNotIn(default_id, result["characters"])
            self.assertEqual(
                service.memory.list_memories(
                    session_id,
                    character=default_id,
                    include_inactive=True,
                ),
                [],
            )

        asyncio.run(run())

    def test_web_and_telegram_character_delete_delegate_to_same_service_method(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:delete-delegate"
            state = service._get_session_state(session_id)
            session_schema.get_saved_characters(state)["角色A"] = {"character": "角色A"}
            service._save_session_state(session_id, state)
            service.delete_character = AsyncMock(return_value={
                "active_id": "",
                "characters": {},
                "deleted": {"memories": 1},
            })
            service.send_message = AsyncMock()
            app = web.Application()
            app["service"] = service
            request = self._request(
                app,
                "DELETE",
                f"/api/sessions/{session_id}/characters/角色A",
                {"session_id": session_id, "character_id": "角色A"},
            )

            response = await api_delete_character(request)
            await service.cmd_character("delete-delegate", session_id, "delete 角色A")

            self.assertTrue(json.loads(response.text)["ok"])
            self.assertEqual(service.delete_character.await_count, 2)
            service.delete_character.assert_any_await(session_id, "角色A")

        asyncio.run(run())

    def test_session_delete_stops_stale_writer_and_cleans_files_caches_and_database(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:8123"
            state = service._get_session_state(session_id)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.get_saved_characters(state)["角色A"] = {"character": "角色A"}
            service._save_session_state(session_id, state)
            self._seed_character(service, session_id, "角色A", "A")
            checkpoint_dir = service._character_checkpoint_dir(session_id, "角色A")
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            (checkpoint_dir / "2026-07-20.json").write_text("{}", encoding="utf-8")
            avatar_path = avatar_file_path(service, session_id, "角色A")
            avatar_path.parent.mkdir(parents=True, exist_ok=True)
            avatar_path.write_bytes(b"avatar")
            log_path = service._user_log_path(session_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("log", encoding="utf-8")
            service._weather_caches[session_id] = {"data": {"desc": "晴"}}
            service._last_prompt_slots_by_session = {session_id: "stale"}

            started = asyncio.Event()
            finished = asyncio.Event()
            stale_state = copy.deepcopy(state)

            async def stale_writer():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stale_state["stale_writer"] = True
                    service._save_session_state(session_id, stale_state)
                    finished.set()

            service._spawn_background(
                stale_writer(),
                name="stale-session-writer",
                session_id=session_id,
                character_key="角色A",
                scope="test-writer",
            )
            await started.wait()

            result = await service.delete_session(session_id)

            self.assertTrue(finished.is_set())
            self.assertNotIn(session_id, service.sessions)
            self.assertNotIn(session_id, service._weather_caches)
            self.assertNotIn(session_id, service._last_prompt_slots_by_session)
            self.assertFalse(checkpoint_dir.parent.exists())
            self.assertFalse(avatar_path.parent.exists())
            self.assertFalse(log_path.exists())
            self.assertIsNone(service.app_store.load_session_state(session_id))
            self.assertEqual(
                service.memory.list_memories(
                    session_id,
                    character="角色A",
                    include_inactive=True,
                ),
                [],
            )
            self.assertEqual(result["session_id"], session_id)

            restarted = type(service)(service.config_path, service.state_path)
            self.assertNotIn(session_id, restarted.sessions)

        asyncio.run(run())

    def test_session_api_distinguishes_hide_unhide_and_confirmed_purge(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:session-api-delete"
            service._save_session_state(session_id, service._get_session_state(session_id))
            app = web.Application()
            app["service"] = service

            hide = self._request(
                app,
                "DELETE",
                f"/api/sessions/{session_id}",
                {"session_id": session_id},
                {"mode": "hide"},
            )
            hide_response = await api_delete_session(hide)
            self.assertEqual(hide_response.status, 200)
            self.assertTrue(session_schema.get_web_hidden(service.sessions[session_id]))
            self.assertIsNotNone(service.app_store.load_session_state(session_id))

            visible_request = self._request(app, "GET", "/api/sessions")
            visible = json.loads((await api_sessions(visible_request)).text)["sessions"]
            self.assertEqual(visible, [])
            all_request = self._request(app, "GET", "/api/sessions?include_hidden=1")
            all_sessions = json.loads((await api_sessions(all_request)).text)["sessions"]
            self.assertEqual([item["session_id"] for item in all_sessions], [session_id])
            self.assertTrue(all_sessions[0]["hidden"])

            unhide = self._request(
                app,
                "DELETE",
                f"/api/sessions/{session_id}",
                {"session_id": session_id},
                {"mode": "unhide"},
            )
            self.assertEqual((await api_delete_session(unhide)).status, 200)
            self.assertFalse(session_schema.get_web_hidden(service.sessions[session_id]))

            bad_purge = self._request(
                app,
                "DELETE",
                f"/api/sessions/{session_id}",
                {"session_id": session_id},
                {"mode": "purge", "confirm": "wrong"},
            )
            self.assertEqual((await api_delete_session(bad_purge)).status, 400)
            self.assertIsNotNone(service.app_store.load_session_state(session_id))

            purge = self._request(
                app,
                "DELETE",
                f"/api/sessions/{session_id}",
                {"session_id": session_id},
                {"mode": "purge", "confirm": session_id},
            )
            purge_response = await api_delete_session(purge)
            self.assertEqual(purge_response.status, 200)
            self.assertIsNone(service.app_store.load_session_state(session_id))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
