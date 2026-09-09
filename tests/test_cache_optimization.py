from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from scripts.cache_usage_audit import audit
from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.app_store import AppStateStore
from telegram_comfyui_selfie.generation import PromptSlots
from telegram_comfyui_selfie.image_planning import plan_animaflow_slots, plan_roleplay_image
from telegram_comfyui_selfie.llm_metrics import cache_usage, request_observation, usage_rates
from telegram_comfyui_selfie.prompt_layout import CHAT_SYSTEM_STATIC_RULES, PHOTO_HISTORY_RULES, build_image_planner_messages
from telegram_comfyui_selfie.sqlite_migrations import migrate_database
from tests.support import ServiceFixtureMixin, make_mock_request, make_project_temp_dir


class CacheUsageTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_usage_api_reports_unknown_separately_and_groups_actual_endpoints(self):
        from aiohttp import web
        from telegram_comfyui_selfie.webui import api_admin_llm_usage

        async def run():
            svc = self.make_service()
            svc.app_store.record_llm_usage(profile_id="p", model="m", prompt_tokens=100, endpoint="https://a.test/v1")
            svc.app_store.record_llm_usage(profile_id="p", model="m", prompt_tokens=100, cached_tokens=60, endpoint="https://b.test/v1")
            app = web.Application()
            app["service"] = svc
            response = await api_admin_llm_usage(make_mock_request(app, "/api/admin/llm-usage?after=0", method="GET", admin=True))
            data = json.loads(response.text)
            self.assertEqual(data["summary"]["cache_hit_rate"], .6)
            self.assertEqual(data["summary"]["cache_coverage"], .5)
            self.assertEqual(data["summary"]["cache_unknown_requests"], 1)
            self.assertEqual(len(data["groups"]), 2)
            unknown = next(row for row in data["groups"] if row["endpoint"] == "https://a.test/v1")
            self.assertIsNone(unknown["cache_hit_rate"])
        asyncio.run(run())

    def test_http_retry_correlates_safe_metrics_and_counts_only_success_usage(self):
        async def run():
            svc = self.make_service()
            svc.config["chat_llm_api_key"] = "PRIVATE_KEY"
            messages = [{"role": "user", "content": "PRIVATE_PROMPT"}]
            output = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
            statuses = iter((500, 200))
            logs = []
            svc._ulog = lambda sid, tag, text="": logs.append((tag, text))

            class Response:
                def __init__(self):
                    self.status = next(statuses)
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    return False

            class Session:
                def __init__(self, *args, **kwargs):
                    pass
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    return False
                def post(self, *args, **kwargs):
                    return Response()

            with patch("telegram_comfyui_selfie.llm_runtime.aiohttp.ClientSession", Session), \
                 patch("telegram_comfyui_selfie.llm_runtime.read_limited_json", AsyncMock(return_value=output)), \
                 patch("telegram_comfyui_selfie.llm_runtime.read_limited_text", AsyncMock(return_value="upstream unavailable")), \
                 patch("telegram_comfyui_selfie.llm_runtime.asyncio.sleep", AsyncMock()):
                result = await svc._call_llm_messages(messages, purpose="chat", tag="retry-test", session_id="telegram:123")
            self.assertEqual(result, output)
            metrics = [json.loads(text) for tag, text in logs if tag == "LLM_METRICS"]
            self.assertEqual([m["attempt"] for m in metrics], [1, 2])
            self.assertEqual([m["status"] for m in metrics], [500, 200])
            self.assertEqual(metrics[0]["request_id"], metrics[1]["request_id"])
            self.assertNotIn("PRIVATE_PROMPT", json.dumps(metrics))
            self.assertNotIn("PRIVATE_KEY", json.dumps(metrics))
            rows = svc.app_store.aggregate_llm_usage()
            self.assertEqual(rows[0]["requests"], 1)
            self.assertEqual(rows[0]["cache_unknown_requests"], 1)
            with svc.app_store._connect() as conn:
                meta = json.loads(conn.execute("SELECT request_meta FROM llm_usage").fetchone()[0])
            self.assertEqual(meta["request_id"], metrics[1]["request_id"])
            self.assertEqual(meta["attempt"], 2)
            self.assertGreaterEqual(meta["duration_ms"], 0)
        asyncio.run(run())

    def test_backfill_is_readonly_by_default_backs_up_and_skips_ambiguous_records(self):
        root = make_project_temp_dir("cache_backfill")
        path = root / "state.sqlite3"
        migrate_database(path, target_version=8)
        with sqlite3.connect(path) as conn:
            conn.executemany("INSERT INTO llm_usage(created_at,model,prompt_tokens,completion_tokens) VALUES(100,?,100,10)", [("unique",), ("ambiguous",), ("ambiguous",)])
        entries = [{"ts": 100, "status": 200, "model": model, "response": {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": cached}}} for model, cached in [("unique", 0), ("ambiguous", 60)]]
        log = root / "debug.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in [*entries, entries[0]]) + "\nnot json", encoding="utf-8")
        original = path.read_bytes()
        preview = audit(path, [log])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(preview["eligible_backfills"], 1)
        self.assertEqual(preview["ambiguous_matches"], 1)
        self.assertEqual(preview["duplicate_log_entries"], 1)
        self.assertEqual(preview["invalid_log_lines"], 1)
        self.assertEqual(preview["applied"], 0)
        applied = audit(path, [log], apply=True)
        self.assertEqual(applied["applied"], 1)
        with sqlite3.connect(applied["backup_path"]) as conn:
            self.assertNotIn("cache_reported", {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)")})
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT cache_reported FROM llm_usage ORDER BY id").fetchall(), [(1,), (0,), (0,)])
        self.assertEqual(audit(path, [log], apply=True)["applied"], 0)

    def test_cache_field_presence_and_validation(self):
        cases = [
            ({}, False, 0),
            ({"prompt_tokens_details": {}}, False, 0),
            ({"cached_tokens": None}, False, 0),
            ({"cached_tokens": 0}, True, 0),
            ({"prompt_tokens_details": {"cached_tokens": 60}}, True, 60),
            ({"prompt_cache_hit_tokens": 0, "cached_tokens": 60}, True, 0),
            ({"prompt_cache_miss_tokens": 0}, True, 100),
            ({"cache_miss_tokens": 25}, True, 75),
            ({"cached_tokens": -1}, False, 0),
            ({"cached_tokens": 101}, False, 0),
            ({"cached_tokens": "bad"}, False, 0),
            ({"cached_tokens": float("nan")}, False, 0),
            ({"cached_tokens": 1.5}, False, 0),
            ({"cached_tokens": True}, False, 0),
        ]
        for raw, reported, tokens in cases:
            with self.subTest(raw=raw):
                result = cache_usage({"prompt_tokens": 100, **raw})
                self.assertEqual(result["cache_reported"], reported)
                self.assertEqual(result["cached_tokens"], tokens)
        conflict = cache_usage({"prompt_tokens": 100, "cached_tokens": 0, "cache_miss_tokens": 25})
        self.assertEqual(conflict["cached_tokens"], 0)
        self.assertEqual(conflict["cache_anomaly"], "conflicting_hit_miss")
        self.assertFalse(cache_usage({"cache_miss_tokens": 0})["cache_reported"])

    def test_migration_preserves_history_and_marks_legacy_zeros_unknown(self):
        path = make_project_temp_dir("cache_migration") / "state.sqlite3"
        migrate_database(path, target_version=8)
        with sqlite3.connect(path) as conn:
            conn.executemany("INSERT INTO llm_usage(created_at,prompt_tokens,cached_tokens) VALUES(1,100,?)", [(0,), (60,), (200,)])
        store = AppStateStore(path)
        rows = store.aggregate_llm_usage(after=0)
        self.assertEqual(rows[0]["requests"], 3)
        self.assertEqual(rows[0]["cache_reported_requests"], 1)
        self.assertEqual(rows[0]["cache_unknown_requests"], 2)
        self.assertEqual(rows[0]["cached_tokens"], 60)
        self.assertEqual(usage_rates(rows[0])["cache_hit_rate"], .6)
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT cached_tokens FROM llm_usage ORDER BY id").fetchall(), [(0,), (60,), (200,)])
            self.assertEqual(conn.execute("SELECT cache_source FROM llm_usage WHERE cached_tokens=60").fetchone()[0], "legacy_positive")
        self.assertTrue(store.schema_migration.backup_path.exists())
        self.assertEqual(AppStateStore(path).schema_migration.applied_versions, ())

    def test_unknown_inputs_do_not_dilute_known_rate_and_endpoint_groups(self):
        svc = self.make_service()
        resolved = {"api_base": "https://provider.test/v1?api_key=secret", "model": "m", "profile_id": "same", "profile_scope": "global"}
        for cached in (None, 0, 60):
            raw = {"prompt_tokens": 100}
            if cached is not None:
                raw["cached_tokens"] = cached
            svc._record_llm_usage_from_response({"usage": raw}, resolved)
        row = svc.app_store.aggregate_llm_usage(group_by=("endpoint", "profile_scope"))[0]
        self.assertNotIn("secret", row["endpoint"])
        self.assertEqual(row["cache_unknown_requests"], 1)
        self.assertEqual(row["cache_reported_prompt_tokens"], 200)
        self.assertEqual(usage_rates(row), {"cache_hit_rate": .3, "cache_coverage": .6667, "reported_cache_share": .2})
        self.assertIsNone(usage_rates({"prompt_tokens": 100})["cache_hit_rate"])
        svc._record_llm_usage_from_response({"usage": {"prompt_tokens": 20}}, {**resolved, "api_base": "https://other.test/v1"})
        self.assertEqual(len(svc.app_store.aggregate_llm_usage(group_by=("endpoint",))), 2)

    def test_observation_contains_no_prompt_credentials_or_route_value(self):
        body = {"model": "m", "messages": [{"role": "system", "content": "PRIVATE_PERSONA"}, {"role": "user", "content": "PRIVATE_INPUT"}], "max_tokens": 50}
        headers = {"Authorization": "Bearer PRIVATE_KEY", "x-opencode-session": "PRIVATE_SESSION", "User-Agent": "SucyuBot/1.0"}
        meta = request_observation(body, "https://user:password@host.test/v1?key=PRIVATE_KEY", headers)
        encoded = json.dumps(meta)
        for secret in ("PRIVATE_PERSONA", "PRIVATE_INPUT", "PRIVATE_KEY", "PRIVATE_SESSION", "password"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(meta["endpoint"], "https://host.test/v1")
        new_body = copy.deepcopy(body)
        new_body["messages"][-1]["content"] = "different input"
        other = request_observation(new_body, "https://host.test/v1", headers)
        self.assertEqual(meta["settings_hash"], other["settings_hash"])
        self.assertEqual(meta["messages"][0], other["messages"][0])
        self.assertNotEqual(meta["prompt_hash"], other["prompt_hash"])
        self.assertEqual(meta["route_hash"], other["route_hash"])

    def test_dynamic_rules_keep_system_authority_in_actual_wire_messages(self):
        async def run():
            svc = self.make_service()
            svc._call_llm_messages = AsyncMock(return_value={"choices": [{"message": {"content": "ok"}}]})
            self.assertEqual(await svc._call_llm("fixed", "request", tag="translate", system_tail="current rules"), "ok")
            messages = svc._call_llm_messages.await_args.args[0]
            self.assertEqual(messages[-2:], [{"role": "system", "content": "current rules"}, {"role": "user", "content": "request"}])
            self.assertEqual(messages[-3], {"role": "system", "content": "fixed"})
        asyncio.run(run())


class PromptLayoutTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_only_known_chat_rules_are_projected_and_chat_keeps_original_rules(self):
        custom = "角色自定义格式：" + CHAT_SYSTEM_STATIC_RULES + "自定义结尾"
        context = [{"role": "system", "content": CHAT_SYSTEM_STATIC_RULES},
                   {"role": "system", "content": "准确人设\n\n" + CHAT_SYSTEM_STATIC_RULES},
                   {"role": "user", "content": CHAT_SYSTEM_STATIC_RULES},
                   {"role": "system", "content": custom}]
        original = copy.deepcopy(context)
        messages = build_image_planner_messages("JSON 规划协议", "当前状态", "请求", context=context)
        self.assertEqual(messages[1]["content"], PHOTO_HISTORY_RULES)
        self.assertEqual(messages[2]["content"], "准确人设\n\n" + PHOTO_HISTORY_RULES)
        self.assertEqual(messages[3:5], original[2:])
        self.assertEqual(context, original)
        svc = self.make_service()
        for persona_first in (False, True):
            svc.config["chat_persona_first"] = persona_first
            chat = svc._build_chat_messages("telegram:123", "用户输入")
            self.assertIn(CHAT_SYSTEM_STATIC_RULES, "\n".join(m["content"] for m in chat))

    def test_exact_dedup_preserves_history_and_conflicting_facts(self):
        persona = "这是准确的角色人设"
        memory = "约定周五去公园"
        history = [{"role": "system", "content": persona}, {"role": "system", "content": memory}, {"role": "user", "content": "最近用户消息"}, {"role": "assistant", "content": "最近回复"}]
        saved = copy.deepcopy(history)
        dynamic = f"导演职责\n{persona}\n\n当前穿搭: 新外套"
        user = f"当前请求\n\n长期记忆:\n{memory}"
        messages = build_image_planner_messages("固定规划协议", dynamic, user, context=history, persona=persona, memory=memory, extra_dynamic="最新天气")
        text = "\n".join(m["content"] for m in messages)
        self.assertEqual(text.count(persona), 1)
        self.assertEqual(text.count(memory), 1)
        self.assertIn("当前穿搭: 新外套", text)
        self.assertIn("最新天气", text)
        self.assertEqual(messages[1:5], saved)
        self.assertEqual(history, saved)
        # 历史引用并非系统权威来源；不同事实不能用模糊匹配删掉。
        messages = build_image_planner_messages("固定", dynamic, user, context=[{"role": "user", "content": persona}], persona=persona, memory=memory)
        self.assertIn(persona, messages[-2]["content"])
        self.assertIn(memory, messages[-1]["content"])

    def test_push_planner_has_fixed_prefix_once_and_retains_continuity(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            svc.has_llm_config = lambda *a, **kw: True
            svc._get_effective_persona = lambda *a, **kw: "角色甲是热爱散步的城市居民。"
            svc._call_llm_messages = AsyncMock(return_value={"choices": [{"message": {"content": json.dumps({"scene": "She walks in a park.", "view": "third", "caption": "今天走一走。"})}}]})
            state = svc._get_session_state(sid)
            session_schema.set_chat_history(state, [{"role": "user", "content": "我们约好周五去散步"}, {"role": "assistant", "content": "约定好了"}])
            fixed = []
            for mode, weather in [("normal", {"desc": "晴", "temp": "20"}), ("followup", {"desc": "小雨", "temp": "18"}), ("morning", {"desc": "多云", "temp": "21"})]:
                await plan_roleplay_image(svc, sid, mode=mode, weather_data=weather)
                messages = svc._call_llm_messages.await_args.args[0]
                fixed.append(messages[0]["content"])
                self.assertTrue(fixed[-1].startswith("Scene boundary:"))
                all_text = "\n".join(m["content"] for m in messages)
                self.assertEqual(all_text.count("角色甲是热爱散步的城市居民。"), 1)
                self.assertIn("我们约好周五去散步", all_text)
                self.assertIn(weather["desc"], all_text)
            self.assertEqual(len(set(fixed)), 1)
        asyncio.run(run())

    def test_life_texture_changes_only_after_stable_memory_and_is_present_for_push(self):
        svc = self.make_service()
        sid = "telegram:123"
        svc._long_term_memory_context = lambda *a, **kw: "长期约定测试句"
        svc._life_plan_chat_context = lambda *a, **kw: "当前时段生活句甲"
        first = svc._build_chat_context_messages_for_push(sid)
        svc._life_plan_chat_context = lambda *a, **kw: "当前时段生活句乙"
        second = svc._build_chat_context_messages_for_push(sid)
        self.assertEqual(first[:-1], second[:-1])
        self.assertEqual(first[-1]["content"], "当前时段生活句甲")
        self.assertEqual(second[-1]["content"], "当前时段生活句乙")
        self.assertIn("长期约定测试句", "\n".join(m["content"] for m in first[:-1]))

    def test_anima_template_is_stable_across_dynamic_rules_and_weather(self):
        async def run():
            svc = self.make_service()
            svc.has_llm_config = lambda *a, **kw: True
            svc._call_llm = AsyncMock(return_value=json.dumps({"tags": "She walks in a park.", "neg": "bad hands"}))
            sid = "telegram:123"
            schema = {"parameters": {"properties": {"tags": {"type": "string"}, "neg": {"type": "string"}, "count": {"type": "string"}}, "required": ["tags"]}}
            fixed = []
            for cfg, level, count, desc in [(1, 7, "", "晴"), (4, 4, "1girl", "小雨")]:
                svc.config["animaflow_cfg"] = str(cfg)
                svc._get_effective_safety = lambda *a, **kw: {"level": level}
                svc._weather_caches[sid] = {"data": {"desc": desc, "temp": "19"}}
                await plan_animaflow_slots(svc, sid, PromptSlots(scene="She walks.", count=count), workflow="example", schema=schema, knowledge={"rules": "Keep the pose."})
                call = svc._call_llm.await_args
                fixed.append(call.args[0])
                self.assertNotIn(f"当前天气: {desc}", call.args[0])
                self.assertIn(desc, call.kwargs["system_tail"])
                self.assertIn("cfg=1" if cfg == 1 else "支持 neg", call.kwargs["system_tail"])
            self.assertEqual(fixed[0], fixed[1])
        asyncio.run(run())

    def test_translate_views_share_prefix_but_keep_view_rules(self):
        async def run():
            svc = self.make_service()
            svc.has_llm_config = lambda *a, **kw: True
            svc._call_llm = AsyncMock(return_value="She walks through a park.")
            fixed = []
            for view in ("selfie", "mirror", "third", "portrait", "pov"):
                await svc._translate_to_tags("她在散步", "telegram:123", view=view)
                call = svc._call_llm.await_args
                fixed.append(call.args[0])
                self.assertIn(view.lower(), call.kwargs["system_tail"].lower())
                self.assertIn("她在散步", call.args[1])
            self.assertEqual(len(set(fixed)), 1)
        asyncio.run(run())

    def test_push_direction_timestamp_is_after_real_context(self):
        async def run():
            svc = self.make_service()
            svc.has_llm_config = lambda *a, **kw: True
            svc._call_llm = AsyncMock(return_value=json.dumps({"topic_mode": "independent", "topic_guides": [{"source": "life", "guide": "公园散步"}]}))
            sid = "telegram:123"
            state = svc._get_session_state(sid)
            base = datetime(2026, 9, 9, 12, 0)
            users = []
            for now in (base, base + timedelta(minutes=1)):
                await svc._decide_push_topic_direction(sid, "normal", state, now)
                users.append(svc._call_llm.await_args.args[1])
            self.assertTrue(users[0].startswith("角色名:"))
            self.assertEqual(users[0].split("当前时间:")[0], users[1].split("当前时间:")[0])
            self.assertNotEqual(users[0], users[1])
        asyncio.run(run())
