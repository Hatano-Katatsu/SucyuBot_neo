from __future__ import annotations

from typing import Any

from aiohttp import web

from . import session_schema


def character_value(state: dict[str, Any], key: str, default: Any = "") -> Any:
    return session_schema.get_character_value(state, key, default)


def service_from(request: web.Request):
    return request.app["service"]


def json_ok(data: dict[str, Any] | None = None):
    payload = {"ok": True}
    if data:
        payload.update(data)
    return web.json_response(payload)


def json_error(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": message}, status=status)


def is_admin(request: web.Request) -> bool:
    return (request.get("web_auth") or {}).get("role") == "admin"


def session_allowed(request: web.Request, session_id: str) -> bool:
    if is_admin(request):
        return True
    auth = request.get("web_auth") or {}
    return auth.get("user_id") == service_from(request)._user_id_for_session(session_id)


def require_admin(request: web.Request):
    if not is_admin(request):
        raise web.HTTPForbidden(text="需要管理员权限")


def human_ago(seconds: float) -> str:
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"
