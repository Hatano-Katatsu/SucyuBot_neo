from __future__ import annotations

import asyncio
import json
import unittest

from aiohttp import web

from telegram_comfyui_selfie.webui import api_auth_login, login_page, web_login
from tests.support import ServiceFixtureMixin


class _JsonRequestStub(dict):
    def __init__(self, app, payload):
        super().__init__()
        self.app = app
        self._payload = payload

    async def json(self):
        return self._payload


class _FormRequestStub(dict):
    def __init__(self, app, data):
        super().__init__()
        self.app = app
        self._data = data

    async def post(self):
        return self._data


class WebUIAuthTestCase(ServiceFixtureMixin, unittest.TestCase):
    def _app_with_service(self):
        svc = self.make_service()
        app = web.Application()
        app["service"] = svc
        return svc, app

    def test_api_auth_login_admin_success_returns_json_and_short_cookie(self):
        async def run():
            svc, app = self._app_with_service()
            resp = await api_auth_login(_JsonRequestStub(app, {"username": "admin", "password": "admin"}))

            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.content_type, "application/json")
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["role"], "admin")
            cookie = resp.cookies["web_session"]
            self.assertEqual(cookie["max-age"], str(24 * 3600))
            self.assertIn(cookie.value, svc._web_admin_sessions)

        asyncio.run(run())

    def test_api_auth_login_user_success_uses_persistent_long_cookie(self):
        async def run():
            svc, app = self._app_with_service()
            svc.app_store.set_web_password("123456", "pw-123456")
            resp = await api_auth_login(_JsonRequestStub(app, {"username": "123456", "password": "pw-123456"}))

            self.assertEqual(resp.status, 200)
            data = json.loads(resp.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["role"], "user")
            cookie = resp.cookies["web_session"]
            self.assertEqual(cookie["max-age"], str(365 * 24 * 3600))
            self.assertEqual(svc.app_store.user_for_token(cookie.value), "123456")

        asyncio.run(run())

    def test_api_auth_login_failure_returns_json_401(self):
        async def run():
            svc, app = self._app_with_service()
            resp = await api_auth_login(_JsonRequestStub(app, {"username": "admin", "password": "wrong"}))

            self.assertEqual(resp.status, 401)
            self.assertEqual(resp.content_type, "application/json")
            data = json.loads(resp.text)
            self.assertFalse(data["ok"])
            self.assertEqual(data["error"], "账号或密码错误")
            self.assertNotIn("web_session", resp.cookies)
            self.assertFalse(getattr(svc, "_web_admin_sessions", set()))

        asyncio.run(run())

    def test_web_login_form_success_still_redirects_with_cookie(self):
        async def run():
            svc, app = self._app_with_service()
            resp = await web_login(_FormRequestStub(app, {"username": "admin", "password": "admin"}))

            self.assertEqual(resp.status, 302)
            self.assertEqual(resp.headers["Location"], "/")
            self.assertIn(resp.cookies["web_session"].value, svc._web_admin_sessions)

        asyncio.run(run())

    def test_web_login_form_failure_rerenders_login_page_with_error(self):
        async def run():
            svc, app = self._app_with_service()
            resp = await web_login(_FormRequestStub(app, {"username": "admin", "password": "wrong"}))

            self.assertEqual(resp.status, 401)
            self.assertIn('class="login-error"', resp.text)
            self.assertIn("账号或密码错误", resp.text)

        asyncio.run(run())

    def test_login_page_hides_error_element_and_submits_via_fetch(self):
        async def run():
            resp = await login_page(None)

            self.assertEqual(resp.status, 200)
            self.assertIn('class="login-error" hidden', resp.text)
            self.assertIn('fetch("/api/auth/login"', resp.text)
            self.assertIn('action="/login"', resp.text)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
