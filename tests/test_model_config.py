from __future__ import annotations

import asyncio
import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import ServiceFixtureMixin, make_project_temp_dir


class JsonRequest(dict):
    def __init__(self, service, payload: dict, *, match_info: dict | None = None, query: dict | None = None):
        super().__init__(web_auth={"role": "admin", "user_id": "admin", "token": "test"})
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

    def test_thinking_fixed_ignores_user_settings(self):
        async def run():
            svc = self.make_service()
            # 默认 chat=deepseek-pro（固定开）、fast=deepseek-flash（固定关）
            svc.app_store.update_user_model_settings(
                "1", chat_profile_id="deepseek-pro", chat_thinking=False,
                fast_profile_id="deepseek-flash", fast_thinking=True,
            )
            _, _, chat_thinking = svc._resolve_llm_profile("chat", "telegram:1")
            _, _, fast_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertTrue(chat_thinking, "deepseek-pro 思考固定开启，用户设置关闭应被忽略")
            self.assertFalse(fast_thinking, "deepseek-flash 思考固定关闭，用户设置开启应被忽略")

            # 切到 glm（固定关）
            svc.app_store.update_user_model_settings("1", fast_profile_id="glm", fast_thinking=True)
            _, _, glm_thinking = svc._resolve_llm_profile("image", "telegram:1")
            self.assertFalse(glm_thinking, "glm 思考固定关闭")

        asyncio.run(run())

    def test_custom_profile_uses_model_bound_thinking(self):
        async def run():
            svc = self.make_service()
            svc.app_store.upsert_model_profile("1", "custom", {
                "name": "Custom", "base_url": "http://localhost/v1", "api_key": "k",
                "model": "custom-model", "timeout": 120, "disable_thinking": True,
            })
            svc.app_store.update_user_model_settings("1", chat_profile_id="custom", chat_thinking=True)
            _, _, thinking = svc._resolve_llm_profile("chat", "telegram:1")
            self.assertFalse(thinking, "用户级 thinking 覆盖已移除，应完全跟随模型 profile")

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
