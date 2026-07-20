from __future__ import annotations

import asyncio
import unittest

from telegram_comfyui_selfie.http_limits import (
    HTTPResponseDecodeError,
    HTTPResponseLimitError,
    MAX_CONFIGURED_RESPONSE_BYTES,
    read_limited_bytes,
    read_limited_json,
    read_limited_text,
    response_limit,
)


class _ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.started = False

    def iter_chunked(self, _size: int):
        async def iterate():
            self.started = True
            for chunk in self.chunks:
                yield chunk

        return iterate()


class _StreamingResponse:
    def __init__(self, chunks: list[bytes], *, content_length: str | None = None) -> None:
        self.content = _ChunkStream(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length


class _ReadResponse:
    headers: dict[str, str] = {}

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self) -> bytes:
        return self.data


class _LegacyJSONResponse:
    headers: dict[str, str] = {}

    async def json(self):
        return {"ok": True}


class HTTPResponseLimitTests(unittest.TestCase):
    def test_content_length_over_limit_is_rejected_before_streaming(self):
        async def run():
            response = _StreamingResponse([b"ignored"], content_length="11")
            with self.assertRaisesRegex(HTTPResponseLimitError, "Content-Length=11"):
                await read_limited_bytes(response, 10, label="测试二进制响应")
            self.assertFalse(response.content.started)

        asyncio.run(run())

    def test_chunked_response_is_rejected_when_accumulated_size_exceeds_limit(self):
        async def run():
            response = _StreamingResponse([b"1234", b"5678", b"90"])
            with self.assertRaisesRegex(HTTPResponseLimitError, "已接收至少 10 字节"):
                await read_limited_bytes(response, 8, label="分块响应")

        asyncio.run(run())

    def test_normal_json_is_decoded_from_limited_stream(self):
        async def run():
            response = _StreamingResponse([b'{"ok":', b'true,"name":"bot"}'])
            payload = await read_limited_json(response, 128, label="JSON 响应")
            self.assertEqual(payload, {"ok": True, "name": "bot"})

        asyncio.run(run())

    def test_normal_binary_response_is_returned(self):
        async def run():
            response = _ReadResponse(b"\x89PNG\r\n")
            payload = await read_limited_bytes(response, 64, label="图片响应")
            self.assertEqual(payload, b"\x89PNG\r\n")

        asyncio.run(run())

    def test_existing_json_only_test_double_remains_supported(self):
        async def run():
            payload = await read_limited_json(_LegacyJSONResponse(), 64)
            self.assertEqual(payload, {"ok": True})

        asyncio.run(run())

    def test_invalid_json_and_text_charset_have_clear_errors(self):
        async def run():
            with self.assertRaisesRegex(HTTPResponseDecodeError, "不是有效的 JSON"):
                await read_limited_json(_ReadResponse(b"not-json"), 64, label="坏 JSON")

            response = _ReadResponse(b"\xff")
            response.charset = "utf-8"
            with self.assertRaisesRegex(HTTPResponseDecodeError, "无法使用字符集"):
                await read_limited_text(response, 64, label="坏文本")

        asyncio.run(run())

    def test_configured_limits_support_nested_flat_and_safe_fallbacks(self):
        self.assertEqual(response_limit({"http_response_limits": {"llm_json": "123"}}, "llm_json"), 123)
        self.assertEqual(response_limit({"http_response_llm_json_max_bytes": 456}, "llm_json"), 456)
        self.assertEqual(response_limit({"http_response_max_bytes": 789}, "llm_json"), 789)
        self.assertEqual(response_limit({"http_response_max_bytes": 0}, "llm_json"), 16 * 1024 * 1024)
        self.assertEqual(
            response_limit({"http_response_max_bytes": MAX_CONFIGURED_RESPONSE_BYTES + 1}, "llm_json"),
            MAX_CONFIGURED_RESPONSE_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
