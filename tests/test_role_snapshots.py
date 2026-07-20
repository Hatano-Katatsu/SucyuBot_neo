from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from telegram_comfyui_selfie import session_schema
from tests.support import ServiceFixtureMixin


class RoleSnapshotTestCase(ServiceFixtureMixin, unittest.TestCase):
    """后台地点和生活线必须绑定启动时角色，不能读取后来切入的 live 角色。"""

    @staticmethod
    def _set_active_character(svc, sid: str, name: str, persona: str, *, day_anchor: str = "home"):
        state = svc._get_session_state(sid)
        session_schema.set_character_value(state, "custom_character", name)
        session_schema.set_character_value(state, "custom_scheduled_persona", persona)
        session_schema.set_character_value(state, "custom_character_day_anchor", day_anchor)
        return state

    def test_location_extract_uses_private_fast_profile_and_updates_captured_frozen_role(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:location-snapshot"
            state = self._set_active_character(svc, sid, "角色A", "A 人格")
            svc._snapshot_character(state)
            svc._save_current_character_context(state)
            version = svc._character_snapshot_version(sid, "角色A")

            started = asyncio.Event()
            release = asyncio.Event()
            config_calls = []
            llm_kwargs = {}

            def has_llm_config(purpose, session_id=""):
                config_calls.append((purpose, session_id))
                return purpose == "image" and session_id == sid

            async def fake_call_llm(_system, _user, **kwargs):
                llm_kwargs.update(kwargs)
                started.set()
                await release.wait()
                return json.dumps({"place": "home", "place_name": "A 的家"}, ensure_ascii=False)

            svc.has_llm_config = has_llm_config
            svc._call_llm = fake_call_llm
            task = asyncio.create_task(svc._update_character_place_from_text(
                sid,
                "我刚到家，在客厅。",
                character_key="角色A",
                expected_character_version=version,
                life_profile={"age_stage": "adult", "day_anchor": "home"},
            ))
            await asyncio.wait_for(started.wait(), timeout=1)

            session_schema.get_saved_characters(state)["角色B"] = {
                "character": "角色B",
                "persona": "B 人格",
                "day_anchor": "company",
            }
            svc._apply_character_payload(state, session_schema.get_saved_characters(state)["角色B"])
            svc._restore_character_context(sid, state)
            release.set()

            self.assertTrue(await task)
            self.assertEqual(svc._context_character_key(sid), "角色B")
            self.assertEqual(session_schema.get_character_place(state), "")
            frozen_a = session_schema.get_character_contexts(state)["角色A"]
            self.assertEqual(session_schema.get_character_place(frozen_a), "home")
            self.assertEqual(session_schema.get_character_place_name(frozen_a), "A 的家")
            self.assertIn(("image", sid), config_calls)
            self.assertEqual(llm_kwargs.get("session_id"), sid)
            self.assertEqual(llm_kwargs.get("purpose"), "image")

        asyncio.run(run())

    def test_run_roleplay_chat_captures_character_key_version_and_profile_for_location_task(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:chat-location-snapshot"
            state = self._set_active_character(svc, sid, "角色A", "A 人格")
            state["life_profile"] = {"age_stage": "adult", "day_anchor": "home"}
            svc._snapshot_character(state)
            captured = {}
            finished = asyncio.Event()

            async def fake_extract(_sid, _text, **kwargs):
                captured.update(kwargs)
                finished.set()
                return False

            svc._ensure_life_profile = AsyncMock(return_value=state["life_profile"])
            svc._call_llm_messages = AsyncMock(return_value={
                "choices": [{"message": {"content": "我刚到家，在客厅里。"}}],
                "usage": {},
            })
            svc._judge_image_moment = AsyncMock(return_value=None)
            svc._update_character_place_from_text = fake_extract

            reply = await svc.run_roleplay_chat(1, sid, "你在哪里？")
            self.assertIn("刚到家", reply)
            await asyncio.wait_for(finished.wait(), timeout=1)
            self.assertEqual(captured.get("character_key"), "角色A")
            self.assertEqual(
                captured.get("expected_character_version"),
                svc._character_snapshot_version(sid, "角色A"),
            )
            self.assertEqual(captured.get("life_profile", {}).get("day_anchor"), "home")

        asyncio.run(run())

    def test_nonactive_life_plan_snapshot_reads_only_matching_character_sources(self):
        svc = self.make_service()
        sid = "telegram:life-materials"
        state = self._set_active_character(svc, sid, "角色B", "B 人格", day_anchor="company")
        session_schema.set_chat_history(state, [
            {"role": "user", "content": "B 用户聊天"},
            {"role": "assistant", "content": "B 角色聊天"},
        ])
        state["life_profile"] = {"age_stage": "adult", "day_anchor": "company"}
        session_schema.get_saved_characters(state)["角色A"] = {
            "character": "角色A",
            "series": "A 作品",
            "persona": "A 人格",
            "role_name": "学生",
            "age_stage": "minor",
            "day_anchor": "school",
        }
        frozen_a = {}
        session_schema.set_chat_history(frozen_a, [
            {"role": "user", "content": "A 用户聊天"},
            {"role": "assistant", "content": "A 角色聊天"},
        ])
        frozen_a["life_profile"] = {"age_stage": "minor", "day_anchor": "school"}
        session_schema.get_character_contexts(state)["角色A"] = frozen_a
        svc.app_store.upsert_character_history_summary(sid, "角色A", "A 历史总结")
        svc.app_store.upsert_character_history_summary(sid, "角色B", "B 历史总结")
        svc.app_store.upsert_diary(sid, "角色A", "2026-07-19", "A 日记")
        svc.app_store.upsert_diary(sid, "角色B", "2026-07-19", "B 日记")
        svc.memory.add_memory(sid, "event", "A 记忆", character="角色A")
        svc.memory.add_memory(sid, "event", "B 记忆", character="角色B")

        snapshot = svc._life_plan_character_snapshot(sid, "角色A")
        materials = svc._life_plan_materials(sid, "角色A", character_snapshot=snapshot)
        serialized = json.dumps(materials, ensure_ascii=False)

        self.assertEqual(snapshot["character_key"], "角色A")
        self.assertTrue(snapshot["character_version"])
        self.assertIn("A 人格", materials["persona"])
        self.assertEqual(materials["life_profile"]["day_anchor"], "school")
        self.assertEqual(materials["history_summary"], "A 历史总结")
        self.assertIn("A 用户聊天", serialized)
        self.assertIn("A 日记", serialized)
        self.assertIn("A 记忆", serialized)
        self.assertNotIn("B 用户聊天", serialized)
        self.assertNotIn("B 日记", serialized)
        self.assertNotIn("B 记忆", serialized)

    def test_life_plan_discards_result_when_character_version_changes(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:life-stale"
            state = self._set_active_character(svc, sid, "角色B", "B 人格")
            session_schema.get_saved_characters(state)["角色A"] = {
                "character": "角色A",
                "persona": "A 人格",
                "day_anchor": "home",
            }
            session_schema.get_character_contexts(state)["角色A"] = {}
            snapshot = svc._life_plan_character_snapshot(sid, "角色A")
            started = asyncio.Event()
            release = asyncio.Event()

            async def fake_generate(_sid, _key, _previous, **kwargs):
                self.assertIs(kwargs.get("character_snapshot"), snapshot)
                started.set()
                await release.wait()
                return {
                    "long_goals": [],
                    "mid_goals": [],
                    "today": {"date": "2026-07-20", "events": [], "texture": "旧角色结果"},
                }, {"status": "ok"}

            svc._generate_life_plan_update = fake_generate
            svc._render_life_plan_texture = AsyncMock(side_effect=lambda _sid, _key, plan, **_kwargs: plan)
            task = asyncio.create_task(svc._update_life_plan_after_dream(
                sid,
                "角色A",
                datetime(2026, 7, 20, tzinfo=timezone.utc),
                force=True,
                character_snapshot=snapshot,
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            session_schema.get_saved_characters(state)["角色A"]["persona"] = "A 新人格"
            release.set()

            result = await task
            self.assertEqual(result["status"], "stale")
            self.assertIsNone(svc.app_store.get_life_plan(sid, "角色A"))

        asyncio.run(run())

    def test_dream_passes_startup_character_snapshot_to_life_plan(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:dream-life-snapshot"
            key = "角色A"
            marker = {"character_key": key, "character_version": "v1", "character_exists": True, "materials": {}}
            svc._life_plan_character_snapshot = Mock(return_value=marker)
            svc.write_character_checkpoint = lambda *_args, **_kwargs: Path("fake-checkpoint.json")
            svc._write_dream_diary = AsyncMock(return_value="日记")
            svc._organize_memories_after_dream = AsyncMock(return_value={"status": "ok"})
            svc._update_life_plan_after_dream = AsyncMock(return_value={"status": "ok"})
            svc._generate_character_history_summary = AsyncMock()
            svc._run_context_checkpoint = AsyncMock()

            await svc._dream_once(sid, key, datetime(2026, 7, 20, tzinfo=timezone.utc), reason="manual")

            self.assertIs(
                svc._update_life_plan_after_dream.await_args.kwargs.get("character_snapshot"),
                marker,
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
