from __future__ import annotations

import asyncio
import base64
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.character_artifacts import wardrobe_preview_dir
from telegram_comfyui_selfie.webui_wardrobe_preview import api_wardrobe_preview, api_wardrobe_preview_image, preview_snapshot
from tests.support import ServiceFixtureMixin

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class WardrobePreviewTestCase(ServiceFixtureMixin, unittest.TestCase):
    def setup_preview(self):
        svc = self.make_service()
        sid = 'telegram:123'
        state = svc._get_session_state(sid)
        session_schema.set_character_value(state, 'custom_character', '试衣角色')
        session_schema.set_character_value(state, 'custom_positive_prefix', '1girl, black hair, brown eyes')
        session_schema.set_wardrobe(state, {'dress': 'red dress', 'footwear': 'white shoes'})
        session_schema.set_outfit(state, 'red dress, white shoes')
        svc._ensure_comfy_session = lambda: None
        return svc, sid, state

    def request(self, svc, sid, method='GET', query='', key='', authorized=True):
        app = web.Application()
        app['service'] = svc
        req = make_mocked_request(method, '/api/preview?character_key=试衣角色' + query, app=app,
                                  match_info={'session_id': sid, 'preview_key': key})
        if authorized:
            req['web_auth'] = {'role': 'admin', 'user_id': 'admin'}
        else:
            req['web_auth'] = {'role': 'user', 'user_id': '999'}
        return req

    def test_cache_returns_saved_outfit_and_does_not_repeat_generation(self):
        async def run():
            svc, sid, state = self.setup_preview()
            before = copy.deepcopy(state)
            async def generate(preview, scene, **kw):
                self.assertIsNot(preview.sessions[sid], state)
                self.assertEqual(kw['orientation'], '2:3')
                preview.sessions[sid]['test_mutation'] = True
                preview._last_generated_nltag = 'preview only'
                return True, [PNG], ''
            mock = AsyncMock(side_effect=generate)
            with patch('telegram_comfyui_selfie.webui_wardrobe_preview.generation.do_generate_locked', mock):
                descriptor = json.loads((await api_wardrobe_preview(self.request(svc, sid))).text)['preview']
                self.assertFalse(descriptor['cached'])
                for _ in range(2):
                    response = await api_wardrobe_preview(self.request(svc, sid, 'POST', '&key='+descriptor['key']))
                    self.assertEqual(response.status, 200)
                    saved = json.loads(response.text)['preview']
                    self.assertTrue(saved['cached'])
                self.assertEqual(mock.await_count, 1)
            self.assertEqual(state, before)
            self.assertFalse(svc._generating)
            self.assertNotEqual(getattr(svc, '_last_generated_nltag', None), 'preview only')
            self.assertEqual(svc.app_store.list_messages(sid, '试衣角色'), [])
            path = wardrobe_preview_dir(svc, sid, '试衣角色') / (saved['key'] + '.image')
            self.assertEqual(path.read_bytes(), PNG)
            # 缓存存在于磁盘，读取不依赖上次请求的内存对象。
            fresh = copy.copy(svc)
            loaded = json.loads((await api_wardrobe_preview(self.request(fresh, sid))).text)['preview']
            self.assertEqual(loaded['image_url'], saved['image_url'])
            image = await api_wardrobe_preview_image(self.request(svc, sid, key=saved['key']))
            self.assertIsInstance(image, web.FileResponse)
            self.assertEqual(image.headers['Content-Type'], 'image/png')
        asyncio.run(run())

    def test_outfit_state_style_and_character_isolation(self):
        svc, sid, state = self.setup_preview()
        original = copy.deepcopy(state)
        key = preview_snapshot(svc, sid)[1]
        session_schema.set_wardrobe(state, {'dress': 'blue dress'})
        session_schema.set_outfit(state, 'blue dress')
        self.assertNotEqual(preview_snapshot(svc, sid)[1], key)
        svc.sessions[sid] = copy.deepcopy(original)
        self.assertEqual(preview_snapshot(svc, sid)[1], key)
        session_schema.set_wardrobe_item_state(svc.sessions[sid], 'dress', 'removed')
        self.assertNotEqual(preview_snapshot(svc, sid)[1], key)
        svc.sessions[sid] = copy.deepcopy(original)
        svc._set_current_style(sid, '@different_artist')
        self.assertNotEqual(preview_snapshot(svc, sid)[1], key)
        self.assertNotEqual(wardrobe_preview_dir(svc, sid, 'a/b'), wardrobe_preview_dir(svc, sid, 'a_b'))
        self.assertNotEqual(wardrobe_preview_dir(svc, sid, 'x'), wardrobe_preview_dir(svc, 'telegram:456', 'x'))

    def test_unauthorized_stale_character_and_stale_outfit_do_not_generate(self):
        async def run():
            svc, sid, state = self.setup_preview()
            with patch('telegram_comfyui_selfie.webui_wardrobe_preview.generation.do_generate_locked', AsyncMock()) as generate:
                self.assertEqual((await api_wardrobe_preview(self.request(svc, sid, authorized=False))).status, 403)
                self.assertEqual((await api_wardrobe_preview_image(self.request(svc, sid, key='a'*64, authorized=False))).status, 403)
                self.assertEqual((await api_wardrobe_preview(self.request(svc, sid, 'POST', '&key=outdated'))).status, 409)
                session_schema.set_character_value(state, 'custom_character', '另一个角色')
                self.assertEqual((await api_wardrobe_preview(self.request(svc, sid))).status, 409)
                generate.assert_not_awaited()
        asyncio.run(run())

    def test_failed_refresh_preserves_saved_preview_and_unlocks(self):
        async def run():
            svc, sid, state = self.setup_preview()
            key = preview_snapshot(svc, sid)[1]
            path = wardrobe_preview_dir(svc, sid, '试衣角色') / (key + '.image')
            path.parent.mkdir(parents=True)
            path.write_bytes(PNG)
            for response in [(False, [], 'upstream unavailable'), (True, [b'<html>error</html>'], '')]:
                with patch('telegram_comfyui_selfie.webui_wardrobe_preview.generation.do_generate_locked', AsyncMock(return_value=response)):
                    self.assertEqual((await api_wardrobe_preview(self.request(svc, sid, 'POST', '&key='+key+'&refresh=1'))).status, 502)
                self.assertEqual(path.read_bytes(), PNG)
                self.assertFalse(svc._generating)
                self.assertFalse(svc._gen_lock.locked())
        asyncio.run(run())

    def test_preview_files_participate_in_character_and_session_deletion(self):
        svc, sid, state = self.setup_preview()
        self.assertIn(wardrobe_preview_dir(svc, sid, '试衣角色'), svc._character_artifact_paths(sid, '试衣角色', '试衣角色'))
        self.assertIn(wardrobe_preview_dir(svc, sid), svc._session_artifact_paths(sid))
