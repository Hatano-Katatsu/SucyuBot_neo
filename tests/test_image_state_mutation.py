from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.image_planning import plan_roleplay_image
from tests.support import ServiceFixtureMixin


class ImageStateMutationTestCase(ServiceFixtureMixin, unittest.TestCase):
    @staticmethod
    def _mutation_plan() -> dict:
        return {
            "scene": "cafe scene",
            "view": "third",
            "aspect_ratio": "2:3",
            "caption": "",
            "new_appearance_tags": "",
            "clothing_off": "completely nude, black choker",
            "is_intimate": False,
            "partner_in_frame": False,
            "device_in_frame": False,
            "state_mutation": {
                "nudity": "completely nude",
                "user_location": {
                    "value": "with_user",
                    "co_located": True,
                    "planned_at": 123.0,
                },
                "character_location": {
                    "value": "cafe",
                    "confidence": 0.6,
                    "source": "image",
                },
                "persistent_accessory_removal": {
                    "clothing_off": "completely nude, black choker",
                    "sources": ["remove the choker", "cafe scene"],
                },
            },
        }

    @staticmethod
    def _state_fingerprint(state: dict) -> dict:
        return {
            "nudity": session_schema.get_nudity(state),
            "nudity_at": session_schema.get_nudity_at(state),
            "user_place": session_schema.get_user_place(state),
            "user_co_located": session_schema.get_user_co_located(state),
            "user_place_updated_at": session_schema.get_user_place_updated_at(state),
            "character_place": session_schema.get_character_place(state),
            "character_place_history": copy.deepcopy(session_schema.get_character_place_history(state)),
            "wardrobe": copy.deepcopy(session_schema.get_wardrobe(state)),
            "photos": copy.deepcopy(session_schema.get_sent_photos_history(state)),
        }

    def _prepared_service(self):
        service = self.make_service()
        session_id = "telegram:123"
        state = service._get_session_state(session_id)
        session_schema.set_user_place(
            state,
            key="mall",
            label="商场",
            updated_at=10.0,
            co_located=False,
            source="tool",
        )
        session_schema.set_character_place(
            state,
            key="home",
            label="家",
            updated_at=10.0,
            confidence=0.95,
            rounds=0,
        )
        session_schema.set_wardrobe(state, {"dress": "red dress", "accessory": "black choker"})
        session_schema.set_outfit(state, "red dress, black choker")
        service._save_session_state(session_id, state)
        service.send_action = AsyncMock()
        service._translate_to_tags = AsyncMock(return_value="english tags")
        service._do_generate = AsyncMock(return_value=(True, [b"image"], ""))
        service.send_photo = AsyncMock()
        return service, session_id, state

    def _prepared_scheduler_service(self):
        service, session_id, state = self._prepared_service()
        session_schema.set_nudity(state, "completely nude", at=10.0)
        session_schema.set_wardrobe_item_state(state, "dress", "half_off")
        plan = self._mutation_plan()
        plan["clothing_off"] = ""
        plan["state_mutation"] = {
            "clear_undress_state": True,
            "user_location": {"value": "with_user", "co_located": True, "planned_at": 123.0},
            "character_location": {"value": "cafe", "confidence": 0.6, "source": "image"},
        }
        service._run_dream = AsyncMock()
        service.ensure_life_plan_for_today = AsyncMock()
        service._ensure_life_profile = AsyncMock()
        service._checkpoint_context_before_push = AsyncMock()
        service.build_world_state = lambda *args, **kwargs: {}
        service._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22", "code": "113"})
        service._llm_write_scene = AsyncMock(return_value=plan)
        return service, session_id, state

    def test_planner_returns_proposed_mutation_without_writing_shared_state(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:planner"
            state = service._get_session_state(session_id)
            session_schema.set_user_place(
                state,
                key="mall",
                label="商场",
                updated_at=10.0,
                co_located=False,
            )
            session_schema.set_wardrobe(state, {"accessory": "black choker"})
            session_schema.set_outfit(state, "black choker")
            before = self._state_fingerprint(state)
            service.config.update({
                "image_llm_api_key": "key",
                "image_llm_model": "model",
                "image_llm_api_base": "https://image.example",
            })
            service._fetch_weather = AsyncMock(return_value={"desc": "晴", "temp": "22"})
            service._call_llm = AsyncMock(return_value=json.dumps({
                "scene": "cafe scene",
                "view": "third",
                "clothing_off": "completely nude, black choker",
                "character_location": "cafe",
                "user_location": "with_user",
            }))

            plan = await plan_roleplay_image(service, session_id, intent="remove the choker")

            self.assertEqual(self._state_fingerprint(state), before)
            self.assertEqual(plan["state_mutation"]["nudity"], "completely nude")
            self.assertEqual(plan["state_mutation"]["user_location"]["value"], "with_user")
            self.assertEqual(plan["state_mutation"]["character_location"]["value"], "cafe")
            self.assertEqual(
                plan["state_mutation"]["persistent_accessory_removal"]["clothing_off"],
                "completely nude, black choker",
            )

        asyncio.run(run())

    def test_hard_transition_proposes_undress_cleanup_without_applying_it(self):
        async def run():
            service = self.make_service()
            session_id = "telegram:transition"
            state = service._get_session_state(session_id)
            session_schema.set_nudity(state, "completely nude", at=10.0)
            session_schema.set_wardrobe(state, {"top": "white camisole"})
            session_schema.set_outfit(state, "white camisole")
            session_schema.set_wardrobe_item_state(state, "top", "half_off")
            service.config.update({
                "image_llm_api_key": "key",
                "image_llm_model": "model",
                "image_llm_api_base": "https://image.example",
            })
            service._push_scene_transition_decision = lambda *args, **kwargs: {
                "should_transition": True,
                "drop_continuity": True,
            }
            service._format_push_scene_transition_context = lambda *args, **kwargs: "切换到新场景"
            self.mock_image_planner_messages(service, {
                "scene": "afternoon cafe scene",
                "view": "selfie",
                "character_location": "cafe",
                "user_location": "unknown",
            })

            plan = await plan_roleplay_image(
                service,
                session_id,
                mode="normal",
                weather_data={"desc": "晴", "temp": "22"},
            )

            self.assertEqual(session_schema.get_nudity(state), "completely nude")
            self.assertEqual(session_schema.get_wardrobe_item_states(state), {"top": "half_off"})
            self.assertEqual(plan["clothing_off"], "")
            self.assertTrue(plan["state_mutation"]["clear_undress_state"])

        asyncio.run(run())

    def test_one_shot_clear_override_renders_current_image_dressed_without_state_write(self):
        service = self.make_service()
        session_id = "telegram:prompt-override"
        state = service._get_session_state(session_id)
        session_schema.set_wardrobe(state, {"top": "white camisole"})
        session_schema.set_outfit(state, "white camisole")
        session_schema.set_nudity(state, "completely nude", at=10.0)
        session_schema.set_wardrobe_item_state(state, "top", "half_off")

        normal_positive, _ = service._build_prompt("standing in a cafe", session_id=session_id)
        override_positive, _ = service._build_prompt(
            "standing in a cafe",
            session_id=session_id,
            ignore_wardrobe_item_states=True,
        )

        self.assertIn("half-removed white camisole", normal_positive)
        self.assertNotIn("half-removed white camisole", override_positive)
        self.assertIn("white camisole", override_positive)
        self.assertEqual(session_schema.get_nudity(state), "completely nude")
        self.assertEqual(session_schema.get_wardrobe_item_states(state), {"top": "half_off"})

    def test_hard_transition_generation_failure_uses_override_without_committing_cleanup(self):
        async def run():
            service, session_id, state = self._prepared_service()
            session_schema.set_nudity(state, "completely nude", at=10.0)
            session_schema.set_wardrobe_item_state(state, "dress", "half_off")
            before = self._state_fingerprint(state)
            plan = self._mutation_plan()
            plan["clothing_off"] = ""
            plan["state_mutation"] = {"clear_undress_state": True}
            service._do_generate = AsyncMock(return_value=(False, [], "generation failed"))

            with patch(
                "telegram_comfyui_selfie.service.plan_roleplay_image",
                new=AsyncMock(return_value=plan),
            ):
                result = await service.tool_generate_image(123, session_id, intent="image")

            self.assertIn("generation failed", result)
            self.assertTrue(service._do_generate.await_args.kwargs["ignore_wardrobe_item_states"])
            self.assertEqual(self._state_fingerprint(state), before)
            self.assertEqual(session_schema.get_wardrobe_item_states(state), {"dress": "half_off"})

        asyncio.run(run())

    def test_hard_transition_success_commits_cleanup_after_history(self):
        async def run():
            service, session_id, state = self._prepared_service()
            session_schema.set_nudity(state, "completely nude", at=10.0)
            session_schema.set_wardrobe_item_state(state, "dress", "half_off")
            plan = self._mutation_plan()
            plan["clothing_off"] = ""
            plan["state_mutation"] = {"clear_undress_state": True}

            with patch(
                "telegram_comfyui_selfie.service.plan_roleplay_image",
                new=AsyncMock(return_value=plan),
            ):
                result = await service.tool_generate_image(123, session_id, intent="image")

            self.assertIn("图片已生成并发送", result)
            self.assertTrue(service._do_generate.await_args.kwargs["ignore_wardrobe_item_states"])
            self.assertEqual(session_schema.get_nudity(state), "")
            self.assertEqual(session_schema.get_wardrobe_item_states(state), {})
            self.assertEqual(len(session_schema.get_sent_photos_history(state)), 1)

        asyncio.run(run())

    def test_scheduled_push_success_commits_proposed_state_after_history(self):
        async def run():
            service, session_id, state = self._prepared_scheduler_service()
            fixed_now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)

            ok = await service._sched_fire(
                session_id,
                fixed_now,
                mode_override="normal",
                skip_active_check=True,
            )

            self.assertTrue(ok)
            self.assertTrue(service._do_generate.await_args.kwargs["ignore_wardrobe_item_states"])
            self.assertEqual(session_schema.get_nudity(state), "")
            self.assertEqual(session_schema.get_wardrobe_item_states(state), {})
            self.assertTrue(session_schema.get_user_co_located(state))
            self.assertEqual(session_schema.get_character_place(state), "cafe")
            self.assertEqual(len(session_schema.get_sent_photos_history(state)), 1)

        asyncio.run(run())

    def test_scheduled_push_send_failure_keeps_proposed_state_uncommitted(self):
        async def run():
            service, session_id, state = self._prepared_scheduler_service()
            fixed_now = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
            before = self._state_fingerprint(state)
            before_item_states = copy.deepcopy(session_schema.get_wardrobe_item_states(state))
            service.send_photo = AsyncMock(side_effect=RuntimeError("telegram down"))

            ok = await service._sched_fire(
                session_id,
                fixed_now,
                mode_override="normal",
                skip_active_check=True,
            )

            self.assertFalse(ok)
            self.assertTrue(service._do_generate.await_args.kwargs["ignore_wardrobe_item_states"])
            self.assertEqual(self._state_fingerprint(state), before)
            self.assertEqual(session_schema.get_wardrobe_item_states(state), before_item_states)

        asyncio.run(run())

    def test_translate_failure_does_not_commit_image_state(self):
        async def run():
            service, session_id, state = self._prepared_service()
            before = self._state_fingerprint(state)
            service._translate_to_tags = AsyncMock(side_effect=RuntimeError("translate failed"))
            with patch(
                "telegram_comfyui_selfie.service.plan_roleplay_image",
                new=AsyncMock(return_value=self._mutation_plan()),
            ):
                with self.assertRaisesRegex(RuntimeError, "translate failed"):
                    await service.tool_generate_image(123, session_id, intent="image")
            self.assertEqual(self._state_fingerprint(state), before)

        asyncio.run(run())

    def test_generation_failure_does_not_commit_image_state(self):
        async def run():
            service, session_id, state = self._prepared_service()
            before = self._state_fingerprint(state)
            service._do_generate = AsyncMock(return_value=(False, [], "generation failed"))
            with patch(
                "telegram_comfyui_selfie.service.plan_roleplay_image",
                new=AsyncMock(return_value=self._mutation_plan()),
            ):
                result = await service.tool_generate_image(123, session_id, intent="image")
            self.assertIn("generation failed", result)
            self.assertEqual(self._state_fingerprint(state), before)

        asyncio.run(run())

    def test_send_failure_or_cancellation_does_not_commit_image_state(self):
        async def run():
            for error in (RuntimeError("send failed"), asyncio.CancelledError()):
                service, session_id, state = self._prepared_service()
                before = self._state_fingerprint(state)
                service.send_photo = AsyncMock(side_effect=error)
                with patch(
                    "telegram_comfyui_selfie.service.plan_roleplay_image",
                    new=AsyncMock(return_value=self._mutation_plan()),
                ):
                    with self.assertRaises(type(error)):
                        await service.tool_generate_image(123, session_id, intent="image")
                self.assertEqual(self._state_fingerprint(state), before)

        asyncio.run(run())

    def test_photo_history_failure_does_not_commit_image_state(self):
        async def run():
            service, session_id, state = self._prepared_service()
            before = self._state_fingerprint(state)
            service._record_sent_photo = Mock(side_effect=RuntimeError("history failed"))
            with patch(
                "telegram_comfyui_selfie.service.plan_roleplay_image",
                new=AsyncMock(return_value=self._mutation_plan()),
            ):
                with self.assertRaisesRegex(RuntimeError, "history failed"):
                    await service.tool_generate_image(123, session_id, intent="image")
            self.assertEqual(self._state_fingerprint(state), before)

        asyncio.run(run())

    def test_success_commits_all_proposed_state_after_photo_history(self):
        async def run():
            service, session_id, state = self._prepared_service()
            before = self._state_fingerprint(state)
            original_record = service._record_sent_photo
            observed = {}

            def record_then_return(*args, **kwargs):
                observed["before_record"] = self._state_fingerprint(state)
                result = original_record(*args, **kwargs)
                observed["after_record"] = self._state_fingerprint(state)
                return result

            service._record_sent_photo = Mock(side_effect=record_then_return)
            with patch(
                "telegram_comfyui_selfie.service.plan_roleplay_image",
                new=AsyncMock(return_value=self._mutation_plan()),
            ):
                result = await service.tool_generate_image(123, session_id, intent="image")

            self.assertIn("图片已生成并发送", result)
            self.assertEqual(observed["before_record"], before)
            self.assertEqual(observed["after_record"]["nudity"], before["nudity"])
            self.assertEqual(observed["after_record"]["character_place"], before["character_place"])
            self.assertEqual(session_schema.get_nudity(state), "completely nude")
            self.assertEqual(session_schema.get_user_place(state), "")
            self.assertTrue(session_schema.get_user_co_located(state))
            self.assertEqual(session_schema.get_user_place_updated_at(state), 123.0)
            self.assertEqual(session_schema.get_character_place(state), "cafe")
            self.assertEqual(session_schema.get_wardrobe(state), {"dress": "red dress"})
            self.assertEqual(len(session_schema.get_sent_photos_history(state)), 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
