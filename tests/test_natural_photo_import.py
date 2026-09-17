from __future__ import annotations

import asyncio
import base64
import copy
import json
import struct
import time
import unittest
import zlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from telegram_comfyui_selfie import character_card, generation, session_schema
from telegram_comfyui_selfie.photo_sharing import normalize_photo_brief, photo_repeat_reason, record_topic_control
from telegram_comfyui_selfie.tavern_import import parse_tavern_file, normalize_conversion, conversion_batches
from telegram_comfyui_selfie.webui_imports import api_import_commit, convert_import
from telegram_comfyui_selfie.photo_sharing import PhotoContentSkipped
from telegram_comfyui_selfie.webui_characters import _switch_state_to_selected_character, _save_character_locked
from tests.support import ServiceFixtureMixin
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telegram_comfyui_selfie import webui_imports


def sample_card():
    return {"spec": "chara_card_v2", "data": {"name": "小雨", "description": "黑发蓝眼的画家", "personality": "说话简短", "mes_example": "{{char}}: 你好。", "first_mes": "刚把画收起来。", "character_book": {"entries": [{"id": 1, "keys": ["画室"], "content": "画室在家中，角色常在那里画画。", "enabled": True}]}}}


def sample_conversion():
    return {"character": {"bot_name": "小雨", "persona": "画家，言语简短", "appearance": "black hair, blue eyes"}, "world": {"name": "日常", "summary": "普通城市生活", "kind": "real", "city": "上海", "photography": "modern"}, "entries": [{"name": "画室", "type": "place", "content": "在家中的画室", "source_ids": ["entry:0"], "known": True, "place_key": "home"}], "unresolved": []}


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def card_png(v2, v3):
    raw = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for name, data in (("chara", v2), ("ccv3", v3)):
        if data is not None:
            raw += png_chunk(b"tEXt", name.encode() + b"\0" + base64.b64encode(json.dumps(data).encode()))
    return raw + png_chunk(b"IEND", b"")


class ImportParsingTests(unittest.TestCase):
    def test_png_v3_precedence_and_crc(self):
        v2, v3 = sample_card(), sample_card()
        v3["spec"] = "chara_card_v3"
        v3["data"]["name"] = "新版"
        png = card_png(v2, v3)
        self.assertEqual(parse_tavern_file(png)["name"], "新版")
        with self.assertRaises(ValueError):
            parse_tavern_file(png[:-5] + b"xxxxx")

    def test_presets_and_plain_images_are_rejected(self):
        for raw in (b'{"temperature":0.7}', card_png(None, None)):
            with self.assertRaises(ValueError):
                parse_tavern_file(raw)

    def test_conditions_do_not_become_facts(self):
        data = sample_card()
        data["data"]["character_book"]["entries"][0]["enabled"] = False
        parsed = parse_tavern_file(json.dumps(data).encode())
        self.assertNotIn("entry:0", [s["id"] for batch in conversion_batches(parsed) for s in batch])
        with self.assertRaises(ValueError):
            normalize_conversion(sample_conversion(), parsed)

    def test_long_source_is_fully_batched(self):
        text = "甲乙丙丁" * 8000
        parsed = parse_tavern_file(json.dumps({"entries": {"0": {"content": text}}}).encode())
        parts = [p["content"] for batch in conversion_batches(parsed) for p in batch]
        self.assertEqual("".join(parts), text)

    def test_examples_are_separate_from_persona(self):
        parsed = parse_tavern_file(json.dumps(sample_card()).encode())
        result = normalize_conversion(sample_conversion(), parsed)
        self.assertIn("{{char}}", result["character"]["dialogue_examples"])
        self.assertNotIn("{{char}}", result["character"]["persona"])


class NaturalPhotoTests(ServiceFixtureMixin, unittest.TestCase):
    def test_invalid_conversion_entry_is_left_unresolved_after_one_repair(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            parsed = parse_tavern_file(json.dumps(sample_card()).encode())
            svc.app_store.create_import_draft("partial", sid, {"parsed": parsed})
            value = sample_conversion()
            value["entries"].append({"name": "编造地点", "type": "place", "content": "错误来源", "source_ids": ["missing"]})
            svc._call_life_plan_json = AsyncMock(return_value=value)
            await convert_import(svc, sid, "partial")
            row = svc.app_store.get_import_draft(sid, "partial")
            self.assertEqual(svc._call_life_plan_json.await_count, 2)
            self.assertEqual(row["status"], "ready")
            self.assertEqual(len(row["data"]["converted"]["world"]["entries"]), 1)
            self.assertIn("编造地点", "".join(row["data"]["converted"]["unresolved"]))
        asyncio.run(run())

    def test_merge_checks_version_and_preserves_chat(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.get_saved_characters(state)["已有角色"] = {"character": "已有角色", "persona": "旧人设"}
            character_card.apply_card_to_state(state, {"character": "已有角色", "persona": "旧人设"})
            session_schema.get_chat_history(state).append({"role": "user", "content": "真实聊天"})
            parsed = parse_tavern_file(json.dumps(sample_card()).encode())
            data = {"parsed": parsed, "avatar": "", "converted": normalize_conversion(sample_conversion(), parsed)}
            svc.app_store.create_import_draft("merge", sid, data)
            svc.app_store.update_import_draft(sid, "merge", data, "ready")
            target = webui_imports._merge_target(svc, sid, "已有角色")
            class Request(dict):
                app = {"service": svc}
                match_info = {"session_id": sid, "draft_id": "merge"}
                version = "outdated"
                async def json(self): return {"merge_character_id": "已有角色", "merge_version": self.version}
            req = Request(web_auth={"role": "admin"})
            self.assertEqual((await api_import_commit(req)).status, 409)
            req.version = target["version"]
            self.assertEqual((await api_import_commit(req)).status, 200)
            state = svc._get_session_state(sid)
            self.assertEqual(list(session_schema.get_saved_characters(state)), ["已有角色"])
            self.assertEqual(session_schema.get_chat_history(state), [{"role": "user", "content": "真实聊天"}])
            self.assertEqual(character_card.card_from_state(state)["persona"], "画家，言语简短")
        asyncio.run(run())

    def test_imported_named_place_uses_existing_route_type(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_world_snapshot", {"name": "浮岛", "kind": "fictional", "entries": [{"id": "house", "name": "浮光居", "aliases": ["光居"], "content": "木屋", "type": "place", "place_key": "home", "known": True, "enabled": True}]})
            await svc.tool_update_location(sid, "回到浮光居")
            self.assertEqual(session_schema.get_character_place(state), "home")
            world = svc.build_world_state(sid)
            self.assertEqual(world["character_place"]["name"], "浮光居")
            self.assertEqual(world["character_place"]["world_place_id"], "house")
            svc._classify_city_region = AsyncMock(side_effect=AssertionError("不可查询现实城市"))
            self.assertEqual((await svc._ensure_city_place_catalog("浮岛", session_id=sid))["status"], "fictional")
        asyncio.run(run())

    def test_import_http_upload_cache_commit_scope_and_world_revision(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._get_session_state(sid)
            svc._call_life_plan_json = AsyncMock(return_value=sample_conversion())
            @web.middleware
            async def auth(request, handler):
                request["web_auth"] = {"role": "admin"}
                return await handler(request)
            app = web.Application(middlewares=[auth])
            app["service"] = svc
            root = "/api/sessions/{session_id}"
            app.router.add_post(root + "/imports", webui_imports.api_import_upload)
            app.router.add_get(root + "/imports/{draft_id}", webui_imports.api_import_status)
            app.router.add_post(root + "/imports/{draft_id}/commit", webui_imports.api_import_commit)
            app.router.add_get(root + "/worlds", webui_imports.api_world_profiles)
            app.router.add_put(root + "/worlds/{world_id}", webui_imports.api_world_profile_update)
            async with TestClient(TestServer(app)) as client:
                path = "/api/sessions/telegram:1"
                raw = card_png(sample_card(), None)
                response = await client.post(path + "/imports?filename=card.png", data=raw)
                self.assertEqual(response.status, 200)
                draft_id = (await response.json())["draft"]["id"]
                task = svc._tavern_import_tasks.get(draft_id)
                if task:
                    await task
                row = await (await client.get(path + "/imports/" + draft_id)).json()
                self.assertEqual(row["draft"]["status"], "ready")
                response = await client.post(path + "/imports", data=raw)
                self.assertEqual((await response.json())["draft"]["id"], draft_id)
                self.assertEqual(svc._call_life_plan_json.await_count, 1)
                hidden = await client.get("/api/sessions/telegram:2/imports/" + draft_id)
                self.assertEqual(hidden.status, 404)
                result = await (await client.post(path + "/imports/" + draft_id + "/commit", json={})).json()
                cid, world_id = result["result"]["character_id"], result["result"]["world_id"]
                card = session_schema.get_saved_characters(svc._get_session_state(sid))[cid]
                self.assertTrue((svc.state_path.parent / card["avatar_path"]).is_file())
                self.assertEqual(session_schema.get_chat_history(svc._get_session_state(sid)), [])
                profile = (await (await client.get(path + "/worlds")).json())["worlds"][0]
                response = await client.put(path + "/worlds/" + world_id, json={"revision": profile["revision"], "summary": "新的公开背景"})
                self.assertEqual(response.status, 200)
                response = await client.put(path + "/worlds/" + world_id, json={"revision": profile["revision"], "summary": "迟到的旧编辑"})
                self.assertEqual(response.status, 409)
                self.assertEqual(svc.app_store.get_world_profile("1", world_id)["data"]["summary"], "新的公开背景")
                self.assertEqual(svc.app_store.list_world_profiles("2"), [])
                # 已创建文件再次上传只复用转换底稿，允许创建独立副本。
                next_draft = (await (await client.post(path + "/imports", data=raw)).json())["draft"]
                self.assertNotEqual(next_draft["id"], draft_id)
                self.assertEqual(next_draft["status"], "ready")
                self.assertEqual(svc._call_life_plan_json.await_count, 1)
                profile_id, profile, _thinking = svc._resolve_llm_profile("chat", sid)
                svc.config["global_model_profiles"][profile_id] = {**profile, "model": "changed-model", "model_think": "changed-model"}
                self.assertNotEqual(webui_imports._conversion_cache_key(svc, sid, parse_tavern_file(raw)), svc.app_store.get_import_draft(sid, draft_id)["data"]["cache_key"])
        asyncio.run(run())

    def test_failed_commit_removes_avatar_and_keeps_recoverable_draft(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._get_session_state(sid)
            raw = card_png(sample_card(), None)
            parsed = parse_tavern_file(raw)
            svc.app_store.create_import_draft("rollback", sid, {"parsed": parsed, "avatar": base64.b64encode(raw).decode(), "converted": normalize_conversion(sample_conversion(), parsed)})
            row = svc.app_store.get_import_draft(sid, "rollback")
            svc.app_store.update_import_draft(sid, "rollback", row["data"], "ready")
            class Request(dict):
                app = {"service": svc}
                match_info = {"session_id": sid, "draft_id": "rollback"}
                async def json(self): return {}
            with patch.object(svc.app_store, "commit_import_draft", side_effect=RuntimeError("database failed")):
                with self.assertRaisesRegex(RuntimeError, "database failed"):
                    await api_import_commit(Request(web_auth={"role": "admin"}))
            self.assertEqual(list((svc.state_path.parent / "avatars").rglob("*.png")), [])
            self.assertFalse(session_schema.get_saved_characters(svc._get_session_state(sid)))
            self.assertEqual(svc.app_store.get_import_draft(sid, "rollback")["status"], "ready")
        asyncio.run(run())

    def prepare_push(self, plan):
        svc = self.make_service()
        sid = "telegram:1"
        svc.config.update(image_llm_api_key="test-key", image_llm_model="test", image_llm_api_base="https://invalid.example")
        svc._get_purity = lambda *_: 6
        svc._fetch_weather = AsyncMock(return_value=None)
        svc._ensure_life_profile = AsyncMock(return_value={"age_stage": "adult", "day_anchor": "unknown"})
        svc.ensure_life_plan_for_today = AsyncMock()
        svc._decide_push_topic_direction = AsyncMock(return_value={"topic_direction": "independent", "topic_guides": []})
        svc._llm_write_scene = AsyncMock(return_value=plan)
        svc._translate_to_tags = AsyncMock(side_effect=lambda text, **_: text)
        svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
        svc.send_photo = AsyncMock(return_value={"message_id": 123})
        return svc, sid

    def test_repeated_push_skips_without_delivery_or_retry(self):
        async def run():
            plan = {"scene": "holding a cup", "caption": "咖啡做好了", "view": "selfie"}
            svc, sid = self.prepare_push(plan)
            svc._append_push_topic(sid, plan["caption"], plan["scene"], "life")
            now = svc._session_now(sid)
            with self.assertRaises(PhotoContentSkipped):
                await svc._sched_fire(sid, now, mode_override="normal", skip_active_check=True)
            self.assertEqual(svc._llm_write_scene.await_count, 2)
            svc.send_photo.assert_not_awaited()
            svc._do_generate.assert_not_awaited()
            svc._sched_fire = AsyncMock(side_effect=PhotoContentSkipped("重复"))
            await svc._create_scheduled_push_task(sid, now, mode_override="normal", trigger_time="12:00")
            self.assertIn("12:00", session_schema.get_daily_triggered_times(svc._get_session_state(sid)))
            self.assertEqual(svc._scheduled_push_retry_info(sid, now, "12:00"), {})
        asyncio.run(run())

    def test_workflow_limitation_replans_to_character_and_records_receipt(self):
        async def run():
            first = {"subject_mode": "environment", "scene": "桌上的画本", "caption": "画好了", "view": "scene"}
            second = {"subject_mode": "character", "scene": "画本旁的自拍", "caption": "墨还没干", "view": "selfie"}
            svc, sid = self.prepare_push(first)
            svc.config["animaflow_enabled"] = True
            svc._llm_write_scene = AsyncMock(side_effect=[first, second])
            schema = {"parameters": {"properties": {"count": {"enum": ["1girl"]}, "tags": {"type": "string"}}}}
            with patch("telegram_comfyui_selfie.animaflow_runtime.load_animaflow_workflow_resources", new=AsyncMock(return_value=("test", {}, {}, schema, {}))):
                self.assertTrue(await svc._sched_fire(sid, svc._session_now(sid), mode_override="normal", skip_active_check=True))
            self.assertEqual(svc._llm_write_scene.await_count, 2)
            self.assertEqual(svc._do_generate.await_args.kwargs.get("subject_mode", "character"), "character")
            self.assertEqual(session_schema.get_recent_push_topics(svc._get_session_state(sid))[-1]["message_id"], 123)
            reply = svc._format_telegram_reply_context({"chat": {"id": 1}, "reply_to_message": {"message_id": 123}})
            self.assertIn("画本旁的自拍", reply)
        asyncio.run(run())

    def test_environment_requests_remain_empty_in_both_anima_paths(self):
        async def run():
            class Response:
                status = 200
                async def __aenter__(self): return self
                async def __aexit__(self, *args): return False
                async def json(self): return {"images": [{"filename": "life.png"}]}
                async def read(self): return b"image"
            class Session:
                closed = False
                def post(self, url, **kwargs):
                    self.payload = kwargs["json"]
                    return Response()
                def get(self, *args, **kwargs): return Response()
            svc = self.make_service()
            svc.comfy_session = Session()
            generation.build_prompt(svc, "a sketchbook on a table", subject_mode="environment")
            schema = {"parameters": {"properties": {"count": {"type": "string", "default": "1girl"}, "appearance": {"type": "string"}, "character": {"type": "string"}, "tags": {"type": "string"}}}}
            for planned in ({"count": "1girl", "appearance": "red dress", "character": "someone", "tags": "a girl"}, None):
                with patch("telegram_comfyui_selfie.generation.load_animaflow_workflow_resources", new=AsyncMock(return_value=("test", {}, {}, schema, {}))), patch("telegram_comfyui_selfie.image_planning.plan_animaflow_slots", new=AsyncMock(return_value=planned)):
                    ok, images, error = await generation._do_generate_animaflow(svc, "scene", "", 7)
                self.assertTrue(ok, error)
                self.assertEqual(images, [b"image"])
                sent = svc.comfy_session.payload
                self.assertEqual(sent["count"], "")
                self.assertEqual(sent["appearance"], "")
                self.assertEqual(sent["character"], "")
                self.assertIn("sketchbook", sent["tags"])
                self.assertNotIn("a girl", sent["tags"])
        asyncio.run(run())

    def test_native_environment_request_omits_identity_and_wardrobe(self):
        async def run():
            class Response:
                status = 200
                def __init__(self, data): self.data = data
                async def __aenter__(self): return self
                async def __aexit__(self, *args): return False
                async def json(self): return self.data
                async def read(self): return b"image"
            class Session:
                closed = False
                def post(self, url, **kwargs):
                    self.payload = kwargs["json"]["prompt"]
                    return Response({"prompt_id": "p"})
                def get(self, url, **kwargs):
                    return Response({"p": {"outputs": {"9": {"images": [{"filename": "life.png"}]}}}})
            svc = self.make_service()
            svc.comfy_session = Session()
            state = svc._get_session_state("telegram:1")
            session_schema.set_outfit(state, "red dress")
            with patch("telegram_comfyui_selfie.generation.asyncio.sleep", new=AsyncMock()):
                ok, images, error = await generation.do_generate_locked(svc, "a tree", session_id="telegram:1", subject_mode="environment")
            self.assertTrue(ok, error)
            sent = json.dumps(svc.comfy_session.payload)
            self.assertIn("a tree", sent)
            self.assertNotIn("1girl", sent)
            self.assertNotIn("red dress", sent)
            self.assertEqual(session_schema.get_outfit(state), "red dress")
        asyncio.run(run())

    def test_switching_legacy_role_isolates_background_and_topic_controls(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        imported = {"character": "导入", "world_snapshot": {"name": "浮岛"}, "dialogue_examples": "例子"}
        character_card.apply_card_to_state(state, imported)
        state["photo_topic_controls"] = [{"topic_key": "coffee", "until": 0}]
        _switch_state_to_selected_character(svc, sid, state, "旧角色", {"character": "旧角色"})
        self.assertFalse(svc._imported_world(sid))
        self.assertFalse(state.get("photo_topic_controls"))
        self.assertEqual(character_card.card_from_state(state)["dialogue_examples"], "")
        _switch_state_to_selected_character(svc, sid, state, "导入", imported)
        self.assertEqual(svc._imported_world(sid)["name"], "浮岛")
        self.assertEqual(state["photo_topic_controls"][0]["topic_key"], "coffee")

    def test_basic_character_edit_preserves_imported_fields(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            card = {"character": "导入", "world_id": "own-world", "world_snapshot": {"name": "浮岛"}, "dialogue_examples": "例子"}
            session_schema.get_saved_characters(state)["导入"] = card
            await _save_character_locked(svc, sid, {"id": "导入", "persona": "修改人设"}, None)
            saved = session_schema.get_saved_characters(state)["导入"]
            self.assertEqual(saved["world_id"], "own-world")
            self.assertEqual(saved["dialogue_examples"], "例子")
        asyncio.run(run())

    def test_life_source_is_dated_and_cannot_invent_new_progress(self):
        svc = self.make_service()
        sid = "telegram:1"
        date = svc._life_today_date(sid)
        svc._save_life_plan_payload(sid, "", {"today": {"date": date, "events": [{"id": "e1", "text": "画画", "status": "planned", "place_key": "home", "time_hint": "morning"}]}})
        plan = {"scene": "画本", "caption": "画了一页", "photo_brief": {"source_ref": f"life::{date}:e1", "actual_change": "new"}}
        first = svc._validate_life_photo_source(sid, plan)
        svc._append_push_topic(sid, "first", "different scene", "life", photo_brief=first["photo_brief"])
        second = svc._validate_life_photo_source(sid, {**plan, "photo_brief": {**plan["photo_brief"], "actual_change": "model invented new result"}})
        self.assertIn("依据", photo_repeat_reason(second, svc._get_session_state(sid)))
        old = svc._validate_life_photo_source(sid, {**plan, "photo_brief": {"source_ref": "life::1900-01-01:e1"}})
        self.assertEqual(old["photo_brief"]["source_ref"], "")

    def test_environment_prompt_and_payload_have_no_character(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        session_schema.set_outfit(state, "red dress")
        before = copy.deepcopy(state)
        positive, _ = generation.build_prompt(svc, "a breakfast plate on a wooden table", session_id=sid, subject_mode="environment")
        self.assertNotIn("1girl", positive)
        self.assertNotIn("red dress", positive)
        self.assertEqual(state, before)
        slots = svc._last_prompt_slots
        schema = {"parameters": {"properties": {"count": {"type": "string", "default": "1girl"}, "appearance": {"type": "string"}, "character": {"type": "string"}, "tags": {"type": "string"}}, "required": ["count", "tags"]}}
        payload = generation.apply_photo_subject_contract({"count": "1girl", "appearance": "blue eyes", "tags": "a girl"}, slots, schema)
        self.assertEqual(payload["count"], "")
        self.assertEqual(payload["appearance"], "")
        self.assertIn("breakfast", payload["tags"])
        self.assertNotIn("a girl", payload["tags"])

    def test_anima_fallback_keeps_scene_subject(self):
        svc = self.make_service()
        generation.build_prompt(svc, "a tree by the window", subject_mode="environment")
        schema = {"parameters": {"properties": {"count": {"type": "string", "default": "1girl"}, "appearance": {"type": "string"}, "tags": {"type": "string"}}, "required": ["tags"]}}
        payload = generation._build_animaflow_payload(svc, svc._last_prompt_slots, svc._last_prompt_slots.positive, "", 1, schema)
        self.assertEqual(payload["count"], "")
        self.assertIn("tree", payload["tags"])

    def test_repeat_and_topic_pause_persist_separately(self):
        svc = self.make_service()
        state = svc._get_session_state("telegram:1")
        plan = {"scene": "sitting with a coffee waiting for you", "caption": "咖啡留给你", "photo_brief": {"topic_key": "coffee", "main_subject": "cup", "activity": "waiting", "framing": "medium", "angle": "eye_level", "composition": "center"}}
        svc._append_push_topic("telegram:1", plan["caption"], plan["scene"], "life", photo_brief=normalize_photo_brief(plan))
        changed = {"scene": "holding a blanket waiting for you", "caption": "毯子准备好了", "photo_brief": {"topic_key": "blanket"}}
        self.assertIn("等待", photo_repeat_reason(changed, state))
        record_topic_control(state, "别再提咖啡了")
        self.assertIn("结束", photo_repeat_reason(plan, state))
        other = {"scene": "overhead view of a freshly baked bread", "caption": "面包出炉了", "subject_mode": "detail", "photo_brief": {"topic_key": "bread", "actual_change": "baked"}}
        self.assertEqual(photo_repeat_reason(other, state), "")

    def test_world_fields_survive_card_round_trip(self):
        state = session_schema.state_defaults()
        card = {"world_id": "abc", "world_snapshot": {"name": "世界"}, "dialogue_examples": "例子", "alternate_greetings": ["你好"]}
        character_card.apply_card_to_state(state, card)
        result = character_card.card_from_state(state)
        for key, value in card.items():
            self.assertEqual(result[key], value)

    def test_fictional_weather_and_hidden_world_facts(self):
        async def run():
            svc = self.make_service()
            state = svc._get_session_state("telegram:1")
            session_schema.set_character_value(state, "custom_world_snapshot", {"name": "浮岛", "kind": "fictional", "summary": "岛上生活", "photography": "scene", "entries": [{"name": "秘密", "content": "隐藏结局", "known": False}, {"name": "小屋", "id": "p1", "type": "place", "content": "木屋", "known": True, "place_key": "home"}]})
            svc.http = None
            self.assertIsNone(await svc._fetch_weather(session_id="telegram:1"))
            self.assertNotIn("隐藏结局", svc._imported_world_context("telegram:1", "秘密"))
            self.assertEqual(svc._session_city("telegram:1"), "浮岛")
        asyncio.run(run())

    def test_llm_conversion_and_idempotent_commit(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._get_session_state(sid)
            parsed = parse_tavern_file(json.dumps(sample_card()).encode())
            svc.app_store.create_import_draft("draft", sid, {"parsed": parsed, "avatar": ""})
            svc._call_life_plan_json = AsyncMock(return_value=sample_conversion())
            await convert_import(svc, sid, "draft")
            row = svc.app_store.get_import_draft(sid, "draft")
            self.assertEqual(row["status"], "ready")
            self.assertFalse(session_schema.get_saved_characters(svc._get_session_state(sid)))
            class Request(dict):
                app = {"service": svc}
                match_info = {"session_id": sid, "draft_id": "draft"}
                async def json(self):
                    return {}
            request = Request(web_auth={"role": "admin"})
            first = json.loads((await api_import_commit(request)).text)["result"]
            second = json.loads((await api_import_commit(request)).text)["result"]
            self.assertEqual(first, second)
            self.assertEqual(len(svc.app_store.list_world_profiles("1")), 1)
            self.assertEqual(len(session_schema.get_saved_characters(svc._get_session_state(sid))), 1)
            self.assertIsNone(svc.app_store.get_import_draft("telegram:2", "draft"))
        asyncio.run(run())
