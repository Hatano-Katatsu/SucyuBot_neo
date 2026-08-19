from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock

from telegram_comfyui_selfie import session_schema
from tests.support import ServiceFixtureMixin


PAIR_CONFIG = [
    {"a": {"chat_id": 1001, "character": "小艾"}, "b": {"chat_id": 2002, "character": "铃音"}},
]

ORCHESTRATION_PAYLOAD = {
    "summary": "午后的咖啡店里，小艾排队时撞洒了铃音的咖啡，两人聊了一个下午。",
    "pov_a": "我第一次来这座城市就闯了祸，还好铃音没生气，我们聊得很开心。",
    "pov_b": "一个外地来的冒失鬼赔了我一杯咖啡，没想到聊得挺投缘。",
    "relationship": "初识，交换了称呼，约定下次小艾再来时再聚",
    "memory_a": "在铃音的城市认识了她，约定下次再聚",
    "memory_b": "认识了外地来的小艾，她答应下次再来",
}

LOCAL_ORCHESTRATION_PAYLOAD = {
    "summary": "小艾按午后的动线去了咖啡店，恰好遇见铃音，两人交换了最近各自忙碌的近况后一起喝完咖啡。",
    "pov_active": "我在咖啡店碰见铃音，和她聊了最近的生活，还约好下次再坐一会儿。",
    "pov_other": "我出门时遇见小艾，听她说了近况，也把自己的计划告诉了她。",
    "relationship": "熟悉了一些，约好下次继续交换近况",
    "memory_active": "在咖啡店遇见铃音，并约好下次继续聊近况",
    "memory_other": "在咖啡店遇见小艾，并约好下次继续聊近况",
    "push_caption": "刚才在咖啡店碰见铃音了，我们坐下来聊了好一会儿，连原本的安排都差点忘了。",
    "scene_hint": "小艾独自坐在咖啡店靠窗座位，桌上有两只用过的咖啡杯，铃音已经离开画面。",
}


class EncounterTestCase(ServiceFixtureMixin, unittest.TestCase):
    def make_encounter_service(self, *, pairs=None, enabled=True):
        svc = self.make_service()
        svc.config["cross_world_enabled"] = enabled
        svc.config["cross_world_pairs"] = PAIR_CONFIG if pairs is None else pairs
        svc.config["location"] = "上海"
        svc.config["cross_world_encounter_chance"] = "1"
        svc.config["cross_world_encounter_cooldown_days"] = "7"
        svc.config["user_log_enabled"] = False
        # 两侧会话：各有一个活动角色，且都不在近期活跃窗口内。
        for chat_id, character, city in ((1001, "小艾", "北京"), (2002, "铃音", "上海")):
            session_id = svc.session_id_for_chat(chat_id)
            state = svc._get_session_state(session_id)
            session_schema.set_character_value(state, "custom_character", character)
            session_schema.set_character_value(state, "custom_location", city)
            session_schema.set_last_interaction(state, 0)
            svc._save_session_state(session_id, state)
        # 全天清醒，避免测试受真实时钟影响。
        svc._character_schedule_minutes = Mock(return_value={"wake": 0, "sleep": 1439})
        # 邂逅编排前的天气查询走外部 HTTP，测试中隔离。
        svc._fetch_weather = AsyncMock(return_value=None)
        return svc

    def _orchestration_mock(self, svc, payload=None):
        svc._call_llm = AsyncMock(return_value=json.dumps(payload or ORCHESTRATION_PAYLOAD, ensure_ascii=False))
        return svc._call_llm

    def _run(self, svc, pair=None):
        pair = pair or svc._cross_world_pairs()[0]
        return asyncio.run(svc._run_encounter(pair))

    def _add_local_inactive_character(self, svc, sid: str, character: str = "铃音"):
        state = svc._get_session_state(sid)
        session_schema.get_saved_characters(state)[character] = {
            "character": character,
            "bot_name": character,
            "persona": "安静但很会观察别人，喜欢在午后出门。",
            "occupation": "自由职业",
            "day_anchor": "flexible",
            "workday_wake_time": "00:01",
            "workday_sleep_time": "23:59",
            "weekend_wake_time": "00:01",
            "weekend_sleep_time": "23:59",
        }
        svc._save_session_state(sid, state)

    def _save_current_local_plan(self, svc, sid: str, character: str, today: str):
        svc._save_life_plan_payload(sid, character, {
            "long_goals": [{"id": "l1", "text": "过好自己的生活", "status": "active", "dimension": "生活"}],
            "mid_goals": [{"id": "m1", "text": "保持日常节奏", "status": "active", "parent_id": "l1"}],
            "today": {
                "date": today,
                "texture": "按自己的节奏度过今天。",
                "events": [{
                    "id": "e1", "time_hint": "noon", "text": "去咖啡店整理近况",
                    "place_key": "cafe", "status": "planned",
                }],
            },
            "npcs": [],
        })

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def test_pair_config_normalization_and_invalid_entries(self):
        svc = self.make_encounter_service(pairs=[
            {"a": {"chat_id": 1001, "character": "小艾"}, "b": {"chat_id": 2002, "character": "铃音"}},
            {"a": {"chat_id": 1001}, "b": {"chat_id": 2002, "character": "铃音"}},  # 缺 character
            {"a": {"chat_id": 1001, "character": "小艾"}, "b": {"chat_id": 1001, "character": "小艾"}},  # 同会话
            "not-a-dict",
        ])
        pairs = svc._cross_world_pairs()
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair["a"]["session_id"], "telegram:1001")
        self.assertEqual(pair["b"]["session_id"], "telegram:2002")
        # pair_key 不区分方向
        self.assertEqual(
            pair["pair_key"],
            svc._encounter_pair_key("telegram:2002", "铃音", "telegram:1001", "小艾"),
        )

    def test_numeric_config_validation(self):
        svc = self.make_encounter_service()
        svc.config["cross_world_encounter_chance"] = "not-a-number"
        self.assertEqual(svc._encounter_chance(), 0.5)
        svc.config["cross_world_encounter_chance"] = float("nan")
        self.assertEqual(svc._encounter_chance(), 0.5)
        svc.config["cross_world_encounter_chance"] = "5"
        self.assertEqual(svc._encounter_chance(), 1.0)
        svc.config["cross_world_encounter_chance"] = "-1"
        self.assertEqual(svc._encounter_chance(), 0.0)
        svc.config["cross_world_encounter_cooldown_days"] = float("inf")
        self.assertEqual(svc._encounter_cooldown_days(), 7.0)
        svc.config["cross_world_encounter_cooldown_days"] = "-3"
        self.assertEqual(svc._encounter_cooldown_days(), 0.0)

    def test_local_interaction_settings_default_off_and_require_two_roles(self):
        svc = self.make_encounter_service()
        sid = svc.session_id_for_chat(1001)
        self._add_local_inactive_character(svc, sid)
        fixed_now = svc._session_now(sid).replace(hour=12, minute=0, second=0, microsecond=0)

        status = svc._local_interaction_push_status(sid, now=fixed_now)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["daily_limit"], 0)
        with self.assertRaises(ValueError):
            svc._configure_local_interaction_push(sid, ["小艾"], 1)

        status = svc._configure_local_interaction_push(sid, ["小艾", "铃音"], 2)
        status = svc._local_interaction_push_status(sid, now=fixed_now)
        self.assertTrue(status["enabled"])
        self.assertTrue(status["available"])
        self.assertEqual(status["remaining"], 2)
        self.assertEqual([item["character_key"] for item in status["candidates"]], ["铃音"])

    def test_local_interaction_prepares_target_route_then_commits_both_histories(self):
        async def run():
            svc = self.make_encounter_service()
            sid = svc.session_id_for_chat(1001)
            self._add_local_inactive_character(svc, sid)
            fixed_now = svc._session_now(sid).replace(hour=12, minute=0, second=0, microsecond=0)
            today = svc._life_today_date(sid, fixed_now)
            self._save_current_local_plan(svc, sid, "小艾", today)
            self._save_current_local_plan(svc, sid, "铃音", today)
            svc._configure_local_interaction_push(sid, ["小艾", "铃音"], 1)
            original_ensure = svc.ensure_life_plan_for_today
            svc.ensure_life_plan_for_today = AsyncMock(wraps=original_ensure)
            svc._orchestrate_local_character_interaction = AsyncMock(
                return_value=dict(LOCAL_ORCHESTRATION_PAYLOAD)
            )

            prepared = await svc._prepare_local_character_interaction_push(
                sid,
                fixed_now,
                weather={"desc": "晴", "temp": "23"},
                active_world={
                    "character_place": {
                        "key": "cafe", "label": "咖啡店", "name": "靠窗座位",
                        "public": True, "indoor": True,
                    },
                },
                target_character="铃音",
            )

            self.assertIsNotNone(prepared)
            svc.ensure_life_plan_for_today.assert_awaited_once()
            self.assertEqual(svc.ensure_life_plan_for_today.await_args.kwargs["character_key"], "铃音")
            pair_key = prepared["pair_key"]
            self.assertEqual(svc.app_store.list_encounters_for_pair(pair_key), [])
            before = session_schema.get_character_interaction_push(svc._get_session_state(sid))
            self.assertEqual(before["count"], 0)

            self.assertTrue(svc._commit_local_character_interaction_push(sid, prepared))
            self.assertEqual(svc._context_character_key(sid), "小艾")
            records = svc.app_store.list_encounters_for_pair(pair_key)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["type"], "local_push")
            after = session_schema.get_character_interaction_push(svc._get_session_state(sid))
            self.assertEqual(after["count"], 1)
            self.assertFalse(svc._commit_local_character_interaction_push(sid, prepared))
            self.assertEqual(len(svc.app_store.list_encounters_for_pair(pair_key)), 1)

            state = svc._get_session_state(sid)
            active_history = session_schema.get_chat_history(state)
            inactive_context = session_schema.get_character_contexts(state)["铃音"]
            inactive_history = session_schema.get_chat_history(inactive_context)
            self.assertIn("铃音是另一个角色", active_history[-1]["content"])
            self.assertIn("小艾是另一个角色", inactive_history[-1]["content"])
            for character in ("小艾", "铃音"):
                stored = svc.app_store.list_messages(sid, character, limit=5)
                self.assertTrue(any("同一用户角色池内" in item["content"] for item in stored))
                plan = svc._load_life_plan_row(sid, character)["payload"]
                self.assertTrue(any(event.get("status") == "done" for event in plan["today"]["events"]))

        asyncio.run(run())

    def test_pair_config_webui_text_format(self):
        # WebUI 文本格式：每行 chat_id:角色名 = chat_id:角色名，兼容全角标点与注释行。
        svc = self.make_encounter_service(pairs=(
            "# 注释行\n"
            "\n"
            "1001:小艾 = 2002:铃音\n"
            "3003：阿澄 ＝ 4004：阿哲\n"
            "没有分隔符的行\n"
            "5005:只有一侧 =\n"
        ))
        pairs = svc._cross_world_pairs()
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0]["a"]["session_id"], "telegram:1001")
        self.assertEqual(pairs[0]["b"]["character"], "铃音")
        self.assertEqual(pairs[1]["a"]["character"], "阿澄")
        self.assertEqual(pairs[1]["b"]["session_id"], "telegram:4004")
        # 角色名含等号/冒号之外的文本不受影响；空串视为未配置
        svc.config["cross_world_pairs"] = ""
        self.assertEqual(svc._cross_world_pairs(), [])

    # ------------------------------------------------------------------
    # travel override
    # ------------------------------------------------------------------
    def test_travel_override_city_read_and_expiry(self):
        svc = self.make_encounter_service()
        sid = svc.session_id_for_chat(1001)
        state = svc._get_session_state(sid)
        self.assertEqual(svc._session_city(sid), "北京")
        session_schema.set_travel_override(state, city="上海", until=time.time() + 3600, home_city="北京")
        self.assertEqual(svc._session_city(sid), "上海")
        # 过期后读取层惰性失效，不落回旅行城市；custom_location 未被污染。
        session_schema.set_travel_override(state, city="上海", until=time.time() - 1, home_city="北京")
        self.assertEqual(svc._session_city(sid), "北京")
        self.assertEqual(svc._get_session_cfg(sid, "location", ""), "北京")

    def test_dream_settles_expired_travel_override(self):
        svc = self.make_encounter_service()
        sid = svc.session_id_for_chat(1001)
        state = svc._get_session_state(sid)
        session_schema.set_travel_override(state, city="上海", until=time.time() + 3600, home_city="北京")
        # 未过期：dream 结算不动它。
        self.assertFalse(svc._settle_travel_override(sid, "小艾"))
        self.assertEqual(svc._session_city(sid), "上海")
        # 已过期：dream 结算清除。
        session_schema.set_travel_override(state, city="上海", until=time.time() - 1, home_city="北京")
        self.assertTrue(svc._settle_travel_override(sid, "小艾"))
        self.assertEqual(session_schema.get_travel_override(state), {})
        # 非活动角色 key 不结算。
        session_schema.set_travel_override(state, city="上海", until=time.time() - 1, home_city="北京")
        self.assertFalse(svc._settle_travel_override(sid, "其他角色"))
        self.assertTrue(session_schema.get_travel_override(state))

    # ------------------------------------------------------------------
    # 编排主流程
    # ------------------------------------------------------------------
    def test_run_encounter_success_persists_both_sides(self):
        svc = self.make_encounter_service()
        llm = self._orchestration_mock(svc)
        pair = svc._cross_world_pairs()[0]

        self.assertTrue(self._run(svc, pair))

        # 编排调用用地主侧（b）会话 profile/记账：session_id 必须是 host 侧。
        self.assertEqual(llm.await_count, 1)
        host_sid = llm.await_args.kwargs["session_id"]
        visitor_sid = svc.session_id_for_chat(1001) if host_sid == svc.session_id_for_chat(2002) else svc.session_id_for_chat(2002)
        self.assertIn(host_sid, (svc.session_id_for_chat(1001), svc.session_id_for_chat(2002)))
        host_char = "铃音" if host_sid == svc.session_id_for_chat(2002) else "小艾"
        visitor_char = "小艾" if host_char == "铃音" else "铃音"

        # encounters 表落完整记录。
        records = svc.app_store.list_encounters_for_pair(pair["pair_key"])
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["type"], "meeting")
        self.assertEqual(record["summary"], ORCHESTRATION_PAYLOAD["summary"])
        self.assertEqual(record["relationship"], ORCHESTRATION_PAYLOAD["relationship"])
        self.assertTrue(record["city"])
        self.assertTrue(record["venue"])

        # 双方历史各注入一条 system 邂逅事件（内存 + SQLite），含视角约束。
        for sid, char in ((host_sid, host_char), (visitor_sid, visitor_char)):
            history = session_schema.get_chat_history(svc._get_session_state(sid))
            self.assertEqual(history[-1]["role"], "system")
            self.assertIn("邂逅事件", history[-1]["content"])
            self.assertIn("可自然承接", history[-1]["content"])
            stored = svc.app_store.list_messages(sid, char, limit=5)
            self.assertTrue(any(m["role"] == "system" and "邂逅事件" in m["content"] for m in stored))

        # 访客侧 travel override 生效：城市读取切到地主城市，custom_location 不变。
        visitor_state = svc._get_session_state(visitor_sid)
        override = session_schema.get_travel_override(visitor_state)
        self.assertTrue(override["city"])
        self.assertEqual(svc._session_city(visitor_sid), override["city"])
        self.assertEqual(override["home_city"], svc._get_session_cfg(visitor_sid, "location", ""))
        # 访客被钉到邂逅场所，source=encounter。
        self.assertTrue(session_schema.get_character_place(visitor_state))
        self.assertEqual(session_schema.get_character_place_name(visitor_state), record["venue"])
        place_history = session_schema.get_character_place_history(visitor_state)
        self.assertEqual(place_history[-1]["source"], "encounter")

        # 记忆建议过范围过滤后落 kind=event，source=encounter:<id>。
        memories = svc.memory.list_memories(host_sid, character=host_char, limit=10)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["kind"], "event")
        self.assertEqual(memories[0]["source"], f"encounter:{record['id']}")

    def test_orchestration_uses_host_session_and_fast_purpose(self):
        svc = self.make_encounter_service()
        llm = self._orchestration_mock(svc)
        self.assertTrue(self._run(svc))
        kwargs = llm.await_args.kwargs
        # 地主侧会话 + fast profile（purpose=image 走 fast_profile_id）。
        self.assertEqual(kwargs["purpose"], "image")
        self.assertIn(kwargs["session_id"], (svc.session_id_for_chat(1001), svc.session_id_for_chat(2002)))
        self.assertEqual(kwargs["tag"], "encounter")

    def test_orchestration_json_with_safe_repairs(self):
        svc = self.make_encounter_service()
        # 相邻 token 之间漏逗号的损坏 JSON 走保守修复成功。
        broken = json.dumps(ORCHESTRATION_PAYLOAD, ensure_ascii=False).replace(
            '", "pov_a"', '" "pov_a"', 1,
        )
        svc._call_llm = AsyncMock(return_value=broken)
        self.assertTrue(self._run(svc))
        self.assertEqual(svc._call_llm.await_count, 1)

    def test_broken_orchestration_json_aborts_without_writes(self):
        svc = self.make_encounter_service()
        svc._call_llm = AsyncMock(return_value="这不是 JSON")
        self.assertFalse(self._run(svc))
        # 重试一次后整体中止：无任何一侧落库。
        self.assertEqual(svc._call_llm.await_count, 2)
        pair = svc._cross_world_pairs()[0]
        self.assertEqual(svc.app_store.list_encounters_for_pair(pair["pair_key"]), [])
        for sid in (svc.session_id_for_chat(1001), svc.session_id_for_chat(2002)):
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_travel_override(state), {})
            self.assertFalse(session_schema.get_chat_history(state))

    def test_incomplete_orchestration_aborts(self):
        svc = self.make_encounter_service()
        payload = dict(ORCHESTRATION_PAYLOAD, pov_b="")
        svc._call_llm = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))
        self.assertFalse(self._run(svc))
        pair = svc._cross_world_pairs()[0]
        self.assertEqual(svc.app_store.list_encounters_for_pair(pair["pair_key"]), [])

    def test_second_side_busy_rolls_back_nothing_written(self):
        svc = self.make_encounter_service()
        llm = self._orchestration_mock(svc)
        # 地主侧近期活跃 → 空闲检查失败，编排调用根本不发生，无任何落库。
        busy_sid = svc.session_id_for_chat(2002)
        session_schema.set_last_interaction(svc._get_session_state(busy_sid), time.time())
        self.assertFalse(self._run(svc))
        self.assertEqual(llm.await_count, 0)
        pair = svc._cross_world_pairs()[0]
        self.assertEqual(svc.app_store.list_encounters_for_pair(pair["pair_key"]), [])

    def test_locks_acquired_in_session_id_order(self):
        svc = self.make_encounter_service()
        self._orchestration_mock(svc)
        acquired: list[str] = []
        real_lock = svc.character_operation_lock

        class RecordingLock:
            def __init__(self, sid):
                self.sid = sid
                self._lock = real_lock(sid)

            def locked(self):
                return self._lock.locked()

            async def __aenter__(self):
                acquired.append(self.sid)
                return await self._lock.__aenter__()

            async def __aexit__(self, *exc):
                return await self._lock.__aexit__(*exc)

        svc.character_operation_lock = lambda sid: RecordingLock(sid)
        self.assertTrue(self._run(svc))
        self.assertEqual(acquired, sorted(acquired))
        self.assertEqual(len(set(acquired)), 2)

    def test_memory_scope_filter_applies(self):
        svc = self.make_encounter_service()
        payload = dict(
            ORCHESTRATION_PAYLOAD,
            # 结构化短期信息（含「城市」无稳定线索）应被 _is_long_memory_in_scope 过滤。
            memory_a="当前城市是上海",
        )
        self._orchestration_mock(svc, payload)
        pair = svc._cross_world_pairs()[0]
        self.assertTrue(self._run(svc, pair))
        record = svc.app_store.list_encounters_for_pair(pair["pair_key"])[0]
        a_sid = record["session_id_a"]
        b_sid = record["session_id_b"]
        self.assertEqual(svc.memory.list_memories(a_sid, character=record["character_a"], limit=10), [])
        memories_b = svc.memory.list_memories(b_sid, character=record["character_b"], limit=10)
        self.assertEqual(len(memories_b), 1)

    def test_life_plan_event_and_npc_recorded(self):
        svc = self.make_encounter_service()
        self._orchestration_mock(svc)
        pair = svc._cross_world_pairs()[0]
        # 预先落一份当日 life_plan，邂逅应追加已完成事件与 NPC。
        for chat_id, character in ((1001, "小艾"), (2002, "铃音")):
            sid = svc.session_id_for_chat(chat_id)
            today = svc._life_today_date(sid)
            svc._save_life_plan_payload(sid, character, {
                "long_goals": [{"id": "l1", "text": "好好生活", "status": "active", "dimension": "生活"}],
                "mid_goals": [{"id": "m1", "text": "这周出去走走", "status": "active", "parent_id": "l1"}],
                "today": {"date": today, "events": [], "texture": ""},
                "npcs": [],
            })
        self.assertTrue(self._run(svc, pair))
        for chat_id, character, other in ((1001, "小艾", "铃音"), (2002, "铃音", "小艾")):
            sid = svc.session_id_for_chat(chat_id)
            plan = svc._load_life_plan_row(sid, character)["payload"]
            events = plan["today"]["events"]
            self.assertTrue(any(
                event.get("status") == "done" and other in event.get("text", "")
                for event in events
            ))
            self.assertTrue(any(
                isinstance(npc, dict) and npc.get("name") == other
                for npc in plan.get("npcs") or []
            ))

    def test_reunion_prompt_includes_history(self):
        svc = self.make_encounter_service()
        pair = svc._cross_world_pairs()[0]
        svc.app_store.record_encounter(
            pair_key=pair["pair_key"],
            session_id_a="telegram:1001", character_a="小艾",
            session_id_b="telegram:2002", character_b="铃音",
            ts=time.time() - 10 * 86400,
            city="上海", venue="江边咖啡店",
            summary="上次在咖啡店初识", relationship="初识，交换了称呼",
        )
        svc.config["cross_world_encounter_cooldown_days"] = "0"
        llm = self._orchestration_mock(svc)
        self.assertTrue(self._run(svc, pair))
        user_prompt = llm.await_args.args[1]
        self.assertIn("既往邂逅记录", user_prompt)
        self.assertIn("江边咖啡店", user_prompt)

    # ------------------------------------------------------------------
    # 调度门：开关 / 冷却 / 概率
    # ------------------------------------------------------------------
    def _collect_spawns(self, svc):
        spawned: list[asyncio.Task] = []
        real_spawn = svc._spawn_background

        def capture(coro, **kwargs):
            task = real_spawn(coro, **kwargs)
            spawned.append(task)
            return task

        svc._spawn_background = capture
        return spawned

    async def _drain(self, svc, spawned):
        if spawned:
            await asyncio.gather(*spawned, return_exceptions=True)
        await svc._shutdown_background_tasks(1.0, final=True)

    def test_scheduler_gate_disabled_never_triggers(self):
        svc = self.make_encounter_service(enabled=False)
        svc._run_encounter = AsyncMock(return_value=True)

        async def main():
            spawned = self._collect_spawns(svc)
            await svc._maybe_schedule_encounters()
            self.assertEqual(spawned, [])
            self.assertEqual(svc._run_encounter.await_count, 0)

        asyncio.run(main())

    def test_scheduler_gate_cooldown_blocks(self):
        svc = self.make_encounter_service()
        svc._run_encounter = AsyncMock(return_value=True)
        pair = svc._cross_world_pairs()[0]
        svc.app_store.record_encounter(
            pair_key=pair["pair_key"],
            session_id_a="telegram:1001", character_a="小艾",
            session_id_b="telegram:2002", character_b="铃音",
            ts=time.time(),
        )

        async def main():
            spawned = self._collect_spawns(svc)
            await svc._maybe_schedule_encounters()
            self.assertEqual(spawned, [])
            # 冷却结束后放行。
            svc.config["cross_world_encounter_cooldown_days"] = "0"
            await svc._maybe_schedule_encounters()
            self.assertEqual(len(spawned), 1)
            await self._drain(svc, spawned)
            self.assertEqual(svc._run_encounter.await_count, 1)

        asyncio.run(main())

    def test_scheduler_gate_chance_zero_blocks(self):
        svc = self.make_encounter_service()
        svc.config["cross_world_encounter_chance"] = "0"
        svc._run_encounter = AsyncMock(return_value=True)

        async def main():
            spawned = self._collect_spawns(svc)
            await svc._maybe_schedule_encounters()
            self.assertEqual(spawned, [])

        asyncio.run(main())

    def test_scheduler_failure_isolated(self):
        svc = self.make_encounter_service()
        svc._run_encounter = AsyncMock(side_effect=RuntimeError("编排炸了"))

        async def main():
            spawned = self._collect_spawns(svc)
            await svc._maybe_schedule_encounters()
            self.assertEqual(len(spawned), 1)
            # 单对失败不抛出、不阻塞；任务自身消费异常。
            await self._drain(svc, spawned)
            self.assertTrue(all(task.done() for task in spawned))

        asyncio.run(main())

    def test_pair_not_active_character_skips(self):
        svc = self.make_encounter_service()
        svc._call_llm = AsyncMock(return_value=json.dumps(ORCHESTRATION_PAYLOAD, ensure_ascii=False))
        # 一侧切到了别的角色 → 配对角色非活动，整场跳过。
        state = svc._get_session_state(svc.session_id_for_chat(2002))
        session_schema.set_character_value(state, "custom_character", "别的角色")
        self.assertFalse(self._run(svc))
        self.assertEqual(svc._call_llm.await_count, 0)


if __name__ == "__main__":
    unittest.main()
