import asyncio
import copy
import json
import os
import shutil
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram_comfyui_selfie import TelegramComfyUIService
from telegram_comfyui_selfie import appearance as appearance_rules
from telegram_comfyui_selfie import character_card
from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.config_store import flatten_config, load_simple_yaml
from telegram_comfyui_selfie.generation import PromptSlots, _build_animatool_turbo_payload
from telegram_comfyui_selfie.image_planning import _detect_intimate_context, _detect_nudity_context, _infer_clothing_off_fallback, format_dialog_context, format_planning_spatial_context, format_sent_photo_context, normalize_scene_visual_subject, plan_animatool_slots, plan_roleplay_image
from telegram_comfyui_selfie.commands import (
    SESSION_GLOBAL_STATE_KEYS,
    _is_character_config_key,
    _is_transient_state_key,
)
from telegram_comfyui_selfie.command_aliases import COMMAND_ALIAS_GROUPS, resolve_command_alias
from telegram_comfyui_selfie.prompt_intake import heuristic_intake
from telegram_comfyui_selfie.webui import build_world_route_preview, cast_config_value, masked_config, serialize_prompt_slots, session_summary


os.environ.setdefault("SUCYUBOT_TEST_FAST_SQLITE", "1")

TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "tests"
_TEST_TMP_READY = False
_TEST_TMP_COUNTER = 0


def make_project_temp_dir(prefix: str = "case") -> Path:
    """为测试创建项目内临时目录；每次测试进程启动先清理上次残留。"""
    global _TEST_TMP_READY, _TEST_TMP_COUNTER
    if not _TEST_TMP_READY:
        if TEST_TMP_ROOT.exists():
            shutil.rmtree(TEST_TMP_ROOT, ignore_errors=True)
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        _TEST_TMP_READY = True
    _TEST_TMP_COUNTER += 1
    path = TEST_TMP_ROOT / f"{prefix}_{int(time.time() * 1000)}_{_TEST_TMP_COUNTER}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=False)
    return path


def make_mock_request(app, path, method="GET", admin=False, query=None):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request(method, path, app=app, headers={"Content-Type": "application/json"})
    if admin:
        req["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
    return req


class ServiceFixtureMixin:
    def make_service(self):
        root = make_project_temp_dir("service")
        cfg = root / "config.json"
        state = root / "state.json"
        cfg.write_text(json.dumps({"telegram_bot_token": "TEST"}, ensure_ascii=False), encoding="utf-8")
        svc = TelegramComfyUIService(cfg, state)
        return svc

    def make_service_from_current_config(self):
        root = make_project_temp_dir("live_config")
        project_root = Path(__file__).resolve().parents[1]
        source_cfg = project_root / "data" / "config.yml"
        if not source_cfg.exists():
            source_cfg = project_root / "data" / "config.json"
        state = root / "state.json"
        svc = TelegramComfyUIService(root / "config.yml", state)
        if source_cfg.exists():
            if source_cfg.suffix.lower() in (".yml", ".yaml"):
                loaded = flatten_config(load_simple_yaml(source_cfg))
            else:
                loaded = json.loads(source_cfg.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                svc.config.update(loaded)
        svc.config["long_memory_db_path"] = ""
        svc.config["user_log_enabled"] = False
        svc.config["user_log_dir"] = str(root / "logs")
        return svc


class ServiceTestCase(ServiceFixtureMixin, unittest.TestCase):

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
        self.assertEqual(svc.parse_command("拍照 神户街头"), ("自拍", "神户街头"))
        self.assertEqual(svc.parse_command("菜单 动线"), ("菜单", "动线"))
        self.assertEqual(svc.parse_command("menu 动线"), ("菜单", "动线"))
        self.assertEqual(svc.parse_command("初始化"), ("初始化", ""))
        self.assertEqual(svc.parse_command("创建角色"), ("初始化", ""))
        self.assertEqual(svc.parse_command("角色创建"), ("初始化", ""))
        self.assertEqual(svc.parse_command("/角色创建"), ("初始化", ""))
        self.assertEqual(svc.parse_command("新建角色"), ("初始化", ""))
        self.assertEqual(svc.parse_command("创建OC"), ("创建OC", ""))
        self.assertEqual(svc.parse_command("oc 名字：小雨"), ("创建OC", "名字：小雨"))
        self.assertEqual(svc.parse_command("/画图 低机位手部特写"), ("配图", "低机位手部特写"))
        self.assertEqual(svc.parse_command("配图 车窗外远景"), ("配图", "车窗外远景"))
        self.assertEqual(svc.parse_command("推送测试 normal"), ("测试推送", "normal"))
        self.assertEqual(svc.parse_command("我想看自拍"), (None, "我想看自拍"))

    def test_command_aliases_are_grouped_lists_and_cover_reversed_forms(self):
        self.assertIsInstance(COMMAND_ALIAS_GROUPS, tuple)
        self.assertEqual(resolve_command_alias("角色创建"), "初始化")
        self.assertEqual(resolve_command_alias("菜单查看"), "菜单")
        self.assertEqual(resolve_command_alias("推送测试"), "测试推送")
        self.assertEqual(resolve_command_alias("画风添加"), "添加画风")
        self.assertEqual(resolve_command_alias("关系设置"), "关系")
        self.assertEqual(resolve_command_alias("画图"), "配图")

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
            # 当前默认菜单是"高频指令"快捷版，面向已初始化用户；
            # 初始化引导走 /初始化 命令，完整命令走 /完整菜单。
            self.assertIn("快速菜单", text)
            self.assertIn("/自拍", text)
            self.assertIn("/角色 list", text)
            self.assertIn("/角色 load <名称>", text)
            self.assertIn("/修改角色", text)
            self.assertIn("/记忆", text)
            self.assertIn("/webui", text)
            self.assertIn("/完整菜单", text)

        asyncio.run(run())

    def test_start_alias_dispatches_to_init_guide(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.dispatch_command(1, "telegram:1", "start", "")

            text = svc.send_message.await_args.args[1]
            self.assertIn("初始化向导", text)
            self.assertIn("第 1/8 步", text)

        asyncio.run(run())

    def test_dispatch_command_aliases_use_canonical_handlers(self):
        async def run():
            svc = self.make_service()
            svc.cmd_init_guide = AsyncMock()
            svc.cmd_menu = AsyncMock()
            svc.cmd_selfie = AsyncMock()
            svc.cmd_scene_image = AsyncMock()
            svc.cmd_test_push = AsyncMock()

            await svc.dispatch_command(1, "telegram:1", "创建角色", "")
            await svc.dispatch_command(1, "telegram:1", "menu", "动线")
            await svc.dispatch_command(1, "telegram:1", "拍照", "窗边")
            await svc.dispatch_command(1, "telegram:1", "画图", "远景")
            await svc.dispatch_command(1, "telegram:1", "推送测试", "normal")

            svc.cmd_init_guide.assert_awaited_once_with(1, "telegram:1", "")
            svc.cmd_menu.assert_awaited_once_with(1, "telegram:1", "动线")
            svc.cmd_selfie.assert_awaited_once_with(1, "telegram:1", "窗边")
            svc.cmd_scene_image.assert_awaited_once_with(1, "telegram:1", "远景")
            svc.cmd_test_push.assert_awaited_once_with(1, "telegram:1", "normal")

        asyncio.run(run())

    def test_init_flow_consumes_plain_replies_before_chat(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc.send_message = AsyncMock()
            svc.handle_chat = AsyncMock()

            await svc.handle_update({"message": {"chat": {"id": 123}, "text": "初始化"}})
            self.assertTrue(session_schema.get_init_flow(svc._get_session_state(sid)).get("active"))
            self.assertIn("第 1/8 步", svc.send_message.await_args.args[1])

            await svc.handle_update({"message": {"chat": {"id": 123}, "text": "小雨"}})
            self.assertEqual(session_schema.get_init_flow(svc._get_session_state(sid)).get("step"), 1)
            self.assertIn("第 2/8 步", svc.send_message.await_args.args[1])

            await svc.handle_update({"message": {"chat": {"id": 123}, "text": "原创"}})
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_init_flow(state).get("step"), 2)
            self.assertEqual(state["custom_character"], "")
            svc.handle_chat.assert_not_awaited()

        asyncio.run(run())

    def test_photo_message_is_converted_to_text_before_chat(self):
        async def run():
            svc = self.make_service()
            svc.handle_chat = AsyncMock()

            async def fake_describe(session_id, photo_sizes, **kwargs):
                return "图片里是一杯放在木桌上的咖啡。" if photo_sizes else ""

            svc._describe_telegram_photo_sizes_for_chat = fake_describe
            await svc.handle_update({
                "message": {
                    "chat": {"id": 123},
                    "caption": "看这个",
                    "photo": [{"file_id": "p1", "width": 100, "height": 100}],
                }
            })

            svc.handle_chat.assert_awaited_once()
            text = svc.handle_chat.await_args.args[2]
            self.assertIsInstance(text, str)
            self.assertIn("【图片描述】", text)
            self.assertIn("图片里是一杯放在木桌上的咖啡。", text)
            self.assertIn("【用户当前输入】\n看这个", text)

        asyncio.run(run())

    def test_photo_only_message_is_ignored_when_vision_model_is_empty(self):
        async def run():
            svc = self.make_service()
            svc.handle_chat = AsyncMock()
            svc._describe_telegram_photo_sizes_for_chat = AsyncMock(return_value="")

            await svc.handle_update({
                "message": {
                    "chat": {"id": 123},
                    "photo": [{"file_id": "p1", "width": 100, "height": 100}],
                }
            })

            svc.handle_chat.assert_not_awaited()

        asyncio.run(run())

    def test_reply_quote_text_is_injected_before_chat(self):
        async def run():
            svc = self.make_service()
            svc.handle_chat = AsyncMock()

            await svc.handle_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "这句是什么意思？",
                    "quote": {"text": "手动选中的片段"},
                    "reply_to_message": {
                        "from": {"is_bot": True},
                        "text": "上一条机器人回复",
                    },
                }
            })

            text = svc.handle_chat.await_args.args[2]
            self.assertIn("【引用内容】", text)
            self.assertIn("手动引用片段: 手动选中的片段", text)
            self.assertIn("回复的机器人消息: 上一条机器人回复", text)
            self.assertIn("【用户当前输入】\n这句是什么意思？", text)

        asyncio.run(run())

    def test_init_flow_creates_character_card_at_the_end(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            translations = {
                "黑色短发，蓝眼睛": "short black hair, blue eyes",
                "白衬衫，深色百褶裙": "white shirt, dark pleated skirt",
            }
            svc._translate_appearance_tags = AsyncMock(side_effect=lambda text: translations[text])
            svc.send_message = AsyncMock()
            svc.handle_chat = AsyncMock()

            for text in (
                "初始化",
                "小雨",
                "原创",
                "黑色短发，蓝眼睛，白衬衫，深色百褶裙",
                "大学生，温柔、慢热",
                "同城恋人，称呼我主人",
                "跳过",
                "3",
                "0",
            ):
                await svc.handle_update({"message": {"chat": {"id": 123}, "text": text}})

            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_init_flow(state), {})
            self.assertEqual(state["custom_character"], "小雨")
            self.assertEqual(state["custom_role_name"], "大学生")
            self.assertEqual(state["custom_character_occupation"], "大学生")
            self.assertEqual(state["custom_user_address"], "主人")
            self.assertEqual(state["custom_spatial_relationship"], "同城恋人")
            self.assertEqual(state["custom_positive_prefix"], "short black hair, blue eyes")
            self.assertEqual(session_schema.get_outfit(state), "white shirt, dark pleated skirt")
            self.assertEqual(state["purity"], 3)
            self.assertTrue(state["purity_user_set"])
            self.assertEqual(state["custom_daily_selfie_limit"], "0")
            self.assertEqual(state["saved_characters"]["小雨"]["user_address"], "主人")
            svc.handle_chat.assert_not_awaited()

        asyncio.run(run())

    def test_init_flow_uses_llm_intake_for_existing_character_identity(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc._normalize_prompt_intake = AsyncMock(return_value={
                "source_type": "existing",
                "series": "Blue Archive",
                "original_name": "Tendou Aris",
                "visual_character": "aris_(blue_archive)",
                "visual_series": "blue_archive",
                "role": "学生",
                "age": "adult",
                "occupation": "学生",
                "anchor": "school",
                "persona": "开朗、认真",
                "base_appearance": "黑色长发，蓝眼睛",
                "user_address": "老师",
                "relationship": "同校朋友",
            })
            svc._translate_appearance_tags = AsyncMock(return_value="long black hair, blue eyes")
            svc.send_message = AsyncMock()
            svc.handle_chat = AsyncMock()

            for text in (
                "/创建角色",
                "爱丽丝卡",
                "Blue Archive / Tendou Aris",
                "黑色长发，蓝眼睛，穿校服",
                "学生，开朗、认真",
                "同校朋友，称呼我老师",
                "跳过",
                "auto",
                "默认",
            ):
                await svc.handle_update({"message": {"chat": {"id": 123}, "text": text}})

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_character"], "爱丽丝卡")
            self.assertEqual(state["custom_bot_name"], "Tendou Aris")
            self.assertEqual(state["custom_series"], "Blue Archive")
            self.assertEqual(state["custom_visual_character"], "aris_(blue_archive)")
            self.assertEqual(state["custom_visual_series"], "blue_archive")
            self.assertEqual(state["custom_character_occupation"], "学生")
            self.assertEqual(state["custom_character_day_anchor"], "school")
            self.assertEqual(state["saved_characters"]["爱丽丝卡"]["original_name"], "Tendou Aris")
            self.assertEqual(state["saved_characters"]["爱丽丝卡"]["visual_character"], "aris_(blue_archive)")
            svc.handle_chat.assert_not_awaited()

        asyncio.run(run())

    def test_character_panel_hides_preference_fields(self):
        app_js = (Path(__file__).resolve().parents[1] / "telegram_comfyui_selfie" / "static" / "app.js").read_text(encoding="utf-8")
        character_fields = app_js.split("const characterFieldSections = [", 1)[1].split("const commands =", 1)[0]
        self.assertIn('["user_address", "对用户称呼", "text"]', character_fields)
        self.assertNotIn('["scene_preference"', character_fields)
        self.assertNotIn('["selfie_preference"', character_fields)

    def test_model_panel_has_no_thinking_controls(self):
        app_js = (Path(__file__).resolve().parents[1] / "telegram_comfyui_selfie" / "static" / "app.js").read_text(encoding="utf-8")
        model_section = app_js.split("async function loadModels()", 1)[1].split("function worldSessionTitle", 1)[0]
        self.assertIn('name="vision_profile_id"', model_section)
        for field in ["profile_id", "name", "base_url", "api_key", "model", "max_tokens", "timeout"]:
            self.assertIn(f'name="{field}"', model_section)
        self.assertNotIn('name="json"', model_section)
        self.assertNotIn("<textarea name=\"json\"", model_section)
        self.assertNotIn("chat_thinking", model_section)
        self.assertNotIn("fast_thinking", model_section)
        self.assertNotIn("disable_thinking", model_section)
        self.assertNotIn("thinking_fixed", model_section)
        self.assertNotIn("thinking_control", model_section)
        self.assertNotIn("api_key_no_think", model_section)
        self.assertNotIn("model_no_think", model_section)
        self.assertNotIn("model_think", model_section)

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

    def test_create_oc_help_includes_template(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc.cmd_create_oc(1, "telegram:1", "help")

            text = svc.send_message.await_args.args[1]
            self.assertIn("创建角色卡", text)
            self.assertIn("名字：小雨", text)
            self.assertIn("初始穿搭", text)

        asyncio.run(run())

    def test_create_oc_without_arg_starts_character_flow(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()

            await svc.cmd_create_oc(1, sid, "")

            state = svc._get_session_state(sid)
            self.assertTrue(session_schema.get_init_flow(state).get("active"))
            self.assertIn("初始化向导", svc.send_message.await_args.args[1])
            self.assertIn("第 1/8 步", svc.send_message.await_args.args[1])

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
            self.assertEqual(session_schema.get_outfit(state), "white shirt, dark pleated skirt")
            self.assertEqual(state["custom_character_age_stage"], "adult")
            self.assertEqual(state["custom_character_occupation"], "大学生")
            self.assertEqual(state["custom_character_day_anchor"], "school")
            self.assertEqual(state["chat_history"], [])
            self.assertEqual(state["saved_characters"]["小雨"]["series"], "")
            text = svc.send_message.await_args.args[1]
            self.assertIn("OC 已创建: 小雨", text)

        asyncio.run(run())

    def test_create_oc_dialog_address_is_user_address_not_self_name(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc._translate_appearance_tags = AsyncMock(return_value="short black hair, blue eyes")
            svc.send_message = AsyncMock()

            await svc.cmd_create_oc(
                1,
                sid,
                "名字：小雨\n"
                "角色类型：大学生\n"
                "对话称呼：主人\n"
                "性格：温柔\n"
                "外貌：黑色短发，蓝眼睛",
            )

            state = svc._get_session_state(sid)
            self.assertEqual(state["custom_bot_name"], "小雨")
            self.assertEqual(state["custom_user_address"], "主人")
            self.assertEqual(state["custom_bot_self_name"], "")
            self.assertEqual(state["saved_characters"]["小雨"]["user_address"], "主人")
            system = svc._build_chat_messages(sid, "你好")[0]["content"]
            self.assertIn("你通常称呼用户为「主人」", system)

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
        # 已有角色的"你是X（作品）。"不是漂移源，不被误删
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
        session_schema.set_outfit(state, "")
        self.assertEqual(svc._effective_dynamic_appearance(sid), "")
        self.assertNotIn("black silk slip dress", svc._get_effective_persona(sid))
        # 角色有自己的临时穿搭时照常用
        session_schema.set_outfit(state, "school uniform")
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

    def test_prompt_intake_prompt_requires_romanized_danbooru_identity(self):
        async def run():
            svc = self.make_service()
            svc.has_llm_config = lambda purpose="image": True
            svc._call_llm = AsyncMock(return_value="{}")

            await svc._normalize_prompt_intake("角色出处与原名：碧蓝档案 / 天童爱丽丝", context="init")

            system = svc._call_llm.await_args.args[0]
            self.assertIn("original_name", system)
            self.assertIn("姓氏在前", system)
            self.assertIn("Danbooru", system)
            self.assertIn("visual_character", system)
            self.assertNotIn("knowledge", system.lower())

        asyncio.run(run())

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
            self.assertEqual(session_schema.get_outfit(state), "oversized white sweater")
            self.assertEqual(state["custom_raw_profile_text"], "小雨，大学生，金发蓝眼，低马尾，穿宽松白毛衣，和用户是同城暧昧对象")
            self.assertIn("base_appearance", state["custom_prompt_intake"])
            self.assertIn("自动归档", svc.send_message.await_args.args[1])

        asyncio.run(run())

    def test_create_oc_strips_hair_eyes_and_dedup_from_outfit(self):
        """OC创建时穿搭字段不含发色/瞳色标签，且去重——防止 LLM 误分类或默认外观污染 dynamic_appearance。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()

            await svc.cmd_create_oc(
                1,
                sid,
                "名字: 林翩翩\n身体特征: brown eyes\n初始穿搭: black hair, brown hair, white hanfu, purple eyes, pink vertical pupils, white hanfu",
            )

            state = svc._get_session_state(sid)
            outfit = session_schema.get_outfit(state)
            # 发色/瞳色应从穿搭中剔除
            self.assertNotIn("black hair", outfit.lower())
            self.assertNotIn("brown hair", outfit.lower())
            self.assertNotIn("purple eyes", outfit.lower())
            self.assertNotIn("pink vertical", outfit.lower())
            # 只保留服装/配饰
            self.assertIn("white hanfu", outfit.lower())
            # 去重（"white hanfu" 只出现一次）
            self.assertEqual(outfit.lower().count("white hanfu"), 1)
            # 基础外观保留用户输入
            self.assertIn("brown eyes", (state.get("custom_positive_prefix") or "").lower())
            # 最终穿搭不含质量词/发瞳，确认干净
            self.assertEqual(outfit.lower(), "white hanfu")

        asyncio.run(run())

    def test_create_oc_empty_outfit_unchanged_after_filter(self):
        """空穿搭不受过滤影响——无输入时不应污染 dynamic_appearance。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            # LLM 外观翻译：中文→英文
            svc._translate_appearance_tags = AsyncMock(side_effect=lambda text: text)

            await svc.cmd_create_oc(
                1,
                sid,
                "名字: 小羽\n身体特征: blue eyes, short blonde hair",
            )

            state = svc._get_session_state(sid)
            # 无穿搭输入 → dynamic_appearance 应为空
            self.assertEqual(session_schema.get_outfit(state), "")
            # 基础外观保留
            self.assertIn("blue eyes", state.get("custom_positive_prefix", "").lower())
            self.assertIn("blonde hair", state.get("custom_positive_prefix", "").lower())

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
            self.assertEqual(session_schema.get_outfit(state), "white sweater")
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
            self.assertIn("white hair", session_schema.get_outfit(state))
            self.assertIn("glasses", session_schema.get_outfit(state))

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
        saved = svc.app_store.load_session_state(sid)
        self.assertEqual(saved["custom_character"], "重启测试角色")
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

    def test_style_command_saves_unlisted_style_to_current_character_card(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.config["style_pool"] = "@base"
            svc.config["current_style"] = "@base"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            svc.send_message = AsyncMock()

            await svc.cmd_style(1, sid, "@new_style, artist:wlop")

            self.assertEqual(session_schema.get_character_value(state, "custom_current_style", ""), "@new_style, artist:wlop")
            self.assertEqual(session_schema.get_saved_characters(state)["小雨"]["style"], "@new_style, artist:wlop")
            self.assertEqual(svc._normalize_style_pool(), ["@base"])
            svc.send_message.assert_awaited_once()
            self.assertIn("当前角色画风已设为", svc.send_message.await_args.args[1])

        asyncio.run(run())

    def test_style_command_can_clear_current_character_style_field(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.config["style_pool"] = "@base"
            svc.config["current_style"] = "@base"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            session_schema.set_character_value(state, "custom_current_style", "@old_style")
            svc._snapshot_character(state)
            svc.send_message = AsyncMock()

            await svc.cmd_style(1, sid, "清空")

            self.assertEqual(session_schema.get_character_value(state, "custom_current_style", ""), "")
            self.assertEqual(session_schema.get_saved_characters(state)["小雨"]["style"], "")
            self.assertEqual(svc._get_current_style(sid), "")
            pos, _ = svc._build_prompt("standing", session_id=sid)
            self.assertNotIn("@base", pos)
            svc.send_message.assert_awaited_once()
            self.assertIn("已清空当前角色画风字段", svc.send_message.await_args.args[1])

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
            app_js = (Path(__file__).resolve().parents[1] / "telegram_comfyui_selfie" / "static" / "app.js").read_text(encoding="utf-8")
            self.assertIn('["style", "画风", "style_combo"]', app_js)
            self.assertIn("state.characterData?.style_pool", app_js)
            self.assertIn("留空表示本角色不注入画风", app_js)

        asyncio.run(run())

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
        session_schema.set_outfit(state, "white hair, glasses")
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
        photo_history = session_schema.get_chat_history(svc._get_session_state(sid))[-1]
        self.assertEqual(photo_history["role"], "system")
        self.assertIn("照片历史", photo_history["content"])
        self.assertIn("standing by a window", photo_history["content"])

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

        all_sys = "\n".join(m["content"] for m in svc._build_chat_messages(sid, "你现在戴着什么？") if m.get("role") == "system")
        self.assertIn("当前可见外型与配饰", all_sys)
        self.assertIn("用户问到外貌、穿搭、配饰或随身物时优先依据这里", all_sys)
        self.assertIn("silver-rimmed glasses", all_sys)
        self.assertIn("dual swords", all_sys)

    def test_static_prefix_stable_across_outfit_change(self):
        """前缀缓存不变量：只换穿搭时 messages[0]（静态前缀）必须不变。

        穿搭是中频变化字段，若焊进静态前缀，每次换装都会作废整条历史的服务端 prefix cache
        （命中率暴跌的根因）。穿搭只应出现在动态层。
        """
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "小雨",
            "custom_scheduled_persona": "温柔体贴",
            "custom_positive_prefix": "black hair, blue eyes",
            "dynamic_appearance": "white shirt",
        })
        before = svc._build_chat_messages(sid, "你好")[0]["content"]
        # 换一套穿搭
        session_schema.set_outfit(state, "red dress, black coat")
        after_msgs = svc._build_chat_messages(sid, "你好")
        # 静态前缀不随穿搭变化（缓存可命中）
        self.assertEqual(before, after_msgs[0]["content"])
        # 但新穿搭仍出现在历史前半稳定状态层，信息没丢
        all_sys = "\n".join(m["content"] for m in after_msgs if m.get("role") == "system")
        self.assertIn("red dress", all_sys)
        visual = next(m["content"] for m in after_msgs if "当前可见外型与配饰" in m.get("content", ""))
        self.assertIn("red dress", visual)
        self.assertNotIn("red dress", after_msgs[0]["content"])

    def test_chat_tools_schema_is_compact_and_keeps_semantics(self):
        svc = self.make_service()
        tools = svc._chat_tools_schema()
        text = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))

        self.assertLess(len(text), 1900)
        self.assertIn("generate_roleplay_image", text)
        self.assertIn("change_appearance", text)
        self.assertIn("update_location", text)
        self.assertIn("update_user_location", text)
        for required in (
            "无手机和手机UI",
            "portrait=别人帮角色拍",
            "NTR",
            "只有mirror允许镜子和手机同框",
            "连衣裙覆盖上下装",
            "胸罩/内裤",
            "临时脱衣或裸体不要调用",
            "场景结束后角色会恢复原着装",
            "无法判断不要编造",
        ):
            self.assertIn(required, text)

    def test_chat_prompt_history_is_checkpoint_anchored_not_sliding(self):
        """前缀缓存不变量：checkpoint 之间的 prompt 历史只追加，不按 keep 滑动。"""
        svc = self.make_service()
        svc.config["checkpoint_keep_message_limit"] = "2"
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        history = [
            {"role": "user", "content": "用户消息 0"},
            {"role": "assistant", "content": "角色回复 0"},
            {"role": "user", "content": "用户消息 1"},
            {"role": "assistant", "content": "角色回复 1"},
            {"role": "user", "content": "用户消息 2"},
            {"role": "assistant", "content": "角色回复 2"},
        ]
        session_schema.set_chat_history(state, history)

        messages = svc._build_chat_messages(sid, "继续")
        contents = [m.get("content") for m in messages]

        for item in history:
            self.assertIn(item["content"], contents)
        history_start = next(i for i, msg in enumerate(messages) if msg.get("content") == history[0]["content"])
        for offset, item in enumerate(history):
            self.assertEqual(messages[history_start + offset]["role"], item["role"])
            self.assertEqual(messages[history_start + offset]["content"], item["content"])

    def test_chat_prompt_history_strips_legacy_current_input_marker(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        session_schema.set_chat_history(state, [
            {
                "role": "user",
                "content": "【引用内容】\n回复的机器人消息: 旧回复\n\n【用户当前输入】\n这句是什么意思？",
            },
            {"role": "assistant", "content": "解释一下。"},
        ])

        messages = svc._build_chat_messages(sid, "继续")
        user_history = [m["content"] for m in messages if m.get("role") == "user"]

        self.assertIn("【引用内容】\n回复的机器人消息: 旧回复\n\n这句是什么意思？", user_history)
        self.assertNotIn("【用户当前输入】", "\n".join(user_history))

    def test_format_store_messages_strips_current_input_marker_for_checkpoint(self):
        svc = self.make_service()
        text = svc._format_store_messages([
            {"role": "user", "content": "【用户当前输入】\n看这个"},
            {"role": "assistant", "content": "看到了。"},
        ])

        self.assertIn("User: 看这个", text)
        self.assertIn("Assistant: 看到了。", text)
        self.assertNotIn("【用户当前输入】", text)

    def test_low_frequency_chat_controls_stay_before_history_not_dynamic(self):
        """前缀缓存不变量：配置型控制放稳定层；发图/照片策略写进 static。"""
        svc = self.make_service()
        svc.config["chat_reply_length"] = "简短"
        svc.config["selfie_frequency"] = "偶尔"
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["purity"] = 8
        session_schema.set_replying_to_selfie(state, True)
        session_schema.set_chat_history(state, [
            {"role": "user", "content": "用户消息 0"},
            {"role": "assistant", "content": "角色回复 0"},
        ])

        messages = svc._build_chat_messages(sid, "继续")
        history_start = next(i for i, msg in enumerate(messages) if msg.get("content") == "用户消息 0")
        stable = "\n".join(m["content"] for m in messages[1:history_start] if m.get("role") == "system")
        dynamic = messages[-2]["content"]

        self.assertIn("照片历史规则", messages[0]["content"])
        self.assertIn("发图节奏规则", messages[0]["content"])
        self.assertIn("对话控制", stable)
        self.assertIn("纯度指令", stable)
        self.assertIn("发图频率", stable)
        self.assertIn("回复长度", stable)
        self.assertNotIn("纯度指令", dynamic)
        self.assertNotIn("回复长度", dynamic)
        self.assertNotIn("发图频率", dynamic)
        self.assertNotIn("你刚向用户发了一张图", dynamic)
        self.assertFalse(session_schema.get_replying_to_selfie(state))

        session_schema.set_rounds_since_image(state, 99)
        nudged = svc._build_chat_messages(sid, "继续")
        self.assertIn("发图节奏规则", nudged[0]["content"])
        self.assertIn("发图提醒", nudged[-2]["content"])
        self.assertNotIn("发图提醒", "\n".join(m.get("content", "") for m in nudged[:-2]))

    def test_semistable_visual_state_is_between_durable_context_and_checkpoint(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "小雨",
            "custom_positive_prefix": "black hair, blue eyes",
            "dynamic_appearance": "red dress",
            "checkpoint_summary": "旧场景摘要",
        })
        session_schema.set_character_history_summary(state, "长期关系阶段")
        svc._long_term_memory_context = lambda session_id, query="": "重要记忆"
        session_schema.set_chat_history(state, [
            {"role": "user", "content": "用户消息 0"},
            {"role": "assistant", "content": "角色回复 0"},
        ])

        messages = svc._build_chat_messages(sid, "继续")
        history_summary_i = next(i for i, msg in enumerate(messages) if "长期关系阶段" in msg.get("content", ""))
        memory_i = next(i for i, msg in enumerate(messages) if "重要记忆" in msg.get("content", ""))
        visual_i = next(i for i, msg in enumerate(messages) if "当前可见外型与配饰" in msg.get("content", ""))
        checkpoint_i = next(i for i, msg in enumerate(messages) if "旧场景摘要" in msg.get("content", ""))
        history_i = next(i for i, msg in enumerate(messages) if msg.get("content") == "用户消息 0")

        self.assertLess(history_summary_i, visual_i)
        self.assertLess(memory_i, visual_i)
        self.assertLess(visual_i, checkpoint_i)
        self.assertLess(checkpoint_i, history_i)
        self.assertIn("red dress", messages[visual_i]["content"])

    def test_importance_memory_context_stays_in_stable_prefix(self):
        """前缀缓存不变量：按重要性选取的长期记忆属于历史前稳定上下文。"""
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        session_schema.set_chat_history(state, [
            {"role": "user", "content": "用户消息 0"},
            {"role": "assistant", "content": "角色回复 0"},
        ])
        svc._long_term_memory_context = lambda session_id, query="": "重要记忆"

        messages = svc._build_chat_messages(sid, "继续")

        memory_i = next(i for i, msg in enumerate(messages) if "重要记忆" in msg.get("content", ""))
        history_i = next(i for i, msg in enumerate(messages) if msg.get("content") == "用户消息 0")
        self.assertEqual(messages[memory_i]["role"], "system")
        self.assertLess(memory_i, history_i)
        self.assertEqual(messages[history_i]["role"], "user")
        self.assertEqual(messages[history_i + 1]["role"], "assistant")
        self.assertEqual(messages[history_i + 1]["content"], "角色回复 0")

    def test_character_own_hair_eyes_win_over_default(self):
        """角色 base 的发/瞳优先于会话/全局默认——根治"刻晴紫发被画成黑发、webui 改不掉"。"""
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "keqing",
            "custom_series": "Genshin Impact",
            "custom_positive_prefix": "purple hair, long hair, purple eyes, fair skin",
            # 用户曾在菜单设过的"默认发/瞳"（旧逻辑会覆盖一切角色）
            "custom_default_hair": "black hair",
            "custom_default_eyes": "brown eyes",
        })
        eff = svc._effective_visual_prompt_tags(sid)
        self.assertIn("purple hair", eff)
        self.assertIn("purple eyes", eff)
        self.assertNotIn("black hair", eff)   # 默认不再覆盖角色自己的发色
        self.assertNotIn("brown eyes", eff)

    def test_default_hair_eyes_only_fill_when_character_lacks(self):
        """角色没写发/瞳时，会话默认才兜底补上（是兜底不是覆盖）。"""
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state.update({
            "custom_character": "someone",
            "custom_positive_prefix": "fair skin, slim figure",  # 无发/瞳
            "custom_default_hair": "silver hair",
            "custom_default_eyes": "red eyes",
        })
        eff = svc._effective_visual_prompt_tags(sid)
        self.assertIn("silver hair", eff)
        self.assertIn("red eyes", eff)

    def test_appearance_hair_command_edits_character_not_global(self):
        """/外型 发色 对已设角色写进衣柜 wardrobe hair 槽，不写 base 也不写全局默认。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "keqing",
                "custom_positive_prefix": "purple hair, purple eyes, fair skin",
            })
            svc._save_session_state(sid, state)
            await svc.cmd_appearance(1, sid, "发色 silver hair")
            after = svc._get_session_state(sid)
            # base 不受影响——发色是可变特征，走衣柜
            self.assertIn("purple hair", after["custom_positive_prefix"])
            # silver hair 进衣柜 hair 槽 + 角色卡 outfit 字段
            w = svc._get_wardrobe(after)
            self.assertEqual(w.get("hair", "").strip(), "silver hair")
            self.assertEqual(after.get("custom_default_hair", ""), "")
            card = after["saved_characters"]["keqing"]
            self.assertIn("silver hair", card.get("outfit", ""))
            self.assertIn("purple hair", card["appearance"])  # base 不动
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
        all_sys = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        self.assertIn("长期记忆", all_sys)
        self.assertIn("温柔安抚式回复", all_sys)

    def test_long_memory_hit_count_does_not_change_retrieval_order(self):
        svc = self.make_service()
        sid = "telegram:123"
        old_id = svc.memory.add_memory(sid, "event", "共同关键词 旧记忆", importance=3)
        time.sleep(0.01)
        new_id = svc.memory.add_memory(sid, "event", "共同关键词 新记忆", importance=3)

        before = svc.memory.context_memories(sid, "共同关键词", limit=2, stable_limit=0)
        self.assertEqual(int(before[0]["id"]), int(new_id))

        for _ in range(10):
            svc.memory.mark_used([int(old_id)])

        after = svc.memory.context_memories(sid, "共同关键词", limit=2, stable_limit=0)
        self.assertEqual(int(after[0]["id"]), int(new_id))

    def test_long_memory_context_uses_importance_not_query_match(self):
        svc = self.make_service()
        sid = "telegram:123"
        svc.memory.add_memory(sid, "event", "普通但重要的关系事实", importance=5)
        svc.memory.add_memory(sid, "event", "共同关键词 低重要事件", importance=1)

        context = svc._long_term_memory_context(sid, "共同关键词", limit=1)

        self.assertIn("普通但重要的关系事实", context)
        self.assertNotIn("低重要事件", context)

    def test_long_memory_context_does_not_maintain_hit_count(self):
        svc = self.make_service()
        sid = "telegram:123"
        memory_id = svc.memory.add_memory(sid, "event", "稳定记忆", importance=5)

        svc._long_term_memory_context(sid, "稳定记忆", limit=1)

        memory = next(m for m in svc.memory.list_memories(sid, limit=10) if int(m["id"]) == int(memory_id))
        self.assertEqual(memory.get("hit_count"), 0)

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
            session_schema.set_outfit(state, "black camisole dress")
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

        svc._apply_llm_user_location(sid, user_location="mall", co_located=False)
        messages = svc._build_chat_messages(sid, "晚上在哪见？")
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        semistable = next(m["content"] for m in messages if "世界状态规则" in m.get("content", ""))
        dynamic = next(m["content"] for m in messages if "本轮动线与位置动态" in m.get("content", ""))

        self.assertIn("世界状态规则", system)
        self.assertIn("本轮动线与位置动态", system)
        self.assertIn("角色当前所在", system)
        self.assertIn("接下来动线", system)
        self.assertIn("商场", system)
        self.assertIn("基础场所目录", system)
        self.assertNotIn("角色当前所在", semistable)
        self.assertNotIn("接下来动线", semistable)
        self.assertIn("角色当前所在", dynamic)
        self.assertIn("接下来动线", dynamic)

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
        system = "\n".join(m["content"] for m in svc._build_chat_messages(sid, "那我现在过去") if m.get("role") == "system")
        self.assertIn("世界状态规则", system)
        self.assertIn("本轮动线与位置动态", system)
        self.assertNotIn("角色当前所在", system)
        self.assertNotIn("空间关系判断", system)
        self.assertIn("以对话为准", system)
        # 冷启动普通寒暄也保留固定世界槽位；只不展开本轮动线动态。
        state["chat_history"] = []
        cold = "\n".join(m["content"] for m in svc._build_chat_messages(sid, "你好") if m.get("role") == "system")
        self.assertIn("世界状态规则", cold)
        self.assertNotIn("本轮动线与位置动态", cold)
        self.assertNotIn("角色当前所在", cold)
        greeting = "\n".join(m["content"] for m in svc._build_chat_messages(sid, "晚上好") if m.get("role") == "system")
        self.assertIn("世界状态规则", greeting)
        # 但用户本轮确实问地点/见面时仍然展开世界上下文
        relevant = "\n".join(m["content"] for m in svc._build_chat_messages(sid, "晚上在哪见？") if m.get("role") == "system")
        self.assertIn("世界状态规则", relevant)
        self.assertIn("角色当前所在", relevant)

    def test_world_semistable_keeps_fixed_prefix_slot_when_dynamic_triggers(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now
        svc._apply_llm_user_location(sid, user_location="mall", co_located=False)

        ordinary = svc._build_chat_messages(sid, "\u4f60\u597d")
        relevant = svc._build_chat_messages(sid, "\u665a\u4e0a\u5728\u54ea\u89c1\uff1f")
        world_marker = "\u4e16\u754c\u72b6\u6001\u89c4\u5219"
        dynamic_marker = "\u672c\u8f6e\u52a8\u7ebf\u4e0e\u4f4d\u7f6e\u52a8\u6001"

        def marker_index(messages, marker):
            return next(index for index, message in enumerate(messages) if marker in message.get("content", ""))

        ordinary_world_index = marker_index(ordinary, world_marker)
        relevant_world_index = marker_index(relevant, world_marker)

        self.assertEqual(ordinary_world_index, relevant_world_index)
        self.assertEqual(ordinary[ordinary_world_index]["role"], "system")
        self.assertEqual(ordinary[ordinary_world_index]["content"], relevant[relevant_world_index]["content"])
        self.assertNotIn(dynamic_marker, ordinary[-2]["content"])
        self.assertIn(dynamic_marker, relevant[-2]["content"])

    def test_character_place_autoextract_overrides_clock(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x"})
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)  # 工作日办公时段
            svc._session_now = lambda session_id="": fixed_now
            state = svc._get_session_state(sid)
            state["custom_character_age_stage"] = "adult"
            state["custom_character_day_anchor"] = "company"
            # 时钟此刻判公司
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "company")
            # mock LLM 返回 home → 自动抽取并持久化 → 压过时钟
            svc._call_llm = AsyncMock(return_value='{"place":"home"}')
            self.assertTrue(await svc._update_character_place_from_text(sid, "我在家呢，刚到客厅"))
            self.assertEqual(session_schema.get_character_place(state), "home")
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "home")
        asyncio.run(run())

    def test_character_place_autoextract_skips_plain_reply(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x"})
            sid = "telegram:123"
            svc._call_llm = AsyncMock(return_value=json.dumps({"place": "home", "place_name": ""}, ensure_ascii=False))

            self.assertFalse(await svc._update_character_place_from_text(sid, "嗯嗯，听着呢。"))
            svc._call_llm.assert_not_awaited()

        asyncio.run(run())

    def test_tool_update_location_sets_and_pins_character_place(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            msg = await svc.tool_update_location(sid, "楼下的咖啡店")
            self.assertIn("咖啡", msg)
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_place(state), "cafe")
            self.assertEqual(session_schema.get_character_place_confidence(state), 0.95)
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "cafe")

        asyncio.run(run())

    def test_tool_update_location_preserves_specific_place_name(self):
        """显式说"去上海海军博物馆"应钉到 museum 类别，并保留完整地名作显示名（而非目录里随便一家馆）。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            await svc.tool_update_location(sid, "上海海军博物馆")
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_place(state), "museum")
            self.assertEqual(session_schema.get_character_place_name(state), "上海海军博物馆")
            cp = svc.build_world_state(sid, weather=None)["character_place"]
            self.assertEqual(cp["key"], "museum")
            self.assertEqual(cp["name"], "上海海军博物馆")  # 显示这一家，而非 PLACE_TYPES 示例

        asyncio.run(run())

    def test_llm_extract_preserves_specific_place_name(self):
        """LLM 从角色回复抽取地点时，带出的具体地名也被保留为显示名。"""
        async def run():
            svc = self.make_service()
            svc.config.update({"image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x"})
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            svc._call_llm = AsyncMock(return_value='{"place":"museum","place_name":"上海海军博物馆"}')
            self.assertTrue(await svc._update_character_place_from_text(sid, "我现在到上海海军博物馆啦"))
            cp = svc.build_world_state(sid, weather=None)["character_place"]
            self.assertEqual(cp["key"], "museum")
            self.assertEqual(cp["name"], "上海海军博物馆")

        asyncio.run(run())

    def test_character_place_expires_after_ttl(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x"})
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            state = svc._get_session_state(sid)
            state["custom_character_age_stage"] = "adult"
            state["custom_character_day_anchor"] = "company"
            svc._call_llm = AsyncMock(return_value='{"place":"home"}')
            self.assertTrue(await svc._update_character_place_from_text(sid, "我在家"))
            self.assertEqual(session_schema.get_character_place(state), "home")
            session_schema.set_character_place_updated_at(state, 1.0)  # 远早于 TTL → 过期
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "company")
        asyncio.run(run())

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

    def test_autoextract_skips_anchor_place_mismatching_identity(self):
        """主妇/自由职业角色随口提"上班/公司"不应被自动钉到公司（仍回落时钟动线）。"""
        async def run():
            svc = self.make_service()
            svc.config.update({"image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x"})
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 23, 30, tzinfo=timezone.utc)  # 工作日深夜
            svc._session_now = lambda session_id="": fixed_now
            state = svc._get_session_state(sid)
            state["custom_character_age_stage"] = "adult"
            state["custom_character_day_anchor"] = "home"  # 无固定职场
            # LLM 返回 company 但身份不符 → 不钉位
            svc._call_llm = AsyncMock(return_value='{"place":"company"}')
            self.assertFalse(await svc._update_character_place_from_text(sid, "今天上班累死了，刚到公司楼下"))
            self.assertEqual(session_schema.get_character_place(state), "")
            # 深夜动线仍回落到家，而不是公司（商务中心）
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "home")
            # 对照：上班族角色提到公司则可被钉位（办公时段）
            state["custom_character_day_anchor"] = "company"
            day_now = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": day_now
            self.assertTrue(await svc._update_character_place_from_text(sid, "还在公司加班"))
            self.assertEqual(session_schema.get_character_place(state), "company")
        asyncio.run(run())

    def test_autoextract_anchor_pin_does_not_persist_into_night(self):
        """上班族傍晚被自动钉到公司，深夜应回落到家，而非整夜停在公司。"""
        async def run():
            svc = self.make_service()
            svc.config.update({"image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x"})
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            state["custom_character_age_stage"] = "adult"
            state["custom_character_day_anchor"] = "company"
            # 傍晚办公时段：自动抽取钉到公司，且当下覆盖时钟生效
            evening = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": evening
            svc._call_llm = AsyncMock(return_value='{"place":"company"}')
            self.assertTrue(await svc._update_character_place_from_text(sid, "还在公司"))
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "company")
            # 同一条持久位置仍在 TTL 内，但到了深夜（时钟判家），低置信锚定职场不再覆盖时钟
            night = datetime(2026, 6, 18, 23, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": night
            self.assertEqual(session_schema.get_character_place(state), "company")  # 持久字段未清，仍新鲜
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "home")
        asyncio.run(run())

    def test_tool_anchor_pin_respected_even_at_night(self):
        """显式 tool_update_location（0.95）声明在公司，深夜也尊重剧情、不被时段规则压回家。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            state["custom_character_age_stage"] = "adult"
            state["custom_character_day_anchor"] = "company"
            night = datetime(2026, 6, 18, 23, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": night
            await svc.tool_update_location(sid, "公司")
            self.assertEqual(session_schema.get_character_place_confidence(state), 0.95)
            self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "company")

        asyncio.run(run())

    def test_new_leisure_place_categories_extract_and_pin(self):
        """新增的休闲/文化类目（博物馆等）能识别并钉位——覆盖'角色出现在上海海军博物馆'诉求。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 14, 0, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            # 模型显式声明在海军博物馆（tool_update_location 走正则识别）→ 钉位为 museum
            msg = await svc.tool_update_location(sid, "上海海军博物馆")
            self.assertIn("博物馆", msg)
            world = svc.build_world_state(sid, weather=None)
            self.assertEqual(world["character_place"]["key"], "museum")
            self.assertEqual(world["character_place"]["label"], "博物馆")

        asyncio.run(run())

    def test_amap_poi_catalog_used_for_china_city(self):
        """LLM 判为中国城市时，目录用高德真实 POI，动线取名用它。"""
        async def run():
            svc = self.make_service()
            svc.config["amap_api_key"] = "test-key"
            svc.config["amap_poi_enabled"] = True
            svc._classify_city_region = AsyncMock(return_value="china")
            sample = {
                "museum": ["上海海军博物馆", "上海博物馆"],
                "park": ["人民公园", "复兴公园"],
            }
            svc._fetch_amap_places = AsyncMock(return_value=sample)
            result = await svc._ensure_city_place_catalog("上海", force=True)
            self.assertEqual(result["status"], "amap")
            self.assertIn("上海海军博物馆", result["places"]["museum"])
            self.assertEqual(svc._place_example("上海", "museum", 0), "上海海军博物馆")
            cat = svc.city_place_catalogs[svc._city_catalog_key("上海")]
            self.assertEqual(cat["source"], "amap")

        asyncio.run(run())

    def test_amap_falls_back_when_no_poi(self):
        """中国城市高德无结果、无谷歌、无 image LLM 时回落 basic（位置系统仍有内置示例兜底）。"""
        async def run():
            svc = self.make_service()
            svc.config["amap_api_key"] = "test-key"
            # 清空模型 profile，使 has_llm_config("image") 返回 False，验证 basic 回落
            svc.config["global_model_profiles"] = {}
            svc.config["default_fast_model_profile"] = ""
            svc.config["default_chat_model_profile"] = ""
            svc.config["llm_api_key"] = ""
            svc._classify_city_region = AsyncMock(return_value="china")
            svc._fetch_amap_places = AsyncMock(return_value={})
            result = await svc._ensure_city_place_catalog("某无POI小城", force=True)
            self.assertEqual(result["status"], "basic")

        asyncio.run(run())

    def test_overseas_city_uses_google_and_never_amap(self):
        """LLM 判为海外时只用谷歌，绝不调用高德（防同名中国地点污染目录）。"""
        async def run():
            svc = self.make_service()
            svc.config["amap_api_key"] = "ak"
            svc.config["google_places_api_key"] = "gk"
            svc._classify_city_region = AsyncMock(return_value="overseas")
            svc._fetch_amap_places = AsyncMock(return_value={"museum": ["错误的中国馆"]})
            svc._fetch_google_places = AsyncMock(return_value={
                "museum": ["Kobe Maritime Museum"], "park": ["Sorakuen Garden"],
            })
            result = await svc._ensure_city_place_catalog("神户", force=True)
            self.assertEqual(result["status"], "google")
            self.assertIn("Kobe Maritime Museum", result["places"]["museum"])
            svc._fetch_amap_places.assert_not_awaited()  # 海外绝不碰高德
            self.assertEqual(svc.city_place_catalogs[svc._city_catalog_key("神户")]["source"], "google")

        asyncio.run(run())

    def test_china_city_prefers_amap_over_google(self):
        """中国城市高德有结果时优先高德，不调用谷歌。"""
        async def run():
            svc = self.make_service()
            svc.config["amap_api_key"] = "ak"
            svc.config["google_places_api_key"] = "gk"
            svc._classify_city_region = AsyncMock(return_value="china")
            svc._fetch_amap_places = AsyncMock(return_value={"museum": ["上海博物馆"]})
            svc._fetch_google_places = AsyncMock(return_value={"museum": ["should-not-be-used"]})
            result = await svc._ensure_city_place_catalog("上海", force=True)
            self.assertEqual(result["status"], "amap")
            self.assertEqual(result["places"]["museum"], ["上海博物馆"])
            svc._fetch_google_places.assert_not_awaited()

        asyncio.run(run())

    def test_persisted_place_not_applied_to_forecast_hours(self):
        """持久 pin 是'此刻'的位置，按指定钟点预测一整天时不应套用，否则整天被钉成同一地点。"""
        svc = self.make_service()
        sid = "telegram:123"
        day = datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)  # 工作日办公时段
        svc._session_now = lambda session_id="": day
        state = svc._get_session_state(sid)
        state["custom_character_age_stage"] = "adult"
        state["custom_character_day_anchor"] = "company"
        # 工具显式钉到咖啡店（高置信，正常应覆盖此刻时钟）
        svc._set_character_place(sid, "cafe", "楼下咖啡店", 0.95)
        # 此刻：采用持久 pin
        self.assertEqual(svc.build_world_state(sid, weather=None)["character_place"]["key"], "cafe")
        # 预测深夜：apply_persisted_place=False → 回落纯时钟动线（家），而不是被 pin 钉成咖啡店
        night = day.replace(hour=23)
        forecast = svc.build_world_state(sid, weather=None, now=night, apply_persisted_place=False)
        self.assertEqual(forecast["character_place"]["key"], "home")
        # 预测办公时段：同样走纯职业动线（公司），不受 pin 影响
        work = svc.build_world_state(sid, weather=None, now=day, apply_persisted_place=False)
        self.assertEqual(work["character_place"]["key"], "company")

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

            scene, caption, _, view, _ = await svc._llm_write_scene(
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
            # plan_roleplay_image handles system prompt internally; verify _call_llm was used.
            svc._call_llm.assert_awaited()

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
            # plan_roleplay_image handles continuity internally; verify _call_llm was called.
            svc._call_llm.assert_awaited()

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
            session_schema.set_outfit(state, "black hoodie")
            await svc._sched_fire(sid, fixed_now, mode_override="normal", skip_active_check=True)

            world_logs = [text for kind, text in logs if kind == "WORLD"]
            self.assertTrue(world_logs)
            self.assertIn("profile=成年·上班族", world_logs[0])
            self.assertIn("current=公司", world_logs[0])
            svc._do_generate.assert_awaited_once_with(
                "english prompt",
                is_ntr=False,
                session_id=sid,
                one_shot_appearance="white dress",
                orientation="2:3",
            )
            self.assertEqual(session_schema.get_outfit(state), "black hoodie")
            svc.send_photo.assert_awaited_once()

        asyncio.run(run())

    def test_scheduled_push_returns_false_when_send_photo_fails(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc)
            logs = []
            svc._ulog = lambda session_id, kind, text: logs.append((kind, text))
            svc._fetch_weather = AsyncMock(return_value={"desc": "sunny", "temp": "22", "code": "113"})
            svc._llm_write_scene = AsyncMock(return_value=("window selfie", "caption", "", "selfie", "2:3"))
            svc._translate_to_tags = AsyncMock(return_value="english prompt")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_photo = AsyncMock(side_effect=RuntimeError("telegram down"))

            ok = await svc._sched_fire(sid, fixed_now, mode_override="normal", skip_active_check=True)

            self.assertFalse(ok)
            self.assertTrue(any(kind == "PUSH" and "telegram down" in text for kind, text in logs))
            self.assertEqual(session_schema.get_sent_photos_history(svc._get_session_state(sid)), [])

        asyncio.run(run())

    def test_scheduled_push_task_marks_trigger_only_after_success(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            fixed_now = datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc)
            svc._ulog = lambda session_id, kind, text: None

            svc._sched_fire = AsyncMock(return_value=False)
            task = svc._create_scheduled_push_task(sid, fixed_now, mode_override="normal", trigger_time="10:30")
            await task
            self.assertNotIn("10:30", session_schema.get_daily_triggered_times(state))

            svc._sched_fire = AsyncMock(return_value=True)
            task = svc._create_scheduled_push_task(sid, fixed_now, mode_override="normal", trigger_time="10:30")
            await task
            self.assertIn("10:30", session_schema.get_daily_triggered_times(state))

        asyncio.run(run())

    def test_background_roleplay_image_logs_exception(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            logs = []
            svc._ulog = lambda session_id, kind, text: logs.append((kind, text))
            svc.tool_generate_image = AsyncMock(side_effect=RuntimeError("image task failed"))

            await svc._run_background_roleplay_image(123, sid, intent="auto image")

            self.assertTrue(any(kind == "ERROR" and "image task failed" in text for kind, text in logs))

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
            {"role": "system", "content": "照片历史（系统记录，不应混进对话上下文）"},
            {"role": "user", "content": "聊聊晚饭吃什么"},
            {"role": "assistant", "content": "今晚可以做点清淡的。"},
        ])

        messages = svc._build_chat_messages(sid, "继续说晚饭")
        all_sys = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        packed = "\n".join(m.get("content", "") for m in messages)

        self.assertIn("短期注意规则", all_sys)
        self.assertIn("聊聊晚饭吃什么", packed)
        self.assertNotIn("卧室窗边", packed)

    def test_new_scene_command_checkpoints_then_clears_prompt_history_and_keeps_dream_source(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            key = svc._context_character_key(sid)
            svc.send_message = AsyncMock()
            old_messages = [
                {"role": "user", "content": "上一幕还在卧室窗边等我"},
                {"role": "assistant", "content": "我靠在卧室窗边看着你。"},
            ]
            ids = svc.app_store.append_messages(sid, key, old_messages)
            state = svc._get_session_state(sid)
            session_schema.set_chat_history(state, old_messages)
            svc.app_store.upsert_checkpoint(sid, key, "更早 checkpoint：刚进卧室", 0)
            session_schema.set_checkpoint_summary(state, "更早 checkpoint：刚进卧室")
            session_schema.set_checkpoint_message_id(state, 0)
            captured_checkpoint = {}

            async def fake_summarize(session_id_arg, previous, msgs):
                captured_checkpoint["session_id"] = session_id_arg
                captured_checkpoint["previous"] = previous
                captured_checkpoint["messages"] = list(msgs)
                return "切换前 checkpoint：卧室窗边未完成拥抱"

            svc._summarize_checkpoint = fake_summarize
            svc._extract_long_term_memories_from_messages = AsyncMock()

            await svc.cmd_new_scene(1, sid, "")

            state = svc._get_session_state(sid)
            self.assertEqual(captured_checkpoint["session_id"], sid)
            self.assertIn("更早 checkpoint", captured_checkpoint["previous"])
            self.assertEqual([m.get("content") for m in captured_checkpoint["messages"]], [
                "上一幕还在卧室窗边等我",
                "我靠在卧室窗边看着你。",
            ])
            svc._extract_long_term_memories_from_messages.assert_awaited_once()
            self.assertEqual(session_schema.get_chat_history(state), [])
            self.assertEqual(session_schema.get_checkpoint_summary(state), "")
            self.assertEqual(session_schema.get_checkpoint_message_id(state), ids[-1])
            cp = svc.app_store.get_checkpoint(sid, key)
            self.assertEqual(cp.get("summary"), "")
            self.assertEqual(int(cp.get("source_until_id") or 0), ids[-1])

            messages = svc._build_chat_messages(sid, "新场景聊晚饭")
            packed = "\n".join(m.get("content", "") for m in messages)
            self.assertIn("短期注意规则", packed)
            self.assertIn("新场景聊晚饭", packed)
            self.assertNotIn("卧室窗边", packed)
            self.assertNotIn("切换前 checkpoint", packed)

            captured = {}

            async def fake_write_dream_diary(session_id, diary_date, source_text, existing_diary="", *, reason=""):
                captured["source_text"] = source_text
                return "dream diary"

            svc._write_dream_diary = fake_write_dream_diary
            svc._organize_memories_after_dream = AsyncMock()
            svc._generate_character_history_summary = AsyncMock()

            await svc._dream_once(sid, key, datetime(2026, 6, 25, tzinfo=timezone.utc), reason="manual")

            self.assertIn("上一幕还在卧室窗边等我", captured.get("source_text", ""))
            self.assertIn("我靠在卧室窗边看着你。", captured.get("source_text", ""))

        asyncio.run(run())

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
            {"role": "system", "content": "照片历史（系统记录，不应混进对话上下文）"},
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
        self.assertNotIn("照片历史（系统记录", dialog)
        self.assertIn("厨房里准备晚饭", photos)
        self.assertNotIn("办公室灯下自拍", photos)

    def test_short_context_reset_demotes_character_place_keeps_user_place(self):
        """① 连续重置（B 方案）：SR 不硬清位置——character_place 降级为 weak（非清空、非 strong、非 None），
        user_place 完全不动（交给 4h TTL）。消除原先 SR 清 user 不清 character 的不对称。"""
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        # 新鲜的强 pin（对话刚确立）+ 用户自报位置
        svc._set_character_place(sid, "home", "在家", 0.95, source="tool")
        session_schema.set_user_place(state, key="mall", label="商场", updated_at=time.time(), confidence=0.85)
        self.assertEqual(svc._active_character_place(state)["authority"], "strong")

        svc._reset_short_context(state, "用户显式切换或结束上一话题/场景")

        active = svc._active_character_place(state)
        self.assertIsNotNone(active, "character_place 不应被清空（连续，非失忆）")
        self.assertEqual(active["key"], "home", "地点保留，仅降级")
        self.assertEqual(active["authority"], "weak", "降级为 weak：生图不再钉死，仅作背景")
        # user_place 原样保留（B 方案：换话题不代表用户物理移动）
        self.assertEqual(session_schema.get_user_place(state), "mall")
        self.assertEqual(session_schema.get_user_place_confidence(state), 0.85)

    def test_clear_conversation_context_clears_both_places(self):
        """① 硬重置对称：换角色/clearup 的 _clear_conversation_context 同时清空 character_place 和
        user_place（修复原先漏清 user_place、用户所在渗进新角色的不对称）。"""
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        svc._set_character_place(sid, "home", "在家", 0.95, source="tool")
        session_schema.set_user_place(state, key="mall", label="商场", updated_at=time.time(), confidence=0.85)
        session_schema.set_user_co_located(state, True)

        svc._clear_conversation_context(state)

        self.assertEqual(session_schema.get_character_place(state), "")
        self.assertEqual(session_schema.get_user_place(state), "")
        self.assertEqual(session_schema.get_user_place_updated_at(state), 0)
        self.assertEqual(session_schema.get_user_place_confidence(state), 0)
        self.assertFalse(session_schema.get_user_co_located(state))

    def test_within_primitive_semantics(self):
        """② 薄时效原语：年龄上限 + 可选 since 切点；ttl=None 表示只按 since 过滤。"""
        svc = self.make_service()
        now = time.time()
        self.assertTrue(svc._within(now - 10, 3600))          # 新鲜
        self.assertFalse(svc._within(now - 7200, 3600))       # 超 ttl
        self.assertFalse(svc._within(0, 3600))                # 无时间戳
        self.assertTrue(svc._within(now, None))               # 无 ttl 上限
        self.assertFalse(svc._within(now - 5, 3600, since=now))   # 早于 since 切点
        self.assertTrue(svc._within(now, 3600, since=now - 5))    # 晚于 since 切点

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
            self.assertIn("可以保留 she/the character", system_prompt)
            self.assertNotIn("不要输出自拍/POV/镜子/手机/主语", system_prompt)
            self.assertTrue(result.startswith("First-person POV from the user's viewpoint"))
            self.assertIn("She sits close on the edge of the bed", result)
            self.assertIn("black camisole dress", result)

        asyncio.run(run())

    def test_translate_to_tags_injects_current_weather(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._weather_caches[sid] = {
                "data": {"desc": "小雨", "temp": "18", "sunrise": "05:00", "sunset": "19:00"},
                "ts": time.time(),
            }
            svc._call_llm = AsyncMock(return_value="She waits by the rainy window. rainy window, wet street")

            await svc._translate_to_tags("在窗边等你", session_id=sid, view="selfie")

            system_prompt = svc._call_llm.await_args.args[0]
            self.assertIn("Current weather: 小雨 18 C", system_prompt)
            self.assertIn("Preserve visible weather", system_prompt)
            self.assertIn("wet surfaces", system_prompt)

        asyncio.run(run())

    def test_selfie_prompt_strips_phone_instead_of_forcing_mirror(self):
        svc = self.make_service()
        pos, neg = svc._build_prompt(
            "A selfie of a woman, solo, holding a smartphone in the bedroom, warm bedside lighting",
            session_id="telegram:123",
        )

        self.assertIn("selfie", pos.lower())
        self.assertIn("looking at viewer", pos.lower())
        # 真·自拍保留 selfie 取景，但绝不写 front-facing phone camera（手机 UI 框的来源）
        self.assertNotIn("phone camera", pos.lower())
        self.assertNotIn("front-facing", pos.lower())
        self.assertNotIn("smartphone", pos.lower())
        self.assertNotIn("mirror reflection", pos.lower())
        neg_tokens = {item.strip().lower() for item in neg.split(",")}
        self.assertNotIn("phone", neg_tokens)
        self.assertIn("visible phone", neg.lower())
        self.assertIn("holding phone", neg.lower())
        self.assertIn("mirror selfie", neg.lower())

    def test_selfie_prompt_removes_phone_screen_ui_without_breaking_sentence(self):
        svc = self.make_service()
        pos, _ = svc._build_prompt(
            "A selfie of a woman, solo, upper body framing, looking at viewer, "
            "a woman sits by the window, gazing at a phone screen with purple eyes gleaming, "
            "the phone screen lit showing a message interface countdown prompt, black dress, phone screen, countdown",
            session_id="telegram:123",
        )

        lower = pos.lower()
        self.assertIn("selfie", lower)
        self.assertIn("looking at viewer", lower)
        self.assertEqual(lower.count("looking at viewer"), 1)
        self.assertNotIn("phone camera", lower)
        self.assertNotIn("phone screen", lower)
        self.assertNotIn("message interface", lower)
        self.assertNotIn("countdown", lower)
        self.assertNotIn("gazing at a with", lower)
        self.assertNotIn("the lit", lower)

    def test_portrait_view_is_third_person_photo_without_phone(self):
        # portrait = 别人帮角色拍的照片：看向镜头、画面里只有角色、不出现手机本体或手机 UI；
        # 负向仍压制 camera frame / phone interface / selfie frame 等手机 UI 框元素。
        svc = self.make_service()
        pos, neg = svc._build_prompt(
            "A photo of a woman, solo, upper body framing, looking at viewer, "
            "posing for the camera, taken by someone else just out of frame, "
            "standing in the kitchen, holding a smartphone, warm daylight",
            session_id="telegram:123",
        )
        pos_lower = pos.lower()
        self.assertIn("looking at viewer", pos_lower)
        self.assertIn("taken by someone else just out of frame", pos_lower)
        self.assertNotIn("smartphone", pos_lower)
        self.assertNotIn("front-facing phone camera", pos_lower)
        neg_lower = neg.lower()
        self.assertIn("camera ui", neg_lower)
        self.assertIn("viewfinder", neg_lower)
        self.assertIn("shutter button", neg_lower)

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
        session_schema.set_outfit(state, "aris (blue archive), school uniform")
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
        svc._save_session_state(sid, state)

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
        saved_state = svc.app_store.load_session_state(sid)
        self.assertEqual(saved_state["custom_count"], "1boy")
        self.assertEqual(saved_state["custom_positive_prefix"], "short hair, blue eyes")

    def test_image_planner_normalizes_second_person_scene_subject(self):
        scene = normalize_scene_visual_subject("\u4f60\u8212\u8212\u670d\u670d\u5730\u7a9d\u5728\u5ba2\u5385\u6c99\u53d1\u91cc")

        self.assertTrue(scene.startswith("\u89d2\u8272"))

    def test_cmd_selfie_runs_scene_translate_generate_chain(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._llm_write_scene = AsyncMock(return_value=("坐在窗边看向镜头", "给你看一眼。", "", "selfie", "2:3"))
            svc._translate_to_tags = AsyncMock(return_value="english prompt")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            await svc.cmd_selfie(123, sid, "")

            svc._llm_write_scene.assert_awaited_once()
            svc._translate_to_tags.assert_awaited_once_with("坐在窗边看向镜头", session_id=sid, view="selfie")
            svc._do_generate.assert_awaited_once_with("english prompt", session_id=sid, one_shot_appearance="", orientation="2:3")
            svc.send_photo.assert_awaited_once_with(123, b"image", "给你看一眼。")

        asyncio.run(run())

    def test_cmd_selfie_uses_planned_appearance_once_without_persisting(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            session_schema.set_outfit(state, "black hoodie")
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._llm_write_scene = AsyncMock(return_value=("坐在窗边看向镜头", "给你看一眼。", "white dress", "selfie", "2:3"))
            svc._translate_to_tags = AsyncMock(return_value="english prompt")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            await svc.cmd_selfie(123, sid, "")

            svc._do_generate.assert_awaited_once_with(
                "english prompt",
                session_id=sid,
                one_shot_appearance="white dress",
                orientation="2:3",
            )
            self.assertEqual(session_schema.get_outfit(state), "black hoodie")
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
        self.assertIn("extra hands", neg.lower())
        self.assertIn("poorly drawn hands", neg.lower())
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
            # 有活跃对话时，动线只作背景，对话已确立的地点优先（防止配图把角色按现实时段"传送"）。
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
                clothing_off="",
                orientation="2:3",
            )
            # 聊天途中的配图不带配文（聊天模型已经在文字里回复了）
            svc.send_photo.assert_awaited_once_with(123, b"image", "")
            self.assertEqual(session_schema.get_outfit(state), "")
            self.assertEqual(state["sent_photos_history"][-1]["caption"], "")
            self.assertEqual(state["sent_photos_history"][-1]["appearance"], "black camisole dress")
            self.assertIn("用户想看角色下班后在家等自己的样子", state["sent_photos_history"][-1]["source_description"])
            self.assertIn("黑色吊带裙", state["sent_photos_history"][-1]["source_description"])

        asyncio.run(run())

    def test_live_chat_context_cache_probe_uses_current_config_when_available(self):
        if str(os.environ.get("SUCYUBOT_TEST_LIVE_CACHE_PROBE") or "").strip().lower() not in TRUE_ENV_VALUES:
            self.skipTest("真实前缀缓存请求测试默认跳过；设置 SUCYUBOT_TEST_LIVE_CACHE_PROBE=1 才运行")

        async def run():
            svc = self.make_service_from_current_config()
            try:
                sid = f"telegram:cache-probe-{int(time.time() * 1000)}"
                if not svc.has_llm_config("chat", sid):
                    self.skipTest("当前配置没有可用 chat 模型，跳过真实缓存命中测试")
                resolved = svc._resolved_llm_config("chat", sid)
                if str(resolved.get("api_key") or "").strip() in {"", "********"}:
                    self.skipTest("当前配置没有可用 chat API key，跳过真实缓存命中测试")

                profiles = svc.config.get("global_model_profiles") or {}
                if isinstance(profiles, dict):
                    for profile in profiles.values():
                        if isinstance(profile, dict):
                            profile["max_tokens"] = 96
                svc.config["chat_llm_max_tokens"] = "96"
                svc.config["context_window_message_limit"] = "30"
                svc.config["checkpoint_keep_message_limit"] = "10"
                svc.config["long_memory_extract_enabled"] = False
                svc.config["selfie_frequency"] = "关闭"

                fixed_now = datetime(2026, 6, 25, 19, 40, tzinfo=timezone.utc)
                svc._session_now = lambda session_id="": fixed_now
                state = svc._get_session_state(sid)
                suffix = str(int(time.time() * 1000))[-6:]
                state.update({
                    "custom_character": f"缓存测试角色{suffix}",
                    "custom_bot_name": f"澪{suffix}",
                    "custom_bot_self_name": "我",
                    "custom_role_name": "同城朋友",
                    "custom_scheduled_persona": "温柔、克制、回复简短，会自然承接用户话题。",
                    "custom_positive_prefix": "1girl, black short hair, blue eyes",
                    "custom_location": "上海",
                    "custom_character_age_stage": "adult",
                    "custom_character_day_anchor": "home",
                    "custom_spatial_relationship": "同城异地，偶尔线下见面",
                })
                session_schema.set_outfit(state, "white blouse, dark pleated skirt")
                session_schema.set_chat_history(state, [
                    {"role": "user", "content": "我刚下班，路上有点堵。"},
                    {"role": "assistant", "content": "那你慢慢来，我在家里把灯留着。"},
                    {"role": "user", "content": "你先别睡，等我一会儿。"},
                    {"role": "assistant", "content": "嗯，我会等你，但你别急。"},
                ])
                svc.app_store.upsert_checkpoint(
                    sid,
                    svc._context_character_key(sid),
                    "用户下班在路上，角色在家等他回来，情绪温柔安定；没有固定新地点。",
                    0,
                )
                svc.memory.add_memory(
                    sid,
                    "preference",
                    "用户喜欢角色回复简短自然，不要连续重复同一件事。",
                    character=svc._context_character_key(sid),
                    importance=5,
                    tags=["对话"],
                )

                questions = [
                    "我大概还有十分钟到。",
                    "你现在在客厅还是卧室？",
                    "到楼下了，电梯有点慢。",
                ]
                cached_counts: list[int] = []
                prompt_counts: list[int] = []
                replies: list[str] = []

                svc.send_action = AsyncMock()
                sent_messages: list[str] = []

                async def capture_send_message(chat_id, text, **kwargs):
                    sent_messages.append(str(text or ""))

                svc.send_message = AsyncMock(side_effect=capture_send_message)
                svc._update_character_place_from_text = AsyncMock(return_value=None)

                def latest_usage_id() -> int:
                    with closing(svc.app_store._connect()) as conn:
                        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM llm_usage").fetchone()
                        return int(row["max_id"] or 0)

                def chat_usage_after(after_id: int) -> dict[str, int]:
                    with closing(svc.app_store._connect()) as conn:
                        row = conn.execute(
                            """
                            SELECT prompt_tokens, cached_tokens
                            FROM llm_usage
                            WHERE id > ?
                              AND session_id = ?
                              AND purpose = 'chat'
                              AND tag = 'chat'
                            ORDER BY id ASC
                            LIMIT 1
                            """,
                            (after_id, sid),
                        ).fetchone()
                    if not row:
                        return {"prompt_tokens": 0, "cached_tokens": 0}
                    return {
                        "prompt_tokens": int(row["prompt_tokens"] or 0),
                        "cached_tokens": int(row["cached_tokens"] or 0),
                    }

                for text in questions:
                    before_usage_id = latest_usage_id()
                    before_sent = len(sent_messages)
                    await svc.handle_chat(123, sid, text)
                    await asyncio.sleep(0)
                    usage = chat_usage_after(before_usage_id)
                    prompt_counts.append(usage["prompt_tokens"])
                    cached_counts.append(usage["cached_tokens"])
                    self.assertGreater(len(sent_messages), before_sent, "handle_chat 应发送真实 AI 回复")
                    reply = sent_messages[-1].strip()
                    if "回复生成失败" in reply:
                        self.skipTest("真实 chat 模型未返回可用回复，跳过连续缓存探针")
                    replies.append(reply[:80])

                if not any(prompt_counts):
                    self.skipTest("模型返回中没有 prompt usage，无法判断缓存命中")
                rates = [
                    (cached / prompt if prompt else 0.0)
                    for cached, prompt in zip(cached_counts, prompt_counts)
                ]
                rate_text = ", ".join(
                    f"round{i + 1}={cached_counts[i]}/{prompt_counts[i]} ({rates[i]:.2%})"
                    for i in range(len(prompt_counts))
                )
                reply_text = " | ".join(f"round{i + 1} reply={replies[i]}" for i in range(len(replies)))
                print(f"\n真实配置缓存探针总结: {reply_text}; cache_hit_rates: {rate_text}")
                self.assertGreater(
                    max(cached_counts[1:] or [0]),
                    0,
                    f"连续真实请求未报告缓存命中: prompt={prompt_counts}, cached={cached_counts}",
                )
            finally:
                await svc.close()

        asyncio.run(run())

    def test_tool_generate_image_persists_explicit_accessory_removal_from_clothing_off(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            wardrobe = {
                "top": "oversized hoodie",
                "accessory": "shell bracelet, light blue round glasses",
            }
            session_schema.set_wardrobe(state, wardrobe)
            session_schema.set_outfit(state, appearance_rules.render_wardrobe(wardrobe))

            svc._translate_to_tags = AsyncMock(return_value="english tags")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            with patch("telegram_comfyui_selfie.service.plan_roleplay_image", AsyncMock(return_value={
                "scene": "She sits on the sofa and removes her light blue round glasses before looking up at the viewer.",
                "view": "pov",
                "clothing_off": "glasses",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
                "aspect_ratio": "2:3",
            })):
                result = await svc.tool_generate_image(
                    123,
                    sid,
                    intent="remove her glasses so the user can see her face",
                )

            self.assertIn("图片已生成并发送", result)
            outfit = session_schema.get_outfit(state)
            self.assertNotIn("light blue round glasses", outfit)
            self.assertIn("shell bracelet", outfit)
            self.assertNotIn("light blue round glasses", state["sent_photos_history"][-1]["appearance"])
            self.assertIn("shell bracelet", state["sent_photos_history"][-1]["appearance"])

        asyncio.run(run())

    def test_tool_generate_image_does_not_persist_outerwear_clothing_off(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            wardrobe = {
                "dress": "black silk slip dress",
                "outerwear": "cotton knit cardigan",
                "accessory": "light blue round glasses",
            }
            session_schema.set_wardrobe(state, wardrobe)
            session_schema.set_outfit(state, appearance_rules.render_wardrobe(wardrobe))

            svc._translate_to_tags = AsyncMock(return_value="english tags")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            with patch("telegram_comfyui_selfie.service.plan_roleplay_image", AsyncMock(return_value={
                "scene": "She slips off her cardigan and drapes it over the chair while leaning by the window in her slip dress.",
                "view": "pov",
                "clothing_off": "cardigan",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
                "aspect_ratio": "2:3",
            })):
                result = await svc.tool_generate_image(
                    123,
                    sid,
                    intent="把开衫脱掉搭在椅背上",
                )

            self.assertIn("图片已生成并发送", result)
            outfit = session_schema.get_outfit(state)
            self.assertIn("cotton knit cardigan", outfit)
            self.assertIn("black silk slip dress", outfit)
            self.assertIn("light blue round glasses", outfit)

        asyncio.run(run())

    def test_cmd_scene_image_uses_full_context_with_user_prompt_priority(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            state["chat_history"] = [
                {"role": "user", "content": "我们在车站等车，雨下得有点大"},
                {"role": "assistant", "content": "我把伞往你那边偏了一点。"},
            ]
            svc.tool_generate_image = AsyncMock(return_value="图片已生成并发送。画面: x")
            svc.send_message = AsyncMock()

            await svc.cmd_scene_image(123, sid, "低机位，只拍她握伞的手部特写")

            svc.tool_generate_image.assert_awaited_once()
            kwargs = svc.tool_generate_image.await_args.kwargs
            self.assertEqual(kwargs["planning_mode"], "illustration")
            self.assertIn("低机位", kwargs["prompt"])
            self.assertIn("手部特写", kwargs["must_include"])
            svc.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_scene_image_tool_passes_free_composition_to_planner_and_translator(self):
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
                {"role": "user", "content": "刚才我们在书店门口躲雨"},
                {"role": "assistant", "content": "我靠在玻璃门边看着雨。"},
            ]
            svc._fetch_weather = AsyncMock(return_value={"desc": "小雨", "temp": "18"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "低机位近景，只拍角色握着伞柄的手，背景是雨中的书店门口",
                "view": "third",
                "aspect_ratio": "3:2",
                "character_location": "bookstore",
                "user_location": "with_user",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
            }, ensure_ascii=False))
            svc._translate_to_tags = AsyncMock(return_value="english tags")
            svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
            svc.send_action = AsyncMock()
            svc.send_photo = AsyncMock()

            result = await svc.tool_generate_image(
                123,
                sid,
                intent="根据当前聊天场景配图，并优先满足用户输入的画面参数",
                prompt="低机位手部特写",
                must_include="低机位手部特写",
                planning_mode="illustration",
            )

            self.assertIn("图片已生成并发送", result)
            planner_system = svc._call_llm.await_args.args[0]
            planner_user = svc._call_llm.await_args.args[1]
            self.assertIn("短期连续性", planner_system)
            self.assertIn("用户本次 /配图 后输入", planner_system)
            self.assertIn("slot/外观/偏好只作为参考", planner_system)
            self.assertIn("刚才我们在书店门口躲雨", planner_user)
            self.assertIn("低机位手部特写", planner_user)
            self.assertNotIn("视角固定为 pov", planner_system)
            svc._translate_to_tags.assert_awaited_once_with(
                "低机位近景，只拍角色握着伞柄的手，背景是雨中的书店门口",
                session_id=sid,
                view="third",
                is_intimate=False,
                free_composition=True,
            )
            svc._do_generate.assert_awaited_once_with(
                "english tags",
                session_id=sid,
                one_shot_appearance="",
                is_intimate=False,
                partner_in_frame=False,
                device_in_frame=False,
                clothing_off="",
                orientation="3:2",
            )

        asyncio.run(run())

    def test_roleplay_image_planner_uses_slim_continuity_and_photo_summary(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            long_user = "我们刚从餐厅坐回卡座，" + ("filler " * 40) + "END_USER_MARKER"
            long_assistant = "我抬眼看了看你，指尖还停在倒计时界面上，" + ("detail " * 40) + "END_BOT_MARKER"
            state["chat_history"] = [
                {"role": "system", "content": "照片历史（系统记录，不应混进 roleplay-image-plan 连续性）"},
                {"role": "user", "content": long_user},
                {"role": "assistant", "content": long_assistant},
            ]
            state["sent_photos_history"] = [{
                "timestamp": time.time(),
                "scene": "restaurant booth smile " + ("scene " * 30) + "END_PHOTO_MARKER",
                "caption": "",
                "appearance": "light blue round glasses, shell bracelet",
                "source_description": "原始描述不该出现在瘦身后的图片摘要里",
                "view": "third",
            }]
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "A close third-person shot across a restaurant booth.",
                "view": "third",
                "aspect_ratio": "2:3",
                "character_location": "restaurant",
                "user_location": "with_user",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
            }, ensure_ascii=False))

            await plan_roleplay_image(svc, sid, intent="坐回座位戳脸倒计时")

            planner_user = svc._call_llm.await_args.args[1]
            self.assertIn("短期连续性:", planner_user)
            self.assertIn("最近已发图片摘要:", planner_user)
            self.assertNotIn("照片历史（系统记录", planner_user)
            self.assertNotIn("原始描述:", planner_user)
            self.assertNotIn("外貌:", planner_user)
            self.assertNotIn("END_USER_MARKER", planner_user)
            self.assertNotIn("END_BOT_MARKER", planner_user)
            self.assertNotIn("END_PHOTO_MARKER", planner_user)

        asyncio.run(run())

    def test_roleplay_image_planner_keeps_spatial_constraints_outside_slim_context(self):
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
                {"role": "user", "content": "过来，我来给你吹干"},
                {"role": "assistant", "content": (
                    "（眼睛一亮，抱着小鲸鱼玩偶从沙发上跳下来）来啦来啦~ "
                    + ("铺垫 " * 35)
                    + "乖乖坐在主人脚边，把后背朝向主人，湿漉漉的长发垂下来"
                )},
            ]
            svc._fetch_weather = AsyncMock(return_value={"desc": "小雨", "temp": "18"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "客厅里，角色抱着小鲸鱼玩偶，期待地看向主人。",
                "view": "pov",
                "aspect_ratio": "2:3",
                "character_location": "home",
                "user_location": "with_user",
                "is_intimate": False,
                "partner_in_frame": True,
                "device_in_frame": False,
            }, ensure_ascii=False))

            spatial = format_planning_spatial_context(
                svc,
                state,
                sid,
                intent="展示汐汐乖乖坐在主人脚边期待吹头发",
            )
            self.assertIn("脚边", spatial or "")
            plan = await plan_roleplay_image(svc, sid, intent="展示汐汐乖乖坐在主人脚边期待吹头发")

            planner_user = svc._call_llm.await_args.args[1]
            slim_block = planner_user.split("短期连续性:", 1)[1].split("空间/身体关系硬约束", 1)[0]
            self.assertNotIn("脚边", slim_block)
            self.assertIn("空间/身体关系硬约束", planner_user)
            self.assertIn("脚边", planner_user)
            self.assertIn("空间/身体关系硬约束", plan["scene"])
            self.assertIn("脚边", plan["scene"])

        asyncio.run(run())

    def test_illustration_planner_does_not_force_intimate_selfie_to_pov(self):
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
                "scene": "手机录像视角里只有角色和画面边缘的伴侣手臂",
                "view": "mirror",
                "is_intimate": True,
                "partner_in_frame": True,
                "device_in_frame": True,
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(
                svc,
                sid,
                mode="illustration",
                prompt="对镜录像，近距离拍身体局部",
            )

            self.assertEqual(plan["view"], "mirror")

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

    def test_roleplay_image_planner_same_space_selfie_without_device_prefers_third(self):
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
                "scene": "角色坐回餐厅卡座，抬眼朝你笑了一下，桌边放着她的手机",
                "view": "selfie",
                "new_appearance_tags": "",
                "user_location": "with_user",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(svc, sid, intent="她坐回座位，温柔地朝你看过来")

            self.assertEqual(plan["view"], "third")
            self.assertFalse(plan["device_in_frame"])

        asyncio.run(run())

    def test_roleplay_image_planner_same_space_help_take_photo_prefers_portrait(self):
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
                "scene": "角色站在窗边整理衣领，想留一张今天的全身照",
                "view": "selfie",
                "new_appearance_tags": "",
                "user_location": "with_user",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(svc, sid, intent="她让你帮她拍一张今天的照片")

            self.assertEqual(plan["view"], "portrait")
            self.assertFalse(plan["device_in_frame"])

        asyncio.run(run())

    def test_roleplay_image_planner_same_space_close_interaction_prefers_pov(self):
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
                "scene": "她俯身替你掖好肩头的薄毯，指尖轻轻碰到你的锁骨",
                "view": "selfie",
                "new_appearance_tags": "",
                "user_location": "with_user",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(svc, sid, intent="她俯身帮你掖好毯子")

            self.assertEqual(plan["view"], "pov")
            self.assertFalse(plan["device_in_frame"])

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
            self.assertEqual(session_schema.get_character_place(svc._get_session_state(sid)), "home")

        asyncio.run(run())

    def test_roleplay_image_planner_does_not_embed_animatool_schema(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_backend": "animatool",
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "A close pov shot across a restaurant table",
                "view": "pov",
                "character_location": "restaurant",
                "user_location": "with_user",
            }, ensure_ascii=False))

            await plan_roleplay_image(svc, sid, intent="坐在餐厅里看过来")

            system = svc._call_llm.await_args.args[0]
            self.assertIn('"scene"', system)
            self.assertNotIn("AnimaTool Turbo", system)
            self.assertNotIn("quality_meta_year_safe", system)
            self.assertNotIn("必填: quality_meta_year_safe", system)

        asyncio.run(run())

    def test_roleplay_image_planner_accepts_legacy_tags_without_losing_place(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_backend": "animatool",
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._set_character_place(sid, "restaurant", "餐厅", 0.8, name="Bistro.Bond")
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "aspect_ratio": "2:3",
                "quality_meta_year_safe": "masterpiece, best quality, highres, newest, year 2025, sensitive",
                "count": "1girl",
                "tags": (
                    "A girl is sitting back in a restaurant booth, looking at the viewer with a gentle smile. "
                    "Warm afternoon sunlight and cozy restaurant lighting create a tender atmosphere."
                ),
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(svc, sid, intent="坐回座位戳脸倒计时")

            self.assertIn("restaurant booth", plan["scene"])
            self.assertNotIn("坐回座位戳脸倒计时", plan["scene"])
            self.assertEqual(session_schema.get_character_place(svc._get_session_state(sid)), "restaurant")

        asyncio.run(run())

    def test_roleplay_image_planner_reinforces_strong_place_when_scene_is_generic(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc._set_character_place(sid, "restaurant", "餐厅", 0.8, name="Bistro.Bond")
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "A finger gently pokes your cheek in warm natural light.",
                "view": "pov",
                "character_location": "home",
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(svc, sid, intent="坐回座位戳脸倒计时")

            self.assertIn("inside Bistro.Bond", plan["scene"])
            self.assertIn("restaurant table", plan["scene"])
            self.assertEqual(session_schema.get_character_place(svc._get_session_state(sid)), "restaurant")

        asyncio.run(run())

    def test_roleplay_image_planner_prefers_passed_weather_data(self):
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
                "scene": "雨天餐厅窗边",
                "view": "selfie",
                "character_location": "restaurant",
            }, ensure_ascii=False))

            await plan_roleplay_image(
                svc,
                sid,
                intent="看看现在的样子",
                weather_data={"desc": "小雨", "temp": "18", "sunrise": "05:00", "sunset": "19:00"},
            )

            svc._fetch_weather.assert_not_awaited()
            system = svc._call_llm.await_args.args[0]
            user = svc._call_llm.await_args.args[1]
            self.assertIn("小雨 18 C", system)
            self.assertIn("当前天气: 小雨 18 C", user)
            self.assertNotIn("晴 22 C", system + user)

        asyncio.run(run())

    def test_animatool_slots_planner_injects_current_weather(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._weather_caches[sid] = {
                "data": {"desc": "小雨", "temp": "18", "sunrise": "05:00", "sunset": "19:00"},
                "ts": time.time(),
            }
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "quality_meta_year_safe": "masterpiece, best quality, highres, newest, year 2025, safe",
                "count": "1girl",
                "nltag": "A girl waits by a rainy restaurant window with wet pavement outside.",
            }, ensure_ascii=False))
            slots = PromptSlots(
                scene="A girl waits by the restaurant window.",
                quality="masterpiece",
                count="1girl",
                effective_appearance="school uniform",
                negative="bad hands",
            )
            schema = {
                "parameters": {
                    "properties": {
                        "quality_meta_year_safe": {"description": "quality and safety"},
                        "count": {"description": "count"},
                        "nltag": {"description": "natural language scene"},
                    },
                    "required": ["quality_meta_year_safe", "count", "nltag"],
                }
            }

            with patch("telegram_comfyui_selfie.image_planning._fetch_animatool_turbo_knowledge", new=AsyncMock(return_value={})), \
                 patch("telegram_comfyui_selfie.image_planning._fetch_animatool_turbo_schema", new=AsyncMock(return_value=schema)):
                payload = await plan_animatool_slots(svc, sid, slots)

            self.assertIsNotNone(payload)
            system_prompt = svc._call_llm.await_args.args[0]
            self.assertIn("当前天气: 小雨 18 C", system_prompt)
            self.assertIn("tags 必须自然体现当前天气", system_prompt)
            self.assertIn("不要输出 neg", system_prompt)
            self.assertIn("湿痕", system_prompt)
            self.assertNotIn("neg", payload)
            self.assertIn("nltag", payload)

        asyncio.run(run())

    def test_animatool_payload_drops_neg(self):
        svc = self.make_service()
        slots = PromptSlots(
            scene="A girl reads by the window.",
            quality="masterpiece",
            count="1girl",
            effective_appearance="school uniform",
            negative="bad hands",
        )
        schema = {
            "parameters": {
                "properties": {
                    "quality_meta_year_safe": {"description": "quality and safety"},
                    "count": {"description": "count"},
                    "nltag": {"description": "natural language scene"},
                },
                "required": ["quality_meta_year_safe", "count", "nltag"],
            }
        }

        payload = _build_animatool_turbo_payload(svc, slots, "positive prompt", "bad hands", 123, schema)

        self.assertNotIn("neg", payload)
        self.assertNotIn("negative", payload)
        self.assertIn("nltag", payload)

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
            self.assertEqual(session_schema.get_character_place(svc._get_session_state(sid)), "cafe")  # 规划器判断回写

        asyncio.run(run())

    def test_photo_history_is_recorded_as_stable_system_history(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        session_schema.set_chat_history(state, [
            {"role": "user", "content": "下班到家了吗"},
            {"role": "assistant", "content": "到了，在玄关。"},
        ])

        svc._record_sent_photo(
            sid,
            "站在玄关等用户回家",
            "快回来，我给你留了灯。",
            appearance="black dress",
            view="selfie",
            source_description="意图: 用户想看角色下班后在家等自己的样子；必须包含: 玄关灯",
        )

        history = session_schema.get_chat_history(svc._get_session_state(sid))
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "system"])
        injected = history[-1]["content"]
        self.assertIn("照片历史", injected)
        self.assertIn("站在玄关等用户回家", injected)
        self.assertIn("快回来，我给你留了灯。", injected)
        self.assertIn("用户想看角色下班后在家等自己的样子", injected)

        messages = svc._build_chat_messages(sid, "刚才那张照片很好看")
        contents = [m.get("content", "") for m in messages]
        self.assertIn(injected, contents)

    def test_photo_history_can_be_deferred_for_chat_tool_turn(self):
        svc = self.make_service()
        sid = "telegram:123"

        svc._record_sent_photo(
            sid,
            "坐在窗边向用户挥手",
            "我在这里等你。",
            appearance="black dress",
            view="selfie",
            source_description="用户想看当前场景",
            defer_history_message=True,
        )

        state = svc._get_session_state(sid)
        self.assertEqual(session_schema.get_chat_history(state), [])
        pending = svc._take_pending_photo_history_messages(sid)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["role"], "system")
        self.assertIn("照片历史", pending[0]["content"])
        self.assertIn("坐在窗边向用户挥手", pending[0]["content"])
        self.assertEqual(svc._take_pending_photo_history_messages(sid), [])


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
            self.assertEqual(session_schema.get_outfit(after), "")
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

    def test_character_load_empty_style_clears_previous_style(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "A")
            session_schema.set_character_value(state, "custom_current_style", "@old_style")
            state["saved_characters"] = {
                "A": {
                    "character": "A",
                    "series": "",
                    "persona": "你是 A。",
                    "appearance": "1girl, black hair",
                    "style": "",
                },
            }
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "load A")

            self.assertEqual(session_schema.get_character_value(state, "custom_current_style", ""), "")
            self.assertEqual(svc._get_current_style(sid), "")

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

    def test_character_card_roundtrips_outfit_and_auto_change(self):
        """角色卡新增的服装标签(outfit↔dynamic_appearance)与自动换装(三态)能存取并回读。"""
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        # 服装标签 + 自动换装关闭
        svc._apply_character_payload(state, {
            "character": "小雨",
            "outfit": "white shirt, dark pleated skirt",
            "allow_change_appearance": "false",
        })
        self.assertEqual(state["character"]["custom_character"], "小雨")
        self.assertEqual(state["character"]["custom_allow_llm_change_appearance"], False)
        self.assertEqual(session_schema.get_outfit(state), "white shirt, dark pleated skirt")
        self.assertIs(state["custom_allow_llm_change_appearance"], False)
        card = svc._character_export_payload(state)
        self.assertEqual(card["outfit"], "white shirt, dark pleated skirt")
        self.assertIs(card["allow_change_appearance"], False)
        # 三态空 → 跟随全局(None)
        svc._apply_character_payload(state, {"allow_change_appearance": ""})
        self.assertIsNone(state["custom_allow_llm_change_appearance"])
        self.assertIsNone(state["character"]["custom_allow_llm_change_appearance"])

    def test_character_card_schema_single_source(self):
        """角色卡字段集单一来源（character_card）：导出/快照/默认卡共用同一字段集，
        且写回→导出往返值一致。防再次出现多处手写表各漏字段的 drift。
        """
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_character"] = "小雨"
        card_keys = set(character_card.CARD_KEYS)

        # 导出 = id + 卡片字段集
        export = svc._character_export_payload(state)
        self.assertEqual(set(export) - {"id"}, card_keys)
        # 快照（saved_characters 条目）字段集 == 卡片字段集
        svc._snapshot_character(state)
        self.assertEqual(set(state["saved_characters"]["小雨"]), card_keys)
        # 默认卡 = id + is_default + 卡片字段集（钉住 config 派生的默认卡不漏字段）
        default = svc._default_character_payload()
        self.assertEqual(set(default) - {"id", "is_default"}, card_keys)
        # 写回→导出往返：值一致
        payload = {k: v for k, v in export.items() if k != "id"}
        fresh = svc._get_session_state("telegram:2")
        svc._apply_character_payload(fresh, payload)
        self.assertEqual(fresh["character"]["custom_character"], payload["character"])
        self.assertEqual(fresh["custom_character"], payload["character"])
        self.assertEqual(
            {k: v for k, v in svc._character_export_payload(fresh).items() if k != "id"},
            payload,
        )

    def test_default_character_card_roundtrips_outfit_and_auto_change(self):
        """默认角色卡的服装标签/自动换装写回全局 config；空(跟随全局)不改写开关。"""
        svc = self.make_service()
        svc._apply_default_character_payload({
            "id": svc._default_character_payload()["id"],
            "outfit": "black silk slip dress",
            "allow_change_appearance": "false",
        })
        self.assertEqual(svc.config["dynamic_appearance"], "black silk slip dress")
        self.assertIs(svc.config["allow_llm_change_appearance"], False)
        card = svc._default_character_payload()
        self.assertEqual(card["outfit"], "black silk slip dress")
        self.assertIs(card["allow_change_appearance"], False)
        # 空(跟随全局)不改写全局开关
        svc._apply_default_character_payload({"id": card["id"], "allow_change_appearance": ""})
        self.assertIs(svc.config["allow_llm_change_appearance"], False)

    def test_default_character_edit_writes_back_to_config(self):
        """卡编辑器改默认角色 → 写回 config（不进 saved_characters），且默认卡读取反映新值。"""
        svc = self.make_service()
        default_id = svc._default_character_payload()["id"]
        svc._apply_default_character_payload({
            "id": default_id,
            "persona": "新的人格",
            "appearance": "succubus, silver hair, red eyes",
            "role_name": "魅魔",
            "bot_self_name": "本座",
            "style": "@rurudo",
            "relationship": "同居恋人",
        })
        # 卡片字段映射到 config 键
        self.assertEqual(svc.config["scheduled_persona"], "新的人格")
        self.assertEqual(svc.config["positive_prefix"], "succubus, silver hair, red eyes")
        self.assertEqual(svc.config["bot_self_name"], "本座")
        self.assertEqual(svc.config["current_style"], "@rurudo")
        self.assertEqual(svc.config["spatial_relationship"], "同居恋人")
        # 默认卡读取反映写回值；appearance 与 positive_prefix 1:1
        card = svc._default_character_payload()
        self.assertEqual(card["appearance"], "succubus, silver hair, red eyes")
        self.assertEqual(card["persona"], "新的人格")
        # 不创建 saved_characters 条目
        sid = "telegram:1"
        self.assertEqual(svc._get_session_state(sid).get("saved_characters") or {}, {})

    def test_default_character_is_a_loadable_card(self):
        """内置默认角色（蕾伊）以正常角色卡形态存在：list 可见、可 load 回到隐式默认、不可删除。"""
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            default_id = svc._default_character_payload()["id"]

            # 角色池为空时，list 也始终展示系统默认角色
            await svc.cmd_character(1, sid, "list")
            self.assertIn(default_id, svc.send_message.await_args.args[1])

            # 先切到一个 OC，再 load 默认角色 → 回到隐式默认态（custom_character 清空、非角色态）
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "小雨",
                "custom_scheduled_persona": "你是小雨。",
                "saved_characters": {"小雨": {"character": "小雨", "persona": "你是小雨。"}},
            })
            svc._save_session_state(sid, state)
            await svc.cmd_character(1, sid, f"load {default_id}")
            after = svc._get_session_state(sid)
            self.assertEqual(after.get("custom_character"), "")
            self.assertFalse(svc._is_character_set(sid))
            self.assertIn(svc.config["scheduled_persona"], svc._get_effective_persona(sid))

            # 系统默认角色不可删除
            await svc.cmd_character(1, sid, f"delete {default_id}")
            self.assertIn("不可删除", svc.send_message.await_args.args[1])

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

    def test_state_schema_single_source_derives_sets(self):
        """阶段1：归属集合 + 默认值表单一来源（session_schema.STATE_SCHEMA）。

        锁死派生结果与重构前的手写值逐字段相等，防 schema 编辑误改分类/丢默认。
        """
        from telegram_comfyui_selfie import session_schema as ss
        # 三个归属集合：逐字段锁定（== 重构前 commands.py 里的手写 frozenset）
        self.assertEqual(set(ss.SESSION_GLOBAL_STATE_KEYS), {
            "last_interaction", "last_morning_greet_date",
            "daily_trigger_times", "daily_trigger_date", "daily_triggered_times",
            "saved_characters", "character_contexts", "init_flow",
            "ntr_stage_reached", "ntr_reconcile_count", "ntr_affection_reset",
            "frozen", "frozen_at",
            "session",
        })
        self.assertEqual(set(ss.CHARACTER_CONFIG_EXTRA_KEYS),
                         {"character", "purity", "purity_user_set", "persona_user_set"})
        # clothing 三字段已收进 clothing 盒；reset 保留的短期态单元现为 clothing + life_profile。
        self.assertEqual(set(ss.RESET_PRESERVED_TRANSIENT_KEYS),
                         {"clothing", "life_profile"})
        # 默认值表：代表性字段 + 无默认字段不进表
        defaults = ss.state_defaults()
        self.assertIn("last_interaction", defaults)          # 动态时间戳
        self.assertEqual(defaults["custom_bot_name"], "")
        self.assertEqual(defaults["character"], {})
        self.assertIsNone(defaults["purity"])
        self.assertEqual(defaults["clothing"]["wardrobe"], {})  # 衣柜在 clothing 盒内
        self.assertNotIn("ntr_affection_reset", defaults)    # 动态产生，无默认
        self.assertNotIn("life_profile", defaults)
        # 每次调用产生独立可变对象，不跨会话共享引用
        self.assertIsNot(ss.state_defaults()["chat_history"], ss.state_defaults()["chat_history"])
        # 三类对 schema 内每个字段恰好命中一类（互斥且全覆盖）
        for k in ss.STATE_SCHEMA:
            hits = [
                k in ss.SESSION_GLOBAL_STATE_KEYS,
                ss.is_character_config_key(k),
                ss.is_transient_state_key(k),
            ]
            self.assertEqual(hits.count(True), 1, f"{k} 必须恰好属于一类，实际命中 {hits}")

    def test_character_box_migration_and_accessors(self):
        """character box：旧扁平角色配置迁移进盒、访问器读写、双写兼容、幂等。"""
        from telegram_comfyui_selfie import session_schema as ss

        self.assertEqual(ss.box_for("character"), ss.BOX_CHARACTER)
        self.assertEqual(ss.box_for("custom_bot_name"), ss.BOX_CHARACTER)
        self.assertEqual(ss.box_for("purity"), ss.BOX_CHARACTER)
        self.assertEqual(ss.box_for("life_profile"), ss.BOX_CONTEXT)

        legacy = {
            "custom_bot_name": "林翩翩",
            "custom_positive_prefix": "1girl, blue eyes",
            "purity": 6,
            "some_other": "keep",
        }
        box = ss.ensure_character_box(legacy)
        self.assertIn("custom_bot_name", legacy)  # 非破坏迁移，旧扁平键保留
        self.assertEqual(box["custom_bot_name"], "林翩翩")
        self.assertEqual(box["custom_positive_prefix"], "1girl, blue eyes")
        self.assertEqual(box["purity"], 6)
        self.assertEqual(ss.get_character_value(legacy, "custom_bot_name"), "林翩翩")
        self.assertEqual(ss.get_custom_value(legacy, "bot_name"), "林翩翩")

        # 扁平直写仍优先，并同步回盒，保证旧访问点兼容。
        legacy["custom_bot_name"] = "新名字"
        self.assertEqual(ss.get_character_value(legacy, "custom_bot_name"), "新名字")
        self.assertEqual(legacy["character"]["custom_bot_name"], "新名字")

        ss.set_character_value(legacy, "custom_current_style", "@00 gx4")
        self.assertEqual(legacy["character"]["custom_current_style"], "@00 gx4")
        self.assertEqual(legacy["custom_current_style"], "@00 gx4")

        boxed_only = {"character": {"custom_bot_name": "盒内角色", "custom_positive_prefix": "silver hair"}}
        ss.ensure_character_box(boxed_only)
        self.assertEqual(boxed_only["custom_bot_name"], "盒内角色")
        self.assertEqual(ss.get_character_value(boxed_only, "custom_positive_prefix"), "silver hair")

        before = copy.deepcopy(legacy["character"])
        ss.ensure_character_box(legacy)
        self.assertEqual(legacy["character"], before)

    def test_clothing_box_migration_and_accessors(self):
        """clothing box：旧扁平字段迁移进盒、访问器读写、子键补齐、幂等。"""
        from telegram_comfyui_selfie import session_schema as ss
        # box_for 归位
        self.assertEqual(ss.box_for("clothing"), ss.BOX_CLOTHING)
        self.assertEqual(ss.box_for("place"), ss.BOX_PLACE)
        self.assertEqual(ss.box_for("custom_bot_name"), ss.BOX_CHARACTER)
        self.assertEqual(ss.box_for("chat_history"), ss.BOX_CONTEXT)
        self.assertEqual(ss.box_for("life_profile"), ss.BOX_CONTEXT)

        # 旧扁平持久态：顶层有 dynamic_appearance/wardrobe/wardrobe_closet → 迁移进盒并删顶层
        legacy = {
            "dynamic_appearance": "red dress",
            "wardrobe": {"dress": "red dress"},
            "wardrobe_closet": {"套装A": {}},
        }
        box = ss.ensure_clothing_box(legacy)
        self.assertNotIn("dynamic_appearance", legacy)   # 顶层已删
        self.assertNotIn("wardrobe", legacy)
        self.assertEqual(legacy["clothing"]["dynamic_appearance"], "red dress")
        self.assertEqual(ss.get_outfit(legacy), "red dress")
        self.assertEqual(ss.get_wardrobe(legacy), {"dress": "red dress"})
        self.assertEqual(ss.get_closet(legacy), {"套装A": {}})
        # 子键补齐 + 默认裸体态为空
        self.assertEqual(ss.get_nudity(legacy), "")
        self.assertEqual(box["nudity_at"], 0.0)

        # 访问器读写
        st = {}
        ss.set_outfit(st, "black coat")
        ss.set_wardrobe(st, {"coat": "black coat"})
        self.assertEqual(ss.get_outfit(st), "black coat")
        # get_wardrobe 返回真对象，可原地改并持久
        ss.get_wardrobe(st)["hat"] = "beret"
        self.assertEqual(st["clothing"]["wardrobe"]["hat"], "beret")
        # 裸体态读写 + 清除
        ss.set_nudity(st, "completely nude", at=1000.0)
        self.assertEqual(ss.get_nudity(st), "completely nude")
        self.assertEqual(ss.get_nudity_at(st), 1000.0)
        ss.clear_nudity(st)
        self.assertEqual(ss.get_nudity(st), "")
        self.assertEqual(ss.get_nudity_at(st), 0.0)

        # 幂等：再 ensure 一次不改变内容
        before = copy.deepcopy(st["clothing"])
        ss.ensure_clothing_box(st)
        self.assertEqual(st["clothing"], before)

    def test_place_box_migration_and_accessors(self):
        """place box：旧扁平位置字段迁移进盒、访问器读写、子键补齐、幂等。"""
        from telegram_comfyui_selfie import session_schema as ss
        # box_for 归位
        self.assertEqual(ss.box_for("place"), ss.BOX_PLACE)

        # 旧扁平持久态：顶层有 user_place/character_place 等 → 迁移进盒并删顶层
        legacy = {
            "user_place": "cafe",
            "user_place_label": "咖啡店",
            "user_place_confidence": 0.85,
            "character_place": "home",
            "character_place_name": "家",
            "character_place_history": [{"key": "transit", "label": "通勤"}],
            "rounds_since_location": 5,
            "some_other": "keep",
        }
        box = ss.ensure_place_box(legacy)
        self.assertNotIn("user_place", legacy)       # 顶层已删
        self.assertNotIn("character_place", legacy)
        self.assertNotIn("character_place_history", legacy)
        self.assertIn("some_other", legacy)          # 非位置字段保留
        self.assertEqual(legacy["place"]["user_place"], "cafe")
        self.assertEqual(ss.get_user_place(legacy), "cafe")
        self.assertEqual(ss.get_character_place(legacy), "home")
        self.assertEqual(ss.get_character_place_name(legacy), "家")
        self.assertEqual(ss.get_rounds_since_location(legacy), 5)
        # 子键补齐 + 默认值
        self.assertFalse(ss.get_user_co_located(legacy))
        self.assertEqual(ss.get_user_place_source(legacy), "")
        self.assertEqual(box["user_place_updated_at"], 0)

        # 访问器读写
        st = {}
        ss.set_user_place(st, key="mall", label="商场", updated_at=1000.0, confidence=0.9, co_located=False)
        self.assertEqual(ss.get_user_place(st), "mall")
        self.assertEqual(ss.get_user_place_label(st), "商场")
        self.assertEqual(ss.get_user_place_confidence(st), 0.9)
        self.assertFalse(ss.get_user_co_located(st))
        ss.set_user_co_located(st, True)
        self.assertTrue(ss.get_user_co_located(st))

        ss.set_character_place(st, key="cafe", label="咖啡店", name="星巴克", updated_at=2000.0, confidence=0.8, rounds=0)
        self.assertEqual(ss.get_character_place(st), "cafe")
        self.assertEqual(ss.get_character_place_name(st), "星巴克")
        self.assertEqual(ss.get_character_place_confidence(st), 0.8)

        # 历史记录 + 轮数递增
        self.assertEqual(ss.get_character_place_history(st), [])
        ss.append_character_place_history(st, {"key": "mall", "label": "商场"})
        self.assertEqual(len(ss.get_character_place_history(st)), 1)
        ss.increment_rounds_since_location(st)
        self.assertEqual(ss.get_rounds_since_location(st), 1)

        # clear_transient 可正确复位 place box
        defaults = ss.state_defaults()
        st["place"] = defaults.get("place", {})
        self.assertEqual(ss.get_user_place(st), "")
        self.assertEqual(ss.get_character_place(st), "")
        self.assertEqual(ss.get_rounds_since_location(st), 0)

        # 幂等：再 ensure 一次不改变内容
        before = copy.deepcopy(st["place"])
        ss.ensure_place_box(st)
        self.assertEqual(st["place"], before)

    def test_context_box_migration_and_accessors(self):
        """context box：旧扁平对话/checkpoint/照片字段迁移进盒、访问器读写、双写兼容、幂等。"""
        from telegram_comfyui_selfie import session_schema as ss
        # box_for 归位
        self.assertEqual(ss.box_for("context"), ss.BOX_CONTEXT)

        # 旧扁平持久态：顶层有 chat_history/recent_message_history 等 → 保障盒存在，不删顶层（非破坏）
        legacy = {
            "chat_history": [{"role": "user", "content": "你好"}],
            "sent_photos_history": [{"timestamp": 999.0, "scene": "test"}],
            "short_context_start": 5,
            "rounds_since_image": 3,
            "some_other": "keep",
        }
        box = ss.ensure_context_box(legacy)
        self.assertIn("chat_history", legacy)         # 扁平键保留（非破坏迁移）
        self.assertIn("short_context_start", legacy)
        self.assertIn("some_other", legacy)
        # 盒内值由扁平拷贝
        self.assertEqual(box["chat_history"], [{"role": "user", "content": "你好"}])
        self.assertEqual(box["rounds_since_image"], 3)
        # 访问器读取（扁平优先）
        self.assertEqual(ss.get_chat_history(legacy), [{"role": "user", "content": "你好"}])
        self.assertEqual(ss.get_rounds_since_image(legacy), 3)

        # 子键补齐 + 默认值
        self.assertEqual(ss.get_checkpoint_summary(legacy), "")
        self.assertFalse(ss.get_replying_to_selfie(legacy))
        self.assertEqual(ss.get_last_sent_selfie_time(legacy), 0.0)

        # 访问器读写（双写：盒 + 扁平）
        st = {}
        ss.set_chat_history(st, [{"role": "assistant", "content": "Hello"}])
        self.assertEqual(ss.get_chat_history(st), [{"role": "assistant", "content": "Hello"}])
        self.assertEqual(st["context"]["chat_history"], [{"role": "assistant", "content": "Hello"}])
        self.assertEqual(st["chat_history"], [{"role": "assistant", "content": "Hello"}])  # 扁平同步

        ss.set_short_context_start(st, 42)
        self.assertEqual(ss.get_short_context_start(st), 42)
        self.assertEqual(st["context"]["short_context_start"], 42)

        ss.set_replying_to_selfie(st, True)
        self.assertTrue(ss.get_replying_to_selfie(st))

        # 拍图记录
        ss.set_last_sent_selfie_time(st, 99999.0)
        ss.set_last_sent_selfie_caption(st, "晚上好")
        ss.set_last_sent_selfie_source_description(st, "玄关灯下")
        ss.set_last_sent_selfie_replied(st, True)
        self.assertEqual(ss.get_last_sent_selfie_time(st), 99999.0)
        self.assertEqual(ss.get_last_sent_selfie_caption(st), "晚上好")
        self.assertEqual(ss.get_last_sent_selfie_source_description(st), "玄关灯下")
        self.assertTrue(ss.get_last_sent_selfie_replied(st))

        # increment
        self.assertEqual(ss.get_rounds_since_image(st), 0)
        ss.increment_rounds_since_image(st)
        self.assertEqual(ss.get_rounds_since_image(st), 1)
        ss.increment_rounds_since_image(st)
        self.assertEqual(ss.get_rounds_since_image(st), 2)

        # 向后兼容：直写扁平键，访问器立即可读（扁平优先策略）
        flat_st = {"context": {}}
        ss.ensure_context_box(flat_st)
        flat_st["chat_history"] = [{"role": "user", "content": "flat write"}]
        self.assertEqual(ss.get_chat_history(flat_st), [{"role": "user", "content": "flat write"}])
        # 清空后空列表也穿透
        flat_st["chat_history"] = []
        self.assertEqual(ss.get_chat_history(flat_st), [])

        # 幂等：再 ensure 一次不改变内容
        before = copy.deepcopy(st["context"])
        ss.ensure_context_box(st)
        self.assertEqual(st["context"], before)

        # clear_transient 可正确复位 context box
        defaults = ss.state_defaults()
        st2 = {}
        st2["context"] = copy.deepcopy(defaults.get("context", {}))
        self.assertEqual(ss.get_chat_history(st2), [])
        self.assertEqual(ss.get_short_context_start(st2), 0)
        self.assertEqual(ss.get_rounds_since_image(st2), 0)

    def test_session_box_migration_and_accessors(self):
        """session box：旧扁平会话全局字段迁移进盒、访问器读写、双写兼容、幂等、容器原地变更一致性。"""
        from telegram_comfyui_selfie import session_schema as ss
        # box_for 归位
        self.assertEqual(ss.box_for("session"), ss.BOX_SESSION)
        self.assertEqual(ss.box_for("frozen"), ss.BOX_SESSION)
        self.assertEqual(ss.box_for("saved_characters"), ss.BOX_SESSION)

        # 旧扁平持久态：顶层有 frozen/saved_characters 等 → 保障盒存在，不删顶层（非破坏）
        legacy = {
            "frozen": True,
            "frozen_at": 999.0,
            "last_interaction": 12345.0,
            "saved_characters": {"小雨": {"character": "小雨"}},
            "daily_trigger_times": ["08:30", "12:00"],
            "ntr_stage_reached": 2,
            "some_other": "keep",
        }
        box = ss.ensure_session_box(legacy)
        self.assertIn("frozen", legacy)           # 扁平键保留（非破坏迁移）
        self.assertIn("saved_characters", legacy)
        self.assertIn("some_other", legacy)
        # 盒内值由扁平拷贝
        self.assertEqual(box["frozen"], True)
        self.assertEqual(box["last_interaction"], 12345.0)
        self.assertEqual(box["saved_characters"]["小雨"]["character"], "小雨")
        self.assertEqual(box["daily_trigger_times"], ["08:30", "12:00"])
        # 访问器读取（扁平优先）
        self.assertTrue(ss.get_frozen(legacy))
        self.assertEqual(ss.get_last_interaction(legacy), 12345.0)
        self.assertEqual(ss.get_daily_trigger_times(legacy), ["08:30", "12:00"])
        self.assertEqual(ss.get_ntr_stage_reached(legacy), 2)

        # 子键补齐 + 默认值
        self.assertFalse(ss.get_ntr_affection_reset(legacy))
        self.assertEqual(ss.get_ntr_reconcile_count(legacy), 0)
        self.assertEqual(ss.get_last_morning_greet_date(legacy), "")
        self.assertEqual(ss.get_character_contexts(legacy), {})
        self.assertEqual(ss.get_init_flow(legacy), {})

        # 访问器读写（双写：盒 + 扁平）
        st = {}
        ss.set_frozen(st, True)
        ss.set_frozen_at(st, 1000.0)
        self.assertTrue(ss.get_frozen(st))
        self.assertEqual(ss.get_frozen_at(st), 1000.0)
        self.assertEqual(st["session"]["frozen"], True)
        self.assertEqual(st["frozen"], True)  # 扁平同步

        ss.set_last_interaction(st, 50000.0)
        self.assertEqual(ss.get_last_interaction(st), 50000.0)
        self.assertEqual(st["session"]["last_interaction"], 50000.0)

        ss.set_ntr_stage_reached(st, 3)
        self.assertEqual(ss.get_ntr_stage_reached(st), 3)

        ss.set_ntr_affection_reset(st, True)
        self.assertTrue(ss.get_ntr_affection_reset(st))
        ss.set_ntr_reconcile_count(st, 5)
        self.assertEqual(ss.get_ntr_reconcile_count(st), 5)

        ss.set_last_morning_greet_date(st, "2026-06-24")
        self.assertEqual(ss.get_last_morning_greet_date(st), "2026-06-24")

        ss.set_daily_trigger_times(st, ["09:00", "14:00"])
        self.assertEqual(ss.get_daily_trigger_times(st), ["09:00", "14:00"])

        ss.set_daily_trigger_date(st, "2026-06-24")
        self.assertEqual(ss.get_daily_trigger_date(st), "2026-06-24")

        ss.set_daily_triggered_times(st, ["09:00"])
        self.assertEqual(ss.get_daily_triggered_times(st), ["09:00"])

        # 容器返回 live 对象：原地变更持久
        saved = ss.get_saved_characters(st)
        saved["新角色"] = {"character": "新角色"}
        self.assertEqual(st["session"]["saved_characters"]["新角色"]["character"], "新角色")
        self.assertEqual(st["saved_characters"]["新角色"]["character"], "新角色")

        contexts = ss.get_character_contexts(st)
        contexts["角色A"] = {"chat_history": []}
        self.assertEqual(st["session"]["character_contexts"]["角色A"]["chat_history"], [])
        self.assertEqual(st["character_contexts"]["角色A"]["chat_history"], [])

        init = ss.get_init_flow(st)
        init["step"] = 1
        self.assertEqual(st["session"]["init_flow"]["step"], 1)
        self.assertEqual(st["init_flow"]["step"], 1)

        # 向后兼容：直写扁平键，访问器立即可读（扁平优先策略）
        flat_st = {"session": {}}
        ss.ensure_session_box(flat_st)
        flat_st["frozen"] = True
        self.assertTrue(ss.get_frozen(flat_st))
        flat_st["saved_characters"] = {"A": {"character": "A"}}
        self.assertEqual(ss.get_saved_characters(flat_st)["A"]["character"], "A")
        # 清空后也能穿透
        flat_st["frozen"] = False
        self.assertFalse(ss.get_frozen(flat_st))

        # 幂等：再 ensure 一次不改变内容
        before = copy.deepcopy(st["session"])
        ss.ensure_session_box(st)
        self.assertEqual(st["session"], before)

        # clear_transient 可正确复位 session box（session 是 G scope，不会被清）
        defaults = ss.state_defaults()
        st2 = {}
        st2["session"] = copy.deepcopy(defaults.get("session", {}))
        self.assertFalse(ss.get_frozen(st2))
        self.assertEqual(ss.get_last_interaction(st2), 0)
        self.assertEqual(ss.get_saved_characters(st2), {})

        # last_interaction 缺失时种 time.time()（factory 语义）
        import time as _time
        empty = {}
        before_ts = _time.time()
        ss.ensure_session_box(empty)
        after_ts = _time.time()
        self.assertGreaterEqual(empty["session"]["last_interaction"], before_ts)
        self.assertLessEqual(empty["session"]["last_interaction"], after_ts)

        # session 盒（G scope）在 clear_transient 中不被清——saved_characters/frozen 保留
        full = {"custom_character": "角色A"}
        saved_box = ss.ensure_session_box(full)
        saved_box["saved_characters"] = {"角色A": {"character": "角色A"}}
        full["saved_characters"] = {"角色A": {"character": "角色A"}}
        ss.set_frozen(full, True)
        # 模拟 _clear_transient_state 行为（只清 transient，不动 G）
        for key in list(full.keys()):
            if ss.is_transient_state_key(key):
                del full[key]
        # session 盒和扁平键都保留
        self.assertTrue(ss.get_frozen(full))
        self.assertEqual(ss.get_saved_characters(full)["角色A"]["character"], "角色A")


    def test_transient_state_partition_classifier(self):
        """字段单一来源（黑名单反推）：会话全局/角色配置/短期态三类不重叠，关键字段各归其位。

        守住分类器，防未来新增字段误分类。短期态 = 既非会话全局、也非角色配置。
        """
        # 会话全局：绝不随角色走
        for k in ["last_interaction", "daily_trigger_times", "saved_characters",
                  "character_contexts", "ntr_stage_reached", "init_flow"]:
            self.assertFalse(_is_transient_state_key(k), f"{k} 应为会话全局")
        # 角色配置（custom_ 前缀 + 纯良度/标志位）：走 saved_characters 卡
        for k in ["custom_character", "custom_scheduled_persona", "custom_positive_prefix",
                  "purity", "purity_user_set", "persona_user_set"]:
            self.assertTrue(_is_character_config_key(k), f"{k} 应为角色配置")
            self.assertFalse(_is_transient_state_key(k), f"{k} 配置不应进短期态")
        # 短期态：随角色冻结/解冻/清空（含原先会串味的 wardrobe，及动态键）
        for k in ["chat_history", "sent_photos_history", "place", "dynamic_appearance",
                  "wardrobe", "wardrobe_closet", "life_profile", "short_context_start"]:
            self.assertTrue(_is_transient_state_key(k), f"{k} 应为短期态")
        # 会话全局与角色配置不重叠
        self.assertFalse(any(_is_character_config_key(k) for k in SESSION_GLOBAL_STATE_KEYS))

    def test_character_switch_roundtrip_restores_full_transient(self):
        """A→B→A 往返：A 的全部短期态（含 wardrobe/位置/照片/穿搭）原样解冻；B 不继承 A 的任何短期态。

        这是 ③ 的根治守卫——不再逐字段列，凡短期态都随角色冻结/解冻，漏配不再串味。
        """
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "角色A",
                "saved_characters": {
                    "角色A": {"character": "角色A", "persona": "我是A"},
                    "角色B": {"character": "角色B", "persona": "我是B"},
                },
                "chat_history": [{"role": "assistant", "content": "A的台词"}],
                "sent_photos_history": [{"timestamp": 9999999999, "scene": "A的画面", "view": "selfie"}],
                "dynamic_appearance": "A的红裙",
                "wardrobe": {"红裙": ["red dress"]},
            })
            session_schema.set_user_place(state, key="mall", updated_at=time.time())
            svc._save_session_state(sid, state)

            await svc.cmd_character(1, sid, "load 角色B")
            after_b = svc._get_session_state(sid)
            # B 不继承 A 的任何短期态（含原先会串味的 wardrobe/dynamic_appearance/user_place）
            self.assertEqual(after_b["chat_history"], [])
            self.assertEqual(after_b["sent_photos_history"], [])
            self.assertEqual(session_schema.get_outfit(after_b), "")
            self.assertEqual(session_schema.get_wardrobe(after_b), {})
            self.assertEqual(session_schema.get_user_place(after_b), "")
            # B 期间产生自己的短期态
            after_b["chat_history"] = [{"role": "assistant", "content": "B的台词"}]
            session_schema.set_outfit(after_b, "B的西装")
            svc._save_session_state(sid, after_b)

            await svc.cmd_character(1, sid, "load 角色A")
            after_a = svc._get_session_state(sid)
            # 切回 A：A 离开时的全部短期态原样解冻
            self.assertEqual(after_a["chat_history"], [{"role": "assistant", "content": "A的台词"}])
            self.assertEqual(after_a["sent_photos_history"][0]["scene"], "A的画面")
            self.assertEqual(session_schema.get_outfit(after_a), "A的红裙")
            self.assertEqual(session_schema.get_wardrobe(after_a), {"红裙": ["red dress"]})
            self.assertEqual(session_schema.get_user_place(after_a), "mall")

        asyncio.run(run())

    def test_character_switch_uses_target_card_outfit_not_previous_wardrobe(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            svc.send_message = AsyncMock()
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "角色A")
            session_schema.get_saved_characters(state).update({
                "角色A": {"character": "角色A", "persona": "我是A", "outfit": "A red dress"},
                "角色B": {"character": "角色B", "persona": "我是B", "outfit": "blue dress"},
            })
            session_schema.set_outfit(state, "A red dress")
            session_schema.set_wardrobe(state, {"dress": "A red dress"})
            svc._save_session_state(sid, state)

            await svc.cmd_character(1, sid, "load 角色B")

            after_b = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_outfit(after_b), "blue dress")
            self.assertEqual(session_schema.get_wardrobe(after_b), {"dress": "blue dress"})
            self.assertNotIn("A red dress", str(session_schema.get_wardrobe(after_b)))

            await svc.cmd_character(1, sid, "load 角色A")

            after_a = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_outfit(after_a), "A red dress")
            self.assertEqual(session_schema.get_wardrobe(after_a), {"dress": "A red dress"})

        asyncio.run(run())

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

    def test_delete_current_character_clears_state_and_does_not_revive(self):
        # 删除当前角色：必须清空当前角色态，且后续 _snapshot_character 不能把它复活。
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "角色A",
                "custom_scheduled_persona": "我是A",
                "custom_bot_name": "角色A",
                "custom_positive_prefix": "black hair",
                "persona_user_set": True,
                "purity": 5,
                "purity_user_set": True,
                "dynamic_appearance": "red dress",
                "wardrobe": {"红裙": ["red dress"]},
                "wardrobe_closet": {"red dress": 1},
                "saved_characters": {
                    "角色A": {"character": "角色A", "persona": "我是A"},
                    "角色B": {"character": "角色B", "persona": "我是B"},
                },
                "chat_history": [{"role": "assistant", "content": "A的专属台词"}],
                "sent_photos_history": [{"timestamp": 9999999999, "scene": "A的画面", "view": "selfie"}],
            })
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "delete 角色A")

            after = svc._get_session_state(sid)
            # 池里 A 删掉、B 保留
            self.assertNotIn("角色A", after["saved_characters"])
            self.assertIn("角色B", after["saved_characters"])
            # 当前角色态清空，回退全局默认
            self.assertEqual(after.get("custom_character"), "")
            self.assertEqual(after.get("custom_scheduled_persona"), "")
            self.assertEqual(after.get("custom_bot_name"), "")
            self.assertEqual(after.get("custom_positive_prefix"), "")
            self.assertFalse(after.get("persona_user_set"))
            self.assertIsNone(after.get("purity"))
            self.assertFalse(after.get("purity_user_set"))
            self.assertEqual(session_schema.get_outfit(after), "")
            self.assertEqual(session_schema.get_wardrobe(after), {})
            self.assertEqual(session_schema.get_closet(after), {})
            # 对话/照片上下文清空
            self.assertEqual(after.get("chat_history"), [])
            self.assertEqual(after.get("sent_photos_history"), [])
            # 关键：后续快照不会把 A 写回 saved_characters（复活已修复）
            svc._snapshot_character(after)
            self.assertNotIn("角色A", after["saved_characters"])

        asyncio.run(run())

    def test_delete_non_current_character_keeps_current_state(self):
        # 删除非当前角色：只删存档，当前角色态/对话上下文不动。
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            state.update({
                "custom_character": "角色A",
                "custom_scheduled_persona": "我是A",
                "custom_bot_name": "角色A",
                "saved_characters": {
                    "角色A": {"character": "角色A", "persona": "我是A"},
                    "角色B": {"character": "角色B", "persona": "我是B"},
                },
                "chat_history": [{"role": "assistant", "content": "A的专属台词"}],
            })
            svc._save_session_state(sid, state)
            svc.send_message = AsyncMock()

            await svc.cmd_character(1, sid, "delete 角色B")

            after = svc._get_session_state(sid)
            self.assertNotIn("角色B", after["saved_characters"])
            self.assertIn("角色A", after["saved_characters"])
            # 当前角色态不变
            self.assertEqual(after.get("custom_character"), "角色A")
            self.assertEqual(after.get("custom_scheduled_persona"), "我是A")
            # 对话上下文不动
            self.assertEqual(len(after.get("chat_history", [])), 1)

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

    def test_default_wardrobe_outfit_renders_for_default_character(self):
        svc = self.make_service()
        # config 里的默认装扮（默认角色的初始穿搭）
        svc.config["dynamic_appearance"] = "black silk slip dress, cotton knit cardigan"
        sid = "telegram:1"
        # 默认角色：未设角色、衣柜为空 → 应回退注入 config 默认装扮到 appearance
        pos, _ = svc._build_prompt("standing in the living room", session_id=sid)
        self.assertIn("slip dress", pos.lower())
        self.assertIn("cardigan", pos.lower())

    def test_clothing_off_strips_named_garment_for_this_image_only(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        session_schema.set_outfit(state, "cotton knit cardigan, black silk slip dress")
        pos, _ = svc._build_prompt("standing by the window", session_id=sid, clothing_off="cardigan")
        self.assertNotIn("cardigan", pos.lower())   # 脱掉的开衫被剥离
        self.assertIn("slip dress", pos.lower())     # 没脱的还在
        # 持久衣柜/dynamic_appearance 不受影响（事后自动复原）
        self.assertEqual(session_schema.get_outfit(state), "cotton knit cardigan, black silk slip dress")

    def test_clothing_off_nude_strips_all_outfit_and_frees_negative(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        session_schema.set_outfit(state, "cotton knit cardigan, black silk slip dress")
        pos, neg = svc._build_prompt("on the bed", session_id=sid, clothing_off="nude")
        self.assertNotIn("cardigan", pos.lower())
        self.assertNotIn("slip dress", pos.lower())
        self.assertIn("nude", pos.lower())
        self.assertNotIn("nude", neg.lower())        # 负向不再压制裸体
        # 脱掉的衣物压进负向，抵消 scene 里可能残留的着装描述，防止被画回去
        self.assertIn("cardigan", neg.lower())
        self.assertIn("slip dress", neg.lower())
        self.assertEqual(session_schema.get_outfit(state), "cotton knit cardigan, black silk slip dress")

    def test_outfit_normalized_so_nude_strips_deterministically(self):
        """脏穿搭(双空格/重复标签)经归一后，全裸能确定性剥掉——回归"脱不掉衣服"。

        根因：remove_tag 是裸字符串 replace，worn 标签带双空格/重复时与渲染串对不上而删不掉。
        """
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        # 历史脏数据：双空格 + 重复同一条裙子
        session_schema.set_outfit(
            state,
            "black silk slip dress with thin spaghetti straps bias cut  liquid-like drape, "
            "black silk slip dress with thin spaghetti straps bias cut liquid-like drape",
        )
        # set_outfit 已归一：去重 + 单空格
        self.assertEqual(
            session_schema.get_outfit(state),
            "black silk slip dress with thin spaghetti straps bias cut liquid-like drape",
        )
        # 全裸 → 裙子被确定性剥掉
        pos, _ = svc._build_prompt("on the bed", session_id=sid, clothing_off="nude")
        self.assertNotIn("slip dress", pos.lower())
        self.assertIn("nude", pos.lower())

        # ensure 懒清理：直接塞进双空格脏值，下次取 state 自动归一
        state["clothing"]["dynamic_appearance"] = "red  dress, red dress, white  hat"
        svc._get_session_state(sid)
        self.assertEqual(session_schema.get_outfit(state), "red dress, white hat")

    def test_nudity_context_detector(self):
        """裸体检测器只对强信号(性行为/明确脱光)命中，对暧昧词/日常不误触发。"""
        self.assertTrue(_detect_nudity_context("两人做爱中"))
        self.assertTrue(_detect_nudity_context("她全裸躺在床上"))
        self.assertTrue(_detect_nudity_context("把衣服都脱了"))
        self.assertTrue(_detect_nudity_context("衣服脱了"))
        self.assertTrue(_detect_nudity_context("脱了衣服"))
        # 暧昧/可能已重新着装的词不触发（宁可漏判不可误脱）
        self.assertFalse(_detect_nudity_context("事后温存，相拥而眠"))
        self.assertFalse(_detect_nudity_context("刚洗完澡出来"))
        self.assertFalse(_detect_nudity_context("今天穿了新裙子"))
        self.assertFalse(_detect_nudity_context("脱了外套"))
        self.assertFalse(_detect_nudity_context("寝衣滑落到手肘"))
        self.assertFalse(_detect_nudity_context(""))

    def test_clothing_off_fallback_distinguishes_full_and_partial_nudity(self):
        self.assertEqual(_infer_clothing_off_fallback("衣服脱了"), "completely nude")
        self.assertEqual(_infer_clothing_off_fallback("她宽衣解带坐到床边"), "completely nude")
        self.assertEqual(_infer_clothing_off_fallback("寝衣滑落到手肘"), "topless")
        self.assertEqual(_infer_clothing_off_fallback("衣襟敞开，露出胸口"), "topless")
        self.assertEqual(_infer_clothing_off_fallback("脱了外套"), "")

    def test_planner_nudity_fallback_fills_clothing_off(self):
        """规划器漏填 clothing_off 但对话有明确裸体信号时，兜底补 completely nude；
        无信号不补；规划器显式填了则不覆盖。"""
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            # 规划器返回里【没有】clothing_off 字段
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "床上贴身依偎", "view": "pov", "is_intimate": True,
            }, ensure_ascii=False))

            # 各 case 用独立会话，隔离持久裸体态（单独测试见 test_persistent_nudity_*）
            # ① 意图含明确性爱/裸体 → 兜底补 nude
            plan = await plan_roleplay_image(svc, "telegram:101", intent="做爱后想要一张全裸的照片")
            self.assertEqual(plan["clothing_off"], "completely nude")
            plan = await plan_roleplay_image(svc, "telegram:104", intent="衣服脱了")
            self.assertEqual(plan["clothing_off"], "completely nude")

            # ② 半脱语义 → 只补局部裸露，不误判全裸；普通意图/脱外套 → 不补
            plan = await plan_roleplay_image(svc, "telegram:105", intent="寝衣滑落到手肘")
            self.assertEqual(plan["clothing_off"], "topless")
            plan = await plan_roleplay_image(svc, "telegram:106", intent="脱了外套")
            self.assertEqual(plan["clothing_off"], "")
            plan = await plan_roleplay_image(svc, "telegram:102", intent="看看你在客厅做什么")
            self.assertEqual(plan["clothing_off"], "")

            # ③ 规划器显式填了 clothing_off → 即使有裸体信号也不覆盖
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "脱了外套", "view": "pov", "clothing_off": "cardigan",
            }, ensure_ascii=False))
            plan = await plan_roleplay_image(svc, "telegram:103", intent="做爱前先脱掉外套")
            self.assertEqual(plan["clothing_off"], "cardigan")

        asyncio.run(run())

    def test_persistent_nudity_continues_until_dressed_or_new_scene(self):
        """持久裸体态（根治脱衣 bug）：一旦全裸，后续图自动续上；换装或新场景解除。"""
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "k", "image_llm_model": "m", "image_llm_api_base": "https://x",
            })
            sid = "telegram:1"
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            state = svc._get_session_state(sid)

            # 图1：性爱意图 → 兜底全裸 + 持久化
            svc._call_llm = AsyncMock(return_value=json.dumps({"scene": "床上", "view": "pov"}, ensure_ascii=False))
            plan1 = await plan_roleplay_image(svc, sid, intent="两人做爱中")
            self.assertEqual(plan1["clothing_off"], "completely nude")
            self.assertEqual(session_schema.get_nudity(state), "completely nude")

            # 图2：普通意图、规划器不判脱衣 → 仍续上裸体（不再被衣服画回去）
            svc._call_llm = AsyncMock(return_value=json.dumps({"scene": "还躺在床上", "view": "pov"}, ensure_ascii=False))
            plan2 = await plan_roleplay_image(svc, sid, intent="看看你现在的样子")
            self.assertEqual(plan2["clothing_off"], "completely nude")

            # 换装 → 解除裸体态
            svc._classify_wardrobe_change = AsyncMock(return_value={"dress": "red dress", "names": {"dress": "红裙"}})
            await svc._apply_wardrobe(sid, "穿上红裙")
            self.assertEqual(session_schema.get_nudity(state), "")
            plan3 = await plan_roleplay_image(svc, sid, intent="看看你现在的样子")
            self.assertEqual(plan3["clothing_off"], "")  # 穿衣后不再续裸体

            # 再次裸体后 /新场景 → 解除
            svc._call_llm = AsyncMock(return_value=json.dumps({"scene": "床上", "view": "pov"}, ensure_ascii=False))
            await plan_roleplay_image(svc, sid, intent="插入她")
            self.assertEqual(session_schema.get_nudity(state), "completely nude")
            svc._reset_short_context(state, "test-new-scene")
            self.assertEqual(session_schema.get_nudity(state), "")

        asyncio.run(run())

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
        sys_default = "\n".join(m["content"] for m in svc._build_chat_messages(sid, "你好") if m.get("role") == "system")
        self.assertNotIn("回复长度", sys_default)
        # 设为简短后注入约束
        svc.config["chat_reply_length"] = "简短"
        sys_short = "\n".join(m["content"] for m in svc._build_chat_messages(sid, "你好") if m.get("role") == "system")
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
            "A selfie of a woman, Dark brown hair spills loosely over her shoulders, demon horns peeking through",
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
        session_schema.set_outfit(state, "oversized white sweater")
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
        session_schema.set_outfit(state, "silver hair, oversized white sweater")
        pos, _ = svc._build_prompt(
            "A woman with dark brown hair sits by a window, wearing a black fitted dress",
            session_id=sid,
        )
        low = pos.lower()
        self.assertIn("silver hair", low)
        self.assertIn("oversized white sweater", low)
        self.assertNotIn("dark brown hair", low)
        self.assertNotIn("black fitted dress", low)

    def test_scene_outfit_cleanup_does_not_split_hyphenated_color_words(self):
        from telegram_comfyui_selfie.generation import _strip_conflicting_scene_outfit

        kept = _strip_conflicting_scene_outfit(
            "moon-white nightgown slips to her elbows",
            ["current hanfu"],
            ["nightgown"],
        )
        self.assertIn("moon-white nightgown", kept)
        self.assertNotIn("moon-wearing the current outfit", kept)

        replaced = _strip_conflicting_scene_outfit(
            "white nightgown slips to her elbows",
            ["current hanfu"],
            ["nightgown"],
        )
        self.assertIn("wearing the current outfit", replaced)

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

    def test_call_llm_messages_places_tools_before_messages_in_request_body(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x"})
            captured = {}

            class FakeResponse:
                status = 200

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def json(self):
                    return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1}}

                async def text(self):
                    return ""

            class FakeSession:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def post(self, *args, **kwargs):
                    body = kwargs["json"]
                    captured["keys"] = list(body.keys())
                    return FakeResponse()

            with patch("telegram_comfyui_selfie.service.aiohttp.ClientSession", FakeSession):
                await svc._call_llm_messages(
                    [{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "x"}}],
                    tool_choice="auto",
                    purpose="chat",
                    session_id="telegram:1",
                )

            self.assertLess(captured["keys"].index("tools"), captured["keys"].index("messages"))
            self.assertLess(captured["keys"].index("tool_choice"), captured["keys"].index("messages"))

        asyncio.run(run())

    def test_call_llm_adds_cache_anchor_for_hot_simple_tasks(self):
        async def run():
            svc = self.make_service()
            captured = []

            async def fake_msgs(messages, **kwargs):
                captured.append((messages, kwargs))
                return {"choices": [{"message": {"content": "ok"}}]}

            svc._call_llm_messages = fake_msgs

            await svc._call_llm("dynamic plan system", "plan user", tag="roleplay-image-plan", purpose="image")
            await svc._call_llm("dynamic translate system", "translate user", tag="translate", purpose="image")
            await svc._call_llm("ordinary system", "ordinary user", tag="memory-extract", purpose="chat")

            plan_messages = captured[0][0]
            self.assertEqual([m["role"] for m in plan_messages], ["system", "system", "user"])
            self.assertIn("Stable prefix for roleplay-image-plan", plan_messages[0]["content"])
            self.assertEqual(plan_messages[1]["content"], "dynamic plan system")
            self.assertEqual(plan_messages[2]["content"], "plan user")

            translate_messages = captured[1][0]
            self.assertEqual([m["role"] for m in translate_messages], ["system", "system", "user"])
            self.assertIn("Stable prefix for image tag translation", translate_messages[0]["content"])

            ordinary_messages = captured[2][0]
            self.assertEqual([m["role"] for m in ordinary_messages], ["system", "user"])
            self.assertEqual(ordinary_messages[0]["content"], "ordinary system")

        asyncio.run(run())

    def test_chat_sampling_params_only_apply_to_reply_requests(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x"})
            captured = []

            class FakeResponse:
                status = 200

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def json(self):
                    return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1}}

                async def text(self):
                    return ""

            class FakeSession:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def post(self, *args, **kwargs):
                    captured.append(dict(kwargs["json"]))
                    return FakeResponse()

            with patch("telegram_comfyui_selfie.service.aiohttp.ClientSession", FakeSession):
                await svc._call_llm_messages(
                    [{"role": "user", "content": "hi"}],
                    purpose="chat",
                    tag="chat",
                    session_id="telegram:1",
                    sampling=True,
                )
                await svc._call_llm_messages(
                    [{"role": "user", "content": "summarize"}],
                    purpose="chat",
                    tag="checkpoint",
                    temp=0.1,
                    session_id="telegram:1",
                )

            reply_body, internal_body = captured
            self.assertEqual(reply_body["top_p"], 0.92)
            self.assertEqual(reply_body["frequency_penalty"], 0.4)
            self.assertNotIn("presence_penalty", reply_body)
            for key in ("top_p", "frequency_penalty", "presence_penalty"):
                self.assertNotIn(key, internal_body)

        asyncio.run(run())

    def test_user_current_input_marker_is_not_persisted_to_chat_history(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x"})
            sid = "telegram:1"
            user_text = (
                "【引用内容】\n回复的机器人消息: 上一条\n\n"
                "【图片描述】\n用户发送的图片: 桌上一杯咖啡。\n\n"
                "【用户当前输入】\n看这个"
            )
            svc._call_llm_messages = AsyncMock(return_value={"choices": [{"message": {"content": "看到了，是咖啡。"}}]})
            svc._ensure_life_profile = AsyncMock(return_value={})
            svc._judge_image_moment = AsyncMock(return_value=None)
            svc._update_character_place_from_text = AsyncMock()
            svc._extract_long_term_memories = AsyncMock()

            await svc.run_roleplay_chat(1, sid, user_text)

            history = session_schema.get_chat_history(svc._get_session_state(sid))
            self.assertEqual(history[-2]["role"], "user")
            self.assertIn("【引用内容】", history[-2]["content"])
            self.assertIn("【图片描述】", history[-2]["content"])
            self.assertIn("看这个", history[-2]["content"])
            self.assertNotIn("【用户当前输入】", history[-2]["content"])
            rows = svc.app_store.list_messages(sid, svc._context_character_key(sid))
            self.assertNotIn("【用户当前输入】", "\n".join(row["content"] for row in rows))

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
            # 预处理（life profile / location extract / long memory）会调 LLM，干扰计数；mock 掉。
            svc._ensure_life_profile = AsyncMock(return_value={})
            svc._update_character_place_from_text = AsyncMock()
            svc._extract_long_term_memories = AsyncMock()
            svc.tool_generate_image = AsyncMock(return_value="图片已生成并发送")
            svc._judge_image_moment = AsyncMock(return_value={"intent": "x"})
            svc.send_message = AsyncMock(); svc.send_action = AsyncMock()

            await svc.run_roleplay_chat(1, sid, "给我看看你")
            await asyncio.sleep(0.02)

            svc._judge_image_moment.assert_not_awaited()      # 模型已主动配图 → 不再判断
            svc.tool_generate_image.assert_awaited_once()     # 只出一张

        asyncio.run(run())

    def test_chat_final_omits_tools_and_logs_empty_tool_call_response(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x"})
            sid = "telegram:1"
            calls = []
            logs = []

            async def fake_msgs(messages, tools=None, tool_choice=None, **kw):
                calls.append({
                    "messages": list(messages),
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "tag": kw.get("tag"),
                })
                if len(calls) == 1:
                    return {"choices": [{"message": {"content": "", "tool_calls": [
                        {"id": "t1", "function": {"name": "update_location", "arguments": json.dumps({"place": "office"})}}
                    ]}}]}
                return {"choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "t2", "function": {"name": "generate_roleplay_image", "arguments": json.dumps({"intent": "again"})}}
                ]}}]}

            svc._call_llm_messages = fake_msgs
            svc._execute_tool_call = AsyncMock(return_value="tool ok")
            svc._ensure_life_profile = AsyncMock(return_value={})
            svc._update_character_place_from_text = AsyncMock()
            svc._extract_long_term_memories = AsyncMock()
            svc._judge_image_moment = AsyncMock(return_value={"intent": "should not run"})
            svc._ulog = lambda session_id, kind, text="": logs.append((session_id, kind, text))

            reply = await svc.run_roleplay_chat(1, sid, "hello")

            self.assertEqual(reply, "")
            self.assertEqual(calls[0]["tool_choice"], "auto")
            self.assertIsNotNone(calls[0]["tools"])
            self.assertEqual(calls[1]["tag"], "chat-final")
            self.assertIsNone(calls[1]["tools"])
            self.assertIsNone(calls[1]["tool_choice"])
            svc._judge_image_moment.assert_not_awaited()
            error_logs = [text for _sid, kind, text in logs if kind == "ERROR"]
            self.assertTrue(any("LLM_FULL_LOG" in text for text in error_logs))
            self.assertTrue(any("chat-final returned tool_calls without content" in text for text in error_logs))

        asyncio.run(run())

    def test_dsml_tool_call_content_is_executed_and_not_leaked(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "chat_llm_api_key": "k",
                "chat_llm_model": "m",
                "chat_llm_api_base": "http://x",
                "selfie_frequency": "关闭",
            })
            sid = "telegram:1"
            calls = {"n": 0}
            dsml = (
                "<｜｜DSML｜｜tool_calls>\n"
                "<｜｜DSML｜｜invoke name=\"update_location\">\n"
                "<｜｜DSML｜｜parameter name=\"place\" string=\"true\">餐厅</｜｜DSML｜｜parameter>\n"
                "</｜｜DSML｜｜invoke>\n"
                "</｜｜DSML｜｜tool_calls>"
            )

            async def fake_msgs(messages, tools=None, tool_choice=None, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"choices": [{"message": {"content": dsml}}], "usage": {"prompt_tokens": 10}}
                self.assertIsNone(tools)
                self.assertIsNone(tool_choice)
                tool_messages = [m for m in messages if m.get("role") == "tool"]
                self.assertEqual(len(tool_messages), 1)
                self.assertIn("已记录角色当前在 餐厅", tool_messages[0]["content"])
                return {"choices": [{"message": {"content": "我还在餐厅，刚放下筷子看你消息。"}}]}

            svc._call_llm_messages = fake_msgs
            svc._ensure_life_profile = AsyncMock(return_value={})
            svc._update_character_place_from_text = AsyncMock()
            svc._extract_long_term_memories = AsyncMock()
            svc._judge_image_moment = AsyncMock(return_value=None)
            reply = await svc.run_roleplay_chat(1, sid, "你还在餐厅吗？")

            self.assertEqual(reply, "我还在餐厅，刚放下筷子看你消息。")
            self.assertNotIn("DSML", reply)
            self.assertNotIn("tool_calls", reply)
            self.assertEqual(calls["n"], 2)
            history = session_schema.get_chat_history(svc._get_session_state(sid))
            self.assertEqual(history[-1]["content"], reply)
            self.assertNotIn("DSML", "\n".join(m["content"] for m in history))

        asyncio.run(run())

    def test_dsml_tool_markup_is_stripped_when_final_reply_leaks_again(self):
        svc = self.make_service()
        text = "嗯，我在这里。\n<||DSML||tool_calls><||DSML||invoke name=\"update_location\"><||DSML||parameter name=\"place\">餐厅</||DSML||parameter></||DSML||invoke></||DSML||tool_calls>"
        self.assertEqual(svc._strip_dsml_tool_markup(text), "嗯，我在这里。")

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

    def test_judge_skips_plain_dialog_without_visual_trigger(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x", "selfie_frequency": "适度"})
            sid = "telegram:1"
            svc._get_session_state(sid)["rounds_since_image"] = 9
            svc._call_llm = AsyncMock(return_value=json.dumps({"send": True, "intent": "x"}))

            self.assertIsNone(await svc._judge_image_moment(sid, "你好", "嗯嗯，听着呢。"))
            svc._call_llm.assert_not_awaited()

        asyncio.run(run())

    def test_judge_clears_non_explicit_selfie_view_hint(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x", "selfie_frequency": "适度"})
            sid = "telegram:1"
            svc._get_session_state(sid)["rounds_since_image"] = 3
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "send": True,
                "intent": "凑近镜头确认论文，表情带点紧张又可爱的陪伴感",
                "mood": "好奇又怕看不懂，但愿意陪主人的撒娇感",
                "view": "selfie",
            }, ensure_ascii=False))

            decision = await svc._judge_image_moment(
                sid,
                "汐汐一会儿陪我看两篇论文吧",
                "（凑过来看了一眼，又缩回去）不过既然是主人要看的，汐汐就陪着～是什么论文呀？",
            )

            self.assertIsNotNone(decision)
            self.assertEqual(decision["intent"], "凑近镜头确认论文，表情带点紧张又可爱的陪伴感")
            self.assertEqual(decision["view"], "")

        asyncio.run(run())

    def test_judge_keeps_explicit_selfie_view_hint(self):
        async def run():
            svc = self.make_service()
            svc.config.update({"chat_llm_api_key": "k", "chat_llm_model": "m", "chat_llm_api_base": "http://x", "selfie_frequency": "适度"})
            sid = "telegram:1"
            svc._get_session_state(sid)["rounds_since_image"] = 3
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "send": True,
                "intent": "铺好被子后，自拍道晚安，确认要一起睡时的害羞又甜蜜的轻松感",
                "mood": "温馨甜蜜，略带害羞的放松",
                "view": "selfie",
            }, ensure_ascii=False))

            decision = await svc._judge_image_moment(
                sid,
                "好了不",
                "（轻快地把被子铺好，拍了拍枕头）好啦~被子铺得松松软软的，晚安啦主人，做个好梦~",
            )

            self.assertIsNotNone(decision)
            self.assertEqual(decision["view"], "selfie")

        asyncio.run(run())

    def test_roleplay_image_planner_coerces_judge_selfie_hint_in_same_space_close_interaction(self):
        async def run():
            svc = self.make_service()
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            sid = "telegram:123"
            svc._fetch_weather = AsyncMock(return_value={"desc": "小雨", "temp": "23"})
            svc._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "沙发角落，室内暖光柔和，角色捧着杯子微微前倾，带着紧张又可爱的神情确认要陪你看论文",
                "view": "selfie",
                "new_appearance_tags": "",
                "user_location": "with_user",
                "is_intimate": False,
                "partner_in_frame": False,
                "device_in_frame": False,
            }, ensure_ascii=False))

            plan = await plan_roleplay_image(
                svc,
                sid,
                intent="凑近镜头确认论文，表情带点紧张又可爱的陪伴感",
                prompt="（凑过来看了一眼，又缩回去）不过既然是主人要看的，汐汐就陪着～是什么论文呀？",
                view="selfie",
            )

            self.assertEqual(plan["view"], "pov")
            self.assertFalse(plan["device_in_frame"])

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

    def test_build_prompt_partner_flag_routes_to_everyday_partner_path(self):
        # 日常 partner_in_frame=True：去掉 solo 冲突，但不要误走性爱/亲密特写路径。
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_positive_prefix"] = "1girl, black long hair, purple eyes"
        state["custom_count"] = "1girl"
        pos, neg = svc._build_prompt(
            "First-person POV from the user's viewpoint, Xixi sits at her owner's feet while waiting for him to dry her hair",
            session_id=sid,
            partner_in_frame=True,
        )
        pos_lower = pos.lower()
        self.assertNotIn("solo", pos_lower)
        self.assertIn("partial male feet visible", pos_lower)
        self.assertIn("everyday close interaction", pos_lower)
        self.assertNotIn("male torso", pos_lower)
        self.assertNotIn("intimate close-up", pos_lower)
        self.assertNotIn("male", neg.lower())

    def test_build_prompt_device_in_frame_keeps_selfie_and_phone(self):
        # 用户明确要"做爱时对镜自拍/录像"：device_in_frame=True 应保留自拍/对镜取景与设备，不强制清掉。
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
            self.assertEqual(session_schema.get_wardrobe(state).get("dress"), "red qipao")
            self.assertIn("red qipao", session_schema.get_outfit(state))
            # 再换胸罩：旗袍保留、bra 新增（衣柜持久）
            svc._classify_wardrobe_change = AsyncMock(return_value={"bra": "black bra"})
            await svc._apply_wardrobe(sid, "换个黑色胸罩")
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_wardrobe(state).get("dress"), "red qipao")
            self.assertEqual(session_schema.get_wardrobe(state).get("bra"), "black bra")

        asyncio.run(run())

    def test_apply_wardrobe_migrates_legacy_dynamic_appearance(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_outfit(state, "red dress, black heels")  # 老数据，无 wardrobe
            session_schema.set_wardrobe(state, {})
            # 只换鞋：旧 dress 应被迁移保留，footwear 被替换
            svc._classify_wardrobe_change = AsyncMock(return_value={"footwear": "white sneakers"})
            await svc._apply_wardrobe(sid, "换白色运动鞋")
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_wardrobe(state).get("dress"), "red dress")
            self.assertEqual(session_schema.get_wardrobe(state).get("footwear"), "white sneakers")

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
            closet = session_schema.get_closet(svc._get_session_state(sid))
            self.assertIn("碎花连衣裙", closet)
            self.assertEqual(closet["碎花连衣裙"]["slot"], "dress")
            # 换上衣 → 衣橱新增上衣，碎花裙仍在收藏
            svc._classify_wardrobe_change = AsyncMock(return_value={"top": "blue blouse", "names": {"top": "蓝衬衫"}})
            await svc._apply_wardrobe(sid, "换蓝衬衫")
            closet = session_schema.get_closet(svc._get_session_state(sid))
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
            self.assertEqual(session_schema.get_wardrobe(state), {})
            self.assertEqual(session_schema.get_outfit(state), "")
            self.assertIn("红裙", session_schema.get_closet(state))  # 衣橱收藏保留

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
            "A selfie of a woman, solo, lying beside him after sex",
            session_id=sid,
            is_intimate=True,
        )
        pos_lower = pos.lower()
        self.assertNotIn("selfie", pos_lower)
        self.assertNotIn("solo", pos_lower)
        self.assertIn("first-person pov", pos_lower)
        self.assertIn("partial male body visible", pos_lower)

    def test_chat_system_static_has_interpretation_rules(self):
        """system_static 应包含语言理解和反重复规则。"""
        svc = self.make_service()
        sid = "telegram:1"
        messages = svc._build_chat_messages(sid, "测试")
        static = messages[0]["content"]
        self.assertIn("不是表白或调情", static)
        self.assertIn("不要反复提及", static)

    def test_checkpoint_summarizer_prompt_has_grounding_rule(self):
        """checkpoint 摘要 prompt 应包含反幻觉约束。"""
        svc = self.make_service()
        import inspect
        src = inspect.getsource(svc._summarize_checkpoint)
        self.assertIn("Do not invent", src)
        self.assertIn("literally stated", src)
        self.assertIn("time anchors", src)
        self.assertIn("deadlines", src)

    def test_memory_extractor_prompt_has_strong_grounding(self):
        """记忆提取 prompt 应包含明确的反编造规则约束。"""
        svc = self.make_service()
        import inspect
        src = inspect.getsource(svc._extract_long_term_memories)
        self.assertIn("只从对话原文提取", src)
        self.assertIn("不要推断", src)
        self.assertIn("时间节点", src)
        self.assertIn("作为 event 记忆保存", src)

    def test_history_summary_prompt_has_grounding(self):
        """角色历史提要 prompt 应包含反幻觉约束。"""
        svc = self.make_service()
        import inspect
        src = inspect.getsource(svc._generate_character_history_summary)
        self.assertIn("不要编造", src)
        self.assertIn("只基于日记原文", src)

    def test_dream_memory_prompt_keeps_time_nodes_until_faded(self):
        """dream 记忆整理应软约束过时时间节点，不是一过期就删。"""
        svc = self.make_service()
        import inspect
        incremental = inspect.getsource(svc._incremental_organize_memories)
        summarize = inspect.getsource(svc._summarize_all_memories)
        self.assertIn("time nodes", incremental)
        self.assertIn("fully faded", incremental)
        self.assertIn("do not drop them merely", summarize)

    def test_scene_stale_hint_when_gap_exceeds_threshold(self):
        """场景断档感知: 距离上次对话超过阈值时在 system_dynamic 注入提示。"""
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        stale_sec = 30 * 60  # 默认 30 分钟
        session_schema.set_last_interaction(state, time.time() - stale_sec - 60)
        svc.config["scene_stale_minutes"] = "30"

        # 避免 LLM 调用：_build_chat_messages 不调 LLM，只查 last_interaction + clock
        messages = svc._build_chat_messages(sid, "你好")
        all_system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        self.assertIn("距离上次对话已过超过半小时", all_system)
        self.assertIn("之前的日常场景可能已自然结束", all_system)

    def test_no_scene_stale_hint_when_gap_within_threshold(self):
        """断档未超阈值时不应注入场景提示。"""
        svc = self.make_service()
        sid = "telegram:2"
        state = svc._get_session_state(sid)
        session_schema.set_last_interaction(state, time.time() - 60)  # 仅 1 分钟前
        svc.config["scene_stale_minutes"] = "30"

        messages = svc._build_chat_messages(sid, "你好")
        all_system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        self.assertNotIn("距离上次对话已过超过半小时", all_system)


class CheckpointTrimTestCase(ServiceFixtureMixin, unittest.TestCase):
    """TODO #9.4: checkpoint 裁剪测试 — 51+ messages 后 checkpoint，窗口 10 messages，不能 assistant 开头。"""

    def test_checkpoint_trims_to_keep_and_never_starts_with_assistant(self):
        async def run():
            svc = self.make_service()
            # _context_window_message_limit 最小值是 10（max(10, ...)），设 10 写 12 条触发 checkpoint
            svc.config["context_window_message_limit"] = "10"
            svc.config["checkpoint_keep_message_limit"] = "2"
            svc.config["checkpoint_hard_limit_chars"] = "9999"
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            # 写入 12 条消息（user/assistant 交替，最后是 assistant）
            messages = []
            for i in range(6):
                messages.append({"role": "user", "content": f"用户消息 {i}"})
                messages.append({"role": "assistant", "content": f"角色回复 {i}"})
            ids = svc.app_store.append_messages(sid, key, messages)
            self.assertEqual(len(ids), 12)

            # 手动跑 checkpoint（mock LLM 摘要，避免真实调用）
            async def fake_summarize(session_id, previous, msgs):
                return "CHECKPOINT SUMMARY"
            svc._summarize_checkpoint = fake_summarize
            svc._extract_long_term_memories_from_messages = AsyncMock()

            await svc._run_context_checkpoint(sid, key, keep=2)

            # chat_history 应被裁剪到 keep 条，且开头是 user（不是 assistant）
            state = svc._get_session_state(sid)
            history = state.get("chat_history", [])
            self.assertLessEqual(len(history), 2)
            if history:
                self.assertEqual(history[0].get("role"), "user",
                                 "裁剪后窗口不应从 assistant 半轮开始")
            # checkpoint_summary 已写入
            self.assertEqual(state.get("checkpoint_summary"), "CHECKPOINT SUMMARY")

        asyncio.run(run())

    def test_checkpoint_overflow_includes_orphan_system_trimmed_from_kept_window(self):
        async def run():
            svc = self.make_service()
            svc.config["context_window_message_limit"] = "10"
            svc.config["checkpoint_keep_message_limit"] = "4"
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            messages = []
            for i in range(5):
                messages.append({"role": "user", "content": f"用户消息 {i}"})
                messages.append({"role": "assistant", "content": f"角色回复 {i}"})
            messages.extend([
                {"role": "assistant", "content": "孤立角色回复，应进入 checkpoint"},
                {"role": "system", "content": "照片历史 system，应进入 checkpoint"},
                {"role": "user", "content": "最后用户消息，应保留"},
                {"role": "assistant", "content": "最后角色回复，应保留"},
            ])
            svc.app_store.append_messages(sid, key, messages)
            state = svc._get_session_state(sid)
            session_schema.set_chat_history(state, messages)

            captured = []

            async def fake_summarize(session_id, previous, msgs):
                captured.extend(msgs)
                return "CHECKPOINT SUMMARY"

            svc._summarize_checkpoint = fake_summarize
            svc._extract_long_term_memories_from_messages = AsyncMock()

            await svc._run_context_checkpoint(sid, key, keep=4)

            captured_text = "\n".join(str(m.get("content") or "") for m in captured)
            self.assertIn("孤立角色回复，应进入 checkpoint", captured_text)
            self.assertIn("照片历史 system，应进入 checkpoint", captured_text)

            kept = session_schema.get_chat_history(state)
            self.assertEqual([m.get("content") for m in kept], ["最后用户消息，应保留", "最后角色回复，应保留"])
            self.assertEqual(kept[0].get("role"), "user")

        asyncio.run(run())


class DreamManualMemoryTestCase(ServiceFixtureMixin, unittest.TestCase):
    """TODO #9.5: dream 记忆整理测试 — manual 记忆不被 update/delete。"""

    def test_write_dream_diary_prompts_first_person_without_postprocessing(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            captured = {}
            svc.has_llm_config = lambda purpose, session_id="": True
            raw_diary = "# 雨后的约定\n今天我终于把心里的话说出来了。"

            async def fake_call_llm(system, user, **kwargs):
                captured["system"] = system
                captured["user"] = user
                return raw_diary

            svc._call_llm = fake_call_llm

            diary = await svc._write_dream_diary(
                sid,
                "2026-06-26",
                "User: 晚安\nAssistant: 我会等你。",
                "# 2026-06-26 星期五 旧日记\n之前写过的内容。",
                reason="manual",
            )

            self.assertEqual(diary, raw_diary)
            self.assertIn("first-person", captured["system"])
            self.assertIn("# 2026-06-26 星期五 标题", captured["system"])
            self.assertIn("will replace that old entry", captured["system"])
            self.assertIn("not append to it", captured["system"])
            self.assertIn("Do not include roleplay advice", captured["system"])
            self.assertIn("Weekday: 星期五", captured["user"])
            self.assertIn("Write mode: overwrite existing diary", captured["user"])

        asyncio.run(run())

    def test_dream_memory_organize_skips_manual(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "test-character"
            # 写入一条 manual 记忆和一条自动记忆
            svc.memory.add_memory(sid, "manual", "手动记忆-不应被改", character=key, importance=5, tags=["手动"], source="manual")
            svc.memory.add_memory(sid, "preference", "自动记忆-可被整理", character=key, importance=3, tags=["auto"], source="chat")
            memories = svc.memory.list_memories(sid, character=key, limit=10)
            manual_id = next(m["id"] for m in memories if m.get("kind") == "manual")
            auto_id = next(m["id"] for m in memories if m.get("kind") == "preference")

            # mock LLM 返回 ops：尝试 delete manual 和 update auto
            async def fake_call_llm(system, user, **kw):
                return json.dumps({"ops": [
                    {"op": "delete", "id": manual_id},
                    {"op": "update", "id": manual_id, "summary": "被改了"},
                    {"op": "update", "id": auto_id, "summary": "自动记忆已更新"},
                ]})
            svc._call_llm = fake_call_llm
            svc.has_llm_config = lambda purpose, session_id="": True

            await svc._organize_memories_after_dream(sid, key)

            # manual 记忆应仍存在且内容不变
            memories_after = svc.memory.list_memories(sid, character=key, limit=10)
            manual_after = next((m for m in memories_after if m["id"] == manual_id), None)
            self.assertIsNotNone(manual_after, "manual 记忆不应被删除")
            self.assertEqual(manual_after.get("summary"), "手动记忆-不应被改",
                             "manual 记忆不应被 update")
            self.assertEqual(manual_after.get("kind"), "manual")

        asyncio.run(run())

    def test_dream_source_ignores_system_history_messages(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            messages = [
                {"role": "user", "content": "用户真实对话"},
                {"role": "system", "content": "照片历史 system 不应进入 dream"},
                {"role": "assistant", "content": "角色真实回复"},
            ]
            svc.app_store.append_messages(sid, key, messages)
            captured = {}

            async def fake_write_dream_diary(session_id, diary_date, source_text, existing_diary="", *, reason=""):
                captured["source_text"] = source_text
                return "梦境日记"

            svc._write_dream_diary = fake_write_dream_diary
            svc._organize_memories_after_dream = AsyncMock()
            svc._generate_character_history_summary = AsyncMock()

            await svc._dream_once(sid, key, datetime(2026, 6, 24, tzinfo=timezone.utc), reason="manual")

            source_text = captured.get("source_text", "")
            self.assertIn("User: 用户真实对话", source_text)
            self.assertIn("Assistant: 角色真实回复", source_text)
            self.assertNotIn("照片历史 system 不应进入 dream", source_text)

        asyncio.run(run())

    def test_dream_adds_current_character_style_to_global_pool(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            session_schema.set_character_value(state, "custom_current_style", "@new_style, artist:wlop")
            key = svc._context_character_key(sid)
            svc.config["style_pool"] = "@base"
            svc.config["current_style"] = "@base"
            svc.app_store.append_messages(sid, key, [
                {"role": "user", "content": "用户真实对话"},
                {"role": "assistant", "content": "角色真实回复"},
            ])
            svc._write_dream_diary = AsyncMock(return_value="梦境日记")
            svc._organize_memories_after_dream = AsyncMock()
            svc._generate_character_history_summary = AsyncMock()

            await svc._dream_once(sid, key, datetime(2026, 6, 24, tzinfo=timezone.utc), reason="manual")

            self.assertEqual(svc._normalize_style_pool(), ["@base", "@new_style, artist:wlop"])
            self.assertEqual(svc.config["current_style"], "@base")

        asyncio.run(run())

    def test_dream_does_not_add_empty_character_style_to_pool(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            session_schema.set_character_value(state, "custom_current_style", "")
            key = svc._context_character_key(sid)
            svc.config["style_pool"] = "@base"
            svc.config["current_style"] = "@base"
            svc.app_store.append_messages(sid, key, [
                {"role": "user", "content": "用户真实对话"},
                {"role": "assistant", "content": "角色真实回复"},
            ])
            svc._write_dream_diary = AsyncMock(return_value="梦境日记")
            svc._organize_memories_after_dream = AsyncMock()
            svc._generate_character_history_summary = AsyncMock()

            await svc._dream_once(sid, key, datetime(2026, 6, 24, tzinfo=timezone.utc), reason="manual")

            self.assertEqual(svc._normalize_style_pool(), ["@base"])

        asyncio.run(run())


class GitUpdatePermissionTestCase(ServiceFixtureMixin, unittest.TestCase):
    """TODO #9: Git 更新权限测试 — 仅管理员可触发。"""

    def test_is_admin_chat_uses_admin_chat_ids_first(self):
        svc = self.make_service()
        svc.config["admin_chat_ids"] = ["111", "222"]
        svc.config["allowed_chat_ids"] = ["333"]
        self.assertTrue(svc._is_admin_chat(111))
        self.assertTrue(svc._is_admin_chat("222"))
        self.assertFalse(svc._is_admin_chat(333))  # 在 allowed 但不在 admin

    def test_is_admin_chat_falls_back_to_allowed_when_admin_empty(self):
        svc = self.make_service()
        svc.config["admin_chat_ids"] = []
        svc.config["allowed_chat_ids"] = ["444"]
        self.assertTrue(svc._is_admin_chat(444))  # 回退到 allowed
        self.assertFalse(svc._is_admin_chat(999))

    def test_git_proxy_env_converts_socks5_to_socks5h(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "socks5://127.0.0.1:7891"
        env = svc._git_proxy_env()
        self.assertEqual(env.get("ALL_PROXY"), "socks5h://127.0.0.1:7891")

    def test_git_proxy_env_http_proxy(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "http://127.0.0.1:7890"
        env = svc._git_proxy_env()
        self.assertEqual(env.get("HTTP_PROXY"), "http://127.0.0.1:7890")
        self.assertEqual(env.get("HTTPS_PROXY"), "http://127.0.0.1:7890")

    def test_git_update_rejects_non_admin(self):
        async def run():
            svc = self.make_service()
            svc.config["admin_chat_ids"] = ["111"]
            svc.send_message = AsyncMock()
            await svc.cmd_git_update(999, "telegram:999", "")
            msg = svc.send_message.await_args.args[1]
            self.assertIn("无权限", msg)
        asyncio.run(run())


class ExternalProxyTestCase(ServiceFixtureMixin, unittest.TestCase):
    """外部 POI 请求复用 Telegram 代理配置。"""

    def test_external_http_proxy_disabled(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = False
        proxy, connector = svc._external_http_proxy()
        self.assertIsNone(proxy)
        self.assertIsNone(connector)

    def test_external_http_proxy_http(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "http://127.0.0.1:7890"
        proxy, connector = svc._external_http_proxy()
        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertIsNone(connector)

    def test_external_http_proxy_socks(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "socks5://127.0.0.1:7891"
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            self.skipTest("aiohttp_socks not installed")
        async def _run():
            return svc._external_http_proxy()
        proxy, connector = asyncio.run(_run())
        self.assertIsNone(proxy)
        self.assertIsInstance(connector, ProxyConnector)


class LLMUsageTestCase(ServiceFixtureMixin, unittest.TestCase):
    """LLM usage 记录与看板接口测试。"""

    def test_record_usage_from_response_with_cache_hit_tokens(self):
        svc = self.make_service()
        resolved = {
            "profile_id": "deepseek",
            "model": "deepseek-v4",
            "api_key": "k",
        }
        data = {
            "usage": {
                "prompt_tokens": 1234,
                "completion_tokens": 567,
                "total_tokens": 1801,
                "prompt_cache_hit_tokens": 1000,
                "prompt_cache_miss_tokens": 234,
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="plan", purpose="image", session_id="telegram:1")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model", "purpose", "tag"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["profile_id"], "deepseek")
        self.assertEqual(row["model"], "deepseek-v4")
        self.assertEqual(row["purpose"], "image")
        self.assertEqual(row["tag"], "plan")
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["prompt_tokens"], 1234)
        self.assertEqual(row["completion_tokens"], 567)
        self.assertEqual(row["cached_tokens"], 1000)
        self.assertEqual(row["total_tokens"], 1801)

    def test_record_usage_from_response_with_cached_tokens_fallback(self):
        svc = self.make_service()
        resolved = {"profile_id": "", "model": "gpt-4o", "api_key": "k"}
        data = {
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 200,
                "prompt_cached_tokens": 600,
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="chat", purpose="chat")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model", "purpose", "tag"))
        row = next(r for r in rows if r["tag"] == "chat")
        self.assertEqual(row["cached_tokens"], 600)
        self.assertEqual(row["total_tokens"], 1000)

    def test_record_usage_from_response_with_prompt_tokens_details_cached(self):
        svc = self.make_service()
        resolved = {"profile_id": "mimo", "model": "mimo-v2.5-pro", "api_key": "k"}
        data = {
            "usage": {
                "prompt_tokens": 5293,
                "completion_tokens": 291,
                "total_tokens": 5584,
                "prompt_tokens_details": {
                    "cached_tokens": 4096,
                    "cache_write_tokens": 0,
                },
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="chat", purpose="chat", session_id="telegram:1")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model", "purpose", "tag"))
        row = rows[0]
        self.assertEqual(row["cached_tokens"], 4096)
        self.assertEqual(row["prompt_tokens"], 5293)

        summary = svc._llm_usage_debug_summary(data)
        self.assertEqual(summary["cached_tokens"], 4096)
        self.assertEqual(summary["cache_hit_rate"], round(4096 / 5293, 4))

    def test_record_usage_from_response_cache_miss_inference(self):
        svc = self.make_service()
        resolved = {"profile_id": "ds", "model": "deepseek-chat", "api_key": "k"}
        data = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_cache_miss_tokens": 300,
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="translate", purpose="image")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model"))
        row = rows[0]
        self.assertEqual(row["cached_tokens"], 700)
        self.assertEqual(row["prompt_tokens"], 1000)

    def test_aggregate_usage_by_time_range(self):
        svc = self.make_service()
        now = time.time()
        svc.app_store.record_llm_usage(profile_id="p1", model="m1", purpose="chat", tag="reply", prompt_tokens=100, completion_tokens=50, total_tokens=150)
        svc.app_store.record_llm_usage(profile_id="p1", model="m1", purpose="chat", tag="reply", prompt_tokens=200, completion_tokens=100, total_tokens=300)
        svc.app_store.record_llm_usage(profile_id="p2", model="m2", purpose="image", tag="plan", prompt_tokens=300, completion_tokens=50, total_tokens=350)
        rows = svc.app_store.aggregate_llm_usage(after=now - 60, before=now + 60, group_by=("profile_id", "purpose"))
        self.assertEqual(len(rows), 2)
        p1 = next(r for r in rows if r["profile_id"] == "p1")
        self.assertEqual(p1["requests"], 2)
        self.assertEqual(p1["total_tokens"], 450)
        p2 = next(r for r in rows if r["profile_id"] == "p2")
        self.assertEqual(p2["requests"], 1)
        self.assertEqual(p2["total_tokens"], 350)
        # 过滤旧数据
        old_rows = svc.app_store.aggregate_llm_usage(after=now + 10, before=now + 60)
        self.assertEqual(old_rows, [])

    def test_cache_hit_rate_calculation(self):
        svc = self.make_service()
        svc.app_store.record_llm_usage(profile_id="p", model="m", purpose="chat", tag="t", prompt_tokens=1000, cached_tokens=250, total_tokens=1200)
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id",))
        self.assertEqual(rows[0]["cached_tokens"], 250)
        self.assertEqual(rows[0]["prompt_tokens"], 1000)

    def test_webui_llm_usage_requires_admin(self):
        from aiohttp import web
        from telegram_comfyui_selfie.webui import api_admin_llm_usage

        async def run():
            svc = self.make_service()
            app = web.Application()
            app["service"] = svc
            req = make_mock_request(app, "/api/admin/llm-usage", method="GET")
            # 非管理员请求应 403
            with self.assertRaises(web.HTTPForbidden):
                await api_admin_llm_usage(req)

        asyncio.run(run())

    def test_webui_llm_usage_returns_summary(self):
        from aiohttp import web
        from telegram_comfyui_selfie.webui import api_admin_llm_usage

        async def run():
            svc = self.make_service()
            svc.app_store.record_llm_usage(profile_id="p", model="m", purpose="chat", tag="t", prompt_tokens=100, completion_tokens=50, cached_tokens=20, total_tokens=150)
            app = web.Application()
            app["service"] = svc
            req = make_mock_request(app, "/api/admin/llm-usage", method="GET", admin=True)
            resp = await api_admin_llm_usage(req)
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.text)
            self.assertTrue(data.get("ok"))
            summary = data.get("summary", {})
            self.assertEqual(summary.get("requests"), 1)
            self.assertEqual(summary.get("prompt_tokens"), 100)
            self.assertEqual(summary.get("cached_tokens"), 20)
            self.assertEqual(summary.get("cache_hit_rate"), 0.2)
            groups = data.get("groups", [])
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].get("profile_id"), "p")

        asyncio.run(run())

    def test_llm_debug_records_are_buffered_and_keep_recent_ten(self):
        svc = self.make_service()
        path = svc._llm_debug_log_path()
        resolved = {
            "profile_id": "debug-profile",
            "model": "debug-model",
            "thinking": False,
        }

        def record(index: int, tag: str = "reply"):
            svc._record_llm_debug(
                purpose="chat",
                tag=tag,
                session_id="telegram:1",
                resolved=resolved,
                request_url="https://example.invalid/v1/chat/completions",
                request_body={
                    "model": "debug-model",
                    "messages": [{"role": "user", "content": f"message-{index}"}],
                },
                response={
                    "choices": [{"message": {"content": f"response-{index}"}}],
                    "usage": {
                        "prompt_tokens": 100 + index,
                        "completion_tokens": 10,
                        "total_tokens": 110 + index,
                        "prompt_cache_hit_tokens": 80 + index,
                    },
                },
                status=200,
            )

        for i in range(9):
            record(i)
        self.assertFalse(path.exists(), "不足 10 条时不应落盘，避免频繁 IO")

        record(9)
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["entries_by_type"]["chat:reply"]
        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[0]["request"]["body"]["messages"][0]["content"], "message-0")
        self.assertEqual(entries[-1]["response"]["choices"][0]["message"]["content"], "response-9")
        self.assertEqual(entries[-1]["usage"]["cached_tokens"], 89)
        self.assertIn("replace whole file after 10", data.get("flush_policy", ""))

        for i in range(10, 20):
            record(i)
        data2 = json.loads(path.read_text(encoding="utf-8"))
        entries2 = data2["entries_by_type"]["chat:reply"]
        self.assertEqual(len(entries2), 10)
        self.assertEqual(entries2[0]["request"]["body"]["messages"][0]["content"], "message-10")
        self.assertEqual(entries2[-1]["request"]["body"]["messages"][0]["content"], "message-19")

        record(20, tag="scene")
        svc._flush_llm_debug(force=True)
        data3 = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("chat:reply", data3["entries_by_type"])
        self.assertEqual(data3["entries_by_type"]["chat:scene"][-1]["request"]["body"]["messages"][0]["content"], "message-20")


class LlmPromptCompareScriptTestCase(unittest.TestCase):
    """LLM prompt 比对脚本测试。"""

    def test_compare_entries_reports_prefix_and_non_prefix_same_messages(self):
        from scripts.compare_llm_chat_prompts import build_entry_view, compare_entries

        def entry(messages):
            return {
                "session_id": "telegram:1",
                "time": "2026-06-26T10:00:00",
                "ts": 1,
                "request": {
                    "body": {
                        "model": "m",
                        "temperature": 0.7,
                        "tools": [{"type": "function", "function": {"name": "tool_a"}}],
                        "tool_choice": "auto",
                        "messages": messages,
                    }
                },
                "usage": {"prompt_tokens": 1000, "cached_tokens": 500},
            }

        old = build_entry_view(0, entry([
            {"role": "system", "content": "stable-a"},
            {"role": "system", "content": "stable-b"},
            {"role": "user", "content": "old-only"},
            {"role": "assistant", "content": "same-after-diff"},
        ]))
        new = build_entry_view(1, entry([
            {"role": "system", "content": "stable-a"},
            {"role": "system", "content": "stable-b"},
            {"role": "user", "content": "new-only"},
            {"role": "assistant", "content": "same-after-diff"},
            {"role": "user", "content": "append"},
        ]))

        comparison = compare_entries(old, new)

        self.assertTrue(comparison.prompt_changed)
        self.assertEqual(comparison.common_prefix_messages, 2)
        self.assertGreater(comparison.common_prefix_chars, 0)
        self.assertEqual(comparison.non_prefix_common_messages, 1)
        self.assertEqual(comparison.non_prefix_lcs_messages, 1)
        self.assertEqual(comparison.same_index_after_prefix, [3])
        self.assertTrue(comparison.prompt_components_same["tools"])
        self.assertFalse(comparison.prompt_components_same["messages"])
        self.assertTrue(comparison.settings_same)

    def test_provider_cache_tokens_reads_nested_usage_fields(self):
        from scripts.compare_llm_chat_prompts import build_entry_view, cache_rate, provider_cache_tokens

        usage = {
            "prompt_tokens": 5000,
            "prompt_tokens_details": {
                "cached_tokens": 4096,
            },
        }

        self.assertEqual(provider_cache_tokens(usage), 4096)
        self.assertAlmostEqual(cache_rate(usage), 4096 / 5000)

        view = build_entry_view(0, {
            "session_id": "telegram:1",
            "time": "2026-06-28T20:00:00",
            "ts": 1,
            "request": {"body": {"messages": []}},
            "usage": {
                "prompt_tokens": 5000,
                "cached_tokens": 0,
                "raw": usage,
            },
        })
        self.assertEqual(provider_cache_tokens(view.usage), 4096)


class ModelProfileTestCase(ServiceFixtureMixin, unittest.TestCase):
    """模型 profile 固定思考、去 kimi 等配置测试。"""

    def test_default_profiles_contain_only_expected_models(self):
        from telegram_comfyui_selfie.defaults import DEFAULT_CONFIG

        profiles = DEFAULT_CONFIG["global_model_profiles"]
        ids = set(profiles.keys())
        self.assertEqual(ids, {"deepseek-pro", "deepseek-flash", "glm"})
        for pid, profile in profiles.items():
            self.assertTrue(profile.get("thinking_fixed"), f"{pid} 应声明 thinking_fixed")

    def test_thinking_fixed_ignores_user_settings(self):
        async def run():
            svc = self.make_service()
            # 默认 chat=deepseek-pro（固定开）、fast=deepseek-flash（固定关）
            svc.app_store.update_user_model_settings(
                "1", chat_profile_id="deepseek-pro", chat_thinking=False,
                fast_profile_id="deepseek-flash", fast_thinking=True,
            )
            _, _, chat_thinking = svc._resolve_llm_profile("chat", "telegram:1")
            _, _, fast_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertTrue(chat_thinking, "deepseek-pro 思考固定开启，用户设置关闭应被忽略")
            self.assertFalse(fast_thinking, "deepseek-flash 思考固定关闭，用户设置开启应被忽略")

            # 切到 glm（固定关）
            svc.app_store.update_user_model_settings("1", fast_profile_id="glm", fast_thinking=True)
            _, _, glm_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertFalse(glm_thinking, "glm 思考固定关闭")

        asyncio.run(run())

    def test_custom_profile_uses_model_bound_thinking(self):
        async def run():
            svc = self.make_service()
            svc.app_store.upsert_model_profile("1", "custom", {
                "name": "Custom", "base_url": "http://localhost/v1", "api_key": "k",
                "model": "custom-model", "timeout": 120, "disable_thinking": True,
            })
            svc.app_store.update_user_model_settings("1", chat_profile_id="custom", chat_thinking=True)
            _, _, thinking = svc._resolve_llm_profile("chat", "telegram:1")
            self.assertFalse(thinking, "用户级 thinking 覆盖已移除，应完全跟随模型 profile")

        asyncio.run(run())

    def test_vision_profile_is_optional_and_user_scoped(self):
        async def run():
            svc = self.make_service()
            self.assertFalse(svc.has_llm_config("vision", "telegram:1"))
            svc.app_store.upsert_model_profile("1", "vision", {
                "name": "Vision", "base_url": "http://localhost/v1", "api_key": "vk",
                "model": "vision-model", "disable_thinking": True,
            })
            svc.app_store.update_user_model_settings("1", vision_profile_id="vision")

            self.assertTrue(svc.has_llm_config("vision", "telegram:1"))
            self.assertFalse(svc.has_llm_config("vision", "telegram:2"))
            profile_id, _, thinking = svc._resolve_llm_profile("vision", "telegram:1")
            self.assertEqual(profile_id, "vision")
            self.assertFalse(thinking)

        asyncio.run(run())

    def test_resolved_config_honors_fixed_thinking_for_glm(self):
        async def run():
            svc = self.make_service()
            svc.app_store.update_user_model_settings("1", chat_profile_id="glm")
            resolved = svc._resolved_llm_config("chat", "telegram:1")
            self.assertFalse(resolved["thinking"])
            self.assertEqual(resolved["thinking_control"], "param")

        asyncio.run(run())


class ConfigStoreTestCase(unittest.TestCase):
    """config_store YAML 解析器测试。"""

    def test_load_nested_model_profiles(self):
        from telegram_comfyui_selfie.config_store import load_simple_yaml, flatten_config

        yml = """
models:
  default_chat_model_profile: "deepseek-pro"
  global_model_profiles:
    deepseek-pro:
      name: "DeepSeek V4 Pro"
      api_key: "k"
      base_url: "https://opencode.ai/zen/go/v1"
      model: "deepseek-v4-pro"
      timeout: 300
      disable_thinking: false
      thinking_fixed: true
    glm:
      name: "GLM 5.2"
      api_key: "k"
      base_url: "https://opencode.ai/zen/go/v1"
      model: "glm-5.2"
      timeout: 300
      disable_thinking: true
      thinking_fixed: true
""".strip()
        path = Path(self.make_temp_dir()) / "config.yml"
        path.write_text(yml, encoding="utf-8")
        flat = flatten_config(load_simple_yaml(path))
        self.assertEqual(set(flat["global_model_profiles"].keys()), {"deepseek-pro", "glm"})
        self.assertTrue(flat["global_model_profiles"]["deepseek-pro"]["thinking_fixed"])
        self.assertTrue(flat["global_model_profiles"]["glm"]["disable_thinking"])

    def test_yaml_roundtrip_preserves_nested_dicts_and_literal_blocks(self):
        from telegram_comfyui_selfie.config_store import load_simple_yaml, flatten_config, dump_simple_yaml

        yml = """
role_defaults:
  outfit_keywords: |
    dress
    shirt
  current_style: "@00 gx4"
models:
  global_model_profiles:
    glm:
      name: "GLM 5.2"
      disable_thinking: true
""".strip()
        base = Path(self.make_temp_dir())
        path = base / "config.yml"
        path.write_text(yml, encoding="utf-8")
        loaded = load_simple_yaml(path)
        dumped = dump_simple_yaml(flatten_config(loaded))
        (base / "config2.yml").write_text(dumped, encoding="utf-8")
        rt = load_simple_yaml(base / "config2.yml")
        self.assertEqual(
            flatten_config(loaded)["global_model_profiles"],
            flatten_config(rt)["global_model_profiles"],
        )
        self.assertIn("\n", flatten_config(rt)["outfit_keywords"])

    def make_temp_dir(self) -> str:
        return str(make_project_temp_dir("config"))


class SessionStateMigrationTestCase(ServiceFixtureMixin, unittest.TestCase):
    """state.json -> SQLite 迁移测试。"""

    def test_state_json_migrates_to_sqlite_on_first_load(self):
        import json as _json

        tmp = make_project_temp_dir("state_json")
        config_path = tmp / "config.yml"
        config_path.write_text("telegram:\n  telegram_bot_token: \"t\"\n", encoding="utf-8")
        state_path = tmp / "state.json"

        # 写一个旧版 state.json
        old_state = {
            "sessions": {
                "telegram:42": {
                    "custom_character": "迁移测试角色",
                    "custom_bot_name": "迁移测试",
                    "purity": 7,
                    "sent_photos_history": [],
                    "chat_history": [],
                },
            },
            "city_place_catalogs": {
                "shanghai": {
                    "city": "上海",
                    "updated_at": 1000,
                    "places": {"home": ["家"]},
                    "source": "test",
                },
            },
        }
        state_path.write_text(_json.dumps(old_state, ensure_ascii=False), encoding="utf-8")

        svc = TelegramComfyUIService(config_path, state_path)

        # 迁移后应从 SQLite 读取
        self.assertEqual(svc.sessions["telegram:42"]["custom_character"], "迁移测试角色")
        self.assertEqual(svc.sessions["telegram:42"]["purity"], 7)
        self.assertEqual(svc.city_place_catalogs["shanghai"]["city"], "上海")

        # SQLite 应有数据
        self.assertTrue(svc.app_store.has_session_states())
        sqlite_state = svc.app_store.load_session_state("telegram:42")
        self.assertEqual(sqlite_state["custom_character"], "迁移测试角色")
        self.assertEqual(sqlite_state["character"]["custom_bot_name"], "迁移测试")
        self.assertIn("context", sqlite_state)
        self.assertTrue(list(tmp.glob("state.state-json-migration-backup-*.json")))

        # city_catalogs 也应在 SQLite
        sqlite_catalog = svc.app_store.load_city_catalog("shanghai")
        self.assertEqual(sqlite_catalog["city"], "上海")

    def test_legacy_sqlite_state_boxes_migrate_on_restart_with_backup(self):
        tmp = make_project_temp_dir("sqlite_box_migration")
        config_path = tmp / "config.json"
        state_path = tmp / "state.json"
        config_path.write_text(json.dumps({"telegram_bot_token": "TEST"}, ensure_ascii=False), encoding="utf-8")

        svc = TelegramComfyUIService(config_path, state_path)
        sid = "telegram:box"
        svc.app_store.save_session_state(sid, {
            "custom_bot_name": "旧角色",
            "custom_positive_prefix": "1girl, red eyes",
            "purity": 5,
            "dynamic_appearance": "blue dress",
            "chat_history": [{"role": "user", "content": "旧消息"}],
            "user_place": "home",
            "saved_characters": {"旧角色": {"character": "旧角色"}},
        })
        self.assertFalse(list(tmp.glob("memory.box-migration-backup-*.sqlite3")))

        restarted = TelegramComfyUIService(config_path, state_path)
        loaded = restarted.app_store.load_session_state(sid)
        self.assertEqual(loaded["character"]["custom_bot_name"], "旧角色")
        self.assertEqual(loaded["character"]["custom_positive_prefix"], "1girl, red eyes")
        self.assertEqual(loaded["clothing"]["dynamic_appearance"], "blue dress")
        self.assertEqual(loaded["context"]["chat_history"][0]["content"], "旧消息")
        self.assertEqual(loaded["place"]["user_place"], "home")
        self.assertEqual(loaded["session"]["saved_characters"]["旧角色"]["character"], "旧角色")
        backups = list(tmp.glob("memory.box-migration-backup-*.sqlite3"))
        self.assertEqual(len(backups), 1)

        TelegramComfyUIService(config_path, state_path)
        self.assertEqual(len(list(tmp.glob("memory.box-migration-backup-*.sqlite3"))), 1)

    def test_save_and_load_session_state_via_sqlite(self):
        svc = self.make_service()
        sid = "telegram:99"
        state = svc._get_session_state(sid)
        state["custom_character"] = "SQLite测试"
        state["purity"] = 3
        svc._save_session_state(sid, state)

        # 重新加载
        svc2 = TelegramComfyUIService(svc.config_path, svc.state_path)
        loaded = svc2._get_session_state(sid)
        self.assertEqual(loaded["custom_character"], "SQLite测试")
        self.assertEqual(loaded["purity"], 3)

    def test_delete_session_removes_from_sqlite(self):
        svc = self.make_service()
        sid = "telegram:77"
        state = svc._get_session_state(sid)
        state["custom_character"] = "待删除"
        svc._save_session_state(sid, state)
        self.assertIsNotNone(svc.app_store.load_session_state(sid))

        svc.sessions.pop(sid, None)
        svc.app_store.delete_session_state(sid)
        self.assertIsNone(svc.app_store.load_session_state(sid))

    def test_city_catalog_persists_to_sqlite(self):
        svc = self.make_service()
        svc._store_city_catalog("beijing", "北京", {"home": ["家"], "park": ["朝阳公园"]}, "test")
        # 内存中有
        self.assertEqual(svc.city_place_catalogs["beijing"]["city"], "北京")
        # SQLite 中也有
        catalog = svc.app_store.load_city_catalog("beijing")
        self.assertEqual(catalog["city"], "北京")
        self.assertEqual(catalog["places"]["park"], ["朝阳公园"])

        # 重新加载
        svc2 = TelegramComfyUIService(svc.config_path, svc.state_path)
        self.assertEqual(svc2.city_place_catalogs["beijing"]["city"], "北京")
