import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram_comfyui_selfie import TelegramComfyUIService
from telegram_comfyui_selfie.image_planning import format_dialog_context, format_sent_photo_context, normalize_scene_visual_subject
from telegram_comfyui_selfie.webui import build_world_route_preview, cast_config_value, masked_config, session_summary


class ServiceTestCase(unittest.TestCase):
    def make_service(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        cfg = root / "config.json"
        state = root / "state.json"
        cfg.write_text(json.dumps({"telegram_bot_token": "TEST"}, ensure_ascii=False), encoding="utf-8")
        svc = TelegramComfyUIService(cfg, state)
        self.addCleanup(tmp.cleanup)
        return svc

    def test_parse_command_with_bot_mention(self):
        svc = self.make_service()
        svc._bot_username = "my_bot"
        self.assertEqual(svc.parse_command("/自拍@my_bot now"), ("自拍", "now"))
        self.assertEqual(svc.parse_command("/自拍@other_bot now"), (None, "/自拍@other_bot now"))

    def test_parse_bare_selfie_shortcut(self):
        svc = self.make_service()

        self.assertEqual(svc.parse_command("自拍"), ("自拍", ""))
        self.assertEqual(svc.parse_command("拍照"), ("自拍", ""))
        self.assertEqual(svc.parse_command(" 自拍 神户街头 "), ("自拍", "神户街头"))
        self.assertEqual(svc.parse_command(" /自拍 "), ("自拍", ""))
        self.assertEqual(svc.parse_command("菜单 动线"), ("菜单", "动线"))
        self.assertEqual(svc.parse_command("我想看自拍"), (None, "我想看自拍"))

    def test_bare_selfie_message_dispatches_to_selfie_command(self):
        async def run():
            svc = self.make_service()
            svc.cmd_selfie = AsyncMock()
            svc.handle_chat = AsyncMock()

            await svc.handle_update({"message": {"chat": {"id": 123}, "text": "自拍"}})

            svc.cmd_selfie.assert_awaited_once_with(123, "telegram:123", "")
            svc.handle_chat.assert_not_awaited()

        asyncio.run(run())

    def test_menu_default_points_users_to_setup_topics(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.cmd_menu(1, "telegram:1", "")

            text = svc.send_message.await_args.args[1]
            self.assertIn("快速菜单", text)
            self.assertIn("推荐先设置", text)
            self.assertIn("/角色 <角色名>", text)
            self.assertIn("/菜单 设置", text)
            self.assertIn("/菜单 动线", text)

        asyncio.run(run())

    def test_menu_topic_alias_returns_focused_help(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.cmd_menu(1, "telegram:1", "memory")

            text = svc.send_message.await_args.args[1]
            self.assertIn("菜单 - 记忆", text)
            self.assertIn("/记住 <内容>", text)
            self.assertIn("当前角色", text)

        asyncio.run(run())

    def test_menu_world_route_topic_explains_daily_route(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.cmd_menu(1, "telegram:1", "world")

            text = svc.send_message.await_args.args[1]
            self.assertIn("菜单 - 动线", text)
            self.assertIn("每日动线", text)
            self.assertIn("/天气设置 <城市>", text)
            self.assertIn("用户位置", text)

        asyncio.run(run())

    def test_help_alias_dispatches_to_menu(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.dispatch_command(1, "telegram:1", "帮助", "")

            text = svc.send_message.await_args.args[1]
            self.assertIn("快速菜单", text)

        asyncio.run(run())

    def test_process_restart_prepares_once_and_flushes_state(self):
        svc = self.make_service()
        svc._spawn_restart_helper = lambda: 4242
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_character"] = "重启测试角色"
        svc.sessions[sid] = state
        svc._mark_dirty(sid)

        info = svc.prepare_process_restart()

        self.assertEqual(info["old_pid"], os.getpid())
        self.assertEqual(info["helper_pid"], 4242)
        saved = json.loads(svc.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["sessions"][sid]["custom_character"], "重启测试角色")
        self.assertTrue(svc._restart_requested)
        self.assertTrue(svc.prepare_process_restart()["already_requested"])

    def test_appearance_merge_replaces_outfit_and_accumulates_accessories(self):
        svc = self.make_service()
        merged = svc._merge_appearance("black hair, red dress, glasses", "white hair, blue dress, necklace")
        self.assertIn("white hair", merged)
        self.assertIn("blue dress", merged)
        self.assertIn("glasses", merged)
        self.assertIn("necklace", merged)
        self.assertNotIn("red dress", merged)

    def test_style_pool_normalizes_semicolon_and_duplicates(self):
        svc = self.make_service()
        svc.config["style_pool"] = "@a; @b\n@a"
        self.assertEqual(svc._normalize_style_pool(), ["@a", "@b"])

    def test_purity_threshold_edges(self):
        svc = self.make_service()
        self.assertEqual(svc._compute_ntr_threshold(0), 1)
        self.assertEqual(svc._compute_ntr_threshold(10), 99999)
        self.assertEqual(svc._compute_ntr_stage(1, 1), 5)

    def test_build_anima_workflow_uses_configurable_models(self):
        svc = self.make_service()
        svc.config["unet_model"] = "u.safetensors"
        svc.config["clip_model"] = "c.safetensors"
        svc.config["vae_model"] = "v.safetensors"
        wf = svc._build_anima_workflow("pos", "neg", 123)
        self.assertEqual(wf["68"]["inputs"]["unet_name"], "u.safetensors")
        self.assertEqual(wf["61"]["inputs"]["clip_name"], "c.safetensors")
        self.assertEqual(wf["62"]["inputs"]["vae_name"], "v.safetensors")
        self.assertEqual(wf["66"]["inputs"]["seed"], 123)

    def test_build_prompt_injects_session_style_and_appearance(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["dynamic_appearance"] = "white hair, glasses"
        state["custom_current_style"] = "@00 gx4"
        pos, neg = svc._build_prompt("sitting by window", session_id=sid)
        self.assertIn("white hair", pos)
        self.assertIn("glasses", pos)
        self.assertIn("@00 gx4", pos)
        self.assertIn("bad anatomy", neg)

    def test_chat_prompt_injects_visible_appearance_and_accessories(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "Kirito",
            "custom_positive_prefix": (
                "masterpiece, best quality, 1boy, black hair, black eyes, black coat, "
                "dual swords, fingerless gloves, black boots"
            ),
            "dynamic_appearance": "silver-rimmed glasses, shoulder-length wavy hair, white shirt dress, black belt",
        })

        context = svc._chat_visible_appearance_context(sid)
        self.assertIn("shoulder-length wavy hair", context)
        self.assertNotIn("black hair", context)
        self.assertIn("black eyes", context)
        self.assertIn("white shirt dress", context)
        self.assertIn("silver-rimmed glasses", context)
        self.assertIn("dual swords", context)
        self.assertIn("fingerless gloves", context)
        self.assertNotIn("masterpiece", context)
        self.assertNotIn("best quality", context)

        system = svc._build_chat_messages(sid, "你现在戴着什么？")[0]["content"]
        self.assertIn("当前可见外型与配饰", system)
        self.assertIn("用户问到外貌、穿搭、配饰或随身物时优先依据这里", system)
        self.assertIn("silver-rimmed glasses", system)
        self.assertIn("dual swords", system)

    def test_webui_masks_secrets(self):
        svc = self.make_service()
        svc.config["telegram_bot_token"] = "secret-token"
        cfg = masked_config(svc)
        self.assertEqual(cfg["values"]["telegram_bot_token"], "")
        self.assertTrue(cfg["secret_present"]["telegram_bot_token"])

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
        self.assertTrue(preview["catalog"]["has_catalog"])
        self.assertEqual(len(preview["timeline"]), 8)
        self.assertTrue(any(item["is_current_slot"] for item in preview["timeline"]))

    def test_llm_config_uses_specific_values_before_legacy_fallback(self):
        svc = self.make_service()
        svc.config.update({
            "llm_api_base": "https://legacy.example/v1",
            "llm_api_key": "legacy-key",
            "llm_model": "legacy-model",
            "chat_llm_api_base": "https://chat.example/v1",
            "chat_llm_api_key": "chat-key",
            "chat_llm_model": "chat-model",
            "image_llm_api_key": "",
            "image_llm_model": "",
        })
        self.assertEqual(svc._get_llm_value("chat", "api_base"), "https://chat.example/v1")
        self.assertEqual(svc._get_llm_value("chat", "api_key"), "chat-key")
        self.assertEqual(svc._get_llm_value("chat", "model"), "chat-model")
        self.assertEqual(svc._get_llm_value("image", "api_key"), "legacy-key")
        self.assertEqual(svc._get_llm_value("image", "model"), "legacy-model")
        self.assertTrue(svc.has_llm_config("chat"))
        self.assertTrue(svc.has_llm_config("image"))

    def test_long_memory_is_retrieved_and_injected_into_chat_prompt(self):
        svc = self.make_service()
        sid = "telegram:123"
        svc.memory.add_memory(
            sid,
            "preference",
            "用户喜欢黑色吊带裙和温柔安抚式回复",
            importance=5,
            tags=["穿搭", "语气"],
        )

        context = svc._long_term_memory_context(sid, "今晚穿黑色吊带裙", limit=4)
        self.assertIn("黑色吊带裙", context)

        messages = svc._build_chat_messages(sid, "今晚穿黑色吊带裙可以吗")
        self.assertIn("长期记忆", messages[0]["content"])
        self.assertIn("温柔安抚式回复", messages[0]["content"])

    def test_long_memory_extraction_writes_structured_memory(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "memories": [{
                    "kind": "preference",
                    "summary": "用户喜欢角色用更温柔的语气回应",
                    "importance": 4,
                    "tags": ["语气"],
                }]
            }, ensure_ascii=False))

            await svc._extract_long_term_memories(sid, "以后温柔一点和我说话", "好，我会更温柔一点。")

            memories = svc.memory.search_memories(sid, "温柔语气", limit=5)
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["kind"], "preference")
            self.assertIn("温柔", memories[0]["summary"])

        asyncio.run(run())

    def test_long_memory_extraction_filters_structured_current_state(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            state["custom_character"] = "天童爱丽丝"
            state["dynamic_appearance"] = "black camisole dress"
            state["custom_location"] = "上海"
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "memories": [
                    {"kind": "profile", "summary": "当前角色是天童爱丽丝", "importance": 5, "tags": ["当前角色"]},
                    {"kind": "visual", "summary": "角色现在穿着 black camisole dress", "importance": 4, "tags": ["当前穿搭"]},
                    {"kind": "setting", "summary": "当前地点是上海", "importance": 3, "tags": ["当前地点"]},
                    {"kind": "preference", "summary": "用户喜欢角色用更温柔的语气回应", "importance": 4, "tags": ["语气"]},
                ]
            }, ensure_ascii=False))

            await svc._extract_long_term_memories(sid, "以后温柔一点", "好，我会更温柔。")

            memories = svc.memory.list_memories(sid, limit=10)
            summaries = [m["summary"] for m in memories]
            self.assertEqual(summaries, ["用户喜欢角色用更温柔的语气回应"])
            extraction_system = svc._call_llm.await_args.args[0]
            extraction_user = svc._call_llm.await_args.args[1]
            self.assertIn("长期记忆不是第二套人设系统", extraction_system)
            self.assertIn("当前结构化状态", extraction_user)
            self.assertIn("天童爱丽丝", extraction_user)

        asyncio.run(run())

    def test_long_memory_allows_stable_visual_preference(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "memories": [{
                    "kind": "visual",
                    "summary": "用户更喜欢角色穿黑色系吊带裙拍照",
                    "importance": 4,
                    "tags": ["穿搭", "偏好"],
                }]
            }, ensure_ascii=False))

            await svc._extract_long_term_memories(sid, "以后可以多穿黑色系吊带裙", "我记住了。")

            memories = svc.memory.search_memories(sid, "黑色系吊带裙", limit=5)
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["kind"], "visual")

        asyncio.run(run())

    def test_world_runtime_infers_user_place_and_injects_chat_prompt(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now

        self.assertTrue(svc._update_user_place_from_text(sid, "我在商场等你"))
        messages = svc._build_chat_messages(sid, "晚上在哪见？")
        system = messages[0]["content"]

        self.assertIn("当前世界状态", system)
        self.assertIn("角色动线", system)
        self.assertIn("商场", system)
        self.assertIn("基础场所目录", system)

    def test_set_location_generates_city_place_catalog_when_image_llm_exists(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22", "lon": "121.47"})
            svc._resolve_city_timezone = AsyncMock(return_value=8)
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "places": {
                    "park": ["世纪公园"],
                    "mall": ["环球港"],
                    "transit": ["人民广场站"],
                }
            }, ensure_ascii=False))
            svc.send_message = AsyncMock()

            await svc.cmd_set_location(1, sid, "上海")

            catalog = svc.city_place_catalogs["上海"]["places"]
            self.assertEqual(catalog["park"], ["世纪公园"])
            self.assertEqual(catalog["mall"], ["环球港"])
            text = svc.send_message.await_args.args[1]
            self.assertIn("城市地点目录", text)
            self.assertIn("增强版", text)

        asyncio.run(run())

    def test_long_memory_is_isolated_per_character(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)

        # 扮演角色 A 时写入记忆。
        state["custom_character"] = "角色A"
        svc.memory.add_memory(sid, "preference", "用户喜欢和A聊星空", character=svc._memory_character(sid), importance=5)

        # 切换到角色 B：召回里不应出现 A 的记忆。
        state["custom_character"] = "角色B"
        ctx_b = svc._long_term_memory_context(sid, "星空")
        self.assertNotIn("星空", ctx_b)
        svc.memory.add_memory(sid, "preference", "用户喜欢和B聊机甲", character=svc._memory_character(sid), importance=5)
        self.assertIn("机甲", svc._long_term_memory_context(sid, "机甲"))

        # 切回角色 A：A 的记忆复原，且看不到 B 的。
        state["custom_character"] = "角色A"
        ctx_a = svc._long_term_memory_context(sid, "星空 机甲")
        self.assertIn("星空", ctx_a)
        self.assertNotIn("机甲", ctx_a)

    def test_remember_command_scopes_to_current_character(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            svc._get_session_state(sid)["custom_character"] = "角色A"
            await svc.cmd_remember(1, sid, "我叫小明")
            # 默认人设（空角色键）看不到角色A的记忆。
            svc._get_session_state(sid)["custom_character"] = ""
            self.assertEqual(svc.memory.count_active(sid, character=""), 0)
            self.assertEqual(svc.memory.count_active(sid, character="角色A"), 1)

        asyncio.run(run())

    def test_short_context_reset_filters_old_chat_from_chat_prompt(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state["chat_history"] = [
            {"role": "user", "content": "上一幕还在卧室窗边等我"},
            {"role": "assistant", "content": "我靠在卧室窗边看着你。"},
        ]
        self.assertTrue(svc._short_context_reset_reason("换个话题，聊晚饭吧", time.time()))
        svc._reset_short_context(state, "用户显式切换或结束上一话题/场景")
        state["chat_history"].extend([
            {"role": "user", "content": "聊聊晚饭吃什么"},
            {"role": "assistant", "content": "今晚可以做点清淡的。"},
        ])

        messages = svc._build_chat_messages(sid, "继续说晚饭")
        packed = "\n".join(m.get("content", "") for m in messages)

        self.assertIn("短期注意规则", messages[0]["content"])
        self.assertIn("聊聊晚饭吃什么", packed)
        self.assertNotIn("卧室窗边", packed)

    def test_short_context_reset_filters_old_chat_and_photos_from_image_context(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        now = time.time()
        state["chat_history"] = [
            {"role": "user", "content": "上一幕在办公室加班"},
            {"role": "assistant", "content": "我坐在办公室灯下。"},
        ]
        state["sent_photos_history"] = [{
            "timestamp": now - 20,
            "scene": "办公室灯下自拍",
            "caption": "",
            "appearance": "",
            "view": "selfie",
        }]
        svc._reset_short_context(state, "用户显式切换或结束上一话题/场景")
        reset_time = state["short_context_reset_time"]
        state["chat_history"].extend([
            {"role": "user", "content": "新场景在厨房准备晚饭"},
            {"role": "assistant", "content": "我把锅放到炉灶上。"},
        ])
        state["sent_photos_history"].append({
            "timestamp": reset_time + 1,
            "scene": "厨房里准备晚饭",
            "caption": "",
            "appearance": "",
            "view": "third",
        })

        dialog = format_dialog_context(svc, state, sid)
        photos = format_sent_photo_context(svc, state, sid)

        self.assertIn("厨房准备晚饭", dialog)
        self.assertNotIn("办公室加班", dialog)
        self.assertIn("厨房里准备晚饭", photos)
        self.assertNotIn("办公室灯下自拍", photos)

    def test_translate_to_tags_uses_anima_mixed_prompt_with_fixed_view(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            svc._call_llm = AsyncMock(return_value=(
                "She sits close on the edge of the bed with a teasing smile. "
                "black camisole dress, warm bedside lighting, intimate atmosphere"
            ))

            result = await svc._translate_to_tags("坐在床边，带着挑逗的笑", session_id="telegram:123", view="pov")

            system_prompt = svc._call_llm.await_args.args[0]
            self.assertIn("英文自然语言画面描述", system_prompt)
            self.assertIn("少量 danbooru 补强标签", system_prompt)
            self.assertIn("不要压缩成纯标签列表", system_prompt)
            self.assertTrue(result.startswith("First-person POV, looking at a woman"))
            self.assertIn("She sits close on the edge of the bed", result)
            self.assertIn("black camisole dress", result)

        asyncio.run(run())

    def test_selfie_prompt_strips_phone_instead_of_forcing_mirror(self):
        svc = self.make_service()
        pos, neg = svc._build_prompt(
            "A front-camera selfie of a woman, solo, holding a smartphone in the bedroom, warm bedside lighting",
            session_id="telegram:123",
        )

        self.assertIn("front-camera selfie", pos)
        self.assertNotIn("smartphone", pos.lower())
        self.assertNotIn("mirror reflection", pos.lower())
        self.assertIn("smartphone", neg.lower())
        self.assertIn("mirror selfie", neg.lower())

    def test_prompt_rewrites_user_subject_and_removes_phone_clause(self):
        svc = self.make_service()
        pos, neg = svc._build_prompt(
            "First-person POV, looking at a woman, You lounge comfortably on the living room sofa "
            "wearing only my white shirt. One hand idly twirls your hair while the other holds a phone. "
            "Warm evening light through the window",
            session_id="telegram:123",
        )

        self.assertIn("The character lounges", pos)
        self.assertIn("the character's hair", pos)
        self.assertNotIn("You lounge", pos)
        self.assertNotIn("holds a", pos)
        self.assertNotIn("phone", pos.lower())
        self.assertIn("phone", neg.lower())

    def test_prompt_uses_character_name_only_with_series(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state["custom_character"] = "\u857e\u4f0a"
        state["custom_series"] = ""

        pos, _ = svc._build_prompt("Rey leans against the office doorframe with a lazy smile", session_id=sid)

        self.assertNotIn("Rey", pos)
        self.assertNotIn("\u857e\u4f0a", pos)
        self.assertIn("the character leans", pos)

        state["custom_character"] = "Yukikaze"
        state["custom_series"] = "Azur Lane"
        pos, _ = svc._build_prompt("Yukikaze smiles beside the window", session_id=sid)

        self.assertIn("Yukikaze", pos)
        self.assertIn("Azur Lane", pos)

    def test_image_planner_normalizes_second_person_scene_subject(self):
        scene = normalize_scene_visual_subject("\u4f60\u8212\u8212\u670d\u670d\u5730\u7a9d\u5728\u5ba2\u5385\u6c99\u53d1\u91cc")

        self.assertTrue(scene.startswith("\u89d2\u8272"))

    def test_cmd_selfie_runs_scene_translate_generate_chain(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._llm_write_scene = AsyncMock(return_value=("坐在窗边看向镜头", "给你看一眼。", "", "selfie"))
            svc._translate_to_tags = AsyncMock(return_value="english prompt")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            await svc.cmd_selfie(123, sid, "")

            svc._llm_write_scene.assert_awaited_once()
            svc._translate_to_tags.assert_awaited_once_with("坐在窗边看向镜头", session_id=sid, view="selfie")
            svc._do_generate.assert_awaited_once_with("english prompt", session_id=sid)
            svc.send_photo.assert_awaited_once_with(123, b"image", "给你看一眼。")

        asyncio.run(run())

    def test_mirror_prompt_allows_one_phone_and_blocks_extra_hands(self):
        svc = self.make_service()
        svc.config["negative_prompt"] += ", phone, smartphone, holding phone"
        pos, neg = svc._build_prompt(
            "A mirror reflection of a woman, solo, single reflected body, only mirror reflection is visible, "
            "holding one smartphone with one hand, looking at viewer through the mirror, black dress",
            session_id="telegram:123",
        )

        self.assertIn("mirror reflection", pos.lower())
        self.assertIn("smartphone", pos.lower())
        neg_tokens = {item.strip().lower() for item in neg.split(",")}
        self.assertNotIn("phone", neg_tokens)
        self.assertNotIn("smartphone", neg_tokens)
        self.assertNotIn("holding phone", neg_tokens)
        self.assertIn("two phones", neg_tokens)
        self.assertIn("three hands", neg.lower())
        self.assertIn("duplicate hands", neg.lower())
        self.assertIn("foreground person", neg.lower())

    def test_roleplay_image_tool_uses_image_planner_context(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            state["chat_history"] = [
                {"role": "user", "content": "今天下班好累，想看你在家等我的样子"},
                {"role": "assistant", "content": "那我就在客厅等你回来。"},
            ]
            state["recent_message_history"] = [{"text": "你穿那件黑色吊带裙吧", "time": 9999999999}]
            state["sent_photos_history"] = [{
                "timestamp": 9999999900,
                "scene": "坐在床边，穿白衬衫",
                "caption": "等你回来",
                "appearance": "",
                "view": "selfie",
            }]

            svc._fetch_weather = AsyncMock(return_value={"desc": "小雨", "temp": "18"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "穿黑色吊带裙坐在客厅沙发上等用户回家",
                "caption": "快回来，我给你留了灯。",
                "view": "selfie",
                "new_appearance_tags": "",
            }, ensure_ascii=False))
            svc._translate_to_tags = AsyncMock(return_value="english tags")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            result = await svc.tool_generate_image(
                123,
                sid,
                intent="用户想看角色下班后在家等自己的样子",
                mood="安慰、暧昧",
                must_include="黑色吊带裙",
            )
            self.assertIn("图片已生成并发送", result)
            planner_user_prompt = svc._call_llm.await_args.args[1]
            self.assertIn("黑色吊带裙", planner_user_prompt)
            self.assertIn("今天下班好累", planner_user_prompt)
            self.assertIn("坐在床边，穿白衬衫", planner_user_prompt)
            planner_system_prompt = svc._call_llm.await_args.args[0]
            self.assertIn("自拍物理规则", planner_system_prompt)
            self.assertIn("当前世界状态", planner_system_prompt)
            self.assertIn("图片规划时优先遵守当前世界状态", planner_system_prompt)
            svc._translate_to_tags.assert_awaited_once_with(
                "穿黑色吊带裙坐在客厅沙发上等用户回家",
                session_id=sid,
                view="selfie",
            )
            svc.send_photo.assert_awaited_once_with(123, b"image", "快回来，我给你留了灯。")
            self.assertEqual(state["sent_photos_history"][-1]["caption"], "快回来，我给你留了灯。")
            self.assertIn("用户想看角色下班后在家等自己的样子", state["sent_photos_history"][-1]["source_description"])
            self.assertIn("黑色吊带裙", state["sent_photos_history"][-1]["source_description"])

        asyncio.run(run())

    def test_roleplay_image_planner_coerces_mirror_scene_to_mirror_view(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "站在浴室镜子前对镜自拍",
                "caption": "给你看一下今天的样子。",
                "view": "selfie",
                "new_appearance_tags": "",
            }, ensure_ascii=False))
            svc._translate_to_tags = AsyncMock(return_value="english tags")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            await svc.tool_generate_image(123, sid, intent="想看对镜自拍")

            svc._translate_to_tags.assert_awaited_once_with("站在浴室镜子前对镜自拍", session_id=sid, view="mirror")

        asyncio.run(run())

    def test_photo_memory_injects_source_description_instead_of_caption(self):
        svc = self.make_service()
        state = svc._get_session_state("telegram:123")
        state["sent_photos_history"] = [{
            "timestamp": 9999999999,
            "scene": "站在玄关等用户回家",
            "caption": "快回来，我给你留了灯。",
            "appearance": "black dress",
            "view": "selfie",
            "source_description": "意图: 用户想看角色下班后在家等自己的样子；必须包含: 玄关灯",
        }]
        messages = [{"role": "system", "content": "persona"}]

        svc._inject_photo_history_messages(messages, state)

        injected = messages[-1]["content"]
        self.assertIn("站在玄关等用户回家", injected)
        self.assertIn("用户想看角色下班后在家等自己的样子", injected)
        self.assertNotIn("快回来，我给你留了灯。", injected)


    def test_user_log_writes_per_chat_file(self):
        svc = self.make_service()
        sid = "telegram:12345"
        svc._ulog(sid, "USER", "你在干嘛")
        svc._ulog(sid, "BOT", "我在等你\n第二行")  # 多行折叠成单行
        # 另一个用户写入独立文件
        svc._ulog("telegram:999", "USER", "hi")

        p = svc._user_log_path(sid)
        self.assertEqual(p.name, "telegram_12345.log")
        text = p.read_text(encoding="utf-8")
        self.assertIn("USER 你在干嘛", text)
        self.assertIn("BOT 我在等你 ⏎ 第二行", text)
        self.assertNotIn("hi", text)  # 别的用户不混进来
        self.assertTrue(svc._user_log_path("telegram:999").read_text(encoding="utf-8").strip().endswith("hi"))

    def test_generation_logs_final_prompt(self):
        class FakeComfyResponse:
            def __init__(self, payload=None, data=b"image", status=200):
                self.payload = payload or {}
                self.data = data
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self):
                return self.payload

            async def read(self):
                return self.data

        class FakeComfySession:
            closed = False

            def post(self, url, json):
                self.submitted = json
                return FakeComfyResponse({"prompt_id": "prompt-1"})

            def get(self, url, params=None):
                if "/history/" in url:
                    return FakeComfyResponse({
                        "prompt-1": {
                            "outputs": {"46": {"images": [{"filename": "out.png"}]}}
                        }
                    })
                return FakeComfyResponse(data=b"image-bytes")

        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc.comfy_session = FakeComfySession()

            with (
                patch("telegram_comfyui_selfie.generation.asyncio.sleep", new=AsyncMock()),
                patch("telegram_comfyui_selfie.generation.random.randint", return_value=123),
            ):
                ok, imgs, err = await svc._do_generate_locked("standing by window", session_id=sid)

            self.assertTrue(ok, err)
            self.assertEqual(imgs, [b"image-bytes"])
            text = svc._user_log_path(sid).read_text(encoding="utf-8")
            self.assertIn("PROMPT", text)
            self.assertIn("seed=123", text)
            self.assertIn("positive=", text)
            self.assertIn("negative=", text)
            self.assertIn("standing by window", text)

        asyncio.run(run())

    def test_user_log_can_be_disabled(self):
        svc = self.make_service()
        svc.config["user_log_enabled"] = False
        sid = "telegram:1"
        svc._ulog(sid, "USER", "不该写")
        self.assertFalse(svc._user_log_path(sid).exists())

    def test_character_reset_clears_character_and_restores_global_default(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "天童爱丽丝",
                "custom_series": "蔚蓝档案",
                "custom_scheduled_persona": "你是天童爱丽丝。",
                "custom_positive_prefix": "1girl, alice, blue eyes",
                "custom_role_name": "学生",
                "custom_current_style": "@00 gx4",
                "dynamic_appearance": "white dress",
                "persona_user_set": True,
            })
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "reset")

            after = svc._get_session_state(sid)
            self.assertFalse(svc._is_character_set(sid))
            self.assertEqual(after.get("custom_character"), "")
            self.assertFalse(after.get("persona_user_set"))
            self.assertEqual(after.get("dynamic_appearance"), "")
            persona = svc._get_effective_persona(sid)
            self.assertTrue(persona)
            self.assertEqual(persona, svc.config["scheduled_persona"])
            pos, _ = svc._build_prompt("standing", session_id=sid)
            # 回退到全局默认 positive_prefix，角色专属标签消失。
            self.assertIn("black long flowing hair", pos.lower())
            self.assertNotIn("alice", pos.lower())
            self.assertNotIn("天童爱丽丝", pos)

        asyncio.run(run())

    def test_character_reset_clears_conversation_and_character_pool(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "天童爱丽丝",
                "custom_scheduled_persona": "你是天童爱丽丝。",
                "persona_user_set": True,
                "saved_characters": {"爱丽丝": {"character": "天童爱丽丝"}},
                "chat_history": [
                    {"role": "user", "content": "爱丽丝在做什么"},
                    {"role": "assistant", "content": "Sensei！爱丽丝正在打游戏！"},
                ],
                "sent_photos_history": [{"timestamp": 9999999999, "scene": "爱丽丝拿着游戏机", "caption": "", "view": "selfie"}],
            })
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "reset")

            after = svc._get_session_state(sid)
            self.assertEqual(after.get("saved_characters"), {})
            self.assertEqual(after.get("chat_history"), [])
            self.assertEqual(after.get("sent_photos_history"), [])
            # 旧角色的历史发言不再回流进新对话提示词，系统提示用默认人设。
            messages = svc._build_chat_messages(sid, "你好")
            packed = "\n".join(m.get("content", "") for m in messages)
            self.assertNotIn("爱丽丝正在打游戏", packed)
            self.assertNotIn("爱丽丝拿着游戏机", packed)
            self.assertIn(svc.config["scheduled_persona"], messages[0]["content"])

        asyncio.run(run())

    def test_switching_character_clears_conversation_context(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "角色A",
                "saved_characters": {
                    "角色A": {"character": "角色A", "persona": "我是A"},
                    "角色B": {"character": "角色B", "persona": "我是B"},
                },
                "chat_history": [{"role": "assistant", "content": "A的专属台词"}],
                "sent_photos_history": [{"timestamp": 9999999999, "scene": "A的画面", "view": "selfie"}],
            })
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "load 角色B")

            after = svc._get_session_state(sid)
            self.assertEqual(after["custom_character"], "角色B")
            self.assertEqual(after["chat_history"], [])
            self.assertEqual(after["sent_photos_history"], [])
            # 切换不清空角色池，B/A 都还在。
            self.assertIn("角色A", after["saved_characters"])
            self.assertIn("角色B", after["saved_characters"])

        asyncio.run(run())

    def test_character_reset_is_the_only_full_reset_entry(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({"custom_character": "X", "custom_scheduled_persona": "p", "persona_user_set": True})
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()
            await svc.cmd_character(1, sid, "reset")
            self.assertFalse(svc._is_character_set(sid))

        asyncio.run(run())

    def test_character_reset_aliases_do_not_hard_reset(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({"custom_character": "X", "custom_scheduled_persona": "p", "persona_user_set": True})
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "clear")

            self.assertTrue(svc._is_character_set(sid))
            self.assertIn("/角色 reset", svc.send_message.await_args.args[1])

        asyncio.run(run())

    def test_personalize_reset_redirects_to_character_reset_without_clearing(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "X",
                "custom_scheduled_persona": "p",
                "persona_user_set": True,
                "chat_history": [{"role": "assistant", "content": "keep me"}],
            })
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()

            await svc.cmd_personalize(1, sid, "reset")

            after = svc._get_session_state(sid)
            self.assertTrue(svc._is_character_set(sid))
            self.assertEqual(after["chat_history"], [{"role": "assistant", "content": "keep me"}])
            self.assertIn("/角色 reset", svc.send_message.await_args.args[1])

        asyncio.run(run())

    def test_effective_persona_never_empty_in_broken_half_state(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        # 模拟历史坏数据：角色态标记还在，但人设/身体特征已空。
        state["custom_character"] = "幽灵角色"
        state["custom_scheduled_persona"] = ""
        state["custom_positive_prefix"] = ""
        self.assertTrue(svc._get_effective_persona(sid))
        pos, _ = svc._build_prompt("standing", session_id=sid)
        # 身体特征为空时回退全局默认，绝不产出无身体特征的提示词。
        self.assertIn("black long flowing hair", pos.lower())

    def test_personalize_persona_marks_user_set(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            await svc.cmd_personalize(1, sid, "人格 温柔体贴")
            self.assertTrue(svc._is_character_set(sid))
            self.assertEqual(svc._get_effective_persona(sid).split("\n")[0], "温柔体贴")

        asyncio.run(run())


    def test_reply_length_directive_injected_into_chat_prompt(self):
        svc = self.make_service()
        sid = "telegram:1"
        # 默认不限制：系统提示里没有长度约束
        sys_default = svc._build_chat_messages(sid, "你好")[0]["content"]
        self.assertNotIn("回复长度", sys_default)
        # 设为简短后注入约束
        svc.config["chat_reply_length"] = "简短"
        sys_short = svc._build_chat_messages(sid, "你好")[0]["content"]
        self.assertIn("回复长度", sys_short)
        self.assertIn("1 到 2 句", sys_short)
        # 非法预设当作不限制
        svc.config["chat_reply_length"] = "乱填"
        self.assertEqual(svc._reply_length_directive(), "")

    def test_chat_always_uses_auto_tool_choice(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x"})
            sid = "telegram:1"
            captured = {}

            async def fake_msgs(messages, tools=None, tool_choice=None, disable_thinking=None, **kw):
                captured["tool_choice"] = tool_choice
                return {"choices": [{"message": {"content": "你好呀~"}}]}

            svc._call_llm_messages = fake_msgs
            svc._judge_image_moment = AsyncMock(return_value=None)
            svc.send_message = AsyncMock(); svc.send_action = AsyncMock()
            await svc.run_roleplay_chat(1, sid, "你好")
            self.assertEqual(captured["tool_choice"], "auto")  # 不再强制

        asyncio.run(run())

    def test_judge_triggers_image_when_content_fits(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x", "selfie_frequency": "适度"})
            sid = "telegram:1"
            svc._get_session_state(sid)["rounds_since_image"] = 3  # >= 最小间隔 2
            svc._call_llm_messages = AsyncMock(return_value={"choices": [{"message": {"content": "在家窝着呢~"}}]})
            svc._judge_image_moment = AsyncMock(return_value={"intent": "展示在家穿搭", "mood": "撩拨", "view": "selfie"})
            svc.tool_generate_image = AsyncMock(return_value="图片已生成并发送")
            svc.send_message = AsyncMock(); svc.send_action = AsyncMock()

            await svc.run_roleplay_chat(1, sid, "你在家干嘛")
            await asyncio.sleep(0.02)  # 让 create_task 跑完

            svc._judge_image_moment.assert_awaited_once()
            svc.tool_generate_image.assert_awaited_once()
            self.assertEqual(svc.tool_generate_image.await_args.kwargs["intent"], "展示在家穿搭")

        asyncio.run(run())

    def test_judge_skipped_when_model_already_imaged(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x"})
            sid = "telegram:1"
            calls = {"n": 0}

            async def fake_msgs(messages, tools=None, tool_choice=None, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"choices": [{"message": {"content": "", "tool_calls": [
                        {"id": "t1", "function": {"name": "generate_roleplay_image", "arguments": json.dumps({"intent": "看看我"})}}
                    ]}}]}
                return {"choices": [{"message": {"content": "给你看~"}}]}

            svc._call_llm_messages = fake_msgs
            svc.tool_generate_image = AsyncMock(return_value="图片已生成并发送")
            svc._judge_image_moment = AsyncMock(return_value={"intent": "x"})
            svc.send_message = AsyncMock(); svc.send_action = AsyncMock()

            await svc.run_roleplay_chat(1, sid, "给我看看你")
            await asyncio.sleep(0.02)

            svc._judge_image_moment.assert_not_awaited()      # 模型已主动配图 → 不再判断
            svc.tool_generate_image.assert_awaited_once()     # 只出一张

        asyncio.run(run())

    def test_judge_respects_min_gap_and_disabled(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x"})
            sid = "telegram:1"
            svc._call_llm = AsyncMock(return_value=json.dumps({"send": True, "intent": "x"}))
            # 刚发过图（间隔不足）→ 不判断、不调用 _call_llm
            svc._get_session_state(sid)["rounds_since_image"] = 1
            self.assertIsNone(await svc._judge_image_moment(sid, "你好", "回复"))
            svc._call_llm.assert_not_awaited()
            # 关闭频率 → 直接 None
            svc.config["selfie_frequency"] = "关闭"
            svc._get_session_state(sid)["rounds_since_image"] = 9
            self.assertIsNone(await svc._judge_image_moment(sid, "你好", "回复"))
            svc._call_llm.assert_not_awaited()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
