import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from telegram_comfyui_selfie import TelegramComfyUIService
from telegram_comfyui_selfie.image_planning import format_dialog_context, format_sent_photo_context
from telegram_comfyui_selfie.webui import cast_config_value, masked_config, session_summary


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


    def test_persona_reset_clears_character_and_restores_global_default(self):
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

            await svc.cmd_persona_cancel(1, sid, "")

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

    def test_persona_reset_clears_conversation_and_character_pool(self):
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

            await svc.cmd_persona_cancel(1, sid, "")

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

    def test_character_and_personalize_reset_share_full_reset(self):
        async def run():
            for cmd, arg in (("cmd_character", "reset"), ("cmd_personalize", "reset")):
                svc = self.make_service()
                sid = "telegram:1"
                state = svc._get_session_state(sid)
                state.update({"custom_character": "X", "custom_scheduled_persona": "p", "persona_user_set": True})
                svc._save_session_state(sid, state)
                svc.send_message = AsyncMock()
                await getattr(svc, cmd)(1, sid, arg)
                self.assertFalse(svc._is_character_set(sid), f"{cmd} should fully reset")

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


if __name__ == "__main__":
    unittest.main()
