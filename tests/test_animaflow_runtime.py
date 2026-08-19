from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from telegram_comfyui_selfie.animaflow_runtime import (
    apply_animaflow_cfg_policy,
    clear_animaflow_caches,
    inspect_animaflow_workflow,
    normalize_animaflow_catalog,
    select_animaflow_workflow,
)


class _Response:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status
        self.content_length = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, **kwargs):
        return self.payload


class _Session:
    closed = False

    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, params=None, timeout=None):
        del timeout
        params = dict(params or {})
        self.calls.append((url, params))
        if url.endswith("/anima/workflows"):
            return _Response({
                "default": "base",
                "workflows": {
                    "base": {
                        "description": "Base",
                        "endpoints": {},
                    },
                    "anima29_turbo": {
                        "description": "Anima 2.9B Turbo（cfg=1, steps 8-12 默认 8）",
                        "endpoints": {
                            "generate": "POST /anima/generate（body 带 workflow）",
                            "schema": "GET /anima/schema?workflow=anima29_turbo",
                            "knowledge": "GET /anima/knowledge?workflow=anima29_turbo",
                        },
                    },
                    "old_turbo": {
                        "description": "legacy",
                        "deprecated": True,
                        "endpoints": {},
                    },
                },
            })
        if url.endswith("/anima/schema"):
            self.assert_workflow(params)
            return _Response({
                "parameters": {
                    "properties": {
                        "tags": {"type": "string"},
                        "neg": {"type": "string"},
                        "cfg": {"type": "number"},
                        "steps": {"type": "integer"},
                    }
                }
            })
        if url.endswith("/anima/knowledge"):
            self.assert_workflow(params)
            return _Response({"expert": "cfg 固定 1；steps 8-12（默认 8）。"})
        raise AssertionError(f"unexpected URL: {url}")

    @staticmethod
    def assert_workflow(params: dict[str, str]) -> None:
        if params.get("workflow") != "anima29_turbo":
            raise AssertionError(f"missing workflow query: {params}")


class _LegacyFallbackSession:
    closed = False

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url: str, *, params=None, timeout=None):
        del params, timeout
        self.calls.append(url)
        # 模拟旧插件仍能生图、但发现接口与资源说明接口均不可用的最差兼容场景。
        return _Response({"error": "not found"}, status=404)


class _ResourceFailureSession(_Session):
    def get(self, url: str, *, params=None, timeout=None):
        if url.endswith("/anima/schema"):
            self.calls.append((url, dict(params or {})))
            return _Response({"error": "schema unavailable"}, status=503)
        if url.endswith("/anima/schema_turbo_v1") or url.endswith("/anima/knowledge_new_models"):
            self.calls.append((url, dict(params or {})))
            return _Response({"error": "legacy docs unavailable"}, status=404)
        return super().get(url, params=params, timeout=timeout)


class AnimaFlowRuntimeTestCase(unittest.TestCase):
    def tearDown(self):
        clear_animaflow_caches()

    def test_catalog_has_no_local_workflow_assumptions(self):
        catalog = normalize_animaflow_catalog({
            "default": "base",
            "workflows": {
                "base": {"endpoints": {}},
                "brand_new_turbo": {
                    "endpoints": {
                        "generate": "POST /anima/future-generate",
                        "schema": "GET /anima/future-schema?workflow=brand_new_turbo",
                        "knowledge": "GET /anima/future-knowledge?workflow=brand_new_turbo",
                    }
                },
            },
        })

        self.assertEqual(select_animaflow_workflow(catalog), "brand_new_turbo")
        endpoints = catalog["workflows"]["brand_new_turbo"]["endpoints"]
        self.assertEqual(endpoints["generate"], "/anima/future-generate")
        self.assertEqual(endpoints["schema"], "/anima/future-schema")
        self.assertEqual(endpoints["knowledge"], "/anima/future-knowledge")

    def test_inspection_fetches_catalog_schema_knowledge_and_defaults(self):
        async def run():
            session = _Session()
            service = SimpleNamespace(
                config={
                    "comfyui_url": "http://127.0.0.1:8188",
                    "animaflow_workflow": "",
                },
                comfyui_url="http://127.0.0.1:8188",
                comfy_session=session,
            )

            state = await inspect_animaflow_workflow(service, force=True)

            self.assertEqual(state["selected"], "anima29_turbo")
            self.assertEqual(state["defaults"], {"cfg": 1.0, "steps": 8})
            self.assertFalse(state["supports_negative"])
            self.assertEqual([url.rsplit("/", 1)[-1] for url, _ in session.calls], ["workflows", "schema", "knowledge"])

        asyncio.run(run())

    def test_cfg_one_removes_every_negative_alias_and_only_marks_explicit(self):
        schema = {
            "parameters": {
                "properties": {
                    "tags": {"type": "string"},
                    "neg": {"type": "string"},
                }
            }
        }
        explicit = apply_animaflow_cfg_policy(
            {"tags": "An adult intimate scene.", "neg": "mosaic", "negative_prompt": "censored"},
            cfg="1.0",
            safety="nsfw",
            schema=schema,
        )
        self.assertNotIn("neg", explicit)
        self.assertNotIn("negative_prompt", explicit)
        self.assertTrue(explicit["tags"].endswith("no mosaic, uncensored"))

        safe = apply_animaflow_cfg_policy(
            {"tags": "A daytime park scene.", "negative": "nsfw"},
            cfg=1,
            safety="safe",
            schema=schema,
        )
        self.assertNotIn("negative", safe)
        self.assertNotIn("uncensored", safe["tags"])

    def test_discovery_failure_falls_back_to_legacy_turbo_v1_contract(self):
        async def run():
            session = _LegacyFallbackSession()
            service = SimpleNamespace(
                config={
                    "comfyui_url": "http://127.0.0.1:8188",
                    "animaflow_workflow": "anima29_turbo",
                },
                comfyui_url="http://127.0.0.1:8188",
                comfy_session=session,
            )

            state = await inspect_animaflow_workflow(service, force=True)

            self.assertEqual(state["selected"], "turbo_v1")
            self.assertTrue(state["legacy_fallback"])
            self.assertIn("HTTP 404", state["fallback_reason"])
            self.assertEqual(state["defaults"], {"cfg": 1.0, "steps": 12})
            self.assertEqual(state["cfg_bounds"], {"minimum": 0.7, "maximum": 1.0})
            self.assertEqual(state["steps_bounds"], {"minimum": 8, "maximum": 12})
            self.assertFalse(state["supports_negative"])
            self.assertTrue(any(url.endswith("/anima/schema_turbo_v1") for url in session.calls))
            self.assertTrue(any(url.endswith("/anima/knowledge_new_models") for url in session.calls))

        asyncio.run(run())

    def test_selected_workflow_resource_failure_also_falls_back(self):
        async def run():
            session = _ResourceFailureSession()
            service = SimpleNamespace(
                config={"comfyui_url": "http://127.0.0.1:8188", "animaflow_workflow": "anima29_turbo"},
                comfyui_url="http://127.0.0.1:8188",
                comfy_session=session,
            )

            state = await inspect_animaflow_workflow(service, force=True)

            self.assertEqual(state["selected"], "turbo_v1")
            self.assertTrue(state["legacy_fallback"])
            self.assertIn("schema HTTP 503", state["fallback_reason"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
