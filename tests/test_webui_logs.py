from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from telegram_comfyui_selfie.webui_logs import api_llm_debug_log, api_log_detail, tail_text_file
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

    def test_llm_debug_jsonl_api_uses_byte_cursor_pagination(self):
        async def run():
            svc = self.make_service()
            path = svc._llm_debug_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {"ts": index, "time": f"2026-07-20T10:00:0{index}", "type": "chat:reply", "index": index}
                for index in range(5)
            ]
            path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
                encoding="utf-8",
            )
            app = web.Application()
            app["service"] = svc

            def request_for(query: str):
                request = make_mocked_request("GET", f"/api/logs/llm-debug?{query}", app=app)
                request["web_auth"] = {"role": "admin", "user_id": "admin", "token": "x"}
                return request

            first = json.loads((await api_llm_debug_log(request_for("limit=2"))).text)
            self.assertEqual([item["index"] for item in first["content"]["chat:reply"]], [3, 4])
            self.assertTrue(first["has_more"])
            self.assertIsInstance(first["next_before"], int)

            second = json.loads((await api_llm_debug_log(
                request_for(f"limit=2&before={first['next_before']}")
            )).text)
            self.assertEqual([item["index"] for item in second["content"]["chat:reply"]], [1, 2])
            self.assertTrue(second["has_more"])

            third = json.loads((await api_llm_debug_log(
                request_for(f"limit=2&before={second['next_before']}")
            )).text)
            self.assertEqual([item["index"] for item in third["content"]["chat:reply"]], [0])
            self.assertFalse(third["has_more"])

        asyncio.run(run())

    def test_legacy_grouped_debug_log_migrates_with_backup(self):
        svc = self.make_service()
        legacy = svc._llm_debug_legacy_log_path()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({
            "entries_by_type": {
                "chat:reply": [
                    {"ts": 2, "type": "chat:reply", "value": "later"},
                    {"ts": 1, "type": "chat:reply", "value": "earlier"},
                ]
            }
        }), encoding="utf-8")

        svc._migrate_legacy_llm_debug_log()

        path = svc._llm_debug_log_path()
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["value"] for item in entries], ["earlier", "later"])
        self.assertFalse(legacy.exists())
        self.assertTrue((legacy.parent / "llm_debug.legacy.json").exists())
