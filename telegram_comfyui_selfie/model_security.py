from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver


_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _require_public_ip(value: str) -> None:
    """拒绝所有非公网地址，包含环回、私网、链路本地与保留地址。"""
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError as exc:
        raise ValueError(f"模型地址解析结果不是有效 IP: {value}") from exc
    if not address.is_global:
        raise ValueError(f"模型地址不能指向非公网 IP: {address}")


def validate_public_model_base_url(value: Any) -> str:
    """校验普通用户模型端点的静态 URL 边界，并返回规范化字符串。"""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("模型 Base URL 不能为空")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("模型 Base URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("模型 Base URL 仅允许 http 或 https")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("模型 Base URL 缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("模型 Base URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("模型 Base URL 不能包含查询参数或片段")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("模型 Base URL 端口无效")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        raise ValueError("模型 Base URL 不能指向本地主机")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        _require_public_ip(host)
    return raw.rstrip("/")


class PublicOnlyResolver(AbstractResolver):
    """在实际建连所用的 DNS 解析结果上执行公网地址校验。"""

    def __init__(self, delegate: AbstractResolver | None = None):
        self._delegate = delegate or DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        results = await self._delegate.resolve(host, port, family)
        if not results:
            raise OSError(f"模型主机无法解析: {host}")
        try:
            for result in results:
                _require_public_ip(str(result.get("host") or ""))
        except ValueError as exc:
            raise OSError(str(exc)) from exc
        return results

    async def close(self) -> None:
        await self._delegate.close()
