from __future__ import annotations

import asyncio
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from telegram_comfyui_selfie import appearance as appearance_rules
from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.webui import (
    api_activate_character,
    api_character_avatar_image,
    api_characters,
    api_diaries,
    api_generate_character_avatar,
    api_get_history_summary,
    api_organize_memories,
    api_save_character,
    api_save_diary,
    api_save_history_summary,
    api_test_push_selected_character,
    api_update_wardrobe,
    build_world_route_preview,
    cast_config_value,
    masked_config,
    required_character_key_from_request,
    serialize_prompt_slots,
    session_summary,
)
from tests.support import ServiceFixtureMixin, make_project_temp_dir


class WebUICharacterTestCase(ServiceFixtureMixin, unittest.TestCase):
    """WebUI 角色、衣柜、头像与角色作用域操作测试。"""

    def test_character_panel_hides_preference_fields(self):
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "telegram_comfyui_selfie" / "static" / "app.js").read_text(encoding="utf-8")
        character_js = (root / "telegram_comfyui_selfie" / "static" / "character_ui.js").read_text(encoding="utf-8")
        styles = (root / "telegram_comfyui_selfie" / "static" / "styles.css").read_text(encoding="utf-8")
        character_fields = app_js.split("const characterFieldSections = [", 1)[1].split("const commands =", 1)[0]
        self.assertIn('["user_address", "对用户称呼", "text", "half"]', character_fields)
        self.assertIn('["workday_wake_time", "工作日起床", "time", "quarter"]', character_fields)
        self.assertIn('["weekend_sleep_time", "周末睡觉", "time", "quarter"]', character_fields)
        self.assertIn('["purity", "纯良度", "number", "third"]', character_fields)
        self.assertNotIn('["边界"', character_fields)
        self.assertNotIn('["scene_preference"', character_fields)
        self.assertNotIn('["selfie_preference"', character_fields)
        self.assertIn(".character-form .field-quarter", styles)
        self.assertIn("scroll-snap-type: x proximity", styles)
        self.assertIn(".runtime-clothing-section", styles)
        self.assertIn("container-type: inline-size", styles)
        self.assertIn("@container (max-width: 720px)", styles)
        self.assertIn('user_profile: "用户画像"', app_js)
        self.assertIn('mem.kind === "user_profile" ? " is-user-profile" : ""', character_js)
        self.assertIn(".memory-row.is-user-profile", styles)

    def test_overview_feedback_board_uses_todo_api(self):
        root = Path(__file__).resolve().parents[1]
        index_html = (root / "telegram_comfyui_selfie" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (root / "telegram_comfyui_selfie" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="feedback-list"', index_html)
        self.assertIn('id="feedback-form"', index_html)
        self.assertIn("loadFeedbackBoard", app_js)
        self.assertIn("/api/feedback", app_js)
        self.assertIn("state.selectedSession", app_js)

    def test_feedback_api_scopes_todo_sections_by_session_and_role(self):
        async def run():
            from aiohttp import web
            from telegram_comfyui_selfie.webui import api_feedback, api_submit_feedback

            class RequestStub(dict):
                def __init__(self, app, *, auth, query=None, payload=None):
                    super().__init__()
                    self.app = app
                    self.query = query or {}
                    self["web_auth"] = auth
                    self._payload = payload or {}

                async def json(self):
                    return self._payload

            svc = self.make_service()
            tmp = make_project_temp_dir("feedback")
            todo = tmp / "TODO.md"
            todo.write_text("# old plan\n\n## 无 session 的旧段落\n不会显示在反馈板。\n", encoding="utf-8")
            svc.feedback_file_path = todo
            state1 = svc._get_session_state("telegram:1")
            state2 = svc._get_session_state("telegram:2")
            session_schema.set_character_value(state1, "custom_character", "小雨")
            session_schema.set_character_value(state2, "custom_character", "小雪")
            app = web.Application()
            app["service"] = svc

            req1 = RequestStub(
                app,
                auth={"role": "user", "user_id": "1", "token": "u1"},
                payload={"content": "希望反馈板能看到自己的内容"},
            )
            await api_submit_feedback(req1)
            req2 = RequestStub(
                app,
                auth={"role": "user", "user_id": "2", "token": "u2"},
                payload={"content": "另一个用户的反馈"},
            )
            await api_submit_feedback(req2)

            text = todo.read_text(encoding="utf-8")
            self.assertIn("## 小雨", text)
            self.assertIn("<!-- session_id: telegram:1 -->", text)
            self.assertIn("## 小雪", text)
            self.assertIn("<!-- session_id: telegram:2 -->", text)

            user_resp = await api_feedback(RequestStub(
                app,
                auth={"role": "user", "user_id": "1", "token": "u1"},
            ))
            user_data = json.loads(user_resp.text)
            self.assertEqual(len(user_data["sections"]), 1)
            self.assertEqual(user_data["sections"][0]["user_name"], "小雨")
            self.assertIn("自己的内容", user_data["sections"][0]["content"])
            self.assertNotIn("另一个用户", user_data["sections"][0]["content"])

            admin_resp = await api_feedback(RequestStub(
                app,
                auth={"role": "admin", "user_id": "admin", "token": "a"},
                query={"session_id": "telegram:1"},
            ))
            admin_data = json.loads(admin_resp.text)
            self.assertEqual({item["session_id"] for item in admin_data["sections"]}, {"telegram:1", "telegram:2"})
            self.assertEqual(admin_data["current_user_name"], "小雨")

        asyncio.run(run())

    def test_webui_outfit_save_reflected_in_build_prompt(self):
        """WebUI角色页保存outfit后，build_prompt 应使用新穿搭，不出现默认发瞳污染。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            # _oc_translate_tags 直接透传英文标签（无 CJK → 不调 LLM）
            svc._oc_translate_tags = AsyncMock(side_effect=lambda text: (text or "").strip())

            # 创建含污穿搭的 OC（模拟 LLM 误分类把默认发瞳灌进穿搭）
            await svc.cmd_create_oc(
                1, sid,
                "名字: 林翩翩\n身体特征: brown eyes, black hair\n"
                "初始穿搭: purple eyes, pink vertical pupils, black hair, white hanfu",
            )
            state = svc._get_session_state(sid)
            # 修复已生效：穿搭不含发瞳
            outfit = session_schema.get_outfit(state)
            self.assertNotIn("purple eyes", outfit.lower())
            self.assertNotIn("pink vertical", outfit.lower())
            self.assertIn("white hanfu", outfit.lower())

            # 模拟 WebUI 角色页保存：把穿搭清空
            payload = {"id": "林翩翩", "outfit": "", "appearance": "brown eyes, black hair"}
            # 复现 api_save_character 的条件逻辑
            active_id = state.get("custom_character") or state.get("custom_bot_name") or ""
            key = str(payload.get("id") or payload.get("character") or payload.get("bot_name") or "").strip()
            self.assertEqual(active_id, key)  # 应该相等，否则 _apply_character_payload 不会调用
            svc._apply_character_payload(state, payload)

            # 穿搭已清空
            self.assertEqual(session_schema.get_outfit(state), "")

            # build_prompt 应只用 base appearance（brown eyes），不出默认紫瞳
            pos, neg = svc._build_prompt("standing in a room", session_id=sid)
            self.assertIn("brown eyes", pos.lower())
            self.assertNotIn("purple eyes", pos.lower())

        asyncio.run(run())

    def test_character_webui_style_field_uses_pool_datalist_and_manual_input(self):
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request
            from telegram_comfyui_selfie.webui import api_characters

            svc = self.make_service()
            sid = "telegram:1"
            svc.config["style_pool"] = "@base\n@dream_style"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            session_schema.set_outfit(state, "black silk slip dress, white cotton knit cardigan")
            session_schema.set_wardrobe(state, {"dress": "black silk slip dress", "outerwear": "white cotton knit cardigan"})
            session_schema.set_public_fallback_outfit(state, {"top": "plain white crew-neck t-shirt", "bottom": "dark blue jeans"})
            session_schema.set_closet(state, {
                "public fallback top": {"slot": "top", "tags": "plain white crew-neck t-shirt", "times_worn": 1, "last_worn": 1.0},
                "丝绸睡裙": {"slot": "dress", "tags": "black silk slip dress", "times_worn": 1, "last_worn": 2.0},
            })
            svc._snapshot_character(state)
            app = web.Application()
            app["service"] = svc
            req = make_mocked_request(
                "GET",
                f"/api/sessions/{sid}/characters",
                app=app,
                match_info={"session_id": sid},
            )
            req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

            resp = await api_characters(req)
            data = json.loads(resp.text)

            self.assertEqual(data["style_pool"], ["@base", "@dream_style"])
            self.assertEqual(data["current_clothing"]["wardrobe"]["dress"], "black silk slip dress")
            self.assertEqual(data["current_clothing"]["wardrobe_display"]["dress"], "丝绸睡裙")
            self.assertEqual(data["current_clothing"]["public_fallback_outfit"]["bottom"], "dark blue jeans")
            self.assertNotIn("public fallback top", data["current_clothing"]["closet"])
            self.assertIn("丝绸睡裙", data["current_clothing"]["closet"])
            static_root = Path(__file__).resolve().parents[1] / "telegram_comfyui_selfie" / "static"
            app_js = (static_root / "app.js").read_text(encoding="utf-8")
            character_js = (static_root / "character_ui.js").read_text(encoding="utf-8")
            self.assertIn('["style", "画风", "style_combo", "half"]', app_js)
            self.assertNotIn('["outfit", "服装标签", "textarea", "wide"]', app_js)
            self.assertIn("当前衣柜", character_js)
            self.assertIn("身上穿着", character_js)
            self.assertIn("衣橱收藏", character_js)
            self.assertIn("closet-slot", character_js)
            self.assertIn("closet-choice", character_js)
            self.assertIn('data-wardrobe-action="apply"', character_js)
            self.assertIn('data-wardrobe-action="save-closet"', character_js)
            self.assertIn('data-wardrobe-action="edit-closet"', character_js)
            self.assertIn('data-wardrobe-action="delete-closet"', character_js)
            # 身上穿着只读：不再有逐槽“脱下”按钮，槽位清空走衣橱的“空”选项
            self.assertNotIn(">脱下</button>", character_js)
            self.assertIn("棉质针织", character_js)
            self.assertIn("wardrobe_display", character_js)
            self.assertNotIn('<span>${escapeHtml(entry.tags || "")}</span>', character_js)
            self.assertIn("state.characterData?.style_pool", app_js)
            self.assertIn("留空表示本角色不注入画风", app_js)

        asyncio.run(run())

    def test_webui_wardrobe_actions_edit_current_clothing(self):
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request

            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            session_schema.set_wardrobe(state, {
                "top": "plain white crew-neck t-shirt",
                "bottom": "dark blue jeans",
                "outerwear": "white cotton knit cardigan",
            })
            session_schema.set_outfit(state, appearance_rules.render_wardrobe(session_schema.get_wardrobe(state)))
            session_schema.set_closet(state, {
                "public fallback top": {"slot": "top", "tags": "plain white crew-neck t-shirt", "times_worn": 1, "last_worn": 1.0},
                "public fallback bottom": {"slot": "bottom", "tags": "dark blue jeans", "times_worn": 1, "last_worn": 1.0},
                "丝绸睡裙": {"slot": "dress", "tags": "black silk slip dress", "times_worn": 1, "last_worn": 2.0},
            })
            app = web.Application()
            app["service"] = svc

            def req(body):
                request = make_mocked_request(
                    "POST",
                    f"/api/sessions/{sid}/wardrobe",
                    app=app,
                    match_info={"session_id": sid},
                    headers={"Content-Type": "application/json"},
                )
                request["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
                request._read_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
                return request

            resp = await api_update_wardrobe(req({"action": "stash_public_fallback"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["current_clothing"]["wardrobe"], {"outerwear": "white cotton knit cardigan"})
            self.assertEqual(data["current_clothing"]["public_fallback_outfit"]["top"], "plain white crew-neck t-shirt")
            self.assertNotIn("public fallback top", data["current_clothing"]["closet"])

            resp = await api_update_wardrobe(req({"action": "wear_closet", "name": "丝绸睡裙"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            wardrobe = data["current_clothing"]["wardrobe"]
            self.assertEqual(wardrobe["dress"], "black silk slip dress")
            self.assertEqual(wardrobe["outerwear"], "white cotton knit cardigan")
            self.assertNotIn("top", wardrobe)
            self.assertNotIn("bottom", wardrobe)

            resp = await api_update_wardrobe(req({"action": "remove_slot", "slot": "outerwear"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["current_clothing"]["wardrobe"], {"dress": "black silk slip dress"})

            svc._classify_wardrobe_change = AsyncMock(return_value={"top": "white blouse", "names": {"top": "白衬衫"}})
            resp = await api_update_wardrobe(req({"action": "apply", "description": "换白衬衫"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            wardrobe = data["current_clothing"]["wardrobe"]
            self.assertEqual(wardrobe["top"], "white blouse")
            self.assertNotIn("dress", wardrobe)

            resp = await api_update_wardrobe(req({"action": "set-item-state", "slot": "top", "state": "half_off"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["current_clothing"]["wardrobe_item_states"], {"top": "half_off"})

            resp = await api_update_wardrobe(req({"action": "clear-item-states"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["current_clothing"]["wardrobe_item_states"], {})

            # 存进衣橱（暂不换上）：只进收藏、不动当前穿搭；action 连字符会被规范成下划线
            svc._classify_wardrobe_items = AsyncMock(return_value={"dress": "red qipao", "names": {"dress": "红旗袍"}})
            resp = await api_update_wardrobe(req({"action": "save-closet", "description": "红色旗袍"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            closet = data["current_clothing"]["closet"]
            self.assertIn("红旗袍", closet)
            self.assertEqual(closet["红旗袍"]["times_worn"], 0)  # 没穿过
            wardrobe = data["current_clothing"]["wardrobe"]
            self.assertNotIn("dress", wardrobe)
            self.assertEqual(wardrobe["top"], "white blouse")

            svc._classify_wardrobe_items = AsyncMock(return_value={"top": "white off shoulder shirt", "names": {}})
            resp = await api_update_wardrobe(req({"action": "save-closet", "description": "露肩白衬衫"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            closet = data["current_clothing"]["closet"]
            self.assertIn("露肩白衬衫", closet)
            self.assertNotIn("white off shoulder shirt", closet)
            self.assertEqual(closet["露肩白衬衫"]["tags"], "white off shoulder shirt")
            self.assertEqual(closet["露肩白衬衫"]["times_worn"], 0)
            self.assertEqual(data["current_clothing"]["wardrobe"]["top"], "white blouse")

            # 编辑收藏：改名 + 改标签；正穿在身上的同步更新当前穿搭
            resp = await api_update_wardrobe(req({
                "action": "closet_edit", "name": "白衬衫",
                "new_name": "露脐白衬衫", "tags": "white crop shirt",
            }))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            closet = data["current_clothing"]["closet"]
            self.assertNotIn("白衬衫", closet)
            self.assertEqual(closet["露脐白衬衫"]["tags"], "white crop shirt")
            self.assertEqual(data["current_clothing"]["wardrobe"]["top"], "white crop shirt")

            # 删除收藏：只删衣橱条目，不影响身上穿着
            resp = await api_update_wardrobe(req({"action": "closet_delete", "name": "红旗袍"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertNotIn("红旗袍", data["current_clothing"]["closet"])
            self.assertEqual(data["current_clothing"]["wardrobe"]["top"], "white crop shirt")

            resp = await api_update_wardrobe(req({"action": "clear"}))
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["current_clothing"]["wardrobe"], {})

        asyncio.run(run())

    def test_webui_current_wardrobe_prefers_closet_display_names(self):
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request
            from telegram_comfyui_selfie.webui import api_characters

            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            session_schema.set_wardrobe(state, {"top": "white", "bottom": "denim shorts"})
            session_schema.set_outfit(state, "white, denim shorts")
            session_schema.set_closet(state, {
                "露脐白衬衫": {"slot": "top", "tags": "white", "times_worn": 1, "last_worn": 3.0},
                "超短牛仔裤": {"slot": "bottom", "tags": "denim shorts", "times_worn": 1, "last_worn": 3.0},
            })
            app = web.Application()
            app["service"] = svc
            req = make_mocked_request(
                "GET",
                f"/api/sessions/{sid}/characters",
                app=app,
                match_info={"session_id": sid},
            )
            req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

            resp = await api_characters(req)
            data = json.loads(resp.text)
            self.assertEqual(data["current_clothing"]["wardrobe"], {"top": "white", "bottom": "denim shorts"})
            self.assertEqual(data["current_clothing"]["wardrobe_display"], {
                "top": "露脐白衬衫",
                "bottom": "超短牛仔裤",
            })

        asyncio.run(run())

    def test_character_avatar_generation_updates_dedicated_file_and_card(self):
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request

            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            session_schema.get_saved_characters(state)["小雨"] = {
                "character": "小雨",
                "persona": "温柔、慢热，说话简短",
                "appearance": "short black hair, blue eyes",
                "outfit": "white shirt",
            }
            svc._save_session_state(sid, state)
            generated = [b"first-avatar", b"second-avatar"]

            async def fake_generate(*args, **kwargs):
                return True, [generated.pop(0)], ""

            svc._do_generate = fake_generate
            app = web.Application()
            app["service"] = svc
            req = make_mocked_request(
                "POST",
                "/api/sessions/telegram:123/characters/%E5%B0%8F%E9%9B%A8/avatar",
                app=app,
                match_info={"session_id": sid, "character_id": "小雨"},
            )
            req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

            first = await api_generate_character_avatar(req)
            first_data = json.loads(first.text)
            self.assertTrue(first_data["ok"])
            self.assertEqual(first_data["characters"]["小雨"]["avatar_path"], "avatars/telegram_123/小雨.png")
            avatar_path = svc.state_path.parent / "avatars" / "telegram_123" / "小雨.png"
            self.assertEqual(avatar_path.read_bytes(), b"first-avatar")

            second = await api_generate_character_avatar(req)
            second_data = json.loads(second.text)
            self.assertTrue(second_data["ok"])
            self.assertEqual(second_data["characters"]["小雨"]["avatar_path"], "avatars/telegram_123/小雨.png")
            self.assertGreater(second_data["characters"]["小雨"]["avatar_updated_at"], first_data["characters"]["小雨"]["avatar_updated_at"])
            self.assertEqual(avatar_path.read_bytes(), b"second-avatar")

            image_resp = await api_character_avatar_image(req)
            self.assertEqual(image_resp.status, 200)

        asyncio.run(run())

    def test_concurrent_character_avatar_generation_keeps_active_state(self):
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request

            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "当前角色")
            session_schema.set_chat_history(state, [{"role": "user", "content": "active chat"}])
            session_schema.get_saved_characters(state).update({
                "当前角色": {"character": "当前角色", "persona": "Current persona"},
                "角色A": {"character": "角色A", "persona": "A persona"},
                "角色B": {"character": "角色B", "persona": "B persona"},
            })
            svc._save_session_state(sid, state)
            app = web.Application()
            app["service"] = svc

            in_flight = 0
            max_in_flight = 0
            seen_characters = []

            async def fake_generate(*args, **kwargs):
                nonlocal in_flight, max_in_flight
                active_state = svc._get_session_state(sid)
                current = session_schema.get_character_value(active_state, "custom_character", "")
                seen_characters.append(current)
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1
                return True, [f"{current}-avatar".encode("utf-8")], ""

            svc._do_generate = fake_generate

            def avatar_req(character_id):
                req = make_mocked_request(
                    "POST",
                    f"/api/sessions/{sid}/characters/{character_id}/avatar",
                    app=app,
                    match_info={"session_id": sid, "character_id": character_id},
                )
                req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
                return req

            responses = await asyncio.gather(
                api_generate_character_avatar(avatar_req("角色A")),
                api_generate_character_avatar(avatar_req("角色B")),
            )
            self.assertTrue(all(json.loads(resp.text)["ok"] for resp in responses))
            self.assertEqual(max_in_flight, 1)
            self.assertEqual(seen_characters, ["角色A", "角色B"])
            after = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(after, "custom_character", ""), "当前角色")
            self.assertEqual(session_schema.get_chat_history(after), [{"role": "user", "content": "active chat"}])
            saved = session_schema.get_saved_characters(after)
            self.assertEqual(saved["角色A"]["avatar_path"], "avatars/telegram_123/角色A.png")
            self.assertEqual(saved["角色B"]["avatar_path"], "avatars/telegram_123/角色B.png")
            self.assertEqual((svc.state_path.parent / "avatars" / "telegram_123" / "角色A.png").read_bytes(), "角色A-avatar".encode("utf-8"))
            self.assertEqual((svc.state_path.parent / "avatars" / "telegram_123" / "角色B.png").read_bytes(), "角色B-avatar".encode("utf-8"))

        asyncio.run(run())

    def test_webui_role_scoped_operations_do_not_fallback_to_active_character(self):
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request

            class JsonRequest(dict):
                def __init__(self, app, method, path, match_info, payload=None, query=None):
                    super().__init__()
                    self.app = app
                    self.match_info = match_info
                    self.query = query or {}
                    self._payload = payload or {}
                    self["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

                async def json(self):
                    return self._payload

            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.set_character_value(state, "custom_scheduled_persona", "A persona")
            session_schema.set_character_history_summary(state, "A active summary")
            session_schema.get_saved_characters(state).update({
                "角色A": {"character": "角色A", "persona": "A persona"},
                "角色B": {"character": "角色B", "persona": "B old"},
            })
            svc._save_session_state(sid, state)
            app = web.Application()
            app["service"] = svc

            get_b = make_mocked_request(
                "GET",
                f"/api/sessions/{sid}/history-summary?character_key=%E8%A7%92%E8%89%B2B",
                app=app,
                match_info={"session_id": sid},
            )
            get_b["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
            history_resp = await api_get_history_summary(get_b)
            history_data = json.loads(history_resp.text)
            self.assertEqual(history_data["character_key"], "角色B")
            self.assertEqual(history_data["summary"], "")

            save_history_req = JsonRequest(
                app, "PUT", f"/api/sessions/{sid}/history-summary",
                {"session_id": sid},
                payload={"character_key": "角色B", "summary": "B summary"},
            )
            await api_save_history_summary(save_history_req)
            self.assertEqual(svc.app_store.get_context_meta(sid, "角色B").get("character_history_summary"), "B summary")
            self.assertEqual(session_schema.get_character_history_summary(svc._get_session_state(sid)), "A active summary")

            save_diary_req = JsonRequest(
                app, "POST", f"/api/sessions/{sid}/diaries/2026-07-01",
                {"session_id": sid, "diary_date": "2026-07-01"},
                payload={"character_key": "角色B", "content": "B diary"},
            )
            await api_save_diary(save_diary_req)
            self.assertEqual(svc.app_store.get_diary(sid, "角色B", "2026-07-01").get("content"), "B diary")
            self.assertFalse(svc.app_store.get_diary(sid, "角色A", "2026-07-01"))

            no_key_diaries = make_mocked_request(
                "GET", f"/api/sessions/{sid}/diaries", app=app, match_info={"session_id": sid}
            )
            no_key_diaries["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
            no_key_resp = await api_diaries(no_key_diaries)
            no_key_data = json.loads(no_key_resp.text)
            self.assertFalse(no_key_data["ok"])
            self.assertIn("character_key", no_key_data["error"])

            svc.has_llm_config = lambda purpose, session_id="": True
            svc._organize_memories_after_dream = AsyncMock(return_value={"status": "no_op"})
            organize_req = make_mocked_request(
                "POST",
                f"/api/sessions/{sid}/organize-memories?character_key=%E8%A7%92%E8%89%B2B",
                app=app,
                match_info={"session_id": sid},
            )
            organize_req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
            organize_resp = await api_organize_memories(organize_req)
            organize_data = json.loads(organize_resp.text)
            self.assertTrue(organize_data["ok"])
            self.assertEqual(organize_data["character"], "角色B")
            svc._organize_memories_after_dream.assert_awaited_once_with(sid, "角色B")

            save_char_req = JsonRequest(
                app, "POST", f"/api/sessions/{sid}/characters",
                {"session_id": sid},
                payload={"id": "角色B", "character": "角色B", "persona": "B new"},
            )
            await api_save_character(save_char_req)
            after = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(after, "custom_character"), "角色A")
            self.assertEqual(session_schema.get_character_value(after, "custom_scheduled_persona"), "A persona")
            self.assertEqual(session_schema.get_saved_characters(after)["角色B"]["persona"], "B new")

        asyncio.run(run())

    def test_webui_manual_push_uses_selected_character_context(self):
        async def run():
            from aiohttp import web

            class DummyHttp:
                closed = False

            class JsonRequest(dict):
                def __init__(self, app, match_info, payload=None):
                    super().__init__()
                    self.app = app
                    self.match_info = match_info
                    self.query = {}
                    self._payload = payload or {}
                    self["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

                async def json(self):
                    return self._payload

            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.set_character_value(state, "custom_scheduled_persona", "A persona")
            session_schema.set_chat_history(state, [{"role": "user", "content": "A current chat"}])
            session_schema.get_saved_characters(state).update({
                "角色A": {"character": "角色A", "persona": "A persona", "outfit": "A outfit"},
                "角色B": {
                    "character": "角色B",
                    "persona": "B persona",
                    "outfit": "B outfit",
                    "avatar_path": "avatars/telegram_123/角色B.png",
                },
            })
            session_schema.get_character_contexts(state)["角色B"] = {
                "chat_history": [{"role": "user", "content": "B old chat"}],
                "sent_photos_history": [{"scene": "B old photo"}],
            }
            svc._save_session_state(sid, state)

            keepalive = asyncio.create_task(asyncio.sleep(60))
            svc.http = DummyHttp()
            svc._bot_tasks = [keepalive]
            observed = {}

            async def fake_sched(session_id, local_dt, mode_override=None, skip_active_check=False, character_lock_held=False):
                active_state = svc._get_session_state(session_id)
                observed["character"] = session_schema.get_character_value(active_state, "custom_character", "")
                observed["mode"] = mode_override
                observed["skip_active_check"] = skip_active_check
                observed["character_lock_held"] = character_lock_held
                svc._record_sent_photo(
                    session_id,
                    "B push scene",
                    "B caption",
                    appearance="B outfit",
                    view="selfie",
                    nltag="B final nltag",
                )
                return True

            svc._sched_fire = fake_sched
            app = web.Application()
            app["service"] = svc
            req = JsonRequest(
                app,
                {"session_id": sid},
                payload={"character_key": "角色B", "mode": "morning"},
            )
            try:
                resp = await api_test_push_selected_character(req)
            finally:
                keepalive.cancel()
                try:
                    await keepalive
                except asyncio.CancelledError:
                    pass
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertTrue(data["triggered"])
            self.assertEqual(observed, {
                "character": "角色B",
                "mode": "morning",
                "skip_active_check": True,
                "character_lock_held": True,
            })

            after = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(after, "custom_character", ""), "角色A")
            self.assertEqual(session_schema.get_chat_history(after), [{"role": "user", "content": "A current chat"}])
            contexts = session_schema.get_character_contexts(after)
            self.assertIn("角色B", contexts)
            b_context = contexts["角色B"]
            self.assertEqual(b_context["sent_photos_history"][-1]["scene"], "B push scene")
            self.assertEqual(
                session_schema.get_saved_characters(after)["角色B"]["avatar_path"],
                "avatars/telegram_123/角色B.png",
            )
            b_history_text = "\n".join(m.get("content", "") for m in b_context.get("chat_history", []))
            self.assertIn("B final nltag", b_history_text)
            active_history_text = "\n".join(m.get("content", "") for m in session_schema.get_chat_history(after))
            self.assertNotIn("B final nltag", active_history_text)

        asyncio.run(run())

    def test_avatar_generation_and_manual_push_share_character_operation_lock(self):
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request

            class DummyHttp:
                closed = False

            class JsonRequest(dict):
                def __init__(self, app, match_info, payload=None):
                    super().__init__()
                    self.app = app
                    self.match_info = match_info
                    self.query = {}
                    self._payload = payload or {}
                    self["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

                async def json(self):
                    return self._payload

            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色C")
            session_schema.set_chat_history(state, [{"role": "user", "content": "C active chat"}])
            session_schema.get_saved_characters(state).update({
                "角色A": {"character": "角色A", "persona": "A persona"},
                "角色B": {"character": "角色B", "persona": "B persona"},
                "角色C": {"character": "角色C", "persona": "C persona"},
            })
            svc._save_session_state(sid, state)
            app = web.Application()
            app["service"] = svc

            keepalive = asyncio.create_task(asyncio.sleep(60))
            svc.http = DummyHttp()
            svc._bot_tasks = [keepalive]
            in_flight = 0
            max_in_flight = 0
            seen = []

            async def enter(label):
                nonlocal in_flight, max_in_flight
                active_state = svc._get_session_state(sid)
                seen.append((label, session_schema.get_character_value(active_state, "custom_character", "")))
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1

            async def fake_generate(*args, **kwargs):
                await enter("avatar")
                return True, [b"a-avatar"], ""

            async def fake_sched(session_id, local_dt, mode_override=None, skip_active_check=False, character_lock_held=False):
                await enter("push")
                svc._record_sent_photo(
                    session_id,
                    "B push scene",
                    "B caption",
                    appearance="B outfit",
                    view="selfie",
                    nltag="B push nltag",
                )
                return True

            svc._do_generate = fake_generate
            svc._sched_fire = fake_sched

            avatar_req = make_mocked_request(
                "POST",
                f"/api/sessions/{sid}/characters/角色A/avatar",
                app=app,
                match_info={"session_id": sid, "character_id": "角色A"},
            )
            avatar_req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
            push_req = JsonRequest(
                app,
                {"session_id": sid},
                payload={"character_key": "角色B", "mode": "normal"},
            )
            try:
                avatar_resp, push_resp = await asyncio.gather(
                    api_generate_character_avatar(avatar_req),
                    api_test_push_selected_character(push_req),
                )
            finally:
                keepalive.cancel()
                try:
                    await keepalive
                except asyncio.CancelledError:
                    pass
            self.assertTrue(json.loads(avatar_resp.text)["ok"])
            self.assertTrue(json.loads(push_resp.text)["ok"])
            self.assertEqual(max_in_flight, 1)
            self.assertEqual([item[1] for item in seen], ["角色A", "角色B"])
            after = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(after, "custom_character", ""), "角色C")
            self.assertEqual(session_schema.get_chat_history(after), [{"role": "user", "content": "C active chat"}])
            contexts = session_schema.get_character_contexts(after)
            self.assertEqual(contexts["角色B"]["sent_photos_history"][-1]["scene"], "B push scene")
            active_history = "\n".join(m.get("content", "") for m in session_schema.get_chat_history(after))
            self.assertNotIn("B push nltag", active_history)

        asyncio.run(run())

    def test_webui_masks_secrets(self):
        svc = self.make_service()
        svc.config["telegram_bot_token"] = "secret-token"
        svc.config["global_model_profiles"] = {
            "secret-profile": {
                "name": "Secret",
                "base_url": "https://example.com/v1",
                "api_key": "profile-secret",
                "api_key_no_think": "profile-secret-2",
                "model": "m",
            }
        }
        cfg = masked_config(svc)
        self.assertEqual(cfg["values"]["telegram_bot_token"], "")
        self.assertTrue(cfg["secret_present"]["telegram_bot_token"])
        profile = cfg["values"]["global_model_profiles"]["secret-profile"]
        self.assertEqual(profile["api_key"], "********")
        self.assertEqual(profile["api_key_no_think"], "********")

    def test_webui_casts_lists_and_booleans(self):
        self.assertEqual(cast_config_value("allowed_chat_ids", "1, 2\n3", []), ["1", "2", "3"])
        self.assertTrue(cast_config_value("turbo_mode", "true", False))
        self.assertEqual(cast_config_value("web_port", "9999", 8787), 9999)

    def test_session_summary_uses_chat_id_and_purity(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state["purity"] = 7
        summary = session_summary(svc, sid, state)
        self.assertEqual(summary["chat_id"], 123)
        self.assertEqual(summary["purity"], 7)

    def test_webui_world_route_preview_is_session_specific(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 6, 18, 19, 20, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "Alice",
            "custom_location": "大阪",
            "custom_timezone_offset": "9",
            "custom_character_age_stage": "adult",
            "custom_character_day_anchor": "company",
            "user_place": "mall",
            "user_place_label": "商场",
            "user_place_text": "我在商场",
            "user_place_updated_at": time.time(),
        })
        svc.city_place_catalogs[svc._city_catalog_key("大阪")] = {
            "updated_at": time.time(),
            "places": {
                "mall": ["心斋桥"],
                "park": ["大阪城公园"],
            },
        }

        preview = build_world_route_preview(svc, sid, weather={"desc": "晴", "temp": "22"})

        self.assertTrue(preview["enabled"])
        self.assertEqual(preview["session"]["chat_id"], 123)
        self.assertEqual(preview["city"], "大阪")
        self.assertEqual(preview["current"]["user_place"]["key"], "mall")
        self.assertEqual(preview["current"]["life_profile"]["day_anchor"], "company")
        self.assertTrue(preview["current"]["next_place"])
        self.assertTrue(preview["catalog"]["has_catalog"])
        self.assertIsInstance(preview["current"].get("character_place_history"), list)
        self.assertEqual(len(preview["timeline"]), 12)
        self.assertTrue(any(item["is_current_slot"] for item in preview["timeline"]))

    def test_webui_prompt_slot_preview_exposes_editable_fields(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state.update({
            "custom_positive_prefix": "1girl, black hair, blue eyes",
            "custom_default_hair": "black hair",
            "custom_default_eyes": "blue eyes",
            "custom_current_style": "@00 gx4",
            "dynamic_appearance": "white dress",
            "custom_scene_preference": "常去咖啡店和公园",
            "custom_selfie_preference": "更喜欢前摄自拍",
        })

        preview = serialize_prompt_slots(svc, sid, scene="standing by a cafe window")

        self.assertIn("positive", preview)
        self.assertIn("items", preview)
        self.assertEqual(preview["editable"]["custom_scene_preference"], "常去咖啡店和公园")
        self.assertEqual(preview["effective"]["scene_preference"], "常去咖啡店和公园")
        self.assertEqual(preview["effective"]["selfie_preference"], "更喜欢前摄自拍")
        self.assertTrue(any(item["key"] == "positive_final" for item in preview["items"]))
        self.assertIn("standing by a cafe window", preview["positive"])

    def test_webui_save_activate_does_not_inherit_current_wardrobe(self):
        from aiohttp import web
        from telegram_comfyui_selfie.webui import api_save_character

        class FakeJsonRequest(dict):
            def __init__(self, app, sid, payload):
                super().__init__()
                self.app = app
                self.match_info = {"session_id": sid}
                self["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
                self._payload = payload

            async def json(self):
                return self._payload

        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.get_saved_characters(state)["角色A"] = {"character": "角色A", "persona": "我是A"}
            session_schema.set_outfit(state, "A red dress")
            session_schema.set_wardrobe(state, {"dress": "A red dress"})
            svc._save_session_state(sid, state)

            app = web.Application()
            app["service"] = svc
            req = FakeJsonRequest(app, sid, {"id": "角色B", "character": "角色B", "persona": "我是B", "activate": True})
            resp = await api_save_character(req)

            self.assertEqual(resp.status, 200)
            after = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(after, "custom_character"), "角色B")
            self.assertEqual(session_schema.get_outfit(after), "")
            self.assertEqual(session_schema.get_wardrobe(after), {})
            self.assertEqual(session_schema.get_character_contexts(after)["角色A"]["clothing"]["dynamic_appearance"], "A red dress")

        asyncio.run(run())

    def test_default_character_webui_key_normalization(self):
        """WebUI required_character_key_from_request 把默认角色 id / __default__ 归一为空串。"""
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        svc = self.make_service()
        sid = "telegram:key-norm"
        # 设置默认角色名 "蕾伊"（bot_name）
        svc.config["bot_name"] = "蕾伊"
        app = web.Application()
        app["service"] = svc

        def _req(key):
            r = make_mocked_request("GET", f"/?character_key={key}", app=app, match_info={"session_id": sid})
            return r

        # 1) __default__ → ""
        self.assertEqual(required_character_key_from_request(_req("__default__")), "")
        # 2) default → ""
        self.assertEqual(required_character_key_from_request(_req("default")), "")
        # 3) 默认角色 payload id（bot_name）→ ""（未在 saved 中）
        self.assertEqual(required_character_key_from_request(_req("蕾伊")), "")
        # 4) 普通自定义角色不受影响
        self.assertEqual(required_character_key_from_request(_req("白子")), "白子")

        # 5) 添加记忆到空串键后，WebUI 用归一后的 key 也能读到同一记忆
        svc.memory.add_memory(sid, "manual", "默认角色记忆", character="")
        memories = svc.memory.list_memories(sid, character="")
        self.assertTrue(any("默认角色记忆" in (m.get("summary") or "") for m in memories))

        # 6) 用户真实创建名为 default 的自定义角色时，不再被旧占位规则吞掉。
        session_schema.get_saved_characters(svc._get_session_state(sid))["default"] = {
            "character": "default",
            "persona": "真实自定义角色",
        }
        self.assertEqual(required_character_key_from_request(_req("default")), "default")

    def test_character_operation_lock_covers_telegram_and_scheduler_runtime(self):
        """Telegram 整轮处理与定时推送实际持锁，而非只在入口瞬时检查。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:lock-runtime"
            entered = asyncio.Event()
            release = asyncio.Event()

            async def fake_locked(chat_id, session_id, msg, text):
                self.assertTrue(svc.character_operation_lock(session_id).locked())
                entered.set()
                await release.wait()

            svc._process_incoming_message_locked = fake_locked
            task = asyncio.create_task(svc._process_incoming_message(1, sid, {}, "hello"))
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertTrue(svc.character_operation_lock(sid).locked())
            release.set()
            await task
            self.assertFalse(svc.character_operation_lock(sid).locked())

            observed = {}

            def stop_push(state):
                observed["locked"] = svc.character_operation_lock(sid).locked()
                return True

            svc._check_goodnight_inhibition = stop_push
            ok = await svc._sched_fire(sid, svc._session_now(sid))
            self.assertFalse(ok)
            self.assertTrue(observed["locked"])
            self.assertFalse(svc.character_operation_lock(sid).locked())

        asyncio.run(run())

    def test_webui_default_character_save_list_and_activation_are_consistent(self):
        """合成默认卡写回 config、默认态正确高亮，并可从自定义角色切回。"""
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request

            class JsonRequest(dict):
                def __init__(self, app, match_info, payload):
                    super().__init__()
                    self.app = app
                    self.match_info = match_info
                    self.query = {}
                    self._payload = payload
                    self["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

                async def json(self):
                    return self._payload

            svc = self.make_service()
            sid = "telegram:default-card"
            svc.config["bot_name"] = "蕾伊"
            app = web.Application()
            app["service"] = svc

            default_card = svc._default_character_payload()
            default_card["persona"] = "修改后的默认人格"
            save_resp = await api_save_character(JsonRequest(app, {"session_id": sid}, default_card))
            self.assertTrue(json.loads(save_resp.text)["ok"])
            self.assertEqual(svc.config["scheduled_persona"], "修改后的默认人格")
            self.assertNotIn("蕾伊", session_schema.get_saved_characters(svc._get_session_state(sid)))

            list_req = make_mocked_request(
                "GET", f"/api/sessions/{sid}/characters", app=app, match_info={"session_id": sid}
            )
            list_req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
            list_data = json.loads((await api_characters(list_req)).text)
            self.assertEqual(list_data["active_id"], "蕾伊")
            self.assertTrue(list_data["characters"]["蕾伊"]["is_default"])

            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.set_character_value(state, "custom_scheduled_persona", "A 人格")
            session_schema.set_character_value(state, "purity", 2)
            session_schema.set_character_value(state, "purity_user_set", True)
            session_schema.get_saved_characters(state)["角色A"] = {"character": "角色A", "persona": "A 人格"}
            activate_req = make_mocked_request(
                "POST",
                f"/api/sessions/{sid}/characters/蕾伊/activate",
                app=app,
                match_info={"session_id": sid, "character_id": "蕾伊"},
            )
            activate_req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
            activate_data = json.loads((await api_activate_character(activate_req)).text)
            self.assertTrue(activate_data["ok"])
            self.assertEqual(activate_data["active_id"], "蕾伊")
            after = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(after, "custom_character", ""), "")
            self.assertEqual(session_schema.get_character_value(after, "custom_scheduled_persona", ""), "")
            self.assertIsNone(session_schema.get_character_value(after, "purity"))
            self.assertFalse(session_schema.get_character_value(after, "purity_user_set", False))

        asyncio.run(run())

    def test_webui_manual_push_accepts_default_character_sentinel(self):
        """前端发送 __default__ 时，后台用隐式默认角色推送并恢复原活动角色。"""
        async def run():
            from aiohttp import web

            class DummyHttp:
                closed = False

            class JsonRequest(dict):
                def __init__(self, app, match_info, payload):
                    super().__init__()
                    self.app = app
                    self.match_info = match_info
                    self.query = {}
                    self._payload = payload
                    self["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

                async def json(self):
                    return self._payload

            svc = self.make_service()
            sid = "telegram:default-push"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.set_character_value(state, "custom_scheduled_persona", "A 人格")
            session_schema.get_saved_characters(state)["角色A"] = {"character": "角色A", "persona": "A 人格"}
            svc._save_session_state(sid, state)
            observed = {}

            async def fake_sched(
                session_id,
                local_dt,
                mode_override=None,
                skip_active_check=False,
                character_lock_held=False,
            ):
                active = svc._get_session_state(session_id)
                observed["character"] = session_schema.get_character_value(active, "custom_character", "")
                observed["lock_held"] = character_lock_held
                return True

            keepalive = asyncio.create_task(asyncio.sleep(60))
            svc.http = DummyHttp()
            svc._bot_tasks = [keepalive]
            svc._sched_fire = fake_sched
            app = web.Application()
            app["service"] = svc
            req = JsonRequest(
                app,
                {"session_id": sid},
                {"character_key": "__default__", "mode": "normal"},
            )
            try:
                data = json.loads((await api_test_push_selected_character(req)).text)
            finally:
                keepalive.cancel()
                try:
                    await keepalive
                except asyncio.CancelledError:
                    pass
            self.assertTrue(data["ok"])
            self.assertTrue(data["triggered"])
            self.assertEqual(observed, {"character": "", "lock_held": True})
            after = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_value(after, "custom_character", ""), "角色A")
            self.assertEqual(session_schema.get_character_value(after, "custom_scheduled_persona", ""), "A 人格")

        asyncio.run(run())

    def test_avatar_generation_restores_session_and_global_prompt_caches(self):
        """头像临时生图结束后恢复活动角色最近一次提示词与 nltag 缓存。"""
        async def run():
            from aiohttp import web
            from aiohttp.test_utils import make_mocked_request

            svc = self.make_service()
            sid = "telegram:avatar-cache"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "当前角色")
            session_schema.get_saved_characters(state)["头像角色"] = {"character": "头像角色"}
            svc._last_prompt_slots_by_session = {sid: "old-slots", "telegram:other": "other-slots"}
            svc._last_generated_nltag_by_session = {sid: "old-nltag", "telegram:other": "other-nltag"}
            svc._last_prompt_slots = "old-global-slots"
            svc._last_generated_nltag = "old-global-nltag"

            async def fake_generate(*args, **kwargs):
                svc._last_prompt_slots_by_session[sid] = "avatar-slots"
                svc._last_generated_nltag_by_session[sid] = "avatar-nltag"
                svc._last_prompt_slots = "avatar-global-slots"
                svc._last_generated_nltag = "avatar-global-nltag"
                return True, [b"avatar"], ""

            svc._do_generate = fake_generate
            app = web.Application()
            app["service"] = svc
            req = make_mocked_request(
                "POST",
                f"/api/sessions/{sid}/characters/头像角色/avatar",
                app=app,
                match_info={"session_id": sid, "character_id": "头像角色"},
            )
            req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
            data = json.loads((await api_generate_character_avatar(req)).text)
            self.assertTrue(data["ok"])
            self.assertEqual(svc._last_prompt_slots_by_session, {
                sid: "old-slots", "telegram:other": "other-slots",
            })
            self.assertEqual(svc._last_generated_nltag_by_session, {
                sid: "old-nltag", "telegram:other": "other-nltag",
            })
            self.assertEqual(svc._last_prompt_slots, "old-global-slots")
            self.assertEqual(svc._last_generated_nltag, "old-global-nltag")

        asyncio.run(run())
