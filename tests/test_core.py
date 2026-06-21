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
from telegram_comfyui_selfie import appearance as appearance_rules
from telegram_comfyui_selfie.image_planning import _detect_intimate_context, format_dialog_context, format_sent_photo_context, normalize_scene_visual_subject, plan_roleplay_image
from telegram_comfyui_selfie.prompt_intake import heuristic_intake
from telegram_comfyui_selfie.webui import build_world_route_preview, cast_config_value, masked_config, serialize_prompt_slots, session_summary


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
        self.assertEqual(svc.parse_command("初始化"), ("初始化", ""))
        self.assertEqual(svc.parse_command("创建OC"), ("创建OC", ""))
        self.assertEqual(svc.parse_command("oc 名字：小雨"), ("创建OC", "名字：小雨"))
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
            self.assertIn("第一次使用", text)
            self.assertIn("/初始化", text)
            self.assertIn("/角色 <角色名>", text)
            self.assertIn("/创建OC", text)
            self.assertIn("/菜单 设置", text)
            self.assertIn("/菜单 动线", text)

        asyncio.run(run())

    def test_start_alias_dispatches_to_init_guide(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.dispatch_command(1, "telegram:1", "start", "")

            text = svc.send_message.await_args.args[1]
            self.assertIn("初始化向导", text)
            self.assertIn("/创建OC", text)

        asyncio.run(run())

    def test_create_oc_help_includes_template(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.cmd_create_oc(1, "telegram:1", "")

            text = svc.send_message.await_args.args[1]
            self.assertIn("创建原创角色 OC", text)
            self.assertIn("名字：小雨", text)
            self.assertIn("初始穿搭", text)

        asyncio.run(run())

    def test_create_oc_sets_identity_without_visual_series(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state["chat_history"] = [{"role": "user", "content": "旧角色的话题"}]
            translations = {
                "黑色短发，蓝眼睛，身材纤细，浅色皮肤": "short black hair, blue eyes, slender body, pale skin",
                "白衬衫，深色百褶裙": "white shirt, dark pleated skirt",
            }
            svc._translate_appearance_tags = AsyncMock(side_effect=lambda text: translations[text])
            svc.send_message = AsyncMock()

            await svc.cmd_create_oc(
                1,
                sid,
                "名字：小雨\n"
                "角色类型：大学生\n"
                "年龄段：adult\n"
                "职业：大学生\n"
                "性格：温柔、慢热\n"
                "外貌：黑色短发，蓝眼睛，身材纤细，浅色皮肤\n"
                "初始穿搭：白衬衫，深色百褶裙\n"
                "与你的关系：同城暧昧对象",
            )

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_character"], "小雨")
            self.assertEqual(state["custom_series"], "")
            # 人设串只存纯人格描述；身份/角色类型/关系都不再焊接，由读时组装。
            self.assertEqual(state["custom_scheduled_persona"], "温柔、慢热")
            self.assertNotIn("一名", state["custom_scheduled_persona"])
            self.assertNotIn("同城暧昧对象", state["custom_scheduled_persona"])
            self.assertIn("你是小雨", svc._get_effective_persona(sid))
            self.assertEqual(state["custom_spatial_relationship"], "同城暧昧对象")
            self.assertEqual(state["custom_count"], "1girl")
            self.assertNotIn("1girl", state["custom_positive_prefix"])
            self.assertIn("short black hair", state["custom_positive_prefix"])
            self.assertIn("short black hair", state["custom_positive_prefix"])
            self.assertEqual(state["dynamic_appearance"], "white shirt, dark pleated skirt")
            self.assertEqual(state["custom_character_age_stage"], "adult")
            self.assertEqual(state["custom_character_occupation"], "大学生")
            self.assertEqual(state["custom_character_day_anchor"], "school")
            self.assertEqual(state["chat_history"], [])
            self.assertEqual(state["saved_characters"]["小雨"]["series"], "")
            text = svc.send_message.await_args.args[1]
            self.assertIn("OC 已创建: 小雨", text)

        asyncio.run(run())

    def test_migrate_legacy_persona_strips_baked_identity_and_relationship(self):
        svc = self.make_service()
        legacy = "你是小雨，一名大学生。\n温柔、慢热\n你和用户的关系: 同城暧昧对象"
        cleaned, changed = svc._strip_legacy_persona_bakein(legacy)
        self.assertTrue(changed)
        self.assertEqual(cleaned, "温柔、慢热")
        # 幂等：剥干净后再跑不再变动
        again, changed2 = svc._strip_legacy_persona_bakein(cleaned)
        self.assertFalse(changed2)
        self.assertEqual(again, "温柔、慢热")
        # 已有角色的“你是X（作品）。”不是漂移源，不被误删
        anime = "你是天童爱丽丝（碧蓝档案）。\n开朗"
        kept, ch = svc._strip_legacy_persona_bakein(anime)
        self.assertFalse(ch)
        self.assertEqual(kept, anime.strip())
        # 会话级 + 角色快照一并迁移
        svc.sessions["telegram:9"] = {
            "custom_scheduled_persona": legacy,
            "saved_characters": {"小雨": {"persona": legacy}},
        }
        svc._migrate_legacy_personas()
        self.assertEqual(svc.sessions["telegram:9"]["custom_scheduled_persona"], "温柔、慢热")
        self.assertEqual(svc.sessions["telegram:9"]["saved_characters"]["小雨"]["persona"], "温柔、慢热")

    def test_rollback_rewinds_n_turns(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            state = svc._get_session_state(sid)
            state["chat_history"] = [
                {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "u3"}, {"role": "assistant", "content": "a3"},
            ]
            await svc.cmd_rollback(1, sid, "2")
            hist = svc._get_session_state(sid)["chat_history"]
            self.assertEqual([m["content"] for m in hist], ["u1", "a1"])
            # 默认回退 1 轮
            await svc.cmd_rollback(1, sid, "")
            hist = svc._get_session_state(sid)["chat_history"]
            self.assertEqual(hist, [])
            # 空历史给出提示，不报错
            await svc.cmd_rollback(1, sid, "")
            self.assertIn("没有可回滚", svc.send_message.await_args.args[1])

        asyncio.run(run())

    def test_weather_refresh_scheduled_only_when_stale(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            # 无缓存 → 调度刷新
            self.assertTrue(svc._schedule_weather_refresh(sid))
            # 新鲜缓存（30 分钟内）→ 不刷新
            svc._weather_caches[sid] = {"data": {}, "ts": time.time()}
            self.assertFalse(svc._schedule_weather_refresh(sid))
            # 过期缓存 → 刷新
            svc._weather_caches[sid] = {"data": {}, "ts": time.time() - 2000}
            self.assertTrue(svc._schedule_weather_refresh(sid))
            await asyncio.sleep(0)  # 让后台刷新任务启动，避免 pending task 警告

        asyncio.run(run())

    def test_set_character_does_not_inherit_default_outfit(self):
        svc = self.make_service()
        sid = "telegram:1"
        svc.config["dynamic_appearance"] = "black silk slip dress, black lace bra"  # 默认魅魔穿搭
        state = svc._get_session_state(sid)
        # 默认角色（未设角色）：用全局默认穿搭
        self.assertIn("black silk slip dress", svc._effective_dynamic_appearance(sid))
        # 设了既有角色且自己没穿搭：不回退默认穿搭（避免串到东云绘名身上）
        state["custom_character"] = "东云绘名"
        state["dynamic_appearance"] = ""
        self.assertEqual(svc._effective_dynamic_appearance(sid), "")
        self.assertNotIn("black silk slip dress", svc._get_effective_persona(sid))
        # 角色有自己的临时穿搭时照常用
        state["dynamic_appearance"] = "school uniform"
        self.assertEqual(svc._effective_dynamic_appearance(sid), "school uniform")

    def test_prompt_intake_splits_natural_oc_profile(self):
        intake = heuristic_intake("小雨，大学生，金发蓝眼，低马尾，穿宽松白毛衣，和用户是同城暧昧对象，住在上海")

        self.assertEqual(intake["name"], "小雨")
        self.assertEqual(intake["role"], "大学生")
        self.assertEqual(intake["age"], "adult")
        self.assertEqual(intake["anchor"], "school")
        self.assertIn("金发蓝眼", intake["base_appearance"])
        self.assertIn("低马尾", intake["base_appearance"])
        self.assertIn("宽松白毛衣", intake["dynamic_appearance"])
        self.assertIn("同城暧昧对象", intake["relationship"])
        self.assertEqual(intake["city"], "上海")

    def test_create_oc_accepts_natural_profile_and_saves_raw_intake(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            translations = {
                "金发蓝眼，低马尾": "blonde hair, blue eyes, low ponytail",
                "穿宽松白毛衣": "oversized white sweater",
            }
            svc._translate_appearance_tags = AsyncMock(side_effect=lambda text: translations[text])
            svc.send_message = AsyncMock()

            await svc.cmd_create_oc(
                1,
                sid,
                "小雨，大学生，金发蓝眼，低马尾，穿宽松白毛衣，和用户是同城暧昧对象",
            )

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_character"], "小雨")
            self.assertEqual(state["custom_series"], "")
            self.assertEqual(state["custom_count"], "1girl")
            self.assertNotIn("1girl", state["custom_positive_prefix"])
            self.assertIn("blonde hair", state["custom_positive_prefix"])
            self.assertEqual(state["dynamic_appearance"], "oversized white sweater")
            self.assertEqual(state["custom_raw_profile_text"], "小雨，大学生，金发蓝眼，低马尾，穿宽松白毛衣，和用户是同城暧昧对象")
            self.assertIn("base_appearance", state["custom_prompt_intake"])
            self.assertIn("自动归档", svc.send_message.await_args.args[1])

        asyncio.run(run())

    def test_appearance_natural_input_splits_stable_and_dynamic_slots(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            translations = {
                "金发蓝眼": "blonde hair, blue eyes",
                "穿白毛衣": "white sweater",
            }
            svc._translate_appearance_tags = AsyncMock(side_effect=lambda text: translations[text])
            svc.send_message = AsyncMock()

            await svc.cmd_appearance(1, sid, "金发蓝眼，穿白毛衣")

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_count"], "1girl")
            self.assertNotIn("1girl", state["custom_positive_prefix"])
            self.assertIn("blonde hair", state["custom_positive_prefix"])
            self.assertEqual(state["dynamic_appearance"], "white sweater")
            text = svc.send_message.await_args.args[1]
            self.assertIn("已按槽位自动归档", text)
            self.assertIn("基础外观", text)
            self.assertIn("穿搭/配饰", text)

        asyncio.run(run())

    def test_appearance_english_tags_keep_legacy_dynamic_behavior(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()

            await svc.cmd_appearance(1, sid, "white hair, glasses")

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_positive_prefix"], "")
            self.assertIn("white hair", state["dynamic_appearance"])
            self.assertIn("glasses", state["dynamic_appearance"])

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

    def test_record_sent_photo_uses_effective_visual_appearance_when_empty(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_default_hair"] = "silver_hair,bun"

        svc._record_sent_photo(sid, "standing by a window", appearance="")

        appearance = svc._get_session_state(sid)["sent_photos_history"][-1]["appearance"].lower()
        self.assertIn("silver hair", appearance)
        self.assertIn("hair bun", appearance)
        self.assertNotIn("silver_hair", appearance)

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
        self.assertEqual(len(preview["timeline"]), 8)
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
        self.assertIn("角色当前所在", system)
        self.assertIn("接下来动线", system)
        self.assertIn("商场", system)
        self.assertIn("基础场所目录", system)

    def test_world_context_unpins_clock_location_during_active_dialog(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now
        state = svc._get_session_state(sid)
        # 对话进行中（有活跃聊天历史）：不钉死时钟算出的具体地点/相对关系，避免瞬移
        state["chat_history"] = [
            {"role": "user", "content": "在家吗"},
            {"role": "assistant", "content": "在家呢，刚到客厅"},
        ]
        state["short_context_start"] = 0
        system = svc._build_chat_messages(sid, "那我现在过去")[0]["content"]
        self.assertIn("当前世界状态", system)
        self.assertNotIn("角色当前所在", system)
        self.assertNotIn("空间关系判断", system)
        self.assertIn("以对话为准", system)
        # 冷启动（无活跃历史）仍然钉时钟地点，供模型自然提及
        state["chat_history"] = []
        cold = svc._build_chat_messages(sid, "你好")[0]["content"]
        self.assertIn("角色当前所在", cold)

    def test_character_place_autoextract_overrides_clock(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)  # 工作日办公时段
        svc._session_now = lambda session_id="": fixed_now
        state = svc._get_session_state(sid)
        state["custom_character_age_stage"] = "adult"
        state["custom_character_day_anchor"] = "company"
        # 时钟此刻判公司
        self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "company")
        # 角色回复说在家 → 自动抽取并持久化 → 压过时钟
        self.assertTrue(svc._update_character_place_from_text(sid, "我在家呢，刚到客厅"))
        self.assertEqual(state["character_place"], "home")
        self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "home")

    def test_tool_update_location_sets_and_pins_character_place(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            msg = await svc.tool_update_location(sid, "楼下的咖啡店")
            self.assertIn("咖啡", msg)
            state = svc._get_session_state(sid)
            self.assertEqual(state["character_place"], "cafe")
            self.assertEqual(state["character_place_confidence"], 0.95)
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "cafe")

        asyncio.run(run())

    def test_character_place_expires_after_ttl(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now
        state = svc._get_session_state(sid)
        state["custom_character_age_stage"] = "adult"
        state["custom_character_day_anchor"] = "company"
        svc._update_character_place_from_text(sid, "我在家")
        self.assertEqual(state["character_place"], "home")
        state["character_place_updated_at"] = 1.0  # 远早于 TTL → 过期
        self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "company")

    def test_life_profile_gates_anchor_places_by_identity(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)  # 周四工作日，办公时段
        svc._session_now = lambda session_id="": fixed_now
        state = svc._get_session_state(sid)

        # 成年上班族：当前应在公司，候选里绝不出现学校
        state["custom_character_age_stage"] = "adult"
        state["custom_character_day_anchor"] = "company"
        world = svc.build_world_state(sid, weather=None)
        keys = [c["key"] for c in world["character_candidates"]]
        self.assertEqual(world["character_place"]["key"], "company")
        self.assertNotIn("school", keys)

        # 在校学生：当前应在学校，候选里绝不出现公司
        state["custom_character_age_stage"] = "minor"
        state["custom_character_day_anchor"] = "school"
        world = svc.build_world_state(sid, weather=None)
        keys = [c["key"] for c in world["character_candidates"]]
        self.assertEqual(world["character_place"]["key"], "school")
        self.assertNotIn("company", keys)

        # 无固定职场（主妇/自由职业/非人类设定）：公司和学校都不出现
        state["custom_character_age_stage"] = "adult"
        state["custom_character_day_anchor"] = "home"
        world = svc.build_world_state(sid, weather=None)
        keys = [c["key"] for c in world["character_candidates"]]
        self.assertNotIn("company", keys)
        self.assertNotIn("school", keys)

        # 工厂工人：当前在工厂，公司/学校不出现
        state["custom_character_day_anchor"] = "工人"  # 中文别名归一到 factory
        world = svc.build_world_state(sid, weather=None)
        keys = [c["key"] for c in world["character_candidates"]]
        self.assertEqual(world["character_place"]["key"], "factory")
        self.assertNotIn("company", keys)
        self.assertNotIn("school", keys)

        # 外卖员：流动型，当前在街道/车站这类公共场所，绝不在公司/学校/工厂
        state["custom_character_day_anchor"] = "外卖员"
        world = svc.build_world_state(sid, weather=None)
        keys = [c["key"] for c in world["character_candidates"]]
        self.assertIn(world["character_place"]["key"], {"street", "transit"})
        for gated in ("company", "school", "factory"):
            self.assertNotIn(gated, keys)

    def test_ensure_life_profile_infers_and_caches_from_persona(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._call_llm = AsyncMock(return_value=json.dumps(
                {"day_anchor": "company", "age_stage": "adult"}, ensure_ascii=False))

            profile = await svc._ensure_life_profile(sid)
            self.assertEqual(profile["day_anchor"], "company")
            self.assertEqual(profile["age_stage"], "adult")
            self.assertEqual(svc._call_llm.await_count, 1)

            # 人设未变：命中缓存，不再调用 LLM
            await svc._ensure_life_profile(sid)
            self.assertEqual(svc._call_llm.await_count, 1)
        asyncio.run(run())

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

    def test_time_context_uses_seasonal_sunrise_and_sunset(self):
        svc = self.make_service()
        fixed_now = datetime(2026, 6, 19, 19, 10, tzinfo=timezone.utc)
        ctx = svc._get_time_context(
            "telegram:123",
            now=fixed_now,
            weather={"sunrise": "04:45 AM", "sunset": "07:16 PM", "lat": "34.69"},
        )

        self.assertEqual(ctx["season"], "夏季")
        self.assertEqual(ctx["period"], "傍晚")
        self.assertEqual(ctx["light_phase"], "黄昏/落日")
        text = svc._format_time_context("telegram:123", now=fixed_now, weather={"sunrise": "04:45 AM", "sunset": "07:16 PM", "lat": "34.69"})
        self.assertIn("夏季", text)
        self.assertIn("日落 19:16", text)
        self.assertIn("落日", text)

    def test_time_context_keeps_late_afternoon_daylight_before_sunset_window(self):
        svc = self.make_service()
        fixed_now = datetime(2026, 6, 19, 17, 30, tzinfo=timezone.utc)
        ctx = svc._get_time_context(
            "telegram:123",
            now=fixed_now,
            weather={"sunrise": "04:45 AM", "sunset": "07:16 PM", "lat": "34.69"},
        )

        self.assertEqual(ctx["period"], "下午")
        self.assertEqual(ctx["light_phase"], "日间自然光")
        self.assertIn("不得写夕阳", svc._format_light_guard("telegram:123", now=fixed_now, weather={"sunrise": "04:45 AM", "sunset": "07:16 PM", "lat": "34.69"}))

    def test_scheduler_scene_ensures_life_profile_and_injects_world_context(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            state = svc._get_session_state(sid)
            state["custom_scene_preference"] = "常去咖啡店和公园"
            state["custom_selfie_preference"] = "偏好前摄自拍"

            async def ensure_profile(session_id, force=False):
                session_state = svc._get_session_state(session_id)
                session_state["life_profile"] = {
                    "age_stage": "adult",
                    "day_anchor": "company",
                    "persona_hash": "test",
                }
                return session_state["life_profile"]

            svc._ensure_life_profile = AsyncMock(side_effect=ensure_profile)
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "在办公室茶水间发来一张自拍",
                "caption": "忙里偷闲给你看一眼。",
                "view": "selfie",
            }, ensure_ascii=False))

            scene, caption, _, view = await svc._llm_write_scene(
                "normal",
                "晴 22 C",
                "星期四",
                "上午",
                None,
                sid,
                now=fixed_now,
            )

            self.assertIn("办公室", scene)
            self.assertEqual(caption, "忙里偷闲给你看一眼。")
            self.assertEqual(view, "selfie")
            svc._ensure_life_profile.assert_awaited()
            system_prompt = svc._call_llm.await_args.args[0]
            self.assertIn("当前世界状态", system_prompt)
            self.assertIn("角色身份: 成年·上班族", system_prompt)
            self.assertIn("角色当前所在: 公司", system_prompt)
            self.assertIn("接下来动线", system_prompt)
            self.assertIn("季节与自然光", system_prompt)
            self.assertIn("季节/自然光", system_prompt)
            self.assertIn("常去咖啡店和公园", system_prompt)
            self.assertIn("偏好前摄自拍", system_prompt)

        asyncio.run(run())

    def test_scheduler_scene_injects_recent_photo_continuity(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 19, 15, 3, tzinfo=timezone.utc)
            ts = fixed_now.timestamp()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            state = svc._get_session_state(sid)
            state["last_interaction"] = ts - 1800
            state["last_message_time"] = ts - 1800
            state["recent_message_history"] = [
                {"text": "切，不和姐姐扯了，晚上等着！", "time": ts - 1800},
            ]
            state["chat_history"] = [
                {"role": "user", "content": "切，不和姐姐扯了，晚上等着！"},
                {"role": "assistant", "content": "晚上七点，老地方见~"},
            ]
            state["sent_photos_history"] = [{
                "timestamp": ts - 1900,
                "scene": "神户三宫站附近的咖啡店内，午后阳光透过落地窗斜洒在木桌上。",
                "caption": "",
                "appearance": "",
                "view": "selfie",
                "source_description": "意图: 咖啡店告别，约定晚上见面",
            }]
            svc._ensure_life_profile = AsyncMock(return_value={})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "还坐在咖啡店窗边，收起冰拿铁准备去车站",
                "caption": "晚上见~",
                "view": "selfie",
            }, ensure_ascii=False))

            await svc._llm_write_scene("normal", "晴 30 C", "星期五", "下午", None, sid, now=fixed_now)

            system_prompt = svc._call_llm.await_args.args[0]
            self.assertIn("短期连续性上下文", system_prompt)
            self.assertIn("咖啡店", system_prompt)
            self.assertIn("晚上七点", system_prompt)
            self.assertIn("短期连续性优先于自动动线", system_prompt)
            self.assertIn("不要突然跳到无关场景", system_prompt)

        asyncio.run(run())

    def test_scheduled_push_logs_world_route(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })

            async def ensure_profile(session_id, force=False):
                state = svc._get_session_state(session_id)
                state["life_profile"] = {
                    "age_stage": "adult",
                    "day_anchor": "company",
                    "persona_hash": "test",
                }
                return state["life_profile"]

            logs = []
            svc._ulog = lambda session_id, kind, text: logs.append((kind, text))
            svc._ensure_life_profile = AsyncMock(side_effect=ensure_profile)
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22", "code": "113"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "在办公室茶水间发来一张自拍",
                "caption": "忙里偷闲给你看一眼。",
                "view": "selfie",
                "new_appearance_tags": "white dress",
            }, ensure_ascii=False))
            svc._translate_to_tags = AsyncMock(return_value="english prompt")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_photo = AsyncMock()
            state = svc._get_session_state(sid)
            state["dynamic_appearance"] = "black hoodie"

            await svc._sched_fire(sid, fixed_now, mode_override="normal", skip_active_check=True)

            world_logs = [text for kind, text in logs if kind == "WORLD"]
            self.assertTrue(world_logs)
            self.assertIn("profile=成年·上班族", world_logs[0])
            self.assertIn("current=公司", world_logs[0])
            svc._do_generate.assert_awaited_once_with(
                "english prompt",
                is_ntr=False,
                session_id=sid,
                one_shot_appearance="",
            )
            self.assertEqual(state["dynamic_appearance"], "black hoodie")
            svc.send_photo.assert_awaited_once()

        asyncio.run(run())

    def test_user_place_inference_ignores_character_location_mentions(self):
        svc = self.make_service()

        self.assertEqual(svc._infer_user_place("那姐姐呢～不会在咖啡厅里吧"), (None, None))
        self.assertEqual(svc._infer_user_place("我在咖啡厅里等着")[0], "cafe")
        self.assertEqual(svc._infer_user_place("唉，姐姐其实我在上大学")[0], "school")
        self.assertEqual(svc._infer_user_place("要不是有课要上我现在就过去")[0], "school")

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
        self.assertIn("off-frame front-facing phone camera", pos)
        self.assertNotIn("smartphone", pos.lower())
        self.assertNotIn("mirror reflection", pos.lower())
        neg_tokens = {item.strip().lower() for item in neg.split(",")}
        self.assertNotIn("smartphone", neg_tokens)
        self.assertNotIn("phone", neg_tokens)
        self.assertIn("visible phone", neg.lower())
        self.assertIn("phone in hand", neg.lower())
        self.assertIn("mirror selfie", neg.lower())

    def test_selfie_prompt_removes_phone_screen_ui_without_breaking_sentence(self):
        svc = self.make_service()
        pos, _ = svc._build_prompt(
            "A front-camera selfie of a woman, solo, upper body framing, looking at viewer, "
            "shot by an off-frame front-facing phone camera, no visible phone, "
            "a woman sits by the window, gazing at a phone screen with purple eyes gleaming, "
            "the phone screen lit showing a message interface countdown prompt, black dress, phone screen, countdown",
            session_id="telegram:123",
        )

        lower = pos.lower()
        self.assertIn("off-frame front-facing phone camera", lower)
        self.assertIn("no visible phone", lower)
        self.assertEqual(lower.count("off-frame front-facing phone camera"), 1)
        self.assertNotIn("phone screen", lower)
        self.assertNotIn("message interface", lower)
        self.assertNotIn("countdown", lower)
        self.assertNotIn("gazing at a with", lower)
        self.assertNotIn("the lit", lower)

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

    def test_prompt_uses_visual_identity_for_non_english_character(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state["custom_character"] = "天童爱丽丝"
        state["custom_series"] = "碧蓝档案"
        state["custom_visual_character"] = "aris (blue archive)"
        state["custom_visual_series"] = "Blue Archive"
        state["custom_positive_prefix"] = "1girl, long black hair, blue eyes"

        pos, _ = svc._build_prompt("天童爱丽丝坐在窗边看雨", session_id=sid)

        self.assertIn("aris (blue archive)", pos)
        self.assertIn("Blue Archive", pos)
        self.assertNotIn("天童爱丽丝", pos)
        self.assertNotIn("碧蓝档案", pos)

    def test_prompt_infers_visual_identity_from_existing_appearance_tag(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state["custom_character"] = "天童爱丽丝"
        state["custom_series"] = "碧蓝档案"
        state["custom_positive_prefix"] = "1girl, aris (blue archive), long black hair, blue eyes"

        pos, _ = svc._build_prompt("standing by a window", session_id=sid)

        self.assertIn("aris (blue archive)", pos)
        self.assertIn("blue archive", pos.lower())
        self.assertNotIn("天童爱丽丝", pos)
        self.assertNotIn("碧蓝档案", pos)

    def test_prompt_infers_visual_identity_from_dynamic_appearance_for_old_sessions(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state["custom_character"] = "天童爱丽丝"
        state["custom_series"] = "碧蓝档案"
        state["custom_positive_prefix"] = "1girl, long black hair, blue eyes"
        state["dynamic_appearance"] = "aris (blue archive), school uniform"

        pos, _ = svc._build_prompt("天童爱丽丝 sits by a window", session_id=sid)

        self.assertIn("aris (blue archive)", pos)
        self.assertIn("blue archive", pos.lower())
        self.assertNotIn("天童爱丽丝", pos)
        self.assertNotIn("碧蓝档案", pos)

    def test_oc_prompt_omits_name_even_with_original_series_marker(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        state["custom_character"] = "小雨"
        state["custom_series"] = "原创角色"
        state["custom_visual_character"] = "xiaoyu"
        state["custom_visual_series"] = "original character"
        state["custom_positive_prefix"] = "1girl, short black hair, blue eyes"

        pos, _ = svc._build_prompt("小雨坐在窗边看雨", session_id=sid)

        self.assertNotIn("小雨", pos)
        self.assertNotIn("原创角色", pos)
        self.assertNotIn("xiaoyu", pos)
        self.assertIn("short black hair", pos)

    def test_character_command_stores_visual_identity_tags(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._llm_classify_character = AsyncMock(return_value={
                "type": "character",
                "name": "天童爱丽丝",
                "series": "碧蓝档案",
                "prompt_name": "aris (blue archive)",
                "prompt_series": "Blue Archive",
                "persona": "你是天童爱丽丝。",
                "appearance": "1girl, aris (blue archive), long black hair, blue eyes",
                "purity": 8,
            })
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "天童爱丽丝")

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_character"], "天童爱丽丝")
            self.assertEqual(state["custom_series"], "碧蓝档案")
            self.assertEqual(state["custom_bot_name"], "天童爱丽丝")
            self.assertEqual(state["custom_visual_character"], "aris (blue archive)")
            self.assertEqual(state["custom_visual_series"], "Blue Archive")
            self.assertEqual(state["saved_characters"]["天童爱丽丝"]["visual_character"], "aris (blue archive)")
            text = svc.send_message.await_args.args[1]
            self.assertIn("生图识别", text)
            self.assertIn("aris (blue archive)", text)

        asyncio.run(run())

    def test_character_command_fills_age_occupation_anchor_relationship(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._llm_classify_character = AsyncMock(return_value={
                "type": "character",
                "name": "天童爱丽丝",
                "series": "碧蓝档案",
                "prompt_name": "aris (blue archive)",
                "prompt_series": "Blue Archive",
                "persona": "你是天童爱丽丝。",
                "appearance": "1girl, long black hair, blue eyes",
                "purity": 8,
                "age": "adult",
                "occupation": "学生",
                "anchor": "school",
                "relationship": "青梅竹马",
            })
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "天童爱丽丝")

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_character_age_stage"], "adult")
            self.assertEqual(state["custom_character_occupation"], "学生")
            self.assertEqual(state["custom_character_day_anchor"], "school")
            self.assertEqual(state["custom_spatial_relationship"], "青梅竹马")
            self.assertEqual(state["saved_characters"]["天童爱丽丝"]["occupation"], "学生")
            # 关系注入聊天系统提示
            system = svc._build_chat_messages(sid, "你好")[0]["content"]
            self.assertIn("你和用户的关系: 青梅竹马", system)
            # 没填城市时提醒补槽位
            text = svc.send_message.await_args.args[1]
            self.assertIn("还差", text)
            self.assertIn("城市", text)

        asyncio.run(run())

    def test_character_command_pins_dialog_identity_when_persona_omits_name(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "role_name": "蕾伊",
                "bot_name": "蕾伊",
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._llm_classify_character = AsyncMock(return_value={
                "type": "character",
                "name": "东云绘名",
                "series": "Project Sekai",
                "prompt_name": "Ena Shinonome",
                "prompt_series": "Project Sekai",
                "persona": "性格内向、缺乏自信，但内心渴望被认可；喜欢绘画。",
                "appearance": "1girl, brown hair, long hair, brown eyes",
                "purity": 8,
            })
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "东云绘名")

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_bot_name"], "东云绘名")
            self.assertIn("性格内向", state["custom_scheduled_persona"])
            self.assertNotIn("你是东云", state["custom_scheduled_persona"])
            system = svc._build_chat_messages(sid, "你好")[0]["content"]
            self.assertIn("你当前扮演的角色是「东云绘名」（Project Sekai）", system)
            self.assertIn("你是东云绘名（Project Sekai）。", system)
            self.assertNotIn("进行蕾伊角色扮演", system)
            self.assertNotIn("你是蕾伊", system)

        asyncio.run(run())

    def test_migrate_visual_identity_tags_updates_old_sessions_and_saved_characters(self):
        svc = self.make_service()
        state = svc._get_session_state("telegram:1")
        state.update({
            "custom_character": "天童爱丽丝",
            "custom_series": "碧蓝档案",
            "custom_positive_prefix": "1girl, long black hair, blue eyes",
            "dynamic_appearance": "aris (blue archive), school uniform",
            "saved_characters": {
                "和泉紗霧": {
                    "character": "和泉紗霧",
                    "series": "エロマンガ先生",
                    "appearance": "1girl, silver hair, blue eyes",
                },
                "淡雪": {
                    "character": "淡雪",
                    "series": "原创",
                    "appearance": "1girl, white hair",
                    "visual_character": "awayuki",
                    "visual_series": "original character",
                },
                "Jeanne": {
                    "character": "Jeanne d'Arc",
                    "series": "Fate/Grand Order",
                    "appearance": "1girl, blonde hair, blue eyes",
                },
            },
        })

        result = svc.migrate_visual_identity_tags(create_backup=False)

        self.assertEqual(result["sessions_updated"], 1)
        self.assertEqual(result["saved_characters_updated"], 3)
        self.assertEqual(state["custom_visual_character"], "aris (blue archive)")
        self.assertEqual(state["custom_visual_series"], "Blue Archive")
        self.assertEqual(state["saved_characters"]["和泉紗霧"]["visual_character"], "izumi sagiri")
        self.assertEqual(state["saved_characters"]["和泉紗霧"]["visual_series"], "Eromanga Sensei")
        self.assertEqual(state["saved_characters"]["淡雪"]["visual_character"], "")
        self.assertEqual(state["saved_characters"]["淡雪"]["visual_series"], "")
        self.assertEqual(state["saved_characters"]["Jeanne"]["visual_character"], "jeanne d'arc (fate)")

    def test_cleanup_prompt_prefix_preview_does_not_mutate(self):
        svc = self.make_service()
        svc.config["positive_prefix"] = "masterpiece, best quality, artist:wlop, 1girl, black hair, purple eyes"
        svc.config["current_style"] = "@00 gx4"
        state = svc._get_session_state("telegram:1")
        state.update({
            "custom_character": "测试角色",
            "custom_positive_prefix": "score_9, @foo, 1boy, short hair, blue eyes",
            "saved_characters": {
                "A": {
                    "character": "A",
                    "appearance": "absurdres, artist:bar, 1girl, blonde hair",
                },
            },
        })

        result = svc.cleanup_prompt_prefix_slots(apply=False)

        self.assertFalse(result["applied"])
        self.assertEqual(len(result["changes"]), 3)
        self.assertEqual(svc.config["positive_prefix"], "masterpiece, best quality, artist:wlop, 1girl, black hair, purple eyes")
        self.assertEqual(state["custom_positive_prefix"], "score_9, @foo, 1boy, short hair, blue eyes")
        self.assertEqual(state["saved_characters"]["A"]["appearance"], "absurdres, artist:bar, 1girl, blonde hair")
        self.assertNotIn("1boy", result["changes"][1]["after"])
        self.assertIn("1boy", result["changes"][1]["removed_count"])
        self.assertIn("@foo", result["changes"][1]["style_after"])

    def test_cleanup_prompt_prefix_apply_backs_up_and_moves_style(self):
        svc = self.make_service()
        sid = "telegram:1"
        svc.config["positive_prefix"] = "masterpiece, best quality, artist:wlop, 1girl, black hair, purple eyes"
        svc.config["current_style"] = "@00 gx4"
        svc.config["style_pool"] = "@00 gx4"
        svc.save_config()
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "测试角色",
            "custom_positive_prefix": "score_9, @foo, 1boy, short hair, blue eyes",
            "saved_characters": {
                "A": {
                    "character": "A",
                    "appearance": "absurdres, artist:bar, 1girl, blonde hair",
                },
            },
        })
        svc._write_state()

        result = svc.cleanup_prompt_prefix_slots(apply=True)

        self.assertTrue(result["applied"])
        self.assertEqual(result["config_updated"], 1)
        self.assertEqual(result["sessions_updated"], 1)
        self.assertEqual(result["saved_characters_updated"], 1)
        self.assertEqual(result["count_migrated"], 2)
        self.assertEqual(svc.config["positive_prefix"], "black hair, purple eyes")
        self.assertEqual(svc.config["current_style"], "@00 gx4, artist:wlop")
        self.assertIn("@00 gx4, artist:wlop", svc.config["style_pool"])
        self.assertEqual(state["custom_count"], "1boy")
        self.assertEqual(state["custom_positive_prefix"], "short hair, blue eyes")
        self.assertEqual(state["custom_current_style"], "@00 gx4, @foo")
        self.assertEqual(state["saved_characters"]["A"]["appearance"], "blonde hair")
        self.assertEqual(state["saved_characters"]["A"]["style"], "@00 gx4, artist:bar")
        self.assertEqual(state["saved_characters"]["A"]["count"], "1girl")
        self.assertEqual(len(result["backup_paths"]), 2)
        for path in result["backup_paths"]:
            self.assertTrue(Path(path).exists())
        saved_state = json.loads(svc.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_state["sessions"][sid]["custom_count"], "1boy")
        self.assertEqual(saved_state["sessions"][sid]["custom_positive_prefix"], "short hair, blue eyes")

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
            svc._do_generate.assert_awaited_once_with("english prompt", session_id=sid, one_shot_appearance="")
            svc.send_photo.assert_awaited_once_with(123, b"image", "给你看一眼。")

        asyncio.run(run())

    def test_cmd_selfie_uses_planned_appearance_once_without_persisting(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            state["dynamic_appearance"] = "black hoodie"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._llm_write_scene = AsyncMock(return_value=("坐在窗边看向镜头", "给你看一眼。", "white dress", "selfie"))
            svc._translate_to_tags = AsyncMock(return_value="english prompt")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            await svc.cmd_selfie(123, sid, "")

            svc._do_generate.assert_awaited_once_with(
                "english prompt",
                session_id=sid,
                one_shot_appearance="white dress",
            )
            self.assertEqual(state["dynamic_appearance"], "black hoodie")
            self.assertEqual(state["sent_photos_history"][-1]["appearance"], "white dress")

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
            state["custom_scene_preference"] = "常去咖啡店和家中客厅"
            state["custom_selfie_preference"] = "偏好半身前摄自拍"
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
                "new_appearance_tags": "black camisole dress",
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
            self.assertIn("用户位置/空间关系判断", planner_system_prompt)
            # 有活跃对话时，动线只作背景，对话已确立的地点优先（防止配图把角色按现实时段“传送”）。
            self.assertIn("对话场景优先级最高", planner_system_prompt)
            self.assertNotIn("应当遵守，角色不要无理由瞬移", planner_system_prompt)
            self.assertIn("季节与自然光", planner_system_prompt)
            self.assertIn("常去咖啡店和家中客厅", planner_system_prompt)
            self.assertIn("偏好半身前摄自拍", planner_system_prompt)
            svc._translate_to_tags.assert_awaited_once_with(
                "穿黑色吊带裙坐在客厅沙发上等用户回家",
                session_id=sid,
                view="selfie",
                is_intimate=False,
            )
            svc._do_generate.assert_awaited_once_with(
                "english tags",
                session_id=sid,
                one_shot_appearance="black camisole dress",
                is_intimate=False,
                partner_in_frame=False,
                device_in_frame=False,
            )
            # 聊天途中的配图不带配文（聊天模型已经在文字里回复了）
            svc.send_photo.assert_awaited_once_with(123, b"image", "")
            self.assertEqual(state.get("dynamic_appearance", ""), "")
            self.assertEqual(state["sent_photos_history"][-1]["caption"], "")
            self.assertEqual(state["sent_photos_history"][-1]["appearance"], "black camisole dress")
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

            svc._translate_to_tags.assert_awaited_once_with("站在浴室镜子前对镜自拍", session_id=sid, view="mirror", is_intimate=False)

        asyncio.run(run())

    def test_roleplay_image_planner_device_hint_preserves_device_view(self):
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
                "scene": "卧室里对镜自拍，角色靠在床边，画面边缘只有伴侣的手臂",
                "view": "mirror",
                "new_appearance_tags": "",
                "user_location": "with_user",
                "co_located": True,
                "is_intimate": True,
                "partner_in_frame": True,
                "device_in_frame": False,
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(svc, sid, intent="想在做爱时录像留念")

            self.assertEqual(plan["view"], "mirror")
            self.assertTrue(plan["is_intimate"])
            self.assertTrue(plan["partner_in_frame"])
            self.assertTrue(plan["device_in_frame"])

        asyncio.run(run())

    def test_roleplay_image_planner_intimate_without_device_forces_pov(self):
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
                "scene": "卧室床边贴身依偎，角色靠近镜头，画面边缘只有伴侣的手臂",
                "view": "selfie",
                "new_appearance_tags": "",
                "user_location": "with_user",
                "co_located": True,
                "is_intimate": True,
                "partner_in_frame": True,
                "device_in_frame": False,
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(svc, sid, intent="想看事后依偎的画面")

            self.assertEqual(plan["view"], "pov")
            self.assertTrue(plan["is_intimate"])
            self.assertTrue(plan["partner_in_frame"])
            self.assertFalse(plan["device_in_frame"])

        asyncio.run(run())

    def test_image_planner_locks_location_to_persisted_place(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._set_character_place(sid, "home", "在家", 0.8)  # 新鲜的对话确立位置
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "坐在客厅沙发", "view": "selfie", "character_location": "mall",
            }, ensure_ascii=False))
            await plan_roleplay_image(svc, sid, intent="看看你在干嘛")
            system = svc._call_llm.await_args.args[0]
            self.assertIn("地点锁定", system)
            self.assertIn("家", system)
            # 已钉死：规划器乱选的 mall 不回写，仍是 home
            self.assertEqual(svc._get_session_state(sid)["character_place"], "home")

        asyncio.run(run())

    def test_image_planner_writes_back_location_when_unpinned(self):
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
                "scene": "在咖啡店窗边", "view": "selfie", "character_location": "cafe",
            }, ensure_ascii=False))
            await plan_roleplay_image(svc, sid, intent="看看你在干嘛")
            system = svc._call_llm.await_args.args[0]
            self.assertNotIn("地点锁定", system)  # 无持久位置，不钉
            self.assertEqual(svc._get_session_state(sid)["character_place"], "cafe")  # 规划器判断回写

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
        self.assertIn("快回来，我给你留了灯。", injected)
        self.assertIn("用户想看角色下班后在家等自己的样子", injected)


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
                ok, imgs, err = await svc._do_generate_locked(
                    "standing by window",
                    session_id=sid,
                    one_shot_appearance="white dress",
                )

            self.assertTrue(ok, err)
            self.assertEqual(imgs, [b"image-bytes"])
            text = svc._user_log_path(sid).read_text(encoding="utf-8")
            self.assertIn("PROMPT", text)
            self.assertIn("PROMPT_SLOTS", text)
            self.assertIn("seed=123", text)
            self.assertIn("quality=", text)
            self.assertIn("base_appearance=", text)
            self.assertIn("scene=standing by window", text)
            self.assertIn("one_shot_appearance=white dress", text)
            self.assertIn("positive=", text)
            self.assertIn("negative=", text)
            self.assertIn("standing by window", text)

        asyncio.run(run())

    def test_show_prompt_includes_prompt_slots(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.cmd_show_prompt(1, "telegram:1", "")

            text = svc.send_message.await_args.args[1]
            self.assertIn("Prompt 槽位", text)
            self.assertIn("[quality]", text)
            self.assertIn("[scene]", text)
            self.assertIn("[positive_final]", text)
            self.assertIn("示例 Positive", text)

        asyncio.run(run())

    def test_prompt_slots_clean_legacy_positive_prefix_pollution(self):
        svc = self.make_service()
        sid = "telegram:1"
        svc.config["positive_prefix"] = (
            "masterpiece, best quality, absurdres, score_9, score_8, artist:wlop, "
            "anime coloring, clean lineart, soft cel shading, detailed illustration, "
            "1girl, solo, succubus, black long flowing hair, purple eyes"
        )

        pos, _ = svc._build_prompt("standing by window", session_id=sid)
        slots = svc._last_prompt_slots_by_session[sid]

        self.assertEqual(slots.count, "1girl, solo")
        self.assertEqual(slots.base_appearance, "succubus, black long flowing hair, purple eyes")
        self.assertIn("@00 gx4", slots.style_artist)
        self.assertIn("artist:wlop", slots.style_artist)
        self.assertNotIn("masterpiece", slots.base_appearance)
        self.assertNotIn("best quality", slots.base_appearance)
        self.assertNotIn("artist:wlop", slots.base_appearance)
        self.assertNotIn("1girl", slots.base_appearance)
        self.assertTrue(pos.startswith("masterpiece, best quality"))
        self.assertIn("artist:wlop", pos)
        self.assertIn("succubus", pos)

    def test_prompt_slots_render_positive_is_source_of_truth(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "Yukikaze",
            "custom_series": "Azur Lane",
            "custom_positive_prefix": "artist:wlop, 1girl, solo, blonde hair, red eyes",
            "custom_current_style": "@00 gx4",
            "dynamic_appearance": "white dress",
        })

        pos, neg = svc._build_prompt(
            "standing by window",
            session_id=sid,
            one_shot_appearance="silver necklace",
        )
        slots = svc._last_prompt_slots_by_session[sid]

        self.assertEqual(pos, slots.positive)
        sequence = [
            "masterpiece",
            "1girl",
            "Yukikaze",
            "@00 gx4",
            "artist:wlop",
            "blonde hair",
            "white dress",
            "standing by window",
            "silver necklace",
        ]
        indexes = [pos.index(term) for term in sequence]
        self.assertEqual(indexes, sorted(indexes))
        self.assertNotIn("clothes", neg.lower())
        self.assertNotIn("clothing", neg.lower())

    def test_negative_drops_clothes_when_positive_has_outfit_even_without_custom_character(self):
        svc = self.make_service()
        svc.config["negative_prompt"] = "bad hands, clothes, clothing, low quality"

        pos, neg = svc._build_prompt("A woman wearing a bathrobe by the window", session_id="telegram:1")

        self.assertIn("bathrobe", pos.lower())
        self.assertNotIn("clothes", neg.lower())
        self.assertNotIn("clothing", neg.lower())
        self.assertIn("bad hands", neg.lower())

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

            await svc.cmd_character(1, sid, "clearup")

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

    def test_character_load_restores_saved_style(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state["saved_characters"] = {
                "A": {
                    "character": "A",
                    "series": "",
                    "persona": "你是 A。",
                    "appearance": "1girl, black hair",
                    "style": "@00 gx4, artist:wlop",
                },
            }
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "load A")

            self.assertEqual(state["custom_current_style"], "@00 gx4, artist:wlop")
            self.assertEqual(state["custom_positive_prefix"], "1girl, black hair")

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

            await svc.cmd_character(1, sid, "clearup")

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
            state.update({
                "custom_character": "X",
                "custom_scheduled_persona": "p",
                "persona_user_set": True,
                "custom_character_age_stage": "adult",
                "custom_character_day_anchor": "company",
                "life_profile": {"age_stage": "adult", "day_anchor": "company", "persona_hash": "old"},
            })
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()
            await svc.cmd_character(1, sid, "clearup")
            after = svc._get_session_state(sid)
            self.assertFalse(svc._is_character_set(sid))
            self.assertEqual(after.get("custom_character_age_stage", ""), "")
            self.assertEqual(after.get("custom_character_day_anchor", ""), "")
            self.assertNotIn("life_profile", after)

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

    def test_legacy_character_state_does_not_fall_back_to_default_identity(self):
        svc = self.make_service()
        sid = "telegram:1"
        svc.config.update({"role_name": "蕾伊", "bot_name": "蕾伊"})
        state = svc._get_session_state(sid)
        state["custom_character"] = "东云绘名"
        state["custom_series"] = "Project Sekai"
        state["custom_bot_name"] = ""
        state["custom_scheduled_persona"] = "性格内向、缺乏自信，但内心渴望被认可。"

        system = svc._build_chat_messages(sid, "你好")[0]["content"]

        self.assertIn("你是东云绘名（Project Sekai）。", system)
        self.assertIn("你当前扮演的角色是「东云绘名」（Project Sekai）", system)
        self.assertNotIn("你当前扮演的角色是「蕾伊」", system)

    def test_persona_define_marks_user_set(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            await svc.cmd_persona_define(1, sid, "温柔体贴")
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

    def test_negative_does_not_suppress_character_hair_color(self):
        svc = self.make_service()
        # 模拟真实配置：负向里有防杂色发的发色守卫
        svc.config["negative_prompt"] = "bad hands, silver hair, white hair, blonde hair, low quality"
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_character"] = "测试角色"
        state["custom_positive_prefix"] = "1girl, blonde hair, blue eyes, medium breasts, pale skin"
        pos, neg = svc._build_prompt("standing in a room", session_id=sid)
        self.assertIn("blonde hair", pos.lower())
        # 角色态下发色由角色 prefix 决定，所有 "<颜色> hair" 守卫都去掉，不再和角色发色对冲
        self.assertNotIn("blonde hair", neg.lower())
        self.assertNotIn("silver hair", neg.lower())
        self.assertNotIn("white hair", neg.lower())
        self.assertIn("bad hands", neg.lower())         # 非发色负向保留

    def test_negative_drops_exact_conflict_with_positive(self):
        # 通用：正负向完全相同的 token 从负向删掉
        from telegram_comfyui_selfie.generation import _resolve_negative_conflicts
        neg = _resolve_negative_conflicts(
            "1girl, solo, white sweater, blonde hair",
            "blonde hair, white sweater, low quality, bad hands",
        )
        self.assertNotIn("blonde hair", neg.lower())
        self.assertNotIn("white sweater", neg.lower())
        self.assertIn("low quality", neg.lower())
        self.assertIn("bad hands", neg.lower())

    def test_custom_hair_override_wins_over_scene_and_negative(self):
        svc = self.make_service()
        svc.config["negative_prompt"] = "bad hands, silver hair, black hair, brown hair, low quality"
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_default_hair"] = "silver_hair,bun"
        pos, neg = svc._build_prompt(
            "A front-camera selfie of a woman, Dark brown hair spills loosely over her shoulders, demon horns peeking through",
            session_id=sid,
        )
        low_pos = pos.lower()
        low_neg = neg.lower()
        self.assertIn("silver hair", low_pos)
        self.assertIn("hair bun", low_pos)
        self.assertNotIn("silver_hair", low_pos)
        self.assertNotIn("dark brown hair", low_pos)
        self.assertNotIn("spills loosely", low_pos)
        self.assertNotIn("silver hair", low_neg)
        self.assertIn("black hair", low_neg)
        self.assertIn("brown hair", low_neg)

    def test_dynamic_outfit_replaces_character_default_outfit(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_character"] = "骑士"
        state["custom_positive_prefix"] = "1girl, blonde hair, blue eyes, white and blue battle dress"
        state["dynamic_appearance"] = "oversized white sweater"
        pos, _ = svc._build_prompt("at home at night", session_id=sid)
        self.assertIn("oversized white sweater", pos.lower())
        self.assertNotIn("battle dress", pos.lower())   # 旧服装被换装替换

    def test_character_traits_win_over_scene_hair_and_eye_descriptions(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_character"] = "Knight"
        state["custom_series"] = "Fiction"
        state["custom_positive_prefix"] = "1girl, blue hair, green eyes, white and blue battle dress"

        pos, _ = svc._build_prompt(
            "A woman with dark brown hair and purple eyes sits by a window, wearing a loose white sweater",
            session_id=sid,
        )
        low = pos.lower()
        self.assertIn("blue hair", low)
        self.assertIn("green eyes", low)
        self.assertNotIn("dark brown hair", low)
        self.assertNotIn("purple eyes", low)
        self.assertIn("white sweater", low)
        self.assertNotIn("battle dress", low)

    def test_dynamic_appearance_wins_over_conflicting_scene_outfit(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_positive_prefix"] = "1girl, blonde hair, blue eyes"
        state["dynamic_appearance"] = "silver hair, oversized white sweater"

        pos, _ = svc._build_prompt(
            "A woman with dark brown hair sits by a window, wearing a black fitted dress",
            session_id=sid,
        )
        low = pos.lower()
        self.assertIn("silver hair", low)
        self.assertIn("oversized white sweater", low)
        self.assertNotIn("dark brown hair", low)
        self.assertNotIn("black fitted dress", low)

    def test_daytime_prompt_rewrites_premature_sunset_terms(self):
        svc = self.make_service()
        sid = "telegram:1"
        svc._get_time_context = lambda session_id="", now=None, weather=None: {
            "period": "下午",
            "light_phase": "日间自然光",
        }

        pos, _ = svc._build_prompt(
            "Evening twilight over Sannomiya Station, orange-pink clouds in the sky, "
            "the warm yellow light of the streetlamp just flickers on, sunset, evening streetlight",
            session_id=sid,
        )
        low = pos.lower()
        self.assertIn("afternoon daylight", low)
        self.assertIn("daytime", low)
        self.assertNotIn("sunset", low)
        self.assertNotIn("twilight", low)
        self.assertNotIn("evening", low)
        self.assertNotIn("streetlamp just flickers on", low)

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

            async def fake_generate(chat_id, session_id, **kwargs):
                history = svc._get_session_state(session_id)["chat_history"]
                self.assertEqual(history[-2:], [
                    {"role": "user", "content": "你在家干嘛"},
                    {"role": "assistant", "content": "在家窝着呢~"},
                ])
                return "图片已生成并发送"

            svc.tool_generate_image = AsyncMock(side_effect=fake_generate)
            svc.send_message = AsyncMock(); svc.send_action = AsyncMock()

            await svc.run_roleplay_chat(1, sid, "你在家干嘛")
            await asyncio.sleep(0.02)  # 让 create_task 跑完

            svc._judge_image_moment.assert_awaited_once()
            svc.tool_generate_image.assert_awaited_once()
            self.assertEqual(svc.tool_generate_image.await_args.kwargs["intent"], "展示在家穿搭")
            self.assertEqual(svc.tool_generate_image.await_args.kwargs["prompt"], "在家窝着呢~")

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

    def test_intimate_context_detection_chinese_keywords(self):
        self.assertTrue(_detect_intimate_context("角色正在与用户交合时的面部特写"))
        self.assertTrue(_detect_intimate_context("", "", "进入她体内"))
        self.assertTrue(_detect_intimate_context("骑乘", "迷离表情"))
        self.assertFalse(_detect_intimate_context("角色坐在窗边看书"))
        self.assertFalse(_detect_intimate_context(""))

    def test_build_prompt_sex_scene_keeps_pov_and_strips_selfie(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_positive_prefix"] = "1girl, black long hair, purple eyes"
        state["custom_count"] = "1girl"
        # POV 亲密场景应保留 pov, 剥离 selfie, 不加 third-person
        pos, neg = svc._build_prompt(
            "First-person POV, looking at a woman, sex, make love, intimate close-up, missionary position",
            session_id=sid,
        )
        pos_lower = pos.lower()
        self.assertIn("first-person pov", pos_lower)
        self.assertNotIn("selfie", pos_lower)
        self.assertNotIn("holding phone", pos_lower)
        self.assertNotIn("third-person perspective", pos_lower)
        self.assertNotIn("solo", pos_lower)
        self.assertIn("partial male body visible", pos_lower)
        self.assertIn("male hands", pos_lower)
        self.assertIn("intimate close-up", pos_lower)
        neg_lower = neg.lower()
        for term in ["selfie", "holding phone", "phone", "arm extended", "third-person perspective"]:
            self.assertIn(term, neg_lower, f"negative should suppress {term}")
        self.assertNotIn("pov", neg_lower)
        self.assertNotIn("male", neg_lower)

    def test_build_prompt_intimate_flag_equivalent_to_english_keywords(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_positive_prefix"] = "1girl, black long hair, purple eyes"
        state["custom_count"] = "1girl"
        # 不含 sex/make love 等英文关键词，仅靠 is_intimate=True 触发
        pos, neg = svc._build_prompt(
            "First-person POV, looking at a woman, close embrace, intimate close-up",
            session_id=sid,
            is_intimate=True,
        )
        pos_lower = pos.lower()
        self.assertIn("first-person pov", pos_lower)
        self.assertNotIn("solo", pos_lower)
        self.assertIn("partial male body visible", pos_lower)
        self.assertIn("male hands", pos_lower)
        neg_lower = neg.lower()
        self.assertNotIn("pov", neg_lower)
        self.assertNotIn("male", neg_lower)

    def test_build_prompt_partner_flag_routes_to_partner_path(self):
        # 规划器 partner_in_frame=True：即便场景没有 him/he 代词，也按伴侣路径处理（去 solo、画男伴局部）。
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_positive_prefix"] = "1girl, black long hair, purple eyes"
        state["custom_count"] = "1girl"
        pos, neg = svc._build_prompt(
            "First-person POV, looking at a woman, lying together in bed at night",
            session_id=sid,
            partner_in_frame=True,
        )
        pos_lower = pos.lower()
        self.assertNotIn("solo", pos_lower)
        self.assertIn("partial male body visible", pos_lower)
        self.assertNotIn("male", neg.lower())

    def test_build_prompt_device_in_frame_keeps_selfie_and_phone(self):
        # 用户明确要“做爱时对镜自拍/录像”：device_in_frame=True 应保留自拍/对镜取景与设备，不强制清掉。
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_positive_prefix"] = "1girl, black long hair, purple eyes"
        state["custom_count"] = "1girl"
        pos, neg = svc._build_prompt(
            "A mirror reflection of a woman, holding a smartphone, sex, riding him, intimate close-up",
            session_id=sid,
            is_intimate=True,
            device_in_frame=True,
        )
        pos_lower = pos.lower()
        # 设备与对镜取景保留
        self.assertIn("mirror reflection", pos_lower)
        self.assertIn("smartphone", pos_lower)
        # 仍按性爱场景去掉 solo、画男伴局部
        self.assertNotIn("solo", pos_lower)
        self.assertIn("partial male body visible", pos_lower)
        # 手机/对镜负向被放开，male 负向去掉
        neg_lower = neg.lower()
        for term in ["holding phone", "visible phone", "mirror selfie"]:
            self.assertNotIn(term, neg_lower)
        self.assertNotIn("male", neg_lower)

    def test_wardrobe_same_slot_replaces_and_dress_excludes(self):
        # 同槽替换、内衣/鞋独立、连衣裙互斥覆盖上下装。
        wd = {}
        wd = appearance_rules.apply_wardrobe_change(wd, {"top": "white blouse", "bottom": "blue jeans"})
        wd = appearance_rules.apply_wardrobe_change(wd, {"bra": "red lace bra"})  # 内衣独立
        self.assertEqual(wd.get("top"), "white blouse")
        self.assertEqual(wd.get("bottom"), "blue jeans")
        self.assertEqual(wd.get("bra"), "red lace bra")
        # 换上衣 → 替换 top，不动 bottom/bra
        wd = appearance_rules.apply_wardrobe_change(wd, {"top": "black tank top"})
        self.assertEqual(wd.get("top"), "black tank top")
        self.assertEqual(wd.get("bottom"), "blue jeans")
        # 连衣裙 → 清掉 top+bottom，保留 bra
        wd = appearance_rules.apply_wardrobe_change(wd, {"dress": "black evening gown"})
        self.assertNotIn("top", wd)
        self.assertNotIn("bottom", wd)
        self.assertEqual(wd.get("dress"), "black evening gown")
        self.assertEqual(wd.get("bra"), "red lace bra")
        # 再设下装 → 清掉连衣裙
        wd = appearance_rules.apply_wardrobe_change(wd, {"bottom": "denim shorts"})
        self.assertNotIn("dress", wd)
        self.assertEqual(wd.get("bottom"), "denim shorts")

    def test_wardrobe_accessory_accumulate_remove_and_reset(self):
        wd = appearance_rules.apply_wardrobe_change({}, {"accessory_add": "glasses, silver necklace"})
        wd = appearance_rules.apply_wardrobe_change(wd, {"accessory_add": "choker"})
        self.assertEqual(wd.get("accessory"), "glasses, silver necklace, choker")
        wd = appearance_rules.apply_wardrobe_change(wd, {"accessory_remove": "silver necklace"})
        self.assertEqual(wd.get("accessory"), "glasses, choker")
        # remove 槽位（脱外套）
        wd = appearance_rules.apply_wardrobe_change(wd, {"outerwear": "denim jacket"})
        wd = appearance_rules.apply_wardrobe_change(wd, {"remove": ["outerwear"]})
        self.assertNotIn("outerwear", wd)
        self.assertEqual(appearance_rules.apply_wardrobe_change(wd, {"reset_all": True}), {})

    def test_wardrobe_seed_from_legacy_flat_text(self):
        acc_kw = ["glasses", "necklace", "choker"]
        seed = appearance_rules.seed_wardrobe_from_text(
            "black long flowing hair, purple eyes, red dress, black heels, glasses", None, acc_kw
        )
        self.assertEqual(seed.get("hair"), "black long flowing hair")
        self.assertEqual(seed.get("eyes"), "purple eyes")
        self.assertEqual(seed.get("dress"), "red dress")
        self.assertEqual(seed.get("footwear"), "black heels")
        self.assertEqual(seed.get("accessory"), "glasses")

    def test_apply_wardrobe_uses_llm_classifier_and_persists(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._classify_wardrobe_change = AsyncMock(return_value={"dress": "red qipao"})
            result = await svc._apply_wardrobe(sid, "换上红色旗袍")
            self.assertIn("red qipao", result)
            state = svc._get_session_state(sid)
            self.assertEqual(state["wardrobe"].get("dress"), "red qipao")
            self.assertIn("red qipao", state["dynamic_appearance"])
            # 再换胸罩：旗袍保留、bra 新增（衣柜持久）
            svc._classify_wardrobe_change = AsyncMock(return_value={"bra": "black bra"})
            await svc._apply_wardrobe(sid, "换个黑色胸罩")
            state = svc._get_session_state(sid)
            self.assertEqual(state["wardrobe"].get("dress"), "red qipao")
            self.assertEqual(state["wardrobe"].get("bra"), "black bra")

        asyncio.run(run())

    def test_apply_wardrobe_migrates_legacy_dynamic_appearance(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state["dynamic_appearance"] = "red dress, black heels"  # 老数据，无 wardrobe
            state["wardrobe"] = {}
            # 只换鞋：旧 dress 应被迁移保留，footwear 被替换
            svc._classify_wardrobe_change = AsyncMock(return_value={"footwear": "white sneakers"})
            await svc._apply_wardrobe(sid, "换白色运动鞋")
            state = svc._get_session_state(sid)
            self.assertEqual(state["wardrobe"].get("dress"), "red dress")
            self.assertEqual(state["wardrobe"].get("footwear"), "white sneakers")

        asyncio.run(run())

    def test_closet_add_dedupes_by_tags_and_caps(self):
        closet = {}
        closet = appearance_rules.closet_add(closet, "碎花裙", "dress", "floral dress", now=1.0)
        closet = appearance_rules.closet_add(closet, "蓝衬衫", "top", "blue shirt", now=2.0)
        self.assertEqual(set(closet), {"碎花裙", "蓝衬衫"})
        # 同 tags 改名 → 视为同一件，旧名清掉
        closet = appearance_rules.closet_add(closet, "碎花连衣裙", "dress", "floral dress", now=3.0)
        self.assertNotIn("碎花裙", closet)
        self.assertIn("碎花连衣裙", closet)
        # cap 淘汰最久没穿的
        for i in range(40):
            closet = appearance_rules.closet_add(closet, f"item{i}", "top", f"tagset{i}", now=10.0 + i, cap=5)
        self.assertLessEqual(len(closet), 5)

    def test_apply_wardrobe_autocaptures_to_closet(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._classify_wardrobe_change = AsyncMock(return_value={"dress": "floral dress", "names": {"dress": "碎花连衣裙"}})
            await svc._apply_wardrobe(sid, "换上碎花连衣裙")
            closet = svc._get_session_state(sid)["wardrobe_closet"]
            self.assertIn("碎花连衣裙", closet)
            self.assertEqual(closet["碎花连衣裙"]["slot"], "dress")
            # 换上衣 → 衣橱新增上衣，碎花裙仍在收藏
            svc._classify_wardrobe_change = AsyncMock(return_value={"top": "blue blouse", "names": {"top": "蓝衬衫"}})
            await svc._apply_wardrobe(sid, "换蓝衬衫")
            closet = svc._get_session_state(sid)["wardrobe_closet"]
            self.assertEqual(set(closet), {"碎花连衣裙", "蓝衬衫"})

        asyncio.run(run())

    def test_wardrobe_reset_keeps_closet(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._classify_wardrobe_change = AsyncMock(return_value={"dress": "red dress", "names": {"dress": "红裙"}})
            await svc._apply_wardrobe(sid, "穿红裙")
            await svc._apply_wardrobe(sid, "reset")  # 脱掉当前外型
            state = svc._get_session_state(sid)
            self.assertEqual(state["wardrobe"], {})
            self.assertEqual(state["dynamic_appearance"], "")
            self.assertIn("红裙", state["wardrobe_closet"])  # 衣橱收藏保留

        asyncio.run(run())

    def test_tool_change_appearance_respects_permission(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._get_session_state(sid)["custom_allow_llm_change_appearance"] = False
            svc._classify_wardrobe_change = AsyncMock(return_value={"dress": "x"})
            out = await svc.tool_change_appearance(sid, "换裙子")
            self.assertIn("已关闭", out)
            svc._classify_wardrobe_change.assert_not_awaited()

        asyncio.run(run())

    def test_build_prompt_intimate_without_device_strips_phone(self):
        # 对照组：同样亲密但没有 device_in_frame，手机/自拍取景应被清掉、补 POV。
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_positive_prefix"] = "1girl, black long hair, purple eyes"
        state["custom_count"] = "1girl"
        pos, neg = svc._build_prompt(
            "A front-camera selfie of a woman, solo, lying beside him after sex",
            session_id=sid,
            is_intimate=True,
        )
        pos_lower = pos.lower()
        self.assertNotIn("front-camera selfie", pos_lower)
        self.assertNotIn("solo", pos_lower)
        self.assertIn("first-person pov", pos_lower)
        self.assertIn("partial male body visible", pos_lower)
