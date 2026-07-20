from __future__ import annotations

import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.webui import (
    api_delete_character,
    api_run_command,
    api_update_session,
    api_update_wardrobe,
)
from tests.support import ServiceFixtureMixin


class WebUICharacterLockTestCase(ServiceFixtureMixin, unittest.TestCase):
    """Web 写入口必须和 Telegram 共用角色锁，跨模型分类不得串角色。"""

    @staticmethod
    def _request(app, method, path, match_info, payload=None):
        request = make_mocked_request(
            method,
            path,
            app=app,
            match_info=match_info,
            headers={"Content-Type": "application/json"},
        )
        request["web_auth"] = {"role": "admin", "user_id": "admin", "token": "test"}
        if payload is not None:
            request._read_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return request

    def test_web_state_writers_hold_character_operation_lock(self):
        async def run():
            class DummyHttp:
                closed = False

            svc = self.make_service()
            sid = "telegram:101"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.get_saved_characters(state).update({
                "角色A": {"character": "角色A"},
                "角色B": {"character": "角色B"},
            })
            app = web.Application()
            app["service"] = svc
            lock = svc.character_operation_lock(sid)
            save_lock_states = []
            original_save = svc._save_session_state

            def save_with_lock(session_id, current):
                if session_id == sid:
                    save_lock_states.append(lock.locked())
                return original_save(session_id, current)

            svc._save_session_state = save_with_lock

            update_request = self._request(
                app,
                "PATCH",
                f"/api/sessions/{sid}",
                {"session_id": sid},
                {"custom_role_name": "新身份"},
            )
            update_response = await api_update_session(update_request)
            self.assertTrue(json.loads(update_response.text)["ok"])
            self.assertTrue(save_lock_states[-1])

            delete_request = self._request(
                app,
                "DELETE",
                f"/api/sessions/{sid}/characters/角色B",
                {"session_id": sid, "character_id": "角色B"},
            )
            delete_response = await api_delete_character(delete_request)
            self.assertTrue(json.loads(delete_response.text)["ok"])
            self.assertTrue(save_lock_states[-1])

            observed = {}

            async def dispatch(chat_id, session_id, command, arg):
                observed["locked"] = lock.locked()
                observed["args"] = (chat_id, session_id, command, arg)

            keepalive = asyncio.create_task(asyncio.sleep(60))
            svc.http = DummyHttp()
            svc._bot_tasks = [keepalive]
            svc.dispatch_command = dispatch
            command_request = self._request(
                app,
                "POST",
                "/api/actions/run-command",
                {},
                {"chat_id": "101", "command": "/人格", "arg": "温柔"},
            )
            try:
                command_response = await api_run_command(command_request)
            finally:
                keepalive.cancel()
                try:
                    await keepalive
                except asyncio.CancelledError:
                    pass
            self.assertTrue(json.loads(command_response.text)["ok"])
            self.assertTrue(observed["locked"])
            self.assertEqual(observed["args"], ("101", sid, "人格", "温柔"))

        asyncio.run(run())

    def test_web_wardrobe_apply_discards_result_if_character_changes_during_llm(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:202"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.set_wardrobe(state, {"dress": "A red dress"})
            session_schema.set_outfit(state, "A red dress")
            app = web.Application()
            app["service"] = svc

            async def classify(*args, **kwargs):
                self.assertTrue(svc.character_operation_lock(sid).locked())
                live = svc._get_session_state(sid)
                session_schema.set_character_value(live, "custom_character", "角色B")
                session_schema.set_wardrobe(live, {"dress": "B blue dress"})
                session_schema.set_outfit(live, "B blue dress")
                return {"top": "white blouse", "names": {"top": "白衬衫"}}

            svc._classify_wardrobe_change = classify
            request = self._request(
                app,
                "POST",
                f"/api/sessions/{sid}/wardrobe",
                {"session_id": sid},
                {"action": "apply", "description": "换上白衬衫"},
            )
            response = await api_update_wardrobe(request)
            data = json.loads(response.text)
            self.assertEqual(response.status, 409)
            self.assertFalse(data["ok"])
            live = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(live, "custom_character"), "角色B")
            self.assertEqual(session_schema.get_wardrobe(live), {"dress": "B blue dress"})
            self.assertNotIn("white blouse", session_schema.get_outfit(live))

        asyncio.run(run())

    def test_web_closet_classification_rechecks_character_before_save(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:303"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.set_closet(state, {
                "A 外套": {"slot": "outerwear", "tags": "A coat"},
            })
            app = web.Application()
            app["service"] = svc

            async def classify(staged, description):
                self.assertTrue(svc.character_operation_lock(sid).locked())
                self.assertIsNot(staged, svc._get_session_state(sid))
                live = svc._get_session_state(sid)
                session_schema.set_character_value(live, "custom_character", "角色B")
                session_schema.set_closet(live, {
                    "B 外套": {"slot": "outerwear", "tags": "B coat"},
                })
                return {"dress": "red qipao", "names": {"dress": "红旗袍"}}

            svc._classify_wardrobe_items = classify
            request = self._request(
                app,
                "POST",
                f"/api/sessions/{sid}/wardrobe",
                {"session_id": sid},
                {"action": "save-closet", "description": "收藏红旗袍"},
            )
            response = await api_update_wardrobe(request)
            data = json.loads(response.text)
            self.assertEqual(response.status, 409)
            self.assertFalse(data["ok"])
            live = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(live, "custom_character"), "角色B")
            self.assertEqual(set(session_schema.get_closet(live)), {"B 外套"})
            self.assertNotIn("红旗袍", session_schema.get_closet(live))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
