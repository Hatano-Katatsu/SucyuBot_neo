from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


MIB = 1024 * 1024
MAX_CONFIGURED_RESPONSE_BYTES = 512 * MIB
DEFAULT_RESPONSE_LIMITS: dict[str, int] = {
    "llm_json": 16 * MIB,
    "telegram_json": 4 * MIB,
    "telegram_file": 20 * MIB,
    "comfy_json": 16 * MIB,
    "generated_image": 64 * MIB,
    "weather_json": 2 * MIB,
    "places_json": 4 * MIB,
    "search_json": 4 * MIB,
    "error_text": 64 * 1024,
}


class HTTPResponseLimitError(RuntimeError):
    """外部 HTTP 响应超过允许的字节数。"""

    def __init__(
        self,
        label: str,
        limit: int,
        *,
        declared: int | None = None,
        received: int | None = None,
    ) -> None:
        self.label = label
        self.limit = limit
        self.declared = declared
        self.received = received
        if declared is not None:
            detail = f"Content-Length={declared}"
        elif received is not None:
            detail = f"已接收至少 {received} 字节"
        else:
            detail = "响应体过大"
        super().__init__(f"{label}超过大小上限 {limit} 字节（{detail}）")


class HTTPResponseDecodeError(RuntimeError):
    """外部 HTTP 响应无法按预期格式解码。"""


def response_limit(config: Mapping[str, Any] | None, category: str) -> int:
    """解析响应上限；分类配置优先，其次全局配置，非法值回退安全默认值。"""

    default = DEFAULT_RESPONSE_LIMITS.get(category, 4 * MIB)
    if not isinstance(config, Mapping):
        return default

    nested = config.get("http_response_limits")
    raw: Any = None
    if isinstance(nested, Mapping):
        raw = nested.get(category)
    if raw in (None, ""):
        raw = config.get(f"http_response_{category}_max_bytes")
    if raw in (None, ""):
        raw = config.get("http_response_max_bytes")
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if value <= 0:
        return default
    return min(value, MAX_CONFIGURED_RESPONSE_BYTES)


def _declared_content_length(response: Any) -> int | None:
    value = getattr(response, "content_length", None)
    if not isinstance(value, (int, str)):
        headers = getattr(response, "headers", None)
        value = headers.get("Content-Length") if isinstance(headers, Mapping) else None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _check_declared_size(response: Any, max_bytes: int, label: str) -> None:
    declared = _declared_content_length(response)
    if declared is not None and declared > max_bytes:
        raise HTTPResponseLimitError(label, max_bytes, declared=declared)


def _validate_limit(max_bytes: int) -> int:
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_bytes 必须是正整数") from exc
    if limit <= 0:
        raise ValueError("max_bytes 必须是正整数")
    return limit


def _check_body_size(data: bytes, max_bytes: int, label: str) -> bytes:
    if len(data) > max_bytes:
        raise HTTPResponseLimitError(label, max_bytes, received=len(data))
    return data


def _has_stream_reader(response: Any) -> bool:
    content = getattr(response, "content", None)
    return callable(getattr(content, "iter_chunked", None))


async def read_limited_bytes(
    response: Any,
    max_bytes: int,
    *,
    label: str = "HTTP 二进制响应",
    chunk_size: int = 64 * 1024,
) -> bytes:
    """在分配完整响应体前检查 Content-Length，并对流式内容累计计数。"""

    limit = _validate_limit(max_bytes)
    _check_declared_size(response, limit, label)

    content = getattr(response, "content", None)
    iter_chunked = getattr(content, "iter_chunked", None)
    if callable(iter_chunked):
        iterator = iter_chunked(max(1, min(int(chunk_size or 1), limit + 1)))
        if hasattr(iterator, "__aiter__"):
            chunks: list[bytes] = []
            received = 0
            async for chunk in iterator:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise HTTPResponseDecodeError(f"{label}返回了非二进制数据块")
                part = bytes(chunk)
                received += len(part)
                if received > limit:
                    raise HTTPResponseLimitError(label, limit, received=received)
                chunks.append(part)
            return b"".join(chunks)

    read = getattr(response, "read", None)
    if not callable(read):
        raise HTTPResponseDecodeError(f"{label}不支持二进制响应读取")
    data = await read()
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise HTTPResponseDecodeError(f"{label}返回的响应体不是二进制数据")
    return _check_body_size(bytes(data), limit, label)


async def _fallback_json(response: Any, max_bytes: int, label: str) -> Any:
    method = getattr(response, "json", None)
    if not callable(method):
        raise HTTPResponseDecodeError(f"{label}不支持 JSON 响应读取")
    try:
        data = await method(content_type=None)
    except TypeError:
        # 兼容现有只实现 json() 的轻量测试桩。
        data = await method()
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPResponseDecodeError(f"{label}返回了不可序列化的 JSON 数据") from exc
    _check_body_size(encoded, max_bytes, label)
    return data


async def read_limited_json(
    response: Any,
    max_bytes: int,
    *,
    label: str = "HTTP JSON 响应",
) -> Any:
    """读取有限大小的 JSON；真实响应走字节流，轻量测试桩保留兼容路径。"""

    limit = _validate_limit(max_bytes)
    _check_declared_size(response, limit, label)
    if not _has_stream_reader(response) and callable(getattr(response, "json", None)):
        return await _fallback_json(response, limit, label)
    raw = await read_limited_bytes(response, limit, label=label)
    if not raw and callable(getattr(response, "json", None)):
        # 部分既有测试桩同时提供空 read() 与独立 json() 数据。
        return await _fallback_json(response, limit, label)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPResponseDecodeError(f"{label}不是有效的 UTF-8 JSON") from exc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPResponseDecodeError(f"{label}不是有效的 JSON") from exc


async def read_limited_text(
    response: Any,
    max_bytes: int,
    *,
    label: str = "HTTP 文本响应",
) -> str:
    """读取有限大小的文本响应，并使用响应声明的字符集解码。"""

    limit = _validate_limit(max_bytes)
    _check_declared_size(response, limit, label)
    if not _has_stream_reader(response) and callable(getattr(response, "text", None)):
        method = getattr(response, "text", None)
        if not callable(method):
            raise HTTPResponseDecodeError(f"{label}不支持文本响应读取")
        text = await method()
        if not isinstance(text, str):
            raise HTTPResponseDecodeError(f"{label}返回的响应体不是文本")
        _check_body_size(text.encode("utf-8"), limit, label)
        return text

    raw = await read_limited_bytes(response, limit, label=label)
    charset = getattr(response, "charset", None) or "utf-8"
    try:
        return raw.decode(str(charset))
    except (LookupError, UnicodeDecodeError) as exc:
        raise HTTPResponseDecodeError(f"{label}无法使用字符集 {charset!r} 解码") from exc
