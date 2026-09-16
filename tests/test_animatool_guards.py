from __future__ import annotations

import asyncio
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_comfyui_selfie.generation import (
    PromptSlots,
    _apply_animatool_guard_contract,
    _build_animaflow_payload,
    _build_animatool_guard_contract,
    _build_animatool_turbo_payload,
    _do_generate_animaflow,
    _prepare_animaflow_generate_payload,
)
from telegram_comfyui_selfie.image_planning import plan_animaflow_slots, plan_animatool_slots


class AnimaFlowGuardContractTestCase(unittest.TestCase):
    # 与反词策略定稿一致：性/裸露类反词只保留最小集（nude），不再维护 nipples 等词。
    GUARDED_NEGATIVE = (
        "bad hands, holding phone, mirror, unrelated extra person, "
        "split screen, nude"
    )

    @staticmethod
    def _schema(*, supports_neg: bool) -> dict:
        properties = {
            "quality_meta_year_safe": {"type": "string"},
            "count": {"type": "string"},
            "tags": {"type": "string"},
        }
        if supports_neg:
            properties["neg"] = {"type": "string"}
        return {
            "parameters": {
                "properties": properties,
                "required": ["quality_meta_year_safe", "count", "tags"],
            }
        }

    @staticmethod
    def _service(workflow: str, *, cfg: str = "1.0") -> SimpleNamespace:
        return SimpleNamespace(
            config={
                "animaflow_workflow": workflow,
                "animaflow_filename_prefix": "guard-test",
                "animaflow_cfg": cfg,
                "animaflow_steps": "8",
                "width": "832",
                "height": "1216",
                "bot_name": "Guard Test",
            }
        )

    def _slots(self, negative: str | None = None) -> PromptSlots:
        return PromptSlots(
            scene="An adult woman reads beside a public library window.",
            safety="safe",
            count="1girl, solo",
            character="guard_test",
            effective_appearance="plain white t-shirt, dark blue jeans",
            negative=negative if negative is not None else self.GUARDED_NEGATIVE,
        )

    def test_animaflow_uses_planned_ratio_and_leaves_dimensions_to_server(self):
        schema = self._schema(supports_neg=False)
        schema["parameters"]["properties"].update({
            "aspect_ratio": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
        })
        service = self._service("anima29_turbo")
        service.config.update({"width": "2048", "height": "512"})

        payload = _build_animaflow_payload(
            service,
            self._slots(),
            "positive prompt",
            self.GUARDED_NEGATIVE,
            7,
            schema,
            orientation="2:3",
        )

        self.assertEqual(payload["aspect_ratio"], "2:3")
        self.assertNotIn("width", payload)
        self.assertNotIn("height", payload)

    def test_animaflow_generation_never_restores_llm_dimensions(self):
        async def run():
            service = self._service("anima29_turbo")
            service._last_prompt_slots = self._slots()
            schema = self._schema(supports_neg=False)
            schema["parameters"]["properties"].update({
                "aspect_ratio": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            })
            meta = {"endpoints": {"generate": "/anima/generate"}}
            planner = AsyncMock(return_value={
                "tags": "A quiet library portrait.",
                "width": 512,
                "height": 2048,
            })
            poster = AsyncMock(return_value=(True, [b"image"], ""))

            with (
                patch("telegram_comfyui_selfie.image_planning.plan_animaflow_slots", new=planner),
                patch(
                    "telegram_comfyui_selfie.generation.load_animaflow_workflow_resources",
                    new=AsyncMock(return_value=("anima29_turbo", meta, {}, schema, {})),
                ),
                patch("telegram_comfyui_selfie.generation._post_animaflow", new=poster),
            ):
                result = await _do_generate_animaflow(
                    service,
                    "unused scene",
                    "telegram:123",
                    7,
                    orientation="3:2",
                )

            self.assertTrue(result[0])
            payload = poster.await_args.args[4]
            self.assertEqual(payload["aspect_ratio"], "3:2")
            self.assertNotIn("width", payload)
            self.assertNotIn("height", payload)

        asyncio.run(run())

    def test_animaflow_schema_fallback_keeps_planned_ratio(self):
        async def run():
            service = self._service("anima29_turbo")
            service._last_prompt_slots = self._slots()
            schema = self._schema(supports_neg=False)
            meta = {"endpoints": {"generate": "/anima/generate"}}
            submitter = AsyncMock(return_value=(True, [b"image"], ""))

            with (
                patch(
                    "telegram_comfyui_selfie.image_planning.plan_animaflow_slots",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "telegram_comfyui_selfie.generation.load_animaflow_workflow_resources",
                    new=AsyncMock(return_value=("anima29_turbo", meta, {}, schema, {})),
                ),
                patch("telegram_comfyui_selfie.generation.submit_animaflow", new=submitter),
            ):
                result = await _do_generate_animaflow(
                    service,
                    "unused scene",
                    "telegram:123",
                    7,
                    orientation="3:2",
                )

            self.assertTrue(result[0])
            self.assertEqual(submitter.await_args.kwargs["orientation"], "3:2")

        asyncio.run(run())

    def test_cfg_one_ignores_schema_neg_and_preserves_guards_as_positive_text(self):
        payload = _build_animatool_turbo_payload(
            self._service("anima29_turbo", cfg="1"),
            self._slots(),
            "positive prompt",
            self.GUARDED_NEGATIVE,
            7,
            self._schema(supports_neg=True),
        )

        self.assertNotIn("neg", payload)
        tags = payload["tags"].lower()
        for phrase in (
            "capture equipment stays outside",
            "one coherent view",
            "only the intended visible subject",
            "one undivided single-frame",
            "fully and naturally covers intimate areas",
        ):
            self.assertIn(phrase, tags)

    def test_llm_negative_can_supplement_but_cannot_delete_guards(self):
        schema = self._schema(supports_neg=True)
        payload = _apply_animatool_guard_contract(
            {"tags": "A quiet library scene.", "neg": "llm supplemental artifact"},
            schema,
            self._slots(),
            "turbo_v1",
        )

        negative = payload["neg"].lower()
        self.assertIn("llm supplemental artifact", negative)
        self.assertIn("holding phone", negative)
        self.assertIn("unrelated extra person", negative)
        self.assertIn("split screen", negative)
        self.assertIn("nude", negative)

    def test_slots_planner_applies_guards_after_llm_output(self):
        async def run():
            schema = self._schema(supports_neg=True)
            service = SimpleNamespace(
                config={"animaflow_workflow": "anima29", "animaflow_cfg": "4.0"},
                comfyui_url="http://animatool.invalid",
                has_llm_config=lambda purpose, session_id: True,
                _get_session_state=lambda session_id: {},
                _get_effective_safety=lambda session_id: {"level": 8},
                _get_purity=lambda session_id: 8,
                _get_time_context=lambda session_id: {},
                _format_time_context=lambda session_id: "",
                _format_light_guard=lambda session_id: "",
                _get_llm_value=lambda *args: "0.1",
                _weather_caches={},
                _call_llm=AsyncMock(return_value=json.dumps({
                    "quality_meta_year_safe": "masterpiece, best quality, safe",
                    "count": "1girl",
                    "tags": "An adult woman reads beside a library window.",
                    "neg": "llm supplemental artifact",
                })),
            )

            with (
                patch(
                    "telegram_comfyui_selfie.image_planning._fetch_animatool_turbo_knowledge",
                    new=AsyncMock(return_value={}),
                ),
                patch(
                    "telegram_comfyui_selfie.image_planning._fetch_animatool_turbo_schema",
                    new=AsyncMock(return_value=schema),
                ),
            ):
                payload = await plan_animatool_slots(service, "telegram:guard", self._slots())

            self.assertIsNotNone(payload)
            negative = payload["neg"].lower()
            self.assertIn("llm supplemental artifact", negative)
            self.assertIn("holding phone", negative)
            self.assertIn("split screen", negative)
            system_prompt = service._call_llm.await_args.args[0] + service._call_llm.await_args.kwargs.get("system_tail", "")
            self.assertIn("系统终裁护栏（只可补充，不可删除）", system_prompt)

        asyncio.run(run())

    def test_realtime_schema_without_neg_falls_back_to_nltag_constraint(self):
        schema = self._schema(supports_neg=False)
        payload = _apply_animatool_guard_contract(
            {"tags": "An adult woman reads beside a library window."},
            schema,
            self._slots(),
            "turbo_v1",
        )

        self.assertNotIn("neg", payload)
        self.assertIn("one undivided single-frame", payload["tags"].lower())
        self.assertIn("fully and naturally covers intimate areas", payload["tags"].lower())

    def test_realtime_schema_neg_field_overrides_registry_metadata(self):
        schema = self._schema(supports_neg=True)
        payload = _apply_animatool_guard_contract(
            {"tags": "An adult woman reads beside a library window.", "neg": "schema term"},
            schema,
            self._slots(),
            "turbo0.2",
        )

        self.assertIn("schema term", payload["neg"])
        self.assertNotIn("Deterministic rendering constraints", payload["tags"])

    def test_mirror_workflow_allows_one_phone_and_one_reflection(self):
        slots = self._slots("two phones, multiple reflections, split screen")
        contract = _build_animatool_guard_contract(slots)
        self.assertEqual(contract.phone, ("two phones",))
        self.assertEqual(contract.mirror, ("multiple reflections",))

        payload = _apply_animatool_guard_contract(
            {"tags": "She takes a mirror selfie while holding one phone."},
            self._schema(supports_neg=False),
            slots,
            "turbo0.2",
        )
        tags = payload["tags"].lower()
        self.assertIn("one coherent set of intended handheld props", tags)
        self.assertIn("one coherent intended reflection", tags)
        self.assertNotIn("capture equipment stays outside", tags)
        self.assertNotIn("appears directly in one coherent view", tags)

    def test_absent_native_guard_is_not_invented(self):
        slots = self._slots("bad hands")
        original = {"tags": "An intimate private-room scene."}

        payload = _apply_animatool_guard_contract(
            original,
            self._schema(supports_neg=False),
            slots,
            "turbo0.2",
        )

        self.assertEqual(payload, original)

    def test_legacy_turbo_v1_endpoint_does_not_receive_unified_workflow_field(self):
        original = {"workflow": "turbo_v1", "tags": "A quiet scene."}
        legacy = _prepare_animaflow_generate_payload(
            original,
            "turbo_v1",
            {"legacy_fallback": True},
        )
        dynamic = _prepare_animaflow_generate_payload(
            {"tags": "A quiet scene."},
            "anima29_turbo",
            {},
        )

        self.assertNotIn("workflow", legacy)
        self.assertEqual(dynamic["workflow"], "anima29_turbo")
        self.assertIn("workflow", original, "构造提交体不能原地修改规划结果")

    def test_automatic_legacy_fallback_uses_cfg_one_policy_before_llm(self):
        async def run():
            schema = self._schema(supports_neg=True)
            service = SimpleNamespace(
                config={"animaflow_workflow": "anima29", "animaflow_cfg": "4.0"},
                _animaflow_catalog={
                    "workflows": {
                        "turbo_v1": {
                            "legacy_fallback": True,
                            "defaults": {"cfg": 1.0, "steps": 12},
                        }
                    }
                },
                has_llm_config=lambda purpose, session_id: True,
                _get_session_state=lambda session_id: {},
                _get_effective_safety=lambda session_id: {"level": 8},
                _get_purity=lambda session_id: 8,
                _get_time_context=lambda session_id: {},
                _format_time_context=lambda session_id: "",
                _format_light_guard=lambda session_id: "",
                _get_llm_value=lambda *args: "0.1",
                _weather_caches={},
                _call_llm=AsyncMock(return_value=json.dumps({
                    "quality_meta_year_safe": "masterpiece, best quality, safe",
                    "count": "1girl",
                    "tags": "A quiet scene.",
                    "neg": "should be removed",
                })),
            )

            payload = await plan_animaflow_slots(
                service,
                "telegram:guard",
                self._slots(),
                workflow="turbo_v1",
                schema=schema,
                knowledge={},
            )

            self.assertNotIn("neg", payload)
            self.assertIn("cfg=1 提示词规则", service._call_llm.await_args.kwargs["system_tail"])

        asyncio.run(run())


class AnimaFlowIdentityGuardTestCase(unittest.TestCase):
    """slots 规划 LLM 丢掉/编造发色时，身份终裁必须补回身份标签并改回源发色。"""

    @staticmethod
    def _schema() -> dict:
        return {
            "parameters": {
                "properties": {
                    "quality_meta_year_safe": {"type": "string"},
                    "count": {"type": "string"},
                    "appearance": {"type": "string"},
                    "tags": {"type": "string"},
                },
                "required": ["quality_meta_year_safe", "count", "appearance", "tags"],
            }
        }

    @staticmethod
    def _service(llm_json: dict) -> SimpleNamespace:
        return SimpleNamespace(
            config={"animaflow_workflow": "anima29", "animaflow_cfg": "1.0"},
            has_llm_config=lambda purpose, session_id="": True,
            _get_session_state=lambda session_id: {},
            _get_effective_safety=lambda session_id: {"level": 8},
            _get_purity=lambda session_id: 8,
            _get_time_context=lambda session_id: {},
            _format_time_context=lambda session_id: "",
            _format_light_guard=lambda session_id: "",
            _get_llm_value=lambda *args: "0.1",
            _weather_caches={},
            _call_llm=AsyncMock(return_value=json.dumps(llm_json)),
        )

    @staticmethod
    def _slots() -> PromptSlots:
        return PromptSlots(
            scene="A young woman sits by a rainy window, sketching.",
            safety="safe",
            count="1girl",
            effective_appearance=(
                "pale skin, black straight hair, low ponytail, loose side bangs, "
                "gentle half-lidded eyes, purple eyes, white hair ribbon, white blouse"
            ),
            negative="",
        )

    async def _plan(self, llm_json: dict) -> dict:
        payload = await plan_animaflow_slots(
            self._service(llm_json),
            "telegram:identity",
            self._slots(),
            workflow="anima29",
            schema=self._schema(),
            knowledge={},
        )
        self.assertIsInstance(payload, dict)
        return payload

    def test_hallucinated_hair_color_is_reverted_and_identity_tags_restored(self):
        async def run():
            return await self._plan({
                "quality_meta_year_safe": "masterpiece, best quality, safe",
                "count": "1girl",
                # LLM 丢掉 black straight hair/low ponytail，并从 white hair ribbon 臆造 white hair。
                "appearance": "pale skin, half-lidded eyes, white blouse, white hair, blue eyes",
                "tags": "Her damp loose white hair falls over the pillow as her blue eyes wander.",
            })

        payload = asyncio.run(run())
        appearance = payload["appearance"]
        tags = payload["tags"]
        self.assertIn("black straight hair", appearance)
        self.assertIn("low ponytail", appearance)
        self.assertIn("white hair ribbon", appearance)
        self.assertIsNone(re.search(r"\bwhite\s+hair\b(?!\s*ribbon)", appearance))
        self.assertIsNone(re.search(r"\bblue\s+eyes\b", appearance))
        self.assertIn("purple eyes", appearance)
        self.assertIn("black hair", tags)
        self.assertNotIn("white hair", tags)
        self.assertNotIn("blue eyes", tags)

    def test_correct_identity_tags_pass_through_without_duplication(self):
        async def run():
            return await self._plan({
                "quality_meta_year_safe": "masterpiece, best quality, safe",
                "count": "1girl",
                "appearance": "pale skin, black straight hair, low ponytail, purple eyes, white blouse",
                "tags": "Her black hair catches the window light.",
            })

        payload = asyncio.run(run())
        appearance = payload["appearance"]
        lowered = [t.strip().lower() for t in appearance.split(",")]
        self.assertEqual(lowered.count("black straight hair"), 1)
        self.assertEqual(lowered.count("low ponytail"), 1)
        self.assertIn("black hair", payload["tags"])

    def test_no_source_color_means_no_color_judgement(self):
        async def run():
            slots = self._slots()
            slots.effective_appearance = "pale skin, low ponytail, white blouse"
            payload = await plan_animaflow_slots(
                self._service({
                    "quality_meta_year_safe": "masterpiece, best quality, safe",
                    "count": "1girl",
                    "appearance": "pale skin, white blouse, silver hair",
                    "tags": "Silver hair glows in the dusk.",
                }),
                "telegram:identity",
                slots,
                workflow="anima29",
                schema=self._schema(),
                knowledge={},
            )
            return payload

        payload = asyncio.run(run())
        # 源槽位没有发色信息时不做颜色裁决，只补齐丢失的发型标签。
        self.assertIn("silver hair", payload["appearance"])
        self.assertIn("low ponytail", payload["appearance"])
        self.assertIn("Silver hair", payload["tags"])


if __name__ == "__main__":
    unittest.main()
