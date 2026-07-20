from __future__ import annotations

import asyncio
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiohttp import web

from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.image_planning import plan_roleplay_image
from telegram_comfyui_selfie.webui import (
    api_world_life_plan_generate,
    api_world_life_plan_goal_create,
    api_world_life_plan_goal_delete,
    api_world_life_plan_goal_update,
    build_world_route_preview,
)
from tests.support import ServiceFixtureMixin


class WorldLifePlanTestCase(ServiceFixtureMixin, unittest.TestCase):
    """世界状态、地点动线、生活线与对应规划测试。"""

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

    def test_life_plan_chat_context_injects_texture_only(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now
        state = svc._get_session_state(sid)
        session_schema.set_character_value(state, "custom_character", "小雨")
        svc._save_session_state(sid, state)
        svc._save_life_plan_payload(sid, "小雨", {
            "long_goals": [{
                "id": "l1",
                "text": "攒钱搬到靠海的房子",
                "motivation": "想给自己留一点安静空间",
                "status": "active",
            }],
            "mid_goals": [{
                "id": "m1",
                "parent_id": "l1",
                "text": "整理作品集",
                "progress_note": "已经改完第一版",
                "status": "active",
            }],
            "today": {
                "date": "2026-07-02",
                "texture": "早上醒来还有点困，心里压着一点细碎的牵挂。",
                "events": [],
            },
        })

        messages = svc._build_chat_messages(sid, "你好")
        life_messages = [m["content"] for m in messages if m.get("role") == "system" and "生活底色" in m.get("content", "")]

        self.assertEqual(len(life_messages), 1)
        self.assertIn("早上醒来还有点困", life_messages[0])
        self.assertNotIn("攒钱搬到靠海的房子", life_messages[0])
        self.assertNotIn("整理作品集", life_messages[0])
        self.assertNotIn("任务", life_messages[0])

        payload = svc.app_store.get_life_plan(sid, "小雨")["payload"]
        payload["today"]["texture"] = "今天计划完成作品集。"
        svc._save_life_plan_payload(sid, "小雨", payload)
        self.assertEqual(svc._life_plan_chat_context(sid, now=fixed_now), "")

    def test_life_plan_push_context_lists_current_period_candidates(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now
        today = svc._life_today_date(sid, fixed_now)
        svc._save_life_plan_payload(sid, "", {
            "long_goals": [],
            "mid_goals": [],
            "today": {
                "date": today,
                "texture": "下午有点犯困，但心里还惦着没收好的尾巴。",
                "events": [
                    {
                        "id": "e1",
                        "time_hint": "afternoon",
                        "text": "去咖啡店把草稿摊开重新看一遍",
                        "place_key": "cafe",
                        "status": "planned",
                        "side_note": "杯壁上还挂着水珠，纸角被压得有点卷。",
                    },
                    {
                        "id": "e2",
                        "time_hint": "afternoon",
                        "text": "顺路买一盒新的便签纸",
                        "place_key": "bookstore",
                        "status": "planned",
                        "side_note": "手指停在颜色架前犹豫了好一会儿。",
                    },
                    {
                        "id": "e3",
                        "time_hint": "morning",
                        "text": "晨跑",
                        "place_key": "park",
                        "status": "planned",
                        "side_note": "鞋带沾了露水。",
                    },
                ],
            },
        })

        ctx = svc._life_plan_push_context(sid, now=fixed_now)

        self.assertIn("今日生活片段候选", ctx)
        self.assertIn("去咖啡店把草稿", ctx)
        self.assertIn("新的便签纸", ctx)
        self.assertNotIn("晨跑", ctx)
        self.assertIn("可以选择其中一个、混合几个", ctx)

    def test_life_plan_ops_apply_caps_and_ignore_unknown_ids(self):
        svc = self.make_service()
        sid = "telegram:123"
        svc.config["life_plan_max_mid"] = "2"
        previous = {
            "long_goals": [{"id": "l1", "text": "把生活过稳一点", "status": "active"}],
            "mid_goals": [{"id": "m1", "parent_id": "l1", "text": "整理手头小事", "status": "active"}],
            "today": {"date": "2026-07-01", "events": [], "texture": ""},
        }
        parsed = {
            "ops": [
                {"op": "progress", "id": "m1", "note": "下午终于松了一点"},
                {"op": "add_mid", "id": "m2", "parent_id": "l1", "text": "把白天的杂事收口"},
                {"op": "add_mid", "id": "m3", "parent_id": "l1", "text": "这条会被上限挡住"},
                {"op": "progress", "id": "missing", "note": "不存在"},
            ],
            "today_events": [{
                "id": "e1",
                "time_hint": "afternoon",
                "text": "找个安静角落缓一缓",
                "place_key": "cafe",
                "related_mid_id": "m2",
                "status": "planned",
            }],
        }

        plan, result = svc._life_plan_from_update(previous, parsed, today_date="2026-07-02", session_id=sid)

        self.assertEqual(len(plan["mid_goals"]), 2)
        self.assertEqual(plan["mid_goals"][0]["progress_note"], "下午终于松了一点")
        self.assertEqual(plan["mid_goals"][1]["id"], "m2")
        self.assertGreaterEqual(result["ignored"], 2)
        self.assertEqual(plan["today"]["events"][0]["related_mid_id"], "m2")

    def test_life_plan_ops_apply_nested_goal_format(self):
        # 模型可能返回 {"op":"add_long","long_goal":{...}} 嵌套格式，代码应展平后正确应用
        svc = self.make_service()
        sid = "telegram:123"
        previous = {
            "long_goals": [],
            "mid_goals": [],
            "today": {"date": "2026-07-03", "events": [], "texture": ""},
        }
        parsed = {
            "ops": [
                {"op": "add_long", "long_goal": {"id": "lg1", "dimension": "身份", "text": "弄清留下来的意义", "motivation": "逃避了一百年", "status": "active"}},
                {"op": "add_long", "long_goal": {"id": "lg2", "dimension": "占有", "text": "把独食钉牢", "motivation": "领地本能", "status": "active"}},
                {"op": "add_mid", "mid_goal": {"id": "mg1", "parent_id": "lg1", "text": "重新说离不开", "description": "等他清醒后重说"}},
                {"op": "add_mid", "mid_goal": {"id": "mg2", "parent_id": "lg2", "text": "推进拉面之约", "description": "看他记不记得"}},
            ],
            "today_events": [],
        }

        plan, result = svc._life_plan_from_update(previous, parsed, today_date="2026-07-03", session_id=sid)

        self.assertEqual(result["applied"], 4)
        self.assertEqual(result["ignored"], 0)
        self.assertEqual(len(plan["long_goals"]), 2)
        self.assertEqual(plan["long_goals"][0]["id"], "lg1")
        self.assertEqual(plan["long_goals"][0]["text"], "弄清留下来的意义")
        self.assertEqual(plan["long_goals"][0]["dimension"], "身份")
        self.assertEqual(plan["long_goals"][0]["motivation"], "逃避了一百年")
        self.assertEqual(len(plan["mid_goals"]), 2)
        self.assertEqual(plan["mid_goals"][0]["id"], "mg1")
        self.assertEqual(plan["mid_goals"][0]["parent_id"], "lg1")
        self.assertEqual(plan["mid_goals"][0]["progress_note"], "等他清醒后重说")
        self.assertEqual(plan["mid_goals"][1]["parent_id"], "lg2")

    def test_life_plan_ignores_long_goal_updates_when_review_not_due(self):
        svc = self.make_service()
        sid = "telegram:123"
        previous = {
            "long_goals": [{
                "id": "l1",
                "dimension": "身份",
                "text": "守住自己的创作身份",
                "motivation": "不想被日常磨平",
                "status": "active",
                "created_date": "2026-07-01",
                "updated_date": "2026-07-01",
            }],
            "mid_goals": [{
                "id": "m1",
                "parent_id": "l1",
                "text": "整理一段作品草稿",
                "progress_note": "",
                "status": "active",
                "created_date": "2026-07-01",
                "updated_date": "2026-07-01",
            }],
            "today": {"date": "2026-07-01", "events": [], "texture": ""},
            "last_long_review_date": "2026-07-01",
        }
        parsed = {
            "long_goals": [{
                "id": "l2",
                "dimension": "关系",
                "text": "这条长期目标不应替换旧目标",
                "motivation": "模型误输出",
                "status": "active",
            }],
            "mid_goals": [{
                "id": "m1",
                "parent_id": "l1",
                "text": "根据昨天状态重排今天的中期推进",
                "progress_note": "今天先收小口",
                "status": "active",
            }],
            "ops": [
                {"op": "update_long", "id": "l1", "text": "不应改写"},
                {"op": "add_long", "id": "l3", "dimension": "自由", "text": "不应新增", "motivation": "未到 review"},
                {"op": "achieve", "id": "l1", "reason": "不应完成长期目标"},
            ],
            "today_events": [],
        }

        plan, result = svc._life_plan_from_update(
            previous,
            parsed,
            today_date="2026-07-02",
            session_id=sid,
            allow_long_goal_update=False,
        )

        self.assertEqual(len(plan["long_goals"]), 1)
        self.assertEqual(plan["long_goals"][0]["id"], "l1")
        self.assertEqual(plan["long_goals"][0]["text"], "守住自己的创作身份")
        self.assertEqual(plan["long_goals"][0]["status"], "active")
        self.assertEqual(plan["last_long_review_date"], "2026-07-01")
        self.assertEqual(plan["mid_goals"][0]["text"], "根据昨天状态重排今天的中期推进")
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["ignored"], 3)
        self.assertTrue(all(item.get("reason") == "long_review_not_due" for item in result["details"]))

    def test_life_plan_texture_retries_purpose_word_output(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc.has_llm_config = lambda purpose, session_id="": purpose == "chat"
            svc._call_llm = AsyncMock(side_effect=[
                json.dumps({"texture": "今天计划完成作品集。", "event_sides": {}}, ensure_ascii=False),
                json.dumps({
                    "texture": "心里还压着一点没有散开的疲惫，语气会比平时更轻。",
                    "event_sides": {"e1": "她刚从咖啡店的嘈杂里缓过来一点。"},
                }, ensure_ascii=False),
            ])
            plan = {
                "long_goals": [{"id": "l1", "text": "把生活过稳一点", "status": "active"}],
                "mid_goals": [{"id": "m1", "parent_id": "l1", "text": "整理手头小事", "status": "active"}],
                "today": {
                    "date": "2026-07-02",
                    "events": [{
                        "id": "e1",
                        "time_hint": "afternoon",
                        "text": "找个角落缓一缓",
                        "place_key": "cafe",
                        "related_mid_id": "m1",
                        "status": "planned",
                    }],
                },
            }

            rendered = await svc._render_life_plan_texture(sid, "小雨", plan, today_date="2026-07-02")

            self.assertEqual(svc._call_llm.await_count, 2)
            self.assertIn("疲惫", rendered["today"]["texture"])
            self.assertIn("咖啡店", rendered["today"]["events"][0]["side_note"])
            self.assertNotIn("计划", rendered["today"]["texture"])
        asyncio.run(run())

    def test_world_preview_serializes_life_plan(self):
        svc = self.make_service()
        sid = "telegram:123"
        fixed_now = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
        svc._session_now = lambda session_id="": fixed_now
        state = svc._get_session_state(sid)
        session_schema.set_character_value(state, "custom_character", "小雨")
        svc._save_session_state(sid, state)
        svc._save_life_plan_payload(sid, "小雨", {
            "long_goals": [{"id": "l1", "text": "把生活过稳一点", "status": "active"}],
            "mid_goals": [{"id": "m1", "parent_id": "l1", "text": "整理手头小事", "progress_note": "还差一点", "status": "active"}],
            "today": {
                "date": "2026-07-02",
                "texture": "心里有点惦记白天没收好的尾巴。",
                "events": [{
                    "id": "e1",
                    "time_hint": "afternoon",
                    "text": "去咖啡店坐一会儿",
                    "place_key": "cafe",
                    "related_mid_id": "m1",
                    "status": "planned",
                    "side_note": "她刚从店里的嘈杂里缓过来一点。",
                }],
            },
        })

        preview = build_world_route_preview(svc, sid, weather={"desc": "晴", "temp": "22"})
        life = preview["life_plan"]
        push_side = svc._life_plan_push_context(sid, now=fixed_now)

        self.assertTrue(life["exists"])
        self.assertEqual(life["character_key"], "小雨")
        self.assertEqual(life["mid_goals"][0]["parent_text"], "把生活过稳一点")
        self.assertEqual(life["today"]["events"][0]["place_label"], "咖啡店")
        self.assertEqual(life["today"]["events"][0]["related_mid_text"], "整理手头小事")
        self.assertIn("嘈杂", push_side)

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

    def test_world_conditions_move_to_tail_keep_resident_slot_stable(self):
        svc = self.make_service()
        sid = "telegram:123"

        def build_at(hour, minute=0):
            svc._session_now = lambda session_id="", _h=hour, _m=minute: datetime(2026, 6, 18, _h, _m, tzinfo=timezone.utc)
            return svc._build_chat_messages(sid, "你好")

        noon = build_at(12)
        noon_later = build_at(12, 10)
        night = build_at(23)
        world_marker = "世界状态规则"  # 世界状态规则
        conditions_marker = "世界当前条件"  # 世界当前条件
        light_marker = "季节/自然光"  # 季节/自然光
        weather_marker = "- 天气:"  # - 天气:

        def resident_world(messages):
            return next(m["content"] for m in messages if world_marker in m.get("content", ""))

        noon_resident = resident_world(noon)
        night_resident = resident_world(night)
        noon_conditions = next(m["content"] for m in noon if conditions_marker in m.get("content", ""))
        noon_later_conditions = next(m["content"] for m in noon_later if conditions_marker in m.get("content", ""))
        night_conditions = next(m["content"] for m in night if conditions_marker in m.get("content", ""))

        # 常驻世界规则槽随 time_period 滚动（中午→深夜）保持字节一致，不再作废前缀缓存。
        self.assertEqual(noon_resident, night_resident)
        # 揮发字段（天气/季节自然光）已从常驻槽剥离。
        self.assertNotIn(light_marker, noon_resident)
        self.assertNotIn(weather_marker, noon_resident)

        # 揮发条件降频到独立半稳定槽，不随精确分钟滚动，但会随自然光阶段变化。
        self.assertIn(light_marker, noon_conditions)
        self.assertIn(weather_marker, noon_conditions)
        self.assertEqual(noon_conditions, noon_later_conditions)
        self.assertNotEqual(noon_conditions, night_conditions)
        noon_tail = noon[-2]["content"]
        night_tail = night[-2]["content"]
        self.assertEqual(noon[-2]["role"], "system")
        self.assertNotIn(conditions_marker, noon_tail)
        self.assertNotIn(light_marker, noon_tail)
        self.assertNotEqual(noon_tail, night_tail)

    def test_world_conditions_change_can_force_checkpoint_after_half_window(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            key = svc._context_character_key(sid)
            svc.config["context_window_message_limit"] = "10"
            messages = []
            for i in range(3):
                messages.append({"role": "user", "content": f"用户消息 {i}"})
                messages.append({"role": "assistant", "content": f"角色回复 {i}"})
            svc.app_store.append_messages(sid, key, messages)
            started = asyncio.Event()

            async def fake_checkpoint(session_id, character_key, keep, *, force=False):
                self.assertTrue(force)
                started.set()

            svc._run_context_checkpoint = fake_checkpoint
            svc._session_now = lambda session_id="": datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
            svc._build_chat_messages(sid, "你好")
            svc._session_now = lambda session_id="": datetime(2026, 6, 18, 23, 0, tzinfo=timezone.utc)
            svc._build_chat_messages(sid, "你好")

            await asyncio.wait_for(started.wait(), timeout=1)

        asyncio.run(run())

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

    def test_tool_update_location_accepts_natural_route_and_aliases(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"

            msg = await svc.tool_update_location(sid, "前往私立中学的路上")
            self.assertIn("大街", msg)
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_character_place(state), "street")
            self.assertEqual(session_schema.get_character_place_name(state), "前往私立中学的路上")

            await svc.tool_update_location(sid, "私立中学")
            self.assertEqual(session_schema.get_character_place(state), "school")
            self.assertEqual(session_schema.get_character_place_name(state), "私立中学")

            user_msg = await svc.tool_update_user_location(sid, "街道")
            self.assertIn("大街", user_msg)
            self.assertEqual(session_schema.get_user_place(state), "street")

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
            self.mock_image_planner_messages(svc, {
                "scene": "在办公室茶水间发来一张自拍",
                "caption": "忙里偷闲给你看一眼。",
                "view": "selfie",
            })

            plan = await svc._llm_write_scene(
                "normal",
                "晴 22 C",
                "星期四",
                "上午",
                None,
                sid,
                now=fixed_now,
            )

            self.assertIn("办公室", plan.get("scene", ""))
            self.assertEqual(plan.get("caption"), "忙里偷闲给你看一眼。")
            self.assertEqual(plan.get("view"), "selfie")
            svc._call_llm_messages.assert_awaited()

        asyncio.run(run())

    def test_planner_warns_private_sleepwear_in_public_world_context(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            fixed_now = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)
            svc._session_now = lambda session_id="": fixed_now
            svc.config.update({
                "image_llm_api_key": "image-key",
                "image_llm_model": "image-model",
                "image_llm_api_base": "https://image.example",
            })
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "purity", 8)
            state["life_profile"] = {
                "age_stage": "adult",
                "day_anchor": "school",
                "persona_hash": "test",
            }
            session_schema.set_outfit(state, "black lace camisole nightgown")
            self.mock_image_planner_messages(svc, {
                "scene": "standing by a university classroom window in modest casual clothes",
                "caption": "课间给你看一眼。",
                "view": "selfie",
                "character_location": "school",
                "user_location": "unknown",
                "new_appearance_tags": "modest casual clothes",
            })

            await plan_roleplay_image(
                svc,
                sid,
                mode="normal",
                now=fixed_now,
                weather_data={"desc": "晴", "temp": "25", "code": "113"},
            )

            system_prompt = "\n".join(
                m.get("content", "")
                for m in svc._call_llm_messages.await_args.args[0]
                if m.get("role") == "system"
            )
            self.assertIn("公开场合穿搭约束", system_prompt)
            self.assertIn("black lace camisole nightgown", system_prompt)
            self.assertIn("modest casual clothes", system_prompt)

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
            self.mock_image_planner_messages(svc, {
                "scene": "在办公室茶水间发来一张自拍",
                "caption": "忙里偷闲给你看一眼。",
                "view": "selfie",
                "new_appearance_tags": "white dress",
            })
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
                one_shot_appearance="",
                orientation="2:3",
                is_intimate=False,
                partner_in_frame=False,
                device_in_frame=False,
                clothing_off="",
                view="selfie",
            )
            self.assertEqual(session_schema.get_outfit(state), "black hoodie")
            svc.send_photo.assert_awaited_once()
            photo = session_schema.get_sent_photos_history(state)[-1]
            self.assertEqual(photo["source_kind"], "manual_push")

        asyncio.run(run())

    def test_life_plan_bootstraps_empty_current_plan(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            today = svc._life_today_date(sid)
            svc._save_life_plan_payload(sid, "", {
                "long_goals": [],
                "mid_goals": [],
                "today": {"date": today, "events": [], "texture": ""},
            })

            result = await svc.ensure_life_plan_for_today(sid, force=False, reason="test")

            self.assertEqual(result["status"], "updated")
            row = svc._load_life_plan_row(sid, "")
            payload = row["payload"]
            self.assertTrue(payload["long_goals"])
            self.assertTrue(payload["mid_goals"])
            self.assertFalse(svc._life_plan_needs_bootstrap(payload))

        asyncio.run(run())

    def test_life_plan_prompt_requires_self_inferred_core_drive_not_hollow_relationship(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc.config.update({
                "chat_llm_api_key": "chat-key",
                "chat_llm_model": "chat-model",
                "chat_llm_api_base": "https://chat.example",
            })
            state = svc._get_session_state(sid)
            session_schema.set_character_value(state, "custom_character", "小雨")
            session_schema.set_character_value(state, "custom_role_name", "见习画师")
            session_schema.set_character_value(state, "custom_character_occupation", "插画师")
            session_schema.set_character_value(state, "custom_scheduled_persona", "敏感、要强，害怕自己的作品没人看见。")
            session_schema.set_chat_history(state, [
                {"role": "user", "content": "你上次说想把画册做出来。"},
                {"role": "assistant", "content": "「嗯，不能一直只存在草稿里。」"},
            ])
            svc._call_life_plan_json = AsyncMock(return_value={
                "long_goals": [{
                    "id": "l1",
                    "dimension": "事业",
                    "text": "把自己的插画作品做出能被看见的风格",
                    "motivation": "不想一直躲在别人评价后面",
                    "status": "active",
                }],
                "mid_goals": [{
                    "id": "m1",
                    "parent_id": "l1",
                    "text": "这周完成一张能代表当前方向的练习稿",
                    "progress_note": "",
                    "status": "active",
                }],
                "today_events": [],
            })

            await svc._generate_life_plan_update(
                sid,
                "小雨",
                {
                    "long_goals": [{
                        "id": "l1",
                        "dimension": "关系",
                        "text": "把照顾用户变成默认状态",
                        "motivation": "害怕被替代",
                        "status": "active",
                    }],
                    "mid_goals": [{
                        "id": "m1",
                        "parent_id": "l1",
                        "text": "每天确认用户有没有想她",
                        "status": "active",
                    }],
                },
                today_date="2026-07-03",
                reason="test",
                goal_instruction="从事业、爱好和生活节奏拆开，不要都围着用户关系",
                rewrite_goals=True,
            )

            system = svc._call_life_plan_json.await_args.args[1]
            user = svc._call_life_plan_json.await_args.args[2]
            self.assertIn("core drive", system)
            self.assertIn("dimension", system)
            self.assertIn("genuinely different dimensions", system)
            self.assertIn("Infer that core drive yourself", system)
            self.assertIn("inside the character's point of view", system)
            self.assertIn("output JSON only", system)
            self.assertIn("维系感情", system)
            self.assertIn("Manual goal rewrite mode", system)
            self.assertIn("complete long_goals and complete mid_goals arrays", system)
            self.assertNotIn("Core drive candidates", user)
            self.assertIn("Goal rewrite mode: full long_goals + mid_goals replacement", user)
            self.assertIn("Original long/mid goals before this manual rewrite", user)
            self.assertIn("把照顾用户变成默认状态", user)
            self.assertIn("从事业、爱好和生活节奏拆开", user)
            self.assertIn("敏感、要强", user)
            self.assertIn("想把画册做出来", user)
            self.assertIn("user = human user; assistant = the current bot roleplay character", user)

        asyncio.run(run())

    def test_life_plan_goal_crud_preserves_dimensions_and_parent_links(self):
        svc = self.make_service()
        sid = "telegram:123"
        state = svc._get_session_state(sid)
        session_schema.set_character_value(state, "custom_character", "小雨")
        svc._save_session_state(sid, state)

        svc._save_life_plan_payload(sid, "小雨", {
            "long_goals": [{"id": "l1", "text": "把作品做出自己的方向", "status": "active"}],
            "mid_goals": [],
            "today": {"date": "2026-07-03", "events": [], "texture": ""},
        })
        loaded = svc._load_life_plan_row(sid, "小雨")["payload"]
        self.assertTrue(loaded["long_goals"][0].get("dimension"))

        svc.upsert_life_plan_goal(sid, "long", {
            "id": "l2",
            "dimension": "生活",
            "text": "把日常节奏整理到能留出喘息",
            "motivation": "不想只被白天推着走",
        })
        svc.upsert_life_plan_goal(sid, "mid", {
            "id": "m1",
            "parent_id": "l2",
            "text": "这周先固定一个晚上给自己",
            "progress_note": "刚开始试",
        })
        row = svc._load_life_plan_row(sid, "小雨")
        payload = row["payload"]
        self.assertEqual([goal["id"] for goal in payload["long_goals"]], ["l1", "l2"])
        self.assertEqual(payload["long_goals"][1]["dimension"], "生活")
        self.assertEqual(payload["mid_goals"][0]["parent_id"], "l2")

        svc.delete_life_plan_goal(sid, "long", "l2")
        payload = svc._load_life_plan_row(sid, "小雨")["payload"]
        self.assertEqual([goal["id"] for goal in payload["long_goals"]], ["l1"])
        self.assertEqual(payload["mid_goals"], [])

    def test_life_plan_manual_rewrite_replaces_all_long_and_mid_goals_once(self):
        svc = self.make_service()
        sid = "telegram:123"
        previous = {
            "long_goals": [{
                "id": "l1",
                "dimension": "关系",
                "text": "把陪伴用户变成生活中心",
                "motivation": "旧版本太单一",
                "status": "active",
            }],
            "mid_goals": [{
                "id": "m1",
                "parent_id": "l1",
                "text": "每天等用户回复",
                "status": "active",
            }],
            "today": {"date": "2026-07-02", "events": []},
        }
        parsed = {
            "long_goals": [
                {"id": "l1", "dimension": "事业", "text": "把插画作品做出自己的方向", "motivation": "想被看见", "status": "active"},
                {"id": "l2", "dimension": "生活", "text": "把日常整理到能喘息", "motivation": "不想只被推着走", "status": "active"},
            ],
            "mid_goals": [
                {"id": "m1", "parent_id": "l1", "text": "这周完成一张练习稿", "status": "active"},
                {"id": "m2", "parent_id": "l2", "text": "固定一个晚上留给自己", "status": "active"},
            ],
            "ops": [{"op": "add_long", "id": "l3", "dimension": "关系", "text": "不应被增量追加"}],
            "today_events": [],
        }

        plan, op_result = svc._life_plan_from_update(
            previous,
            parsed,
            today_date="2026-07-03",
            session_id=sid,
            replace_goals=True,
        )

        self.assertEqual([item["id"] for item in plan["long_goals"]], ["l1", "l2"])
        self.assertEqual([item["id"] for item in plan["mid_goals"]], ["m1", "m2"])
        self.assertEqual(op_result.get("mode"), "replace_goals")
        self.assertFalse(any(item.get("id") == "l3" for item in plan["long_goals"]))
        self.assertEqual(plan["last_long_review_date"], "2026-07-03")

    def test_world_life_plan_goal_apis_and_instruction_regenerate(self):
        async def run():
            from aiohttp import web

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
            session_schema.set_character_value(state, "custom_character", "小雨")
            svc._save_session_state(sid, state)
            svc._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            svc.regenerate_life_plan_goals = AsyncMock(return_value={
                "status": "updated",
                "life_plan": svc._save_life_plan_payload(sid, "小雨", {
                    "long_goals": [{
                        "id": "l1",
                        "dimension": "事业",
                        "text": "把插画做出自己的方向",
                        "motivation": "想被看见",
                        "status": "active",
                    }],
                    "mid_goals": [],
                    "today": {"date": "2026-07-03", "events": [], "texture": ""},
                }),
            })
            app = web.Application()
            app["service"] = svc

            regen = JsonRequest(app, {"session_id": sid}, payload={"instruction": "分散到事业和生活", "regenerate_goals": True})
            resp = await api_world_life_plan_generate(regen)
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            svc.regenerate_life_plan_goals.assert_awaited_once()
            self.assertEqual(svc.regenerate_life_plan_goals.await_args.kwargs["instruction"], "分散到事业和生活")
            self.assertEqual(data["life_plan"]["long_goals"][0]["dimension"], "事业")

            svc.regenerate_life_plan_goals.reset_mock()
            blank_regen = JsonRequest(app, {"session_id": sid}, payload={"instruction": "", "regenerate_goals": True})
            blank_resp = await api_world_life_plan_generate(blank_regen)
            blank_data = json.loads(blank_resp.text)
            self.assertTrue(blank_data["ok"])
            svc.regenerate_life_plan_goals.assert_awaited_once()
            self.assertEqual(svc.regenerate_life_plan_goals.await_args.kwargs["reason"], "web-goal-regenerate")

            create = JsonRequest(app, {"session_id": sid}, payload={
                "kind": "long",
                "id": "l2",
                "dimension": "生活",
                "text": "把住处和作息整理出自己的余裕",
                "motivation": "想喘口气",
            })
            created = json.loads((await api_world_life_plan_goal_create(create)).text)
            self.assertTrue(created["ok"])
            self.assertTrue(any(goal["id"] == "l2" for goal in created["life_plan"]["long_goals"]))

            update = JsonRequest(app, {"session_id": sid, "kind": "long", "goal_id": "l2"}, payload={
                "dimension": "理想",
                "text": "把生活整理成能支撑创作的样子",
            })
            updated = json.loads((await api_world_life_plan_goal_update(update)).text)
            self.assertTrue(updated["ok"])
            edited = [goal for goal in updated["life_plan"]["long_goals"] if goal["id"] == "l2"][0]
            self.assertEqual(edited["dimension"], "理想")

            delete = JsonRequest(app, {"session_id": sid, "kind": "long", "goal_id": "l2"})
            deleted = json.loads((await api_world_life_plan_goal_delete(delete)).text)
            self.assertTrue(deleted["ok"])
            self.assertFalse(any(goal["id"] == "l2" for goal in deleted["life_plan"]["long_goals"]))

        asyncio.run(run())

    def test_create_oc_message_includes_generated_life_plan_summary(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()

            await svc._create_oc_from_fields(
                123,
                "telegram:123",
                "小雨，原创角色，插画师，敏感要强",
                {
                    "name": "小雨",
                    "role": "原创角色",
                    "persona": "敏感、要强，害怕作品没人看见",
                    "appearance": "short black hair, blue eyes",
                    "outfit": "white shirt",
                    "occupation": "插画师",
                },
                {},
            )

            text = svc.send_message.await_args.args[1]
            self.assertIn("OC 已创建: 小雨", text)
            self.assertIn("生活主线", text)
            self.assertIn("长期线", text)
            self.assertIn("[", text)
            self.assertIn("中期线", text)

        asyncio.run(run())

    def test_life_plan_goal_instruction_command_regenerates_and_displays_summary(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc.send_message = AsyncMock()
            row = svc._save_life_plan_payload(sid, "", {
                "long_goals": [{
                    "id": "l1",
                    "dimension": "事业",
                    "text": "把插画作品做出自己的方向",
                    "motivation": "想被看见",
                    "status": "active",
                }],
                "mid_goals": [{
                    "id": "m1",
                    "parent_id": "l1",
                    "text": "这周完成一张练习稿",
                    "status": "active",
                }],
                "today": {"date": "2026-07-03", "events": [], "texture": ""},
            })
            svc.regenerate_life_plan_goals = AsyncMock(return_value={"status": "updated", "life_plan": row})

            await svc.dispatch_command(123, sid, "生活主线", "目标指示 从事业和爱好两个角度整理")

            svc.regenerate_life_plan_goals.assert_awaited_once()
            self.assertEqual(svc.regenerate_life_plan_goals.await_args.kwargs["instruction"], "从事业和爱好两个角度整理")
            text = svc.send_message.await_args.args[1]
            self.assertIn("已按目标指示更新生活主线", text)
            self.assertIn("[事业]", text)

        asyncio.run(run())

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
