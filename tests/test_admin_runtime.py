from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

from tests.support import ServiceFixtureMixin, make_mock_request


class GitUpdatePermissionTestCase(ServiceFixtureMixin, unittest.TestCase):
    """TODO #9: Git 更新权限测试 — 仅管理员可触发。"""

    def test_is_admin_chat_uses_admin_chat_ids_first(self):
        svc = self.make_service()
        svc.config["admin_chat_ids"] = ["111", "222"]
        svc.config["allowed_chat_ids"] = ["333"]
        self.assertTrue(svc._is_admin_chat(111))
        self.assertTrue(svc._is_admin_chat("222"))
        self.assertFalse(svc._is_admin_chat(333))  # 在 allowed 但不在 admin

    def test_is_admin_chat_falls_back_to_allowed_when_admin_empty(self):
        svc = self.make_service()
        svc.config["admin_chat_ids"] = []
        svc.config["allowed_chat_ids"] = ["444"]
        self.assertTrue(svc._is_admin_chat(444))  # 回退到 allowed
        self.assertFalse(svc._is_admin_chat(999))

    def test_git_proxy_env_converts_socks5_to_socks5h(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "socks5://127.0.0.1:7891"
        env = svc._git_proxy_env()
        self.assertEqual(env.get("ALL_PROXY"), "socks5h://127.0.0.1:7891")

    def test_git_proxy_env_http_proxy(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "http://127.0.0.1:7890"
        env = svc._git_proxy_env()
        self.assertEqual(env.get("HTTP_PROXY"), "http://127.0.0.1:7890")
        self.assertEqual(env.get("HTTPS_PROXY"), "http://127.0.0.1:7890")

    def test_git_update_rejects_non_admin(self):
        async def run():
            svc = self.make_service()
            svc.config["admin_chat_ids"] = ["111"]
            svc.send_message = AsyncMock()
            await svc.cmd_git_update(999, "telegram:999", "")
            msg = svc.send_message.await_args.args[1]
            self.assertIn("无权限", msg)
        asyncio.run(run())


class ExternalProxyTestCase(ServiceFixtureMixin, unittest.TestCase):
    """外部 POI 请求复用 Telegram 代理配置。"""

    def test_external_http_proxy_disabled(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = False
        proxy, connector = svc._external_http_proxy()
        self.assertIsNone(proxy)
        self.assertIsNone(connector)

    def test_external_http_proxy_http(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "http://127.0.0.1:7890"
        proxy, connector = svc._external_http_proxy()
        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertIsNone(connector)

    def test_external_http_proxy_socks(self):
        svc = self.make_service()
        svc.config["telegram_proxy_enabled"] = True
        svc.config["telegram_proxy_url"] = "socks5://127.0.0.1:7891"
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            self.skipTest("aiohttp_socks not installed")
        async def _run():
            return svc._external_http_proxy()
        proxy, connector = asyncio.run(_run())
        self.assertIsNone(proxy)
        self.assertIsInstance(connector, ProxyConnector)


class LLMUsageTestCase(ServiceFixtureMixin, unittest.TestCase):
    """LLM usage 记录与看板接口测试。"""

    def test_record_usage_from_response_with_cache_hit_tokens(self):
        svc = self.make_service()
        resolved = {
            "profile_id": "deepseek",
            "model": "deepseek-v4",
            "api_key": "k",
        }
        data = {
            "usage": {
                "prompt_tokens": 1234,
                "completion_tokens": 567,
                "total_tokens": 1801,
                "prompt_cache_hit_tokens": 1000,
                "prompt_cache_miss_tokens": 234,
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="plan", purpose="image", session_id="telegram:1")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model", "purpose", "tag"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["profile_id"], "deepseek")
        self.assertEqual(row["model"], "deepseek-v4")
        self.assertEqual(row["purpose"], "image")
        self.assertEqual(row["tag"], "plan")
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["prompt_tokens"], 1234)
        self.assertEqual(row["completion_tokens"], 567)
        self.assertEqual(row["cached_tokens"], 1000)
        self.assertEqual(row["total_tokens"], 1801)

    def test_record_usage_from_response_with_cached_tokens_fallback(self):
        svc = self.make_service()
        resolved = {"profile_id": "", "model": "gpt-4o", "api_key": "k"}
        data = {
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 200,
                "prompt_cached_tokens": 600,
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="chat", purpose="chat")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model", "purpose", "tag"))
        row = next(r for r in rows if r["tag"] == "chat")
        self.assertEqual(row["cached_tokens"], 600)
        self.assertEqual(row["total_tokens"], 1000)

    def test_record_usage_from_response_with_prompt_tokens_details_cached(self):
        svc = self.make_service()
        resolved = {"profile_id": "mimo", "model": "mimo-v2.5-pro", "api_key": "k"}
        data = {
            "usage": {
                "prompt_tokens": 5293,
                "completion_tokens": 291,
                "total_tokens": 5584,
                "prompt_tokens_details": {
                    "cached_tokens": 4096,
                    "cache_write_tokens": 0,
                },
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="chat", purpose="chat", session_id="telegram:1")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model", "purpose", "tag"))
        row = rows[0]
        self.assertEqual(row["cached_tokens"], 4096)
        self.assertEqual(row["prompt_tokens"], 5293)

        summary = svc._llm_usage_debug_summary(data)
        self.assertEqual(summary["cached_tokens"], 4096)
        self.assertEqual(summary["cache_hit_rate"], round(4096 / 5293, 4))

    def test_record_usage_from_response_cache_miss_inference(self):
        svc = self.make_service()
        resolved = {"profile_id": "ds", "model": "deepseek-chat", "api_key": "k"}
        data = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_cache_miss_tokens": 300,
            }
        }
        svc._record_llm_usage_from_response(data, resolved, tag="translate", purpose="image")
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id", "model"))
        row = rows[0]
        self.assertEqual(row["cached_tokens"], 700)
        self.assertEqual(row["prompt_tokens"], 1000)

    def test_aggregate_usage_by_time_range(self):
        svc = self.make_service()
        now = time.time()
        svc.app_store.record_llm_usage(profile_id="p1", model="m1", purpose="chat", tag="reply", prompt_tokens=100, completion_tokens=50, total_tokens=150)
        svc.app_store.record_llm_usage(profile_id="p1", model="m1", purpose="chat", tag="reply", prompt_tokens=200, completion_tokens=100, total_tokens=300)
        svc.app_store.record_llm_usage(profile_id="p2", model="m2", purpose="image", tag="plan", prompt_tokens=300, completion_tokens=50, total_tokens=350)
        rows = svc.app_store.aggregate_llm_usage(after=now - 60, before=now + 60, group_by=("profile_id", "purpose"))
        self.assertEqual(len(rows), 2)
        p1 = next(r for r in rows if r["profile_id"] == "p1")
        self.assertEqual(p1["requests"], 2)
        self.assertEqual(p1["total_tokens"], 450)
        p2 = next(r for r in rows if r["profile_id"] == "p2")
        self.assertEqual(p2["requests"], 1)
        self.assertEqual(p2["total_tokens"], 350)
        # 过滤旧数据
        old_rows = svc.app_store.aggregate_llm_usage(after=now + 10, before=now + 60)
        self.assertEqual(old_rows, [])

    def test_cache_hit_rate_calculation(self):
        svc = self.make_service()
        svc.app_store.record_llm_usage(profile_id="p", model="m", purpose="chat", tag="t", prompt_tokens=1000, cached_tokens=250, total_tokens=1200)
        rows = svc.app_store.aggregate_llm_usage(after=0, group_by=("profile_id",))
        self.assertEqual(rows[0]["cached_tokens"], 250)
        self.assertEqual(rows[0]["prompt_tokens"], 1000)

    def test_webui_llm_usage_requires_admin(self):
        from aiohttp import web
        from telegram_comfyui_selfie.webui import api_admin_llm_usage

        async def run():
            svc = self.make_service()
            app = web.Application()
            app["service"] = svc
            req = make_mock_request(app, "/api/admin/llm-usage", method="GET")
            # 非管理员请求应 403
            with self.assertRaises(web.HTTPForbidden):
                await api_admin_llm_usage(req)

        asyncio.run(run())

    def test_webui_llm_usage_returns_summary(self):
        from aiohttp import web
        from telegram_comfyui_selfie.webui import api_admin_llm_usage

        async def run():
            svc = self.make_service()
            svc.app_store.record_llm_usage(profile_id="p", model="m", purpose="chat", tag="t", prompt_tokens=100, completion_tokens=50, cached_tokens=20, total_tokens=150)
            app = web.Application()
            app["service"] = svc
            req = make_mock_request(app, "/api/admin/llm-usage", method="GET", admin=True)
            resp = await api_admin_llm_usage(req)
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.text)
            self.assertTrue(data.get("ok"))
            summary = data.get("summary", {})
            self.assertEqual(summary.get("requests"), 1)
            self.assertEqual(summary.get("prompt_tokens"), 100)
            self.assertEqual(summary.get("cached_tokens"), 20)
            self.assertEqual(summary.get("cache_hit_rate"), 0.2)
            groups = data.get("groups", [])
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].get("profile_id"), "p")

        asyncio.run(run())

    def test_llm_debug_records_are_buffered_then_appended_as_jsonl(self):
        svc = self.make_service()
        path = svc._llm_debug_log_path()
        resolved = {
            "profile_id": "debug-profile",
            "model": "debug-model",
            "thinking": False,
        }

        def record(index: int, tag: str = "reply"):
            svc._record_llm_debug(
                purpose="chat",
                tag=tag,
                session_id="telegram:1",
                resolved=resolved,
                request_url="https://example.invalid/v1/chat/completions",
                request_body={
                    "model": "debug-model",
                    "messages": [{"role": "user", "content": f"message-{index}"}],
                },
                response={
                    "choices": [{"message": {"content": f"response-{index}"}}],
                    "usage": {
                        "prompt_tokens": 100 + index,
                        "completion_tokens": 10,
                        "total_tokens": 110 + index,
                        "prompt_cache_hit_tokens": 80 + index,
                    },
                },
                status=200,
            )

        for i in range(9):
            record(i)
        self.assertFalse(path.exists(), "不足 10 条时不应落盘，避免频繁 IO")

        record(9)
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[0]["request"]["body"]["messages"][0]["content"], "message-0")
        self.assertEqual(entries[-1]["response"]["choices"][0]["message"]["content"], "response-9")
        self.assertEqual(entries[-1]["usage"]["cached_tokens"], 89)
        self.assertEqual(path.suffix, ".jsonl")

        for i in range(10, 20):
            record(i)
        entries2 = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("type") == "chat:reply"
        ]
        self.assertEqual(len(entries2), 20)
        entries2 = entries2[-10:]
        self.assertEqual(len(entries2), 10)
        self.assertEqual(entries2[0]["request"]["body"]["messages"][0]["content"], "message-10")
        self.assertEqual(entries2[-1]["request"]["body"]["messages"][0]["content"], "message-19")

        record(20, tag="scene")
        svc._flush_llm_debug(force=True)
        entries3 = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertIn("chat:reply", {entry["type"] for entry in entries3})
        scene = [entry for entry in entries3 if entry["type"] == "chat:scene"][-1]
        self.assertEqual(scene["request"]["body"]["messages"][0]["content"], "message-20")
