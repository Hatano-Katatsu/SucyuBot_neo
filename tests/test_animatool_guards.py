from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_comfyui_selfie.generation import (
    ANIMATOOL_WORKFLOWS,
    PromptSlots,
    _apply_animatool_guard_contract,
    _build_animatool_guard_contract,
    _build_animatool_turbo_payload,
)
from telegram_comfyui_selfie.image_planning import plan_animatool_slots


class AnimaToolGuardContractTestCase(unittest.TestCase):
    GUARDED_NEGATIVE = (
        "bad hands, holding phone, mirror, unrelated extra person, "
        "split screen, nipples"
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
    def _service(workflow: str) -> SimpleNamespace:
        return SimpleNamespace(
            config={
                "animatool_workflow": workflow,
                "animatool_filename_prefix": "guard-test",
                "animatool_turbo_cfg": "1.0",
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

    def test_all_four_workflows_preserve_the_same_guard_contract(self):
        for workflow, metadata in ANIMATOOL_WORKFLOWS.items():
            with self.subTest(workflow=workflow):
                supports_neg = bool(metadata.get("supports_neg"))
                schema = self._schema(supports_neg=supports_neg)
                payload = _build_animatool_turbo_payload(
                    self._service(workflow),
                    self._slots(),
                    "positive prompt",
                    self.GUARDED_NEGATIVE,
                    7,
                    schema,
                )

                if supports_neg:
                    negative = payload["neg"].lower()
                    for term in (
                        "holding phone",
                        "mirror",
                        "unrelated extra person",
                        "split screen",
                        "nipples",
                    ):
                        self.assertIn(term, negative)
                else:
                    self.assertNotIn("neg", payload)
                    tags = payload["tags"].lower()
                    for phrase in (
                        "no phone",
                        "no mirror",
                        "no unrelated extra person",
                        "one undivided single frame",
                        "fully covers intimate areas",
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
        self.assertIn("nipples", negative)

    def test_slots_planner_applies_guards_after_llm_output(self):
        async def run():
            schema = self._schema(supports_neg=True)
            service = SimpleNamespace(
                config={"animatool_workflow": "turbo_v1"},
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
            system_prompt = service._call_llm.await_args.args[0]
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
        self.assertIn("one undivided single frame", payload["tags"].lower())
        self.assertIn("fully covers intimate areas", payload["tags"].lower())

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
        self.assertIn("no duplicate phone", tags)
        self.assertIn("at most one intended reflection", tags)
        self.assertNotIn("no phone, camera interface", tags)
        self.assertNotIn("no mirror or reflected duplicate", tags)

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


if __name__ == "__main__":
    unittest.main()
