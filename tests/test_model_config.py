from __future__ import annotations

import asyncio
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.support import ServiceFixtureMixin, make_project_temp_dir


class JsonRequest(dict):
    def __init__(
        self,
        service,
        payload: dict,
        *,
        match_info: dict | None = None,
        query: dict | None = None,
        web_auth: dict | None = None,
    ):
        super().__init__(web_auth=web_auth or {"role": "admin", "user_id": "admin", "token": "test"})
        self.app = {"service": service}
        self.match_info = match_info or {}
        self.query = query or {}
        self._payload = payload

    async def json(self):
        return copy.deepcopy(self._payload)


class LlmPromptCompareScriptTestCase(unittest.TestCase):
    """LLM prompt 比对脚本测试。"""

    def test_compare_entries_reports_prefix_and_non_prefix_same_messages(self):
        from scripts.compare_llm_chat_prompts import build_entry_view, compare_entries

        def entry(messages):
            return {
                "session_id": "telegram:1",
                "time": "2026-06-26T10:00:00",
                "ts": 1,
                "request": {
                    "body": {
                        "model": "m",
                        "temperature": 0.7,
                        "tools": [{"type": "function", "function": {"name": "tool_a"}}],
                        "tool_choice": "auto",
                        "messages": messages,
                    }
                },
                "usage": {"prompt_tokens": 1000, "cached_tokens": 500},
            }

        old = build_entry_view(0, entry([
            {"role": "system", "content": "stable-a"},
            {"role": "system", "content": "stable-b"},
            {"role": "user", "content": "old-only"},
            {"role": "assistant", "content": "same-after-diff"},
        ]))
        new = build_entry_view(1, entry([
            {"role": "system", "content": "stable-a"},
            {"role": "system", "content": "stable-b"},
            {"role": "user", "content": "new-only"},
            {"role": "assistant", "content": "same-after-diff"},
            {"role": "user", "content": "append"},
        ]))

        comparison = compare_entries(old, new)

        self.assertTrue(comparison.prompt_changed)
        self.assertEqual(comparison.common_prefix_messages, 2)
        self.assertGreater(comparison.common_prefix_chars, 0)
        self.assertEqual(comparison.non_prefix_common_messages, 1)
        self.assertEqual(comparison.non_prefix_lcs_messages, 1)
        self.assertEqual(comparison.same_index_after_prefix, [3])
        self.assertTrue(comparison.prompt_components_same["tools"])
        self.assertFalse(comparison.prompt_components_same["messages"])
        self.assertTrue(comparison.settings_same)

    def test_compare_entries_prefix_uses_request_key_order(self):
        """前缀字符数必须基于真实请求键序，语义相等判断不受键序影响。"""
        from scripts.compare_llm_chat_prompts import build_entry_view, compare_entries

        def entry(messages):
            return {
                "session_id": "telegram:1",
                "time": "2026-06-26T10:00:00",
                "ts": 1,
                "request": {"body": {"messages": messages}},
                "usage": {},
            }

        old = build_entry_view(0, entry([{"role": "system", "content": "stable"}]))
        # 语义相同但键插入顺序不同：真实请求字节序在第一个键处即分岔。
        new = build_entry_view(1, entry([{"content": "stable", "role": "system"}]))

        comparison = compare_entries(old, new)

        self.assertFalse(comparison.prompt_changed)
        self.assertEqual(comparison.common_prefix_messages, 1)
        self.assertEqual(comparison.common_prefix_chars, len('{"messages":[{"'))
        self.assertLess(comparison.common_prefix_char_rate, 1.0)

        same_order = build_entry_view(2, entry([{"role": "system", "content": "stable!"}]))
        same_order_comparison = compare_entries(old, same_order)
        self.assertGreater(
            same_order_comparison.common_prefix_chars,
            comparison.common_prefix_chars,
        )

    def test_provider_cache_tokens_reads_nested_usage_fields(self):
        from scripts.compare_llm_chat_prompts import build_entry_view, cache_rate, provider_cache_tokens

        usage = {
            "prompt_tokens": 5000,
            "prompt_tokens_details": {
                "cached_tokens": 4096,
            },
        }

        self.assertEqual(provider_cache_tokens(usage), 4096)
        self.assertAlmostEqual(cache_rate(usage), 4096 / 5000)

        view = build_entry_view(0, {
            "session_id": "telegram:1",
            "time": "2026-06-28T20:00:00",
            "ts": 1,
            "request": {"body": {"messages": []}},
            "usage": {
                "prompt_tokens": 5000,
                "cached_tokens": 0,
                "raw": usage,
            },
        })
        self.assertEqual(provider_cache_tokens(view.usage), 4096)


class ModelProfileTestCase(ServiceFixtureMixin, unittest.TestCase):
    """模型 profile 固定思考、去 kimi 等配置测试。"""

    def test_default_profiles_contain_only_expected_models(self):
        from telegram_comfyui_selfie.defaults import DEFAULT_CONFIG

        profiles = DEFAULT_CONFIG["global_model_profiles"]
        ids = set(profiles.keys())
        self.assertEqual(ids, {"deepseek-pro", "deepseek-flash", "glm"})
        for pid, profile in profiles.items():
            self.assertTrue(profile.get("thinking_fixed"), f"{pid} 应声明 thinking_fixed")

    def test_default_chat_max_tokens_is_high_enough_for_thinking(self):
        svc = self.make_service()
        resolved = svc._resolved_llm_config("chat", "telegram:1")
        self.assertEqual(str(resolved.get("max_tokens")), "12000")

    def test_user_thinking_settings_override_profile(self):
        async def run():
            svc = self.make_service()
            # 默认 chat=deepseek-pro（profile disable_thinking=false → 思考开）、fast=deepseek-flash（思考关）
            _, _, chat_thinking = svc._resolve_llm_profile("chat", "telegram:1")
            _, _, fast_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertTrue(chat_thinking, "deepseek-pro 默认思考开启")
            self.assertFalse(fast_thinking, "deepseek-flash 默认思考关闭")

            # 用户显式设置覆盖 profile
            svc.app_store.update_user_model_settings(
                "1", chat_profile_id="deepseek-pro", chat_thinking=False,
                fast_profile_id="deepseek-flash", fast_thinking=True,
            )
            _, _, chat_thinking = svc._resolve_llm_profile("chat", "telegram:1")
            _, _, fast_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertFalse(chat_thinking, "用户关闭思考应覆盖 deepseek-pro 默认开启")
            self.assertTrue(fast_thinking, "用户开启思考应覆盖 deepseek-flash 默认关闭")

            # 切到 glm（profile 默认关闭），用户开启应生效
            svc.app_store.update_user_model_settings("1", fast_profile_id="glm", fast_thinking=True)
            _, _, glm_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertTrue(glm_thinking, "glm profile 默认关闭，但用户开启应生效")

        asyncio.run(run())

    def test_global_thinking_enabled_config_overrides_profile(self):
        async def run():
            svc = self.make_service()
            # 全局配置强制开启 fast thinking，覆盖 deepseek-flash 默认关闭
            svc.config["fast_thinking_enabled"] = True
            _, _, fast_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertTrue(fast_thinking)

            # 全局配置强制关闭 chat thinking，覆盖 deepseek-pro 默认开启
            svc.config["chat_thinking_enabled"] = "false"
            _, _, chat_thinking = svc._resolve_llm_profile("chat", "telegram:1")
            self.assertFalse(chat_thinking)

            # 用户设置优先于全局配置
            svc.app_store.update_user_model_settings("1", fast_thinking=False)
            _, _, fast_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertFalse(fast_thinking)

        asyncio.run(run())

    def test_custom_profile_uses_model_bound_thinking(self):
        async def run():
            svc = self.make_service()
            svc.app_store.upsert_model_profile("1", "custom", {
                "name": "Custom", "base_url": "http://localhost/v1", "api_key": "k",
                "model": "custom-model", "timeout": 120, "disable_thinking": True,
            })
            svc.app_store.update_user_model_settings("1", chat_profile_id="custom")
            _, _, thinking = svc._resolve_llm_profile("chat", "telegram:1")
            self.assertFalse(thinking, "profile disable_thinking=True 默认关闭思考")

            # 用户显式开启思考应覆盖 profile
            svc.app_store.update_user_model_settings("1", chat_thinking=True)
            _, _, thinking = svc._resolve_llm_profile("chat", "telegram:1")
            self.assertTrue(thinking, "用户显式开启思考应覆盖 profile disable_thinking")

        asyncio.run(run())

    def test_vision_profile_thinking_settings(self):
        async def run():
            svc = self.make_service()
            svc.app_store.upsert_model_profile("1", "vision", {
                "name": "Vision", "base_url": "http://localhost/v1", "api_key": "vk",
                "model": "vision-model", "disable_thinking": True,
            })
            svc.app_store.update_user_model_settings("1", vision_profile_id="vision")
            _, _, thinking = svc._resolve_llm_profile("vision", "telegram:1")
            self.assertFalse(thinking, "vision profile 默认关闭思考")

            # 用户显式开启 vision 思考
            svc.app_store.update_user_model_settings("1", vision_thinking=True)
            _, _, thinking = svc._resolve_llm_profile("vision", "telegram:1")
            self.assertTrue(thinking, "用户显式开启 vision 思考应生效")

            # 用户清除设置回到跟随 profile
            svc.app_store.update_user_model_settings("1", vision_thinking=None)
            _, _, thinking = svc._resolve_llm_profile("vision", "telegram:1")
            self.assertFalse(thinking, "清除用户设置后回到 profile 默认")

        asyncio.run(run())

    def test_model_settings_api_roundtrips_thinking_tri_state(self):
        from telegram_comfyui_selfie.webui_models import api_update_model_settings

        async def run():
            svc = self.make_service()
            # 三态设置：chat=false、fast=true、vision 未设置
            response = await api_update_model_settings(JsonRequest(svc, {
                "chat_thinking": False,
                "fast_thinking": True,
                "vision_thinking": "",
            }))
            data = json.loads(response.text)
            self.assertTrue(data["ok"])
            settings = data["settings"]
            self.assertFalse(settings["chat_thinking"])
            self.assertTrue(settings["fast_thinking"])
            self.assertIsNone(settings["vision_thinking"])

            # 清除为跟随 profile
            response = await api_update_model_settings(JsonRequest(svc, {
                "chat_thinking": "",
                "fast_thinking": "false",
                "vision_thinking": "true",
            }))
            data = json.loads(response.text)
            self.assertIsNone(data["settings"]["chat_thinking"])
            self.assertFalse(data["settings"]["fast_thinking"])
            self.assertTrue(data["settings"]["vision_thinking"])

        asyncio.run(run())

    def test_vision_profile_is_optional_and_user_scoped(self):
        async def run():
            svc = self.make_service()
            self.assertFalse(svc.has_llm_config("vision", "telegram:1"))
            svc.app_store.upsert_model_profile("1", "vision", {
                "name": "Vision", "base_url": "http://localhost/v1", "api_key": "vk",
                "model": "vision-model", "disable_thinking": True,
            })
            svc.app_store.update_user_model_settings("1", vision_profile_id="vision")

            self.assertTrue(svc.has_llm_config("vision", "telegram:1"))
            self.assertFalse(svc.has_llm_config("vision", "telegram:2"))
            profile_id, _, thinking = svc._resolve_llm_profile("vision", "telegram:1")
            self.assertEqual(profile_id, "vision")
            self.assertFalse(thinking)

        asyncio.run(run())

    def test_model_settings_roundtrip_reasoning_effort_in_same_field(self):
        from telegram_comfyui_selfie.webui_models import api_update_model_settings

        async def run():
            svc = self.make_service()
            response = await api_update_model_settings(JsonRequest(svc, {
                "chat_thinking": "high",
                "fast_thinking": "minimal",
                "vision_thinking": "none",
            }))
            data = json.loads(response.text)

            self.assertTrue(data["ok"])
            self.assertEqual(data["settings"]["chat_thinking"], "high")
            self.assertEqual(data["settings"]["fast_thinking"], "minimal")
            self.assertEqual(data["settings"]["vision_thinking"], "none")
            stored = svc.app_store.get_user_model_settings("admin")
            self.assertEqual(stored["chat_thinking"], "high")

            response = await api_update_model_settings(JsonRequest(svc, {"chat_thinking": "turbo"}))
            self.assertEqual(response.status, 400)
            self.assertIn("effort", json.loads(response.text)["error"])

        asyncio.run(run())

    def test_reasoning_effort_request_and_full_chat_endpoint_are_supported(self):
        async def run():
            svc = self.make_service()
            svc.config["global_model_profiles"] = {
                "effort": {
                    "name": "Effort",
                    "base_url": "https://opencode.example/zen/go/v1/chat/completions",
                    "api_key": "secret",
                    "model": "gpt-effort",
                    "disable_thinking": True,
                    "thinking_control": "param_always",
                },
            }
            svc.config["default_chat_model_profile"] = "effort"
            svc.app_store.update_user_model_settings("1", chat_thinking="high")
            effort_resolved = svc._resolved_llm_config("chat", "telegram:1")
            self.assertTrue(effort_resolved["thinking"])
            self.assertEqual(effort_resolved["thinking_effort"], "high")
            forced_off = svc._resolved_llm_config("chat", "telegram:1", disable_thinking=True)
            self.assertFalse(forced_off["thinking"])
            self.assertEqual(forced_off["thinking_effort"], "")
            captured: list[tuple[str, dict]] = []

            class FakeResponse:
                status = 200

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def json(self):
                    return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

            class FakeSession:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def post(self, url, **kwargs):
                    captured.append((url, copy.deepcopy(kwargs["json"])))
                    return FakeResponse()

            with patch("telegram_comfyui_selfie.llm_runtime.aiohttp.ClientSession", FakeSession):
                await svc._call_llm_messages(
                    [{"role": "user", "content": "hello"}],
                    purpose="chat",
                    session_id="telegram:1",
                )
                svc.app_store.update_user_model_settings("1", chat_thinking=False)
                await svc._call_llm_messages(
                    [{"role": "user", "content": "hello"}],
                    purpose="chat",
                    session_id="telegram:1",
                )

            self.assertEqual(captured[0][0], "https://opencode.example/zen/go/v1/chat/completions")
            self.assertEqual(captured[0][1]["reasoning_effort"], "high")
            self.assertNotIn("thinking", captured[0][1])
            self.assertNotIn("reasoning_effort", captured[1][1])
            self.assertEqual(captured[1][1]["thinking"], {"type": "disabled"})
            resolved = svc._resolved_llm_config("chat", "telegram:1")
            self.assertEqual(resolved["api_base"], "https://opencode.example/zen/go/v1")

        asyncio.run(run())

    def test_global_model_catalog_loads_once_and_deduplicates_sources(self):
        async def run():
            svc = self.make_service()
            svc.config["global_model_profiles"] = {
                "source-a": {
                    "base_url": "https://catalog.example/v1/chat/completions",
                    "api_key": "shared-key",
                    "model": "old-a",
                },
                "source-b": {
                    "base_url": "https://catalog.example/v1",
                    "api_key": "shared-key",
                    "model": "old-b",
                },
                "broken": {
                    "base_url": "https://broken.example/v1/chat/completions",
                    "api_key": "broken-key",
                    "model": "old-c",
                },
            }
            svc._global_model_catalog_loaded = False
            calls: list[tuple[str, dict]] = []

            class FakeResponse:
                def __init__(self, status, payload=None, text=""):
                    self.status = status
                    self._payload = payload
                    self._text = text

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def json(self):
                    return copy.deepcopy(self._payload)

                async def text(self):
                    return self._text

            class FakeSession:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                def get(self, url, **kwargs):
                    calls.append((url, dict(kwargs.get("headers") or {})))
                    if "broken.example" in url:
                        return FakeResponse(503, text="unavailable")
                    return FakeResponse(200, {
                        "data": [
                            {"id": "gpt-5"},
                            {"id": "gpt-5-mini"},
                            {"id": "gpt-5"},
                        ],
                    })

            with patch("telegram_comfyui_selfie.llm_runtime.aiohttp.ClientSession", FakeSession):
                first = await svc.load_global_model_catalog_once()
                second = await svc.load_global_model_catalog_once()

            self.assertEqual(len(calls), 2, "同 URL 与密钥的 profile 应合并为一次请求")
            self.assertIn(("https://catalog.example/v1/models", "Bearer shared-key"), {
                (url, headers.get("Authorization")) for url, headers in calls
            })
            self.assertEqual([item["id"] for item in first], ["gpt-5", "gpt-5-mini"])
            self.assertEqual(first[0]["source_profile_ids"], ["source-a", "source-b"])
            self.assertEqual(second, first)

        asyncio.run(run())

    def test_model_catalog_is_admin_only_and_can_reuse_source_secret(self):
        from telegram_comfyui_selfie.webui_models import api_model_profiles, api_save_model_profile

        async def run():
            svc = self.make_service()
            svc.config["global_model_profiles"] = {
                "source": {
                    "name": "Source",
                    "base_url": "https://catalog.example/v1/chat/completions",
                    "api_key": "source-secret",
                    "model": "old-model",
                },
            }
            svc._global_model_catalog = [{
                "id": "new-model",
                "api_base": "https://catalog.example/v1",
                "source_profile_id": "source",
                "source_profile_ids": ["source"],
            }]

            admin_response = await api_model_profiles(JsonRequest(svc, {}))
            admin_data = json.loads(admin_response.text)
            self.assertEqual(admin_data["available_global_models"][0]["id"], "new-model")

            user_request = JsonRequest(
                svc,
                {},
                web_auth={"role": "user", "user_id": "1", "token": "test"},
            )
            user_data = json.loads((await api_model_profiles(user_request)).text)
            self.assertEqual(user_data["available_global_models"], [])

            save_response = await api_save_model_profile(JsonRequest(
                svc,
                {
                    "_scope": "global",
                    "_catalog_source_profile_id": "source",
                    "name": "New",
                    "base_url": "https://catalog.example/v1",
                    "model": "new-model",
                },
                match_info={"profile_id": "new"},
            ))
            self.assertTrue(json.loads(save_response.text)["ok"])
            self.assertEqual(svc.config["global_model_profiles"]["new"]["api_key"], "source-secret")
            self.assertEqual(
                json.loads(save_response.text)["global_profiles"]["new"]["api_key"],
                "********",
            )

        asyncio.run(run())

    def test_global_model_profile_update_preserves_hidden_fields_and_delete_works(self):
        from telegram_comfyui_selfie.webui_models import api_delete_model_profile, api_save_model_profile

        async def run():
            svc = self.make_service()
            svc.config["global_model_profiles"] = {
                "editable": {
                    "name": "Before",
                    "base_url": "https://models.example/v1",
                    "api_key": "keep-secret",
                    "model": "old-model",
                    "disable_thinking": True,
                    "thinking_fixed": True,
                    "thinking_control": "param_always",
                },
            }

            save_response = await api_save_model_profile(JsonRequest(
                svc,
                {
                    "_scope": "global",
                    "name": "After",
                    "base_url": "https://models.example/v1/chat/completions",
                    "model": "new-model",
                    "max_tokens": "8192",
                    "timeout": "120",
                },
                match_info={"profile_id": "editable"},
            ))

            self.assertTrue(json.loads(save_response.text)["ok"])
            updated = svc.config["global_model_profiles"]["editable"]
            self.assertEqual(updated["name"], "After")
            self.assertEqual(updated["model"], "new-model")
            self.assertEqual(updated["api_key"], "keep-secret")
            self.assertTrue(updated["disable_thinking"])
            self.assertTrue(updated["thinking_fixed"])
            self.assertEqual(updated["thinking_control"], "param_always")

            delete_response = await api_delete_model_profile(JsonRequest(
                svc,
                {},
                match_info={"profile_id": "editable"},
                query={"scope": "global"},
            ))
            self.assertTrue(json.loads(delete_response.text)["ok"])
            self.assertNotIn("editable", svc.config["global_model_profiles"])

        asyncio.run(run())

    def test_service_loads_model_catalog_before_web_console(self):
        async def run():
            svc = self.make_service()
            svc.config["web_enabled"] = True
            svc.config["telegram_bot_token"] = ""
            svc.load_global_model_catalog_once = AsyncMock(return_value=[])

            async def start_web():
                svc.load_global_model_catalog_once.assert_awaited_once()
                svc._stop_event.set()

            svc.start_web_console = AsyncMock(side_effect=start_web)
            svc.stop_bot = AsyncMock()
            svc.stop_web_console = AsyncMock()
            svc.close = AsyncMock()

            await svc.run()

            svc.start_web_console.assert_awaited_once()

        asyncio.run(run())

    def test_native_multimodal_matches_resolved_endpoint_and_model(self):
        svc = self.make_service()
        for profile_id in ("chat-copy", "vision-copy"):
            svc.app_store.upsert_model_profile("1", profile_id, {
                "name": profile_id,
                "base_url": (
                    "https://multimodal.example/v1/chat/completions"
                    if profile_id == "vision-copy"
                    else "https://multimodal.example/v1/"
                ),
                "api_key": f"key-{profile_id}",
                "model": "same-multimodal-model",
                "disable_thinking": True,
            })
        svc.app_store.update_user_model_settings(
            "1",
            chat_profile_id="chat-copy",
            vision_profile_id="vision-copy",
        )

        self.assertTrue(svc._chat_uses_native_multimodal("telegram:1"))

        svc.app_store.upsert_model_profile("1", "vision-copy", {
            "name": "vision-copy",
            "base_url": "https://multimodal.example/v1/",
            "api_key": "key-vision-copy",
            "model": "different-model",
            "disable_thinking": True,
        })
        self.assertFalse(svc._chat_uses_native_multimodal("telegram:1"))

    def test_resolved_config_honors_fixed_thinking_for_glm(self):
        async def run():
            svc = self.make_service()
            svc.app_store.update_user_model_settings("1", chat_profile_id="glm")
            resolved = svc._resolved_llm_config("chat", "telegram:1")
            self.assertFalse(resolved["thinking"])
            self.assertEqual(resolved["thinking_control"], "param")

        asyncio.run(run())


class ConfigTransactionTestCase(ServiceFixtureMixin, unittest.TestCase):
    """Web 配置候选校验与原子落盘事务测试。"""

    def test_enabling_animaflow_forces_discovery_and_applies_workflow_defaults(self):
        from telegram_comfyui_selfie.webui_models import api_save_config

        async def run():
            svc = self.make_service()
            discovered = {
                "selected": "anima29_turbo",
                "defaults": {"cfg": 1.0, "steps": 8},
                "workflows": [{"name": "anima29_turbo", "description": "turbo"}],
                "schema": {},
                "knowledge": {},
            }
            discover_mock = AsyncMock(return_value=discovered)

            with patch(
                "telegram_comfyui_selfie.webui_models.inspect_animaflow_workflow",
                new=discover_mock,
            ):
                response = await api_save_config(JsonRequest(svc, {
                    "values": {
                        "animaflow_enabled": True,
                        "animaflow_workflow": "anima29_turbo",
                        # 开启时必须以工作流默认值为准，不能保留表单里的旧值。
                        "animaflow_cfg": "7",
                        "animaflow_steps": "99",
                    }
                }))

            self.assertEqual(response.status, 200)
            discover_mock.assert_awaited_once_with(svc, "anima29_turbo", force=True)
            self.assertTrue(svc.config["animaflow_enabled"])
            self.assertEqual(svc.config["animaflow_workflow"], "anima29_turbo")
            self.assertEqual(svc.config["animaflow_cfg"], "1.0")
            self.assertEqual(svc.config["animaflow_steps"], "8")
            persisted = json.loads(svc.config_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["animaflow_cfg"], "1.0")
            self.assertEqual(persisted["animaflow_steps"], "8")

        asyncio.run(run())

    def test_animaflow_discovery_failure_does_not_mutate_runtime_or_file(self):
        from telegram_comfyui_selfie.animaflow_runtime import AnimaFlowError
        from telegram_comfyui_selfie.webui_models import api_save_config

        async def run():
            svc = self.make_service()
            before_config = copy.deepcopy(svc.config)
            before_file = svc.config_path.read_bytes()
            discover_mock = AsyncMock(side_effect=AnimaFlowError("工作流目录不可用"))

            with patch(
                "telegram_comfyui_selfie.webui_models.inspect_animaflow_workflow",
                new=discover_mock,
            ):
                response = await api_save_config(JsonRequest(svc, {
                    "values": {"animaflow_enabled": True},
                }))

            self.assertEqual(response.status, 502)
            self.assertIn("工作流目录不可用", json.loads(response.text)["error"])
            discover_mock.assert_awaited_once()
            self.assertEqual(svc.config, before_config)
            self.assertEqual(svc.config_path.read_bytes(), before_file)

        asyncio.run(run())

    def test_switching_animaflow_workflow_resets_cfg_and_steps(self):
        from telegram_comfyui_selfie.webui_models import api_save_config

        async def run():
            svc = self.make_service()
            svc.config.update({
                "animaflow_enabled": True,
                "animaflow_workflow": "legacy_turbo",
                "animaflow_cfg": "2.5",
                "animaflow_steps": "20",
            })
            svc.save_config()
            discover_mock = AsyncMock(return_value={
                "selected": "anima29_turbo",
                "defaults": {"cfg": 1.0, "steps": 8},
                "workflows": [],
                "schema": {},
                "knowledge": {},
            })

            with patch(
                "telegram_comfyui_selfie.webui_models.inspect_animaflow_workflow",
                new=discover_mock,
            ):
                response = await api_save_config(JsonRequest(svc, {
                    "values": {
                        "animaflow_workflow": "anima29_turbo",
                        "animaflow_cfg": "6",
                        "animaflow_steps": "60",
                    }
                }))

            self.assertEqual(response.status, 200)
            discover_mock.assert_awaited_once_with(svc, "anima29_turbo", force=True)
            self.assertEqual(svc.config["animaflow_cfg"], "1.0")
            self.assertEqual(svc.config["animaflow_steps"], "8")

        asyncio.run(run())

    def test_legacy_animatool_config_is_migrated_without_runtime_aliases(self):
        from telegram_comfyui_selfie import TelegramComfyUIService

        root = make_project_temp_dir("animaflow_config_migration")
        config_path = root / "config.json"
        config_path.write_text(json.dumps({
            "image_backend": "animatool",
            "animatool_workflow": "turbo_v1",
            "animatool_turbo_cfg": "0.9",
            "animatool_turbo_steps": "11",
            "animatool_filename_prefix": "legacy",
        }), encoding="utf-8")

        svc = TelegramComfyUIService(config_path, root / "state.json")

        self.assertTrue(svc.config["animaflow_enabled"])
        self.assertEqual(svc.config["image_backend"], "animaflow")
        self.assertEqual(svc.config["animaflow_workflow"], "turbo_v1")
        self.assertEqual(svc.config["animaflow_cfg"], "0.9")
        self.assertEqual(svc.config["animaflow_steps"], "11")
        self.assertEqual(svc.config["animaflow_filename_prefix"], "legacy")
        self.assertFalse(any(key.startswith("animatool_") for key in svc.config))

    def test_late_cast_failure_does_not_apply_earlier_fields(self):
        from telegram_comfyui_selfie.webui_models import api_save_config

        async def run():
            svc = self.make_service()
            before_config = copy.deepcopy(svc.config)
            before_file = svc.config_path.read_bytes()
            request = JsonRequest(svc, {
                "values": {
                    "web_admin_username": "changed-before-error",
                    "push_continuity_hours": "not-an-int",
                }
            })

            response = await api_save_config(request)
            data = json.loads(response.text)

            self.assertEqual(response.status, 400)
            self.assertFalse(data["ok"])
            self.assertIn("push_continuity_hours", data["error"])
            self.assertEqual(svc.config, before_config)
            self.assertEqual(svc.config_path.read_bytes(), before_file)

        asyncio.run(run())

    def test_non_finite_and_cross_field_values_are_rejected_before_save(self):
        from telegram_comfyui_selfie.webui_models import api_save_config

        async def run():
            svc = self.make_service()
            before_config = copy.deepcopy(svc.config)
            before_file = svc.config_path.read_bytes()
            cases = (
                ({"chat_llm_temperature": "NaN"}, "有限数值"),
                ({
                    "post_chat_push_delay_min_minutes": "20",
                    "post_chat_push_delay_max_minutes": "10",
                }, "不能大于"),
            )
            for values, expected_error in cases:
                with self.subTest(values=values):
                    response = await api_save_config(JsonRequest(svc, {"values": values}))
                    data = json.loads(response.text)
                    self.assertEqual(response.status, 400)
                    self.assertIn(expected_error, data["error"])
                    self.assertEqual(svc.config, before_config)
                    self.assertEqual(svc.config_path.read_bytes(), before_file)

        asyncio.run(run())

    def test_replace_failure_rolls_back_runtime_and_keeps_original_file(self):
        svc = self.make_service()
        before_config = copy.deepcopy(svc.config)
        before_file = svc.config_path.read_bytes()
        svc.config["location"] = "不会落盘的城市"

        with patch("telegram_comfyui_selfie.state_runtime.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                svc.save_config()

        self.assertEqual(svc.config, before_config)
        self.assertEqual(svc.config_path.read_bytes(), before_file)
        self.assertEqual(list(svc.config_path.parent.glob(f".{svc.config_path.name}.*.tmp")), [])

    def test_web_replace_failure_returns_error_without_runtime_mutation(self):
        from telegram_comfyui_selfie.webui_models import api_save_config

        async def run():
            svc = self.make_service()
            before_config = copy.deepcopy(svc.config)
            before_file = svc.config_path.read_bytes()
            request = JsonRequest(svc, {"values": {"web_admin_username": "not-persisted"}})

            with patch("telegram_comfyui_selfie.state_runtime.os.replace", side_effect=OSError("replace failed")):
                response = await api_save_config(request)

            self.assertEqual(response.status, 500)
            self.assertIn("配置保存失败", json.loads(response.text)["error"])
            self.assertEqual(svc.config, before_config)
            self.assertEqual(svc.config_path.read_bytes(), before_file)
            self.assertEqual(list(svc.config_path.parent.glob(f".{svc.config_path.name}.*.tmp")), [])

        asyncio.run(run())

    def test_json_and_yaml_saves_use_same_directory_atomic_replace(self):
        from telegram_comfyui_selfie import TelegramComfyUIService
        from telegram_comfyui_selfie.config_store import flatten_config, load_simple_yaml

        real_fsync = os.fsync
        real_replace = os.replace
        for suffix in (".json", ".yml"):
            with self.subTest(suffix=suffix):
                root = make_project_temp_dir(f"atomic_config_{suffix[1:]}")
                config_path = root / f"config{suffix}"
                svc = TelegramComfyUIService(config_path, root / "state.json")
                svc.config["location"] = f"atomic-{suffix[1:]}"

                with (
                    patch("telegram_comfyui_selfie.state_runtime.os.fsync", wraps=real_fsync) as fsync_mock,
                    patch("telegram_comfyui_selfie.state_runtime.os.replace", wraps=real_replace) as replace_mock,
                ):
                    svc.save_config()

                fsync_mock.assert_called_once()
                replace_mock.assert_called_once()
                temp_path, target_path = replace_mock.call_args.args
                self.assertEqual(Path(temp_path).parent, config_path.parent)
                self.assertEqual(Path(target_path), config_path)
                self.assertEqual(list(config_path.parent.glob(f".{config_path.name}.*.tmp")), [])
                if suffix == ".json":
                    saved = json.loads(config_path.read_text(encoding="utf-8"))
                else:
                    saved = flatten_config(load_simple_yaml(config_path))
                self.assertEqual(saved["location"], f"atomic-{suffix[1:]}")

    def test_concurrent_web_saves_rebase_under_async_lock(self):
        from telegram_comfyui_selfie.webui_models import api_save_config

        async def run():
            svc = self.make_service()
            first = JsonRequest(svc, {"values": {"web_admin_username": "serial-user"}})
            second = JsonRequest(svc, {"values": {"web_public_host": "https://example.test"}})

            responses = await asyncio.gather(api_save_config(first), api_save_config(second))

            self.assertTrue(all(response.status == 200 for response in responses))
            self.assertEqual(svc.config["web_admin_username"], "serial-user")
            self.assertEqual(svc.config["web_public_host"], "https://example.test")
            persisted = json.loads(svc.config_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["web_admin_username"], "serial-user")
            self.assertEqual(persisted["web_public_host"], "https://example.test")

        asyncio.run(run())


class ConfigStoreTestCase(unittest.TestCase):
    """config_store YAML 解析器测试。"""

    def test_load_nested_model_profiles(self):
        from telegram_comfyui_selfie.config_store import load_simple_yaml, flatten_config

        yml = """
models:
  default_chat_model_profile: "deepseek-pro"
  global_model_profiles:
    deepseek-pro:
      name: "DeepSeek V4 Pro"
      api_key: "k"
      base_url: "https://opencode.ai/zen/go/v1"
      model: "deepseek-v4-pro"
      timeout: 300
      disable_thinking: false
      thinking_fixed: true
    glm:
      name: "GLM 5.2"
      api_key: "k"
      base_url: "https://opencode.ai/zen/go/v1"
      model: "glm-5.2"
      timeout: 300
      disable_thinking: true
      thinking_fixed: true
""".strip()
        path = Path(self.make_temp_dir()) / "config.yml"
        path.write_text(yml, encoding="utf-8")
        flat = flatten_config(load_simple_yaml(path))
        self.assertEqual(set(flat["global_model_profiles"].keys()), {"deepseek-pro", "glm"})
        self.assertTrue(flat["global_model_profiles"]["deepseek-pro"]["thinking_fixed"])
        self.assertTrue(flat["global_model_profiles"]["glm"]["disable_thinking"])

    def test_yaml_roundtrip_preserves_nested_dicts_and_literal_blocks(self):
        from telegram_comfyui_selfie.config_store import load_simple_yaml, flatten_config, dump_simple_yaml

        yml = """
role_defaults:
  outfit_keywords: |
    dress
    shirt
  current_style: "@00 gx4"
models:
  global_model_profiles:
    glm:
      name: "GLM 5.2"
      disable_thinking: true
""".strip()
        base = Path(self.make_temp_dir())
        path = base / "config.yml"
        path.write_text(yml, encoding="utf-8")
        loaded = load_simple_yaml(path)
        dumped = dump_simple_yaml(flatten_config(loaded))
        (base / "config2.yml").write_text(dumped, encoding="utf-8")
        rt = load_simple_yaml(base / "config2.yml")
        self.assertEqual(
            flatten_config(loaded)["global_model_profiles"],
            flatten_config(rt)["global_model_profiles"],
        )
        self.assertIn("\n", flatten_config(rt)["outfit_keywords"])

    def make_temp_dir(self) -> str:
        return str(make_project_temp_dir("config"))
