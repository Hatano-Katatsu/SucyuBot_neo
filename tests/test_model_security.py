from __future__ import annotations

import asyncio
import json
import socket
import unittest
from unittest.mock import AsyncMock

from telegram_comfyui_selfie.model_security import PublicOnlyResolver, validate_public_model_base_url
from tests.support import ServiceFixtureMixin


class FakeResolver:
    def __init__(self, addresses: list[str]):
        self.addresses = addresses
        self.closed = False

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        return [
            {"hostname": host, "host": address, "port": port, "family": family, "proto": 0, "flags": 0}
            for address in self.addresses
        ]

    async def close(self):
        self.closed = True


class JsonRequest(dict):
    def __init__(self, service, payload: dict):
        super().__init__(web_auth={"role": "user", "user_id": "123", "token": "test"})
        self.app = {"service": service}
        self.match_info = {"profile_id": "private"}
        self.query = {}
        self._payload = payload

    async def json(self):
        return dict(self._payload)


class ModelEndpointSecurityTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_static_validation_rejects_local_and_ambiguous_urls(self):
        invalid = (
            "http://127.0.0.1:8000/v1",
            "http://[::1]/v1",
            "http://169.254.169.254/latest",
            "https://localhost/v1",
            "https://model.internal/v1",
            "ftp://api.example.com/v1",
            "https://user:password@example.com/v1",
            "https://example.com/v1?target=internal",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_model_base_url(value)
        self.assertEqual(
            validate_public_model_base_url("https://api.example.com/v1/"),
            "https://api.example.com/v1",
        )

    def test_dns_resolver_rejects_any_non_public_result(self):
        async def run():
            unsafe = PublicOnlyResolver(FakeResolver(["93.184.216.34", "10.0.0.8"]))
            with self.assertRaisesRegex(OSError, "非公网"):
                await unsafe.resolve("api.example.com", 443)
            await unsafe.close()

            safe_delegate = FakeResolver(["93.184.216.34"])
            safe = PublicOnlyResolver(safe_delegate)
            results = await safe.resolve("api.example.com", 443)
            self.assertEqual(results[0]["host"], "93.184.216.34")
            await safe.close()
            self.assertTrue(safe_delegate.closed)

        asyncio.run(run())

    def test_private_profile_without_key_never_inherits_global_secret(self):
        svc = self.make_service()
        svc.config.update({
            "llm_api_key": "global-legacy-secret",
            "chat_llm_api_key": "global-chat-secret",
        })
        svc.app_store.upsert_model_profile("123", "private", {
            "base_url": "https://api.example.com/v1",
            "model": "private-model",
        })
        svc.app_store.update_user_model_settings("123", chat_profile_id="private")

        resolved = svc._resolved_llm_config("chat", "telegram:123")

        self.assertEqual(resolved["profile_scope"], "user")
        self.assertEqual(resolved["api_key"], "")
        self.assertFalse(svc.has_llm_config("chat", "telegram:123"))

    def test_web_profile_save_rejects_private_network_endpoint(self):
        from telegram_comfyui_selfie.webui_models import api_save_model_profile

        async def run():
            svc = self.make_service()
            response = await api_save_model_profile(JsonRequest(svc, {
                "base_url": "http://127.0.0.1:8080/v1",
                "api_key": "private-key",
                "model": "private-model",
            }))
            self.assertEqual(response.status, 400)
            self.assertIn("非公网", json.loads(response.text)["error"])
            self.assertNotIn("private", svc.app_store.list_model_profiles("123"))

        asyncio.run(run())

    def test_telegram_profile_save_rejects_private_network_endpoint(self):
        async def run():
            svc = self.make_service()
            svc.send_message = AsyncMock()
            payload = json.dumps({
                "base_url": "http://169.254.169.254/v1",
                "api_key": "private-key",
                "model": "private-model",
            })
            await svc.cmd_model(123, "telegram:123", f"add private {payload}")
            self.assertNotIn("private", svc.app_store.list_model_profiles("123"))
            self.assertIn("不安全", svc.send_message.await_args.args[1])

        asyncio.run(run())
