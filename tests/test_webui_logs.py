from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from telegram_comfyui_selfie.webui_logs import api_log_detail, tail_text_file
from tests.support import ServiceFixtureMixin


class WebUILogTailTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_tail_text_file_reads_last_utf8_lines_across_small_chunks(self):
        root = Path(self.make_service().config_path).parent
        path = root / "tail.log"
        path.write_text("".join(f"第{i:04d}行·内容\n" for i in range(300)), encoding="utf-8")

        result = tail_text_file(path, 7, chunk_size=1024)

        self.assertEqual(result["lines"], [f"第{i:04d}行·内容" for i in range(293, 300)])
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["total_lines"])

    def test_log_detail_offloads_reverse_tail_and_does_not_use_read_text(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:123"
            path = svc._user_log_path(sid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(f"line-{i:05d}\n" for i in range(20000)), encoding="utf-8")
            app = web.Application()
            app["service"] = svc
            request = make_mocked_request(
                "GET",
                "/api/logs/123?tail=5",
                app=app,
                match_info={"chat_id": "123"},
            )
            request["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}

            original_to_thread = asyncio.to_thread
            with patch("telegram_comfyui_selfie.webui_logs.asyncio.to_thread", wraps=original_to_thread) as offload:
                with patch.object(Path, "read_text", side_effect=AssertionError("tail 不应整文件 read_text")):
                    response = await api_log_detail(request)

            data = json.loads(response.text)
            self.assertTrue(data["ok"])
            self.assertEqual(data["shown_lines"], 5)
            self.assertTrue(data["truncated"])
            self.assertIsNone(data["total_lines"])
            self.assertEqual(data["content"].splitlines(), [f"line-{i:05d}" for i in range(19995, 20000)])
            offload.assert_awaited_once()

        asyncio.run(run())
