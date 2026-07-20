from __future__ import annotations

import asyncio
import json
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from telegram_comfyui_selfie import session_schema
from tests.support import ServiceFixtureMixin


class CheckpointTrimTestCase(ServiceFixtureMixin, unittest.TestCase):
    """TODO #9.4: checkpoint 裁剪测试 — 51+ messages 后 checkpoint，窗口 10 messages，不能 assistant 开头。"""

    def test_queue_checkpoint_schedules_background_task_without_blocking_chat(self):
        async def run():
            svc = self.make_service()
            svc.config["context_window_message_limit"] = "10"
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            messages = []
            for i in range(6):
                messages.append({"role": "user", "content": f"用户消息 {i}"})
                messages.append({"role": "assistant", "content": f"角色回复 {i}"})
            svc.app_store.append_messages(sid, key, messages)
            started = asyncio.Event()
            blocker = asyncio.Event()

            async def slow_checkpoint(session_id, character_key, keep, *, force=False):
                started.set()
                await blocker.wait()

            svc._run_context_checkpoint = slow_checkpoint

            before = time.perf_counter()
            svc._queue_checkpoint_if_needed(sid, messages)
            elapsed = time.perf_counter() - before

            self.assertLess(elapsed, 0.05)
            await asyncio.wait_for(started.wait(), timeout=1)
            task = svc._checkpoint_tasks[f"{sid}\n{key}"]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

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
            async def fake_summarize(session_id, previous, msgs, **kwargs):
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

            async def fake_summarize(session_id, previous, msgs, **kwargs):
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

            self.assertIn(raw_diary, diary)
            self.assertIn("之前写过的内容。", diary)
            self.assertIn("补记", diary)
            self.assertIn("first-person", captured["system"])
            self.assertIn("# 2026-06-26 星期五 标题", captured["system"])
            self.assertIn("will replace that old entry", captured["system"])
            self.assertIn("not append to it", captured["system"])
            self.assertIn("preserve every concrete fact", captured["system"])
            self.assertIn("Treat Existing diary as the archived record", captured["system"])
            self.assertIn("Do not invent events", captured["system"])
            self.assertIn("first-person 'I' is always the current bot roleplay character", captured["system"])
            self.assertIn("User means the human user", captured["system"])
            self.assertIn("Assistant means the bot character", captured["system"])
            self.assertIn("Do not swap who felt, promised, touched, moved, or spoke", captured["system"])
            self.assertIn("Do not include roleplay advice", captured["system"])
            self.assertIn("Weekday: 星期五", captured["user"])
            self.assertIn("Write mode: overwrite existing diary", captured["user"])
            self.assertIn("Dialogue role legend", captured["user"])
            self.assertIn("User = human user; Assistant = the current bot roleplay character", captured["user"])

        asyncio.run(run())

    def test_dream_diary_preserves_existing_when_model_omits_old_content(self):
        svc = self.make_service()
        old = "# 2026-06-26 星期五 旧日记\n我和用户约好周末去水族馆。\n我还记得他不喜欢太吵的地方。"
        new = "# 2026-06-26 星期五 新日记\n今天只写了新的晚安。"

        merged = svc._ensure_diary_preserves_existing(old, new)

        self.assertIn("今天只写了新的晚安", merged)
        self.assertIn("我和用户约好周末去水族馆", merged)
        self.assertIn("不喜欢太吵的地方", merged)
        self.assertIn("补记", merged)

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

    def test_dream_memory_organize_runs_without_diaries_and_reports_noop(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "test-character"
            svc.memory.add_memory(sid, "event", "自动记忆-待整理", character=key, source="chat")
            svc.has_llm_config = lambda purpose, session_id="": True
            captured = {}

            async def fake_call_llm(system, user, **kw):
                captured["user"] = user
                return json.dumps({"ops": []})

            svc._call_llm = fake_call_llm

            result = await svc._organize_memories_after_dream(sid, key)

            self.assertEqual(result.get("status"), "no_op")
            self.assertIn("Recent diaries:", captured["user"])
            self.assertIn("Editable memories:", captured["user"])

        asyncio.run(run())

    def test_checkpoint_restores_latest_wardrobe_system_event(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            state = svc._get_session_state(sid)
            session_schema.set_wardrobe(state, {"dress": "red dress", "footwear": "black heels"})
            session_schema.set_outfit(state, "red dress, black heels")
            session_schema.set_wardrobe_item_state(state, "dress", "damaged")
            expected = svc._wardrobe_state_snapshot(sid, state)
            event = svc._format_wardrobe_state_system_message(expected)

            messages = [
                {"role": "user", "content": "换好衣服了吗"},
                {"role": "assistant", "content": "换好了。"},
                event,
                {"role": "user", "content": "我们继续聊"},
                {"role": "assistant", "content": "好。"},
            ]
            svc.app_store.append_messages(sid, key, messages)
            session_schema.set_chat_history(state, messages)
            session_schema.set_wardrobe_semistable_snapshot(state, {
                "visual_context": "旧半稳定穿搭",
                "closet_context": "旧衣橱",
            })
            # 模拟运行态衣橱数据丢失/陈旧；checkpoint 应以最新 system 快照校准。
            session_schema.set_wardrobe(state, {"top": "stale shirt"})
            session_schema.set_outfit(state, "stale shirt")
            session_schema.clear_wardrobe_item_states(state)

            svc._summarize_checkpoint = AsyncMock(return_value="CHECKPOINT SUMMARY")
            svc._extract_long_term_memories_from_messages = AsyncMock()
            await svc._run_context_checkpoint(sid, key, keep=2, force=True)

            self.assertEqual(session_schema.get_wardrobe(state), {
                "dress": "red dress",
                "footwear": "black heels",
            })
            self.assertEqual(session_schema.get_wardrobe_item_states(state), {"dress": "damaged"})
            self.assertEqual(session_schema.get_outfit(state), "red dress, black heels")
            self.assertEqual(session_schema.get_wardrobe_semistable_snapshot(state), {})
            self.assertEqual([message["content"] for message in session_schema.get_chat_history(state)], ["我们继续聊", "好。"])

        asyncio.run(run())

    def test_checkpoint_uses_latest_kept_wardrobe_event_and_advances_frozen_baseline(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            state = svc._get_session_state(sid)

            session_schema.set_wardrobe(state, {"top": "white blouse"})
            session_schema.set_outfit(state, "white blouse")
            first = svc._wardrobe_state_snapshot(sid, state)
            first_event = svc._format_wardrobe_state_system_message(first)
            session_schema.set_wardrobe(state, {"dress": "green dress"})
            session_schema.set_outfit(state, "green dress")
            second = svc._wardrobe_state_snapshot(sid, state)
            second_event = svc._format_wardrobe_state_system_message(second)

            messages = [
                {"role": "user", "content": "第一轮"},
                {"role": "assistant", "content": "第一轮回复"},
                first_event,
                {"role": "user", "content": "第二轮"},
                {"role": "assistant", "content": "第二轮回复"},
                second_event,
            ]
            svc.app_store.append_messages(sid, key, messages)
            session_schema.set_chat_history(state, messages)
            session_schema.set_wardrobe_semistable_snapshot(state, {
                "visual_context": "更早的穿搭",
                "closet_context": "",
                "state_signature": "older",
            })
            session_schema.set_wardrobe(state, {"top": "stale shirt"})
            session_schema.set_outfit(state, "stale shirt")
            svc._summarize_checkpoint = AsyncMock(return_value="CHECKPOINT SUMMARY")
            svc._extract_long_term_memories_from_messages = AsyncMock()

            await svc._run_context_checkpoint(sid, key, keep=3, force=True)

            self.assertEqual(session_schema.get_wardrobe(state), {"dress": "green dress"})
            frozen = session_schema.get_wardrobe_semistable_snapshot(state)
            self.assertEqual(frozen.get("state_signature"), first.get("state_signature"))
            self.assertIn("white blouse", frozen.get("visual_context", ""))
            kept = session_schema.get_chat_history(state)
            self.assertEqual([message["role"] for message in kept], ["user", "assistant", "system"])
            self.assertEqual(
                svc._parse_wardrobe_state_system_message(kept[-1]).get("state_signature"),
                second.get("state_signature"),
            )

        asyncio.run(run())

    def test_checkpoint_does_not_roll_back_unrecorded_webui_wardrobe_change(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            state = svc._get_session_state(sid)
            session_schema.set_wardrobe(state, {"top": "white blouse"})
            session_schema.set_outfit(state, "white blouse")
            old_snapshot = svc._wardrobe_state_snapshot(sid, state)
            session_schema.set_wardrobe_observed_snapshot(state, old_snapshot)
            event = svc._format_wardrobe_state_system_message(old_snapshot)
            messages = [
                {"role": "user", "content": "旧轮"},
                {"role": "assistant", "content": "旧回复"},
                event,
                {"role": "user", "content": "保留轮"},
                {"role": "assistant", "content": "保留回复"},
            ]
            svc.app_store.append_messages(sid, key, messages)
            session_schema.set_chat_history(state, messages)

            # WebUI 已写入更新状态，但下一条用户消息尚未来，因此还没有新 system 事件。
            session_schema.set_wardrobe(state, {"dress": "new webui dress"})
            session_schema.set_outfit(state, "new webui dress")
            svc._summarize_checkpoint = AsyncMock(return_value="CHECKPOINT SUMMARY")
            svc._extract_long_term_memories_from_messages = AsyncMock()

            await svc._run_context_checkpoint(sid, key, keep=2, force=True)

            self.assertEqual(session_schema.get_wardrobe(state), {"dress": "new webui dress"})
            self.assertEqual(session_schema.get_outfit(state), "new webui dress")
            self.assertTrue(svc._record_external_wardrobe_change_before_user(sid))
            latest = svc._parse_wardrobe_state_system_message(session_schema.get_chat_history(state)[-1])
            self.assertEqual(latest["wardrobe"], {"dress": "new webui dress"})

        asyncio.run(run())

    def test_structured_wardrobe_tool_updates_multiple_items_and_states(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_wardrobe(state, {
                "top": "white blouse",
                "bottom": "blue jeans",
                "outerwear": "gray cardigan",
            })
            session_schema.set_outfit(state, "white blouse, blue jeans, gray cardigan")
            svc._classify_wardrobe_change = AsyncMock(side_effect=AssertionError("structured tool must not reclassify"))

            result = await svc.tool_change_appearance(sid, items=[
                {"slot": "top", "action": "wear", "tags": "red silk blouse", "name": "红色丝绸衬衫"},
                {"slot": "footwear", "action": "wear", "tags": "black ankle boots", "name": "黑色短靴"},
                {"slot": "outerwear", "action": "remove"},
                {"slot": "bottom", "action": "set_state", "state": "damaged"},
            ])

            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_wardrobe(state), {
                "top": "red silk blouse",
                "bottom": "blue jeans",
                "footwear": "black ankle boots",
            })
            self.assertEqual(session_schema.get_wardrobe_item_states(state), {"bottom": "damaged"})
            self.assertIn("red silk blouse", result)
            self.assertIn("bottom=damaged", result)
            self.assertIn("state_json:", result)
            svc._classify_wardrobe_change.assert_not_awaited()
            pending = svc._take_pending_wardrobe_history_messages(sid)
            self.assertEqual(len(pending), 1)
            parsed = svc._parse_wardrobe_state_system_message(pending[0])
            self.assertEqual(parsed["wardrobe"]["footwear"], "black ankle boots")
            self.assertEqual(parsed["item_states"], {"bottom": "damaged"})

        asyncio.run(run())

    def test_wardrobe_tool_appends_round_end_system_event_and_keeps_semistable_prefix(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_wardrobe(state, {"top": "white blouse", "bottom": "blue jeans"})
            session_schema.set_outfit(state, "white blouse, blue jeans")
            calls = {"count": 0}

            async def fake_messages(messages, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {"choices": [{"message": {"content": "", "tool_calls": [{
                        "id": "wardrobe-1",
                        "function": {
                            "name": "change_appearance",
                            "arguments": json.dumps({
                                "items": [
                                    {"slot": "top", "action": "wear", "tags": "red blouse", "name": "红衬衫"},
                                    {"slot": "bottom", "action": "set_state", "state": "half_off"},
                                ]
                            }, ensure_ascii=False),
                        },
                    }]}}]}
                return {"choices": [{"message": {"content": "（她整理了一下衣摆。）\n\n「这样呢？」"}}]}

            svc._call_llm_messages = fake_messages
            svc._ensure_life_profile = AsyncMock(return_value={})
            svc._judge_image_moment = AsyncMock(return_value=None)
            svc._update_character_place_from_text = AsyncMock()

            reply = await svc.run_roleplay_chat(1, sid, "换件红上衣，把牛仔裤褪下一半")
            self.assertIn("这样呢", reply)
            history = session_schema.get_chat_history(state)
            self.assertEqual([message["role"] for message in history], ["user", "assistant", "system"])
            parsed = svc._parse_wardrobe_state_system_message(history[-1])
            self.assertEqual(parsed["wardrobe"]["top"], "red blouse")
            self.assertEqual(parsed["item_states"], {"bottom": "half_off"})

            messages = svc._build_chat_messages(sid, "继续")
            semistable = next(
                message["content"] for message in messages
                if message.get("role") == "system" and "当前可见外型与配饰" in message.get("content", "")
            )
            self.assertIn("white blouse", semistable)
            self.assertNotIn("red blouse", semistable)
            historical_event = next(
                message["content"] for message in messages
                if message.get("role") == "system" and message.get("content", "").startswith("衣橱状态（系统记录")
            )
            self.assertIn("red blouse", historical_event)
            await asyncio.sleep(0)

        asyncio.run(run())

    def test_webui_wardrobe_change_is_recorded_before_next_user_message(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_wardrobe(state, {"top": "white blouse", "bottom": "blue jeans"})
            session_schema.set_outfit(state, "white blouse, blue jeans")
            self.assertFalse(svc._record_external_wardrobe_change_before_user(sid))

            # 模拟 WebUI 在两轮聊天之间直接修改当前穿搭和部件状态。
            session_schema.set_wardrobe(state, {"dress": "black cocktail dress"})
            session_schema.set_outfit(state, "black cocktail dress")
            session_schema.set_wardrobe_item_state(state, "dress", "damaged")
            self.assertTrue(svc._record_external_wardrobe_change_before_user(sid))
            self.assertFalse(svc._record_external_wardrobe_change_before_user(sid))

            before_chat = session_schema.get_chat_history(state)
            self.assertEqual(len(before_chat), 1)
            self.assertEqual(before_chat[0]["role"], "system")
            parsed = svc._parse_wardrobe_state_system_message(before_chat[0])
            self.assertEqual(parsed["wardrobe"], {"dress": "black cocktail dress"})
            self.assertEqual(parsed["item_states"], {"dress": "damaged"})

            messages = svc._build_chat_messages(sid, "你换好了吗")
            event_index = next(
                index for index, message in enumerate(messages)
                if message.get("content", "").startswith("衣橱状态（系统记录")
            )
            self.assertLess(event_index, len(messages) - 1)
            self.assertEqual(messages[-1], {"role": "user", "content": "你换好了吗"})
            semistable = next(
                message["content"] for message in messages
                if message.get("role") == "system" and "当前可见外型与配饰" in message.get("content", "")
            )
            self.assertIn("white blouse", semistable)
            self.assertNotIn("black cocktail dress", semistable)

        asyncio.run(run())

    def test_dream_memory_organize_merges_user_profile_after_noop(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "test-character"
            svc.memory.add_memory(sid, "user_profile", "用户喜欢夜跑", character=key, importance=3, tags=["兴趣"])
            svc.memory.add_memory(sid, "user_profile", "用户自述是短发女性", character=key, importance=5, tags=["外貌"])
            svc.memory.add_memory(sid, "user_profile", "其他角色画像不应合并", character="other-character", importance=5)
            svc.has_llm_config = lambda purpose, session_id="": True

            async def fake_call_llm(system, user, **kw):
                return json.dumps({"ops": []})

            svc._call_llm = fake_call_llm

            result = await svc._organize_memories_after_dream(sid, key)

            profiles = [m for m in svc.memory.list_memories(sid, character=key, limit=10) if m.get("kind") == "user_profile"]
            self.assertEqual(len(profiles), 1)
            self.assertIn("用户喜欢夜跑", profiles[0].get("summary", ""))
            self.assertIn("用户自述是短发女性", profiles[0].get("summary", ""))
            other_profiles = [
                m for m in svc.memory.list_memories(sid, character="other-character", limit=10)
                if m.get("kind") == "user_profile"
            ]
            self.assertEqual(len(other_profiles), 1)
            self.assertEqual(result.get("user_profile_merge", {}).get("merged"), 2)

        asyncio.run(run())

    def test_dream_memory_organize_logs_failed_operation_request_and_result(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "test-character"
            svc.memory.add_memory(sid, "event", "自动记忆-待整理", character=key, source="chat")
            svc.has_llm_config = lambda purpose, session_id="": True
            logs = []
            svc._ulog = lambda session_id, tag, message="": logs.append((tag, message))

            async def fake_call_llm(system, user, **kw):
                return json.dumps({"ops": [
                    {"op": "update", "id": 999999, "summary": "不存在的记忆更新"},
                ]})

            svc._call_llm = fake_call_llm

            result = await svc._organize_memories_after_dream(sid, key)

            self.assertEqual(result.get("status"), "failed")
            error_logs = [message for tag, message in logs if tag == "ERROR"]
            self.assertTrue(error_logs)
            joined = "\n".join(error_logs)
            self.assertIn("MEMORY_OP_FAILED", joined)
            self.assertIn("999999", joined)
            self.assertIn("request", joined)
            self.assertIn("result", joined)

        asyncio.run(run())

    def test_dream_memory_summarize_falls_back_to_fast_model(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "test-character"
            for i in range(10):
                svc.memory.add_memory(
                    sid, "event", f"自动记忆 {i}", character=key, importance=3, tags=[f"m{i}"], source="chat")
            editable = svc.memory.list_memories(sid, character=key, limit=20)
            svc.has_llm_config = lambda purpose, session_id="": purpose in {"chat", "image"}
            calls = []

            async def fake_call_llm(system, user, **kw):
                calls.append(kw)
                if kw.get("purpose") == "chat":
                    return "```"
                return json.dumps({"memories": [
                    {"kind": "event", "summary": "压缩后的记忆", "importance": 4, "tags": ["压缩"]},
                ]})

            svc._call_llm = fake_call_llm

            result = await svc._summarize_all_memories(sid, key, editable, target_n=4)

            self.assertEqual(result.get("status"), "ok")
            self.assertEqual(result.get("llm_purpose"), "image")
            self.assertEqual([call.get("purpose") for call in calls], ["chat", "image"])
            self.assertTrue(calls[0].get("disable_thinking"))
            self.assertIsNone(calls[1].get("disable_thinking"))
            self.assertEqual(calls[1].get("tag"), "dream-memory-summarize-fast-fallback")
            self.assertEqual(calls[0].get("max_tokens"), 8192)
            self.assertEqual(calls[1].get("max_tokens"), 8192)
            active = svc.memory.list_memories(sid, character=key, limit=20)
            self.assertTrue(any(m.get("summary") == "压缩后的记忆" for m in active))

        asyncio.run(run())

    def test_dream_memory_summarize_max_tokens_can_be_configured(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "test-character"
            svc.config["dream_memory_summarize_max_tokens"] = "12000"
            for i in range(3):
                svc.memory.add_memory(
                    sid, "event", f"auto memory {i}", character=key, importance=3, tags=[f"m{i}"], source="chat")
            editable = svc.memory.list_memories(sid, character=key, limit=20)
            svc.has_llm_config = lambda purpose, session_id="": purpose == "chat"
            calls = []

            async def fake_call_llm(system, user, **kw):
                calls.append(kw)
                return json.dumps({"memories": [
                    {"kind": "event", "summary": "compressed memory", "importance": 4, "tags": ["compressed"]},
                ]})

            svc._call_llm = fake_call_llm

            result = await svc._summarize_all_memories(sid, key, editable, target_n=2)

            self.assertEqual(result.get("status"), "ok")
            self.assertEqual(calls[0].get("max_tokens"), 12000)

        asyncio.run(run())

    def test_dream_memory_summarize_empty_json_does_not_deactivate(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "test-character"
            for i in range(3):
                svc.memory.add_memory(sid, "event", f"自动记忆 {i}", character=key, source="chat")
            editable = svc.memory.list_memories(sid, character=key, limit=10)
            before_ids = {int(m["id"]) for m in editable}
            svc.has_llm_config = lambda purpose, session_id="": purpose in {"chat", "image"}

            async def fake_call_llm(system, user, **kw):
                return "```json\n```"

            svc._call_llm = fake_call_llm

            result = await svc._summarize_all_memories(sid, key, editable, target_n=2)

            self.assertEqual(result.get("status"), "failed")
            self.assertEqual(result.get("mode"), "summarize")
            self.assertIn("空 JSON", result.get("error", ""))
            after_ids = {int(m["id"]) for m in svc.memory.list_memories(sid, character=key, limit=10)}
            self.assertEqual(before_ids, after_ids)

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

    def test_dream_writes_character_checkpoint_before_diary(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            svc._save_session_state(sid, state)
            key = svc._context_character_key(sid)
            ids = svc.app_store.append_messages(sid, key, [
                {"role": "user", "content": "当天对话"},
                {"role": "assistant", "content": "当天回复"},
            ])
            tz = svc._session_tz(sid)
            created_at = datetime(2026, 6, 24, 10, tzinfo=tz).timestamp()
            with closing(svc.app_store._connect()) as conn:
                conn.execute("UPDATE chat_messages SET created_at = ? WHERE id IN (?, ?)", (created_at, ids[0], ids[1]))
                conn.commit()
            order = []

            def fake_write_checkpoint(session_id, character_key, checkpoint_date, *, reason="", to_message_id=None):
                order.append(("checkpoint", checkpoint_date, reason, to_message_id))
                return Path("fake-checkpoint.json")

            async def fake_write_dream_diary(session_id, diary_date, source_text, existing_diary="", *, reason=""):
                order.append(("diary", diary_date, reason))
                return "梦境日记"

            svc.write_character_checkpoint = fake_write_checkpoint
            svc._write_dream_diary = fake_write_dream_diary
            svc._organize_memories_after_dream = AsyncMock()
            svc._generate_character_history_summary = AsyncMock()

            await svc._dream_once(sid, key, datetime(2026, 6, 24, 12, tzinfo=timezone.utc), reason="manual")

            self.assertEqual(order[0][0], "checkpoint")
            self.assertEqual(order[1][0], "diary")
            self.assertEqual(order[0][1], "2026-06-24")
            self.assertEqual(order[0][2], "dream:manual")
            self.assertEqual(order[0][3], max(ids))

        asyncio.run(run())

    def test_dream_summary_chain_extracts_memory_before_history_and_checkpoint(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = svc._context_character_key(sid)
            svc.app_store.append_messages(sid, key, [
                {"role": "user", "content": "今天发生了重要转折"},
                {"role": "assistant", "content": "我会记住这件事"},
            ])
            svc.has_llm_config = lambda purpose, session_id="": purpose == "chat"
            order = []
            svc.write_character_checkpoint = lambda *args, **kwargs: Path("fake-checkpoint.json")

            async def fake_diary(*args, **kwargs):
                order.append("diary")
                return "梦境日记"

            async def fake_extract(*args, **kwargs):
                order.append("extract-long-memory")

            async def fake_organize(*args, **kwargs):
                order.append("organize-long-memory")
                return {"status": "ok"}

            async def fake_life(*args, **kwargs):
                order.append("life-plan")
                return {"status": "ok"}

            async def fake_history(*args, **kwargs):
                order.append("character-history")

            async def fake_checkpoint(*args, **kwargs):
                order.append(("checkpoint", kwargs.get("extract_memory")))

            svc._write_dream_diary = fake_diary
            svc._extract_long_term_memories_from_messages = fake_extract
            svc._organize_memories_after_dream = fake_organize
            svc._update_life_plan_after_dream = fake_life
            svc._generate_character_history_summary = fake_history
            svc._run_context_checkpoint = fake_checkpoint

            await svc._dream_once(sid, key, datetime(2026, 7, 6, tzinfo=timezone.utc), reason="manual")

            self.assertLess(order.index("extract-long-memory"), order.index("organize-long-memory"))
            self.assertLess(order.index("organize-long-memory"), order.index("character-history"))
            checkpoint_index = next(i for i, item in enumerate(order) if isinstance(item, tuple) and item[0] == "checkpoint")
            self.assertLess(order.index("character-history"), checkpoint_index)
            self.assertEqual(order[checkpoint_index], ("checkpoint", False))

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
