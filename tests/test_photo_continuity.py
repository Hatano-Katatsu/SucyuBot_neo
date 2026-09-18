from __future__ import annotations

import asyncio
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock

from telegram_comfyui_selfie import session_schema, generation
from telegram_comfyui_selfie.image_planning import plan_roleplay_image
from telegram_comfyui_selfie.photo_sharing import (
    PhotoContentSkipped, PHOTO_RULES, photo_history_context, photo_repeat_reason,
    record_photo_feedback, photo_scene_summary,
)
from tests.support import ServiceFixtureMixin


class PhotoContinuityTests(ServiceFixtureMixin, unittest.TestCase):
    def setup_push(self, plan):
        svc = self.make_service()
        sid = "telegram:1"
        svc.config.update(image_llm_api_key="test-key", image_llm_model="test", image_llm_api_base="https://invalid.example")
        svc._get_purity = lambda *_: 6
        svc._fetch_weather = AsyncMock(return_value=None)
        svc._ensure_life_profile = AsyncMock(return_value={"age_stage": "adult", "day_anchor": "unknown"})
        svc.ensure_life_plan_for_today = AsyncMock()
        svc._run_dream = AsyncMock()
        svc._decide_push_topic_direction = AsyncMock(return_value={"topic_direction": "independent", "topic_guides": []})
        svc._llm_write_scene = AsyncMock(return_value=plan)
        svc._translate_to_tags = AsyncMock(side_effect=lambda text, **_: text)
        svc._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
        svc.send_photo = AsyncMock(return_value={"message_id": 123})
        return svc, sid

    def base_plan(self, date="2026-09-17"):
        return {"long_goals": [{"id": "l1", "text": "完成服装设计作品", "status": "active"}],
                "mid_goals": [{"id": "m1", "parent_id": "l1", "text": "画好肩线", "status": "active"}],
                "today": {"date": date, "events": [{"id": "e1", "text": "在工作台修改肩线草图", "related_mid_id": "m1", "status": "planned"}]}}

    def test_terminal_goals_do_not_block_incremental_or_manual_additions(self):
        svc = self.make_service()
        sid = "telegram:1"
        previous = self.base_plan()
        previous["mid_goals"] = [{"id": f"m{i}", "parent_id": "l1", "text": f"已放弃的旧方向{i}", "status": "abandoned"} for i in range(1, 5)]
        plan, result = svc._life_plan_from_update(previous, {"ops": [{"op": "add_mid", "id": "m5", "text": "画一张新草图", "parent_id": "l1"}]}, today_date="2026-09-18", session_id=sid)
        self.assertEqual(result["applied"], 1)
        svc._save_life_plan_payload(sid, "", plan)
        saved = svc.upsert_life_plan_goal(sid, "mid", {"text": "修改另一处剪裁"})["payload"]
        self.assertEqual(svc._life_active_count(saved["mid_goals"]), 2)
        self.assertEqual(sum(g["status"] == "abandoned" for g in saved["mid_goals"]), 4)
        svc.upsert_life_plan_goal(sid, "mid", {"id": "m5", "text": "新草图完成第一笔"})
        self.assertEqual(svc._load_life_plan_row(sid)["payload"]["mid_goals"][4]["text"], "新草图完成第一笔")

    def test_normalization_bounds_archive_preserves_references_and_active_slots(self):
        svc = self.make_service()
        raw = self.base_plan()
        raw["mid_goals"] = [{"id": f"m{i}", "text": f"旧目标{i}", "parent_id": "l1", "status": "achieved"} for i in range(1, 60)]
        raw["mid_goals"] += [{"id": f"m{i}", "text": f"新目标{i}", "parent_id": "l1", "status": "active"} for i in range(60, 66)]
        normalized = svc._normalize_life_plan_payload(raw)
        self.assertEqual(svc._life_active_count(normalized["mid_goals"]), 4)
        self.assertEqual(len(normalized["mid_goals"]), 37)  # 32 条归档 + 仍被事件引用的 m1 + 4 活动目标。
        self.assertEqual(normalized["today"]["events"][0]["related_mid_id"], "m1")
        self.assertEqual(svc._normalize_life_plan_payload(normalized), normalized)

    def test_automatic_full_update_cannot_resurrect_archived_goal_id(self):
        svc = self.make_service()
        old = self.base_plan()
        old["mid_goals"][0]["status"] = "abandoned"
        new = {"mid_goals": [{"id": "m1", "text": "把放弃的事再做一次", "status": "active", "parent_id": "l1"},
                             {"id": "m2", "text": "画一个不同款式", "status": "active", "parent_id": "l1"}]}
        plan, _ = svc._life_plan_from_update(old, new, today_date="2026-09-18")
        self.assertEqual(plan["mid_goals"][0]["status"], "abandoned")
        self.assertEqual([g["id"] for g in plan["mid_goals"] if g["status"] == "active"], ["m2"])

    def test_bootstrap_retries_once_and_rejects_empty_success(self):
        async def run():
            svc = self.make_service()
            svc.has_llm_config = lambda *_: True
            good = self.base_plan()
            svc._call_life_plan_json = AsyncMock(side_effect=[{"ops": []}, good])
            plan, _ = await svc._generate_life_plan_update("telegram:1", "", {}, today_date="2026-09-18")
            self.assertFalse(svc._life_plan_needs_bootstrap(plan))
            self.assertEqual(svc._call_life_plan_json.await_count, 2)
            svc._call_life_plan_json = AsyncMock(return_value={"ops": []})
            with self.assertRaisesRegex(ValueError, "active goals"):
                await svc._generate_life_plan_update("telegram:1", "", {}, today_date="2026-09-18")
            self.assertEqual(svc._call_life_plan_json.await_count, 2)
        asyncio.run(run())

    def test_no_model_bootstrap_preserves_terminal_archive(self):
        async def run():
            svc = self.make_service()
            old = self.base_plan()
            old["long_goals"][0]["status"] = "abandoned"
            old["mid_goals"][0]["status"] = "abandoned"
            plan, _ = await svc._generate_life_plan_update("telegram:1", "", old, today_date="2026-09-18")
            self.assertEqual(plan["long_goals"][0]["status"], "abandoned")
            self.assertEqual(plan["mid_goals"][0]["status"], "abandoned")
            self.assertFalse(svc._life_plan_needs_bootstrap(plan))
        asyncio.run(run())

    def test_source_identity_survives_day_change_and_only_relevant_evidence_changes_version(self):
        svc = self.make_service()
        sid = "telegram:1"
        today = svc._life_today_date(sid)
        old = svc._normalize_life_plan_payload(self.base_plan())
        event = copy.deepcopy(old["today"]["events"][0])
        event.update(id="e4", text="在工作台重新看肩线草图", time_hint="evening")
        new, _ = svc._life_plan_from_update(old, {"today_events": [event]}, today_date=today, session_id=sid)
        self.assertEqual(new["today"]["events"][0]["continuity_id"], old["today"]["events"][0]["continuity_id"])
        svc._save_life_plan_payload(sid, "", new)
        ref = svc._life_photo_source_ref(sid, today, event["continuity_id"])
        image = {"scene": "sketch", "caption": "肩线还留着空白", "photo_brief": {"source_ref": ref, "topic_key": "pattern_shoulder_seam_unfilled", "main_subject": "肩线草图"}}
        first = svc._validate_life_photo_source(sid, image)
        svc._append_push_topic(sid, image["caption"], image["scene"], "life", photo_brief=first["photo_brief"], message_id=11)
        state = svc._get_session_state(sid)
        session_schema.set_last_message_time(state, time.time())
        session_schema.set_last_message_text(state, "晚安")
        unchanged = svc._validate_life_photo_source(sid, image)
        self.assertEqual(first["photo_brief"]["source_version"], unchanged["photo_brief"]["source_version"])
        record_photo_feedback(state, "那个颜色换一下", "tg:22", reply_to_message_id=11)
        changed = svc._validate_life_photo_source(sid, image)
        self.assertNotEqual(first["photo_brief"]["source_version"], changed["photo_brief"]["source_version"])
        self.assertEqual(svc._load_life_plan_row(sid)["payload"]["today"]["events"][0]["status"], "planned")

    def test_same_topic_synonym_does_not_accept_model_claimed_progress(self):
        state = {}
        session_schema.set_recent_push_topics(state, [{"ts": time.time(), "caption": "昨天的线稿", "scene": "a drawing", "photo_brief": {"topic_key": "pattern_shoulder_seam_unfilled"}}])
        self.assertIn("依据", photo_repeat_reason({"scene": "a sketch on the table", "caption": "肩线还是留白", "photo_brief": {"topic_key": "pattern_shoulder_seam_left_blank", "actual_change": "宣称有全新的进展"}}, state))

    def test_every_automatic_mode_checks_repeat_and_persists_skip_metrics(self):
        async def run():
            for mode, temporary in (("normal", ""), ("morning", ""), ("followup", ""), ("ntr", ""), ("normal", "自然晚安")):
                with self.subTest(mode=mode, temporary=temporary):
                    plan = {"scene": "a cup", "caption": "咖啡做好了", "view": "selfie"}
                    svc, sid = self.setup_push(plan)
                    svc._append_push_topic(sid, plan["caption"], plan["scene"], "life")
                    with self.assertRaises(PhotoContentSkipped):
                        await svc._sched_fire(sid, svc._session_now(sid), mode_override=mode, temporary_system_prompt=temporary, skip_active_check=True)
                    self.assertEqual(svc._llm_write_scene.await_count, 2)
                    svc.send_photo.assert_not_awaited()
                    outcomes = [r["outcome"] for r in session_schema.get_push_diagnostics(svc._get_session_state(sid))]
                    self.assertEqual(outcomes, ["attempt", "replan", "skipped"])
        asyncio.run(run())

    def test_failed_send_does_not_commit_photo_or_exposure(self):
        async def run():
            svc, sid = self.setup_push({"scene": "a sketch", "caption": "今天画了这一角", "view": "scene"})
            svc.send_photo = AsyncMock(side_effect=RuntimeError("delivery failed"))
            self.assertFalse(await svc._sched_fire(sid, svc._session_now(sid), skip_active_check=True))
            state = svc._get_session_state(sid)
            self.assertEqual(session_schema.get_recent_push_topics(state), [])
            self.assertEqual(session_schema.get_sent_photos_history(state), [])
            self.assertEqual(session_schema.get_push_diagnostics(state)[-1]["outcome"], "failed")
        asyncio.run(run())

    def test_photo_summary_survives_planner_send_and_history(self):
        async def run():
            payload = {"scene": "a sketchbook on a wooden table", "caption": "这条线还是没想好", "view": "scene", "subject_mode": "detail"}
            svc, sid = self.setup_push(payload)
            svc._call_llm = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))
            plan = await plan_roleplay_image(svc, sid, intent="分享生活照片")
            self.assertNotIn("scene_summary_zh", plan["photo_brief"])
            self.assertNotIn("scene_summary_zh", svc._call_llm.await_args.args[0])
            svc._llm_write_scene = AsyncMock(return_value={**plan, "subject_mode": "detail", "view": "scene"})
            svc._last_generated_nltag_by_session = {sid: "sketchbook, unfinished design, A sketchbook lies open on the workbench with an unfinished shoulder seam."}
            self.assertTrue(await svc._sched_fire(sid, svc._session_now(sid), skip_active_check=True))
            history = session_schema.get_chat_history(svc._get_session_state(sid))
            photo_text = next(m["content"] for m in history if m["role"] == "system" and m["content"].startswith("照片记录"))
            self.assertIn("unfinished shoulder seam", photo_text)
            self.assertIn("unfinished design", photo_text)
            self.assertIn("角色眼前的景物", photo_text)
            self.assertNotIn("一张贴合", photo_text)
            self.assertEqual(session_schema.get_push_diagnostics(svc._get_session_state(sid))[-1]["message_id"], 123)
            topics = session_schema.get_recent_push_topics(svc._get_session_state(sid))
            self.assertEqual(len(topics), 1)
            self.assertIn("unfinished shoulder seam", topics[0]["visual_summary"])
        asyncio.run(run())

    def test_feedback_links_explicit_old_photo_and_ignores_unrelated_short_reply(self):
        state = {}
        session_schema.set_recent_push_topics(state, [{"ts": time.time(), "message_id": n, "photo_brief": {"main_subject": s}} for n, s in ((11, "草图"), (12, "早餐"))])
        self.assertIsNone(record_photo_feedback(state, "晚安", "tg:30"))
        self.assertIsNone(record_photo_feedback(state, "今天工作很忙", "tg:31"))
        record_photo_feedback(state, "这个颜色换一下", "tg:32", reply_to_message_id=11)
        self.assertIsNone(record_photo_feedback(state, "这个颜色换一下", "tg:32", reply_to_message_id=11))
        photos = session_schema.get_recent_push_topics(state)
        self.assertEqual(photos[0]["user_feedback"][0]["reply_to_message_id"], 11)
        self.assertNotIn("user_feedback", photos[1])
        self.assertIsNone(record_photo_feedback(state, "这个颜色换一下", "tg:33", reply_to_message_id=999))

    def test_related_pause_applies_to_source_and_synonymous_topic(self):
        state = {}
        session_schema.set_recent_push_topics(state, [{"ts": time.time(), "caption": "肩线还没画好", "message_id": 11, "photo_brief": {"source_ref": "life::le_example", "topic_key": "pattern_shoulder_seam_unfilled"}}])
        record_photo_feedback(state, "先别画了", "tg:12", reply_to_message_id=11)
        reason = photo_repeat_reason({"scene": "a new scene", "caption": "再看一眼", "photo_brief": {"topic_key": "pattern_shoulder_seam_left_blank", "source_ref": "life::le_example", "source_version": "new"}}, state)
        self.assertIn("暂缓", reason)
        self.assertGreater(state["photo_topic_controls"][0]["until"], time.time())

    def test_committed_chat_feedback_uses_persisted_message_id(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        svc._append_push_topic(sid, "草图画好了", "a sketch", "life", photo_brief={"main_subject": "草图"}, message_id=11)
        svc._queue_checkpoint_if_needed = lambda *_: None
        svc._append_chat_history_messages(sid, [{"role": "user", "content": "草图的颜色可以换一下"}])
        feedback = session_schema.get_recent_push_topics(state)[-1]["user_feedback"][0]
        self.assertTrue(feedback["user_message_id"].startswith("db:"))
        snapshot = svc._life_plan_character_snapshot(sid, "")["materials"]
        self.assertIn("草图的颜色可以换一下", snapshot["shared_photos"])

    def test_compact_history_keeps_fact_caption_and_feedback_without_full_brief(self):
        state = {}
        records = [{"ts": time.time(), "caption": "薄荷糖已经补满", "message_id": i, "visual_summary": "A full jar of mint candies sits on the table.", "photo_brief": {
            "topic_key": f"supply{i}", "source_ref": "life::le_123", "subject_mode": "detail", "framing": "close",
            "composition": "a detailed long description " * 20, "capture_source": "character_camera", "actual_change": "new"}} for i in range(8)]
        session_schema.set_recent_push_topics(state, records)
        context = photo_history_context(state)
        self.assertIn("薄荷糖已经补满", context)
        self.assertIn("life::le_123", context)
        self.assertLess(len(context), len(json.dumps([r["photo_brief"] for r in records], ensure_ascii=False)) * .5)

    def test_nltag_summary_uses_actual_scene_and_legacy_fallback(self):
        summary = photo_scene_summary({"nltag": "red cup, rainy window, A red cup stands by a rainy window. Soft light falls across the table.", "scene": "旧规划中的蓝杯子"})
        self.assertIn("red cup", summary)
        self.assertNotIn("蓝杯子", summary)
        full = "A drawing sits on the table beside a window. " * 50
        self.assertEqual(photo_scene_summary({"nltag": full}), full.strip())
        self.assertLessEqual(len(photo_scene_summary({"nltag": full}, limit=240)), 241)
        self.assertEqual(photo_scene_summary({"scene": "窗边的杯子"}), "窗边的杯子")
        state = {}
        session_schema.set_sent_photos_history(state, [{"timestamp": time.time(), "source_kind": "scheduled_push", "nltag": "A red cup stands by a rainy window.", "caption": "雨还没停"}])
        self.assertIn("red cup", photo_history_context(state))

    def test_animaflow_scene_combines_tags_and_nltag_and_accepts_tag_only(self):
        full = generation._payload_nltag({"tags": "red cup, rainy window", "nltag": "A cup stands on the table.", "quality": "masterpiece", "appearance": "black hair"})
        self.assertEqual(full, "red cup, rainy window, A cup stands on the table.")
        self.assertEqual(generation._payload_nltag({"tag": "A cup stands on the table."}), "A cup stands on the table.")
        self.assertEqual(generation._payload_nltag({"tags": "same scene", "nltag": "same scene"}), "same scene")
        self.assertEqual(generation._preferred_animatool_nltag_field({"tag": {"type": "string"}}), "tag")

    def test_nltag_cannot_fall_back_to_another_sessions_last_image(self):
        svc = self.make_service()
        svc._last_generated_nltag = "another person's image"
        svc._last_generated_nltag_by_session = {"telegram:2": "another person's image"}
        self.assertEqual(svc._last_generated_photo_nltag("telegram:1"), "")
        generation._remember_generated_nltag(svc, "telegram:2", "")
        self.assertEqual(svc._last_generated_photo_nltag("telegram:2"), "")

    def test_push_protocol_is_shared_once_before_session_facts(self):
        async def run():
            svc, _ = self.setup_push({})
            svc._call_llm_messages = AsyncMock(return_value={"choices": [{"message": {"content": json.dumps({"scene": "a cup on the table", "caption": "咖啡好了", "view": "scene", "subject_mode": "detail"})}}]})
            requests = []
            for sid, caption in (("telegram:1", "紫色肩线草图"), ("telegram:2", "红色杯子里的茶")):
                svc._append_push_topic(sid, caption, "previous drawing on the table", "life")
                await plan_roleplay_image(svc, sid, mode="normal", weather_data={"desc": "晴", "temp": "22"})
                messages = svc._call_llm_messages.await_args.args[0]
                joined = "\n".join(m["content"] for m in messages)
                self.assertEqual(joined.count(PHOTO_RULES), 1)
                self.assertIn(caption, joined)
                requests.append(messages[0]["content"])
            self.assertEqual(requests[0], requests[1])
        asyncio.run(run())

    def test_translation_explicitly_disables_optional_thinking(self):
        async def run():
            svc = self.make_service()
            svc.has_llm_config = lambda *_: True
            svc._call_llm = AsyncMock(return_value="a sketchbook on a table")
            result = await svc._translate_to_tags("工作台上的草图", session_id="telegram:1", subject_mode="detail")
            self.assertEqual(result, "a sketchbook on a table")
            self.assertIs(svc._call_llm.await_args.kwargs["disable_thinking"], True)
            self.assertEqual(svc._call_llm.await_args.kwargs["purpose"], "image")
        asyncio.run(run())

    def test_diagnostics_and_feedback_are_readable_from_sqlite_without_private_text(self):
        from scripts.push_quality_metrics import evaluate
        svc = self.make_service()
        sid = "telegram:1"
        start = time.time() - 5
        svc._append_push_topic(sid, "PRIVATE_PHOTO", "a drawing", "life", photo_brief={"main_subject": "草图", "subject_mode": "detail"}, message_id=11)
        state = svc._get_session_state(sid)
        record_photo_feedback(state, "PRIVATE_USER 建议换个颜色", "tg:12", reply_to_message_id=11)
        svc._record_push_diagnostic(sid, "a1", "normal", "sent", message_id=11)
        svc._flush_sessions(force=True)
        before = svc.app_store.path.stat().st_mtime_ns
        metrics = evaluate(svc.app_store.path, start)
        self.assertEqual(metrics["diagnostic_outcomes"], {"sent": 1})
        self.assertEqual(metrics["subject_modes"], {"detail": 1})
        self.assertEqual(metrics["explicit_photo_replies"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(metrics))
        self.assertNotIn(sid, json.dumps(metrics))
        self.assertEqual(before, svc.app_store.path.stat().st_mtime_ns)
