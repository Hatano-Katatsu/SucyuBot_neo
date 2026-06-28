from __future__ import annotations

import asyncio
import json
import secrets
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import session_schema
from .commands import SESSION_CUSTOM_RESET_KEYS
from .world_runtime import PLACE_TYPES


SECRET_KEYS = {
    "telegram_bot_token", "llm_api_key", "chat_llm_api_key", "image_llm_api_key",
    "amap_api_key", "google_places_api_key",
}
MODEL_SECRET_PLACEHOLDER = "********"
MODEL_SECRET_KEYS = {"api_key", "api_key_no_think"}
FEEDBACK_MAX_LENGTH = 6000
YAML_ONLY_CONFIG_KEYS = {
    "comfyui_url", "image_backend", "animatool_turbo_steps", "animatool_turbo_cfg",
    "animatool_filename_prefix", "unet_model", "clip_model", "vae_model",
    "turbo_lora_model", "comfyui_workflow_file", "steps", "cfg",
    # 全局模型 profile 走专用模型接口，避免通用配置表单把嵌套 dict 字符串化。
    "global_model_profiles",
    # 基础设施/运维配置，不允许 WebUI 修改
    "long_memory_db_path", "user_log_enabled", "user_log_dir",
    "web_enabled", "web_host", "web_port",
}
WORLD_TIMELINE_HOURS = (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)


def character_value(state: dict[str, Any], key: str, default: Any = "") -> Any:
    return session_schema.get_character_value(state, key, default)


def active_character_id(state: dict[str, Any]) -> str:
    return (
        character_value(state, "custom_character", "")
        or character_value(state, "custom_bot_name", "")
        or character_value(state, "custom_role_name", "")
        or ""
    ).strip()


@web.middleware
async def _no_cache_assets(request: web.Request, handler):
    """控制台 HTML/JS/CSS 不走浏览器缓存，避免改完 UI 还显示旧界面。"""
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        try:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        except Exception:
            pass
    return resp


def _auth_from_request(request: web.Request) -> dict[str, Any] | None:
    service = service_from(request)
    token = (
        request.query.get("token")
        or request.headers.get("X-Web-Token")
        or request.cookies.get("web_session")
        or ""
    ).strip()
    if token:
        user_id = service.app_store.user_for_token(token)
        if user_id:
            return {"role": "user", "user_id": user_id, "token": token}
        sessions = getattr(service, "_web_admin_sessions", set())
        if token in sessions:
            return {"role": "admin", "user_id": "admin", "token": token}
    return None


def _is_admin(request: web.Request) -> bool:
    return (request.get("web_auth") or {}).get("role") == "admin"


def _session_allowed(request: web.Request, session_id: str) -> bool:
    if _is_admin(request):
        return True
    auth = request.get("web_auth") or {}
    return auth.get("user_id") == service_from(request)._user_id_for_session(session_id)


def _require_admin(request: web.Request):
    if not _is_admin(request):
        raise web.HTTPForbidden(text="需要管理员权限")


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path.startswith("/static/") or request.path in {"/login", "/api/auth/login"}:
        return await handler(request)
    auth = _auth_from_request(request)
    if auth:
        request["web_auth"] = auth
        return await handler(request)
    if request.path.startswith("/api/"):
        return json_error("未登录", status=401)
    return await login_page(request)


def create_web_app(service) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[_auth_middleware, _no_cache_assets])
    app["service"] = service
    static_dir = Path(__file__).with_name("static")

    app.router.add_get("/", index)
    app.router.add_post("/login", web_login)
    app.router.add_post("/api/auth/login", api_auth_login)
    app.router.add_get("/api/auth/me", api_auth_me)
    app.router.add_static("/static/", static_dir)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/feedback", api_feedback)
    app.router.add_post("/api/feedback", api_submit_feedback)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/config", api_save_config)
    app.router.add_get("/api/sessions", api_sessions)
    # 具体子资源路由先注册，避免被下面 {session_id:.+} 贪婪匹配吞掉
    app.router.add_get("/api/sessions/{session_id:.+}/memories", api_memories)
    app.router.add_post("/api/sessions/{session_id:.+}/memories", api_add_memory)
    app.router.add_patch(r"/api/sessions/{session_id:.+}/memories/{memory_id:\d+}", api_update_memory)
    app.router.add_delete(r"/api/sessions/{session_id:.+}/memories/{memory_id:\d+}", api_delete_memory)
    app.router.add_get("/api/sessions/{session_id:.+}/characters", api_characters)
    app.router.add_post("/api/sessions/{session_id:.+}/characters", api_save_character)
    app.router.add_delete("/api/sessions/{session_id:.+}/characters/{character_id:.+}", api_delete_character)
    app.router.add_post("/api/sessions/{session_id:.+}/characters/{character_id:.+}/activate", api_activate_character)
    app.router.add_get("/api/sessions/{session_id:.+}/diaries", api_diaries)
    app.router.add_get("/api/sessions/{session_id:.+}/diaries/{diary_date:.+}", api_diary_detail)
    app.router.add_post("/api/sessions/{session_id:.+}/diaries/{diary_date:.+}", api_save_diary)
    app.router.add_delete("/api/sessions/{session_id:.+}/diaries/{diary_date:.+}", api_delete_diary)
    app.router.add_post("/api/sessions/{session_id:.+}/freeze", api_freeze_session)
    app.router.add_post("/api/sessions/{session_id:.+}/unfreeze", api_unfreeze_session)
    app.router.add_post("/api/sessions/{session_id:.+}/organize-memories", api_organize_memories)
    app.router.add_get("/api/sessions/{session_id:.+}/history-summary", api_get_history_summary)
    app.router.add_put("/api/sessions/{session_id:.+}/history-summary", api_save_history_summary)
    # 通用会话路由放在最后，且只匹配不含 / 的 session_id（session_id 含 : 但不含 /）
    app.router.add_get("/api/sessions/{session_id:[^/]+}", api_session_detail)
    app.router.add_patch("/api/sessions/{session_id:[^/]+}", api_update_session)
    app.router.add_delete("/api/sessions/{session_id:[^/]+}", api_delete_session)
    app.router.add_get("/api/models", api_model_profiles)
    app.router.add_post("/api/models/{profile_id}", api_save_model_profile)
    app.router.add_patch("/api/models/settings", api_update_model_settings)
    app.router.add_get("/api/prompt-slots/{session_id:.+}", api_prompt_slots)
    app.router.add_post("/api/world/{session_id:.+}/places/refresh", api_world_refresh_places)
    app.router.add_get("/api/world/{session_id:.+}", api_world_route)
    app.router.add_post("/api/bot/start", api_bot_start)
    app.router.add_post("/api/bot/stop", api_bot_stop)
    app.router.add_post("/api/service/restart", api_service_restart)
    app.router.add_post("/api/service/stop", api_service_stop)
    app.router.add_get("/api/admin/llm-usage", api_admin_llm_usage)
    app.router.add_post("/api/admin/migrate-visual-tags", api_migrate_visual_tags)
    app.router.add_post("/api/admin/cleanup-prompt-prefix", api_cleanup_prompt_prefix)
    app.router.add_post("/api/admin/git-update", api_admin_git_update)
    app.router.add_post("/api/admin/freeze-inactive", api_freeze_inactive)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/logs/{chat_id:.+}", api_log_detail)
    app.router.add_delete("/api/logs/{chat_id:.+}", api_log_clear)
    app.router.add_get("/api/logs/llm-debug", api_llm_debug_log)
    app.router.add_get("/api/logs/system-errors", api_system_error_log)
    app.router.add_post("/api/actions/test-comfyui", api_test_comfyui)
    app.router.add_post("/api/actions/test-llm", api_test_llm)
    app.router.add_post("/api/actions/send-message", api_send_message)
    app.router.add_post("/api/actions/run-command", api_run_command)
    return app


async def index(request: web.Request):
    service = service_from(request)
    token = (request.query.get("token") or "").strip()
    if token:
        # 普通用户 token：app_store 持久化，长期 cookie。
        if service.app_store.user_for_token(token):
            resp = web.HTTPFound("/")
            resp.set_cookie("web_session", token, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
            raise resp
        # 管理员 token：内存会话集合，短期 cookie（与登录 API 一致 24h）。
        admin_sessions = getattr(service, "_web_admin_sessions", set())
        if token in admin_sessions:
            resp = web.HTTPFound("/")
            resp.set_cookie("web_session", token, max_age=24 * 3600, httponly=True, samesite="Lax")
            raise resp
    return web.FileResponse(Path(__file__).with_name("static") / "index.html")


async def login_page(request: web.Request):
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sucyubot Console 登录</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: "Segoe UI", "Microsoft YaHei", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f8fafc;
      color: #0f172a;
    }
    .card {
      width: min(380px, calc(100vw - 32px));
      background: white;
      border: 1px solid #e2e8f0;
      border-radius: 18px;
      padding: 32px;
      box-shadow: 0 10px 25px -5px rgba(15, 23, 42, 0.08), 0 4px 6px -4px rgba(15, 23, 42, 0.04);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
      margin-bottom: 24px;
    }
    .brand-mark {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, #0d9488, #14b8a6);
      display: grid;
      place-items: center;
      font-weight: 700;
      font-size: 16px;
      color: white;
      box-shadow: 0 4px 12px rgba(13, 148, 136, 0.35);
    }
    .brand h1 { margin: 0; font-size: 20px; font-weight: 700; }
    .brand p { margin: 2px 0 0; color: #64748b; font-size: 13px; }
    label { display: block; margin: 14px 0 6px; font-size: 13px; color: #475569; font-weight: 500; }
    input {
      box-sizing: border-box;
      width: 100%;
      padding: 11px 14px;
      border: 1px solid #e2e8f0;
      border-radius: 10px;
      font-size: 15px;
      transition: all 0.15s ease;
    }
    input:focus {
      outline: none;
      border-color: #0d9488;
      box-shadow: 0 0 0 3px rgba(13, 148, 136, 0.08);
    }
    button {
      margin-top: 22px;
      width: 100%;
      border: 0;
      border-radius: 10px;
      padding: 12px 14px;
      background: #0d9488;
      color: white;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
      box-shadow: 0 2px 6px rgba(13, 148, 136, 0.25);
    }
    button:hover {
      background: #0f766e;
      box-shadow: 0 4px 10px rgba(13, 148, 136, 0.3);
      transform: translateY(-1px);
    }
    p.note { margin: 18px 0 0; color: #64748b; font-size: 13px; line-height: 1.55; }
  </style>
</head>
<body>
  <form method="post" action="/login" class="card">
    <div class="brand">
      <div class="brand-mark">SC</div>
      <div>
        <h1>Sucyubot Console</h1>
        <p>登录以继续</p>
      </div>
    </div>
    <label>账号</label>
    <input name="username" autocomplete="username" required>
    <label>密码</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">登录</button>
    <p class="note">Telegram 用户账号为你的 TG 数字 ID，密码用 bot 命令 /web密码 设置。管理员账号密码来自配置文件。</p>
  </form>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def _login_with_credentials(request: web.Request, username: str, password: str) -> web.Response:
    service = service_from(request)
    admin_user = str(service.config.get("web_admin_username") or "admin")
    admin_password = str(service.config.get("web_admin_password") or "admin")
    if username == admin_user and password == admin_password:
        token = secrets.token_urlsafe(32)
        sessions = getattr(service, "_web_admin_sessions", None)
        if sessions is None:
            sessions = set()
            service._web_admin_sessions = sessions
        sessions.add(token)
        resp = web.HTTPFound("/")
        resp.set_cookie("web_session", token, max_age=24 * 3600, httponly=True, samesite="Lax")
        return resp
    if service.app_store.verify_user_password(username, password):
        token = service.app_store.get_or_create_web_token(username)
        resp = web.HTTPFound("/")
        resp.set_cookie("web_session", token, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
        return resp
    raise web.HTTPUnauthorized(text="账号或密码错误")


async def web_login(request: web.Request):
    data = await request.post()
    return await _login_with_credentials(request, str(data.get("username") or ""), str(data.get("password") or ""))


async def api_auth_login(request: web.Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    resp = await _login_with_credentials(request, username, password)
    return json_ok({"redirect": "/"}) if not isinstance(resp, web.HTTPFound) else resp


async def api_auth_me(request: web.Request):
    return json_ok({"auth": request.get("web_auth") or {}})


def service_from(request: web.Request):
    return request.app["service"]


def json_ok(data: dict[str, Any] | None = None):
    payload = {"ok": True}
    if data:
        payload.update(data)
    return web.json_response(payload)


def json_error(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": message}, status=status)


def feedback_file_path(service) -> Path:
    configured = getattr(service, "feedback_file_path", None)
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "TODO.md"


def feedback_user_name(service, session_id: str) -> str:
    state = getattr(service, "sessions", {}).get(session_id) or {}
    name = active_character_id(state) or str(service.chat_id_from_session(session_id) if hasattr(service, "chat_id_from_session") else session_id)
    name = str(name or session_id).replace("\r", " ").replace("\n", " ").strip()
    return name[:80] or session_id


def feedback_session_for_request(request: web.Request, data: dict[str, Any] | None = None) -> str:
    data = data or {}
    if _is_admin(request):
        return str(data.get("session_id") or request.query.get("session_id") or "").strip()
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    return f"telegram:{user_id}" if user_id else ""


def parse_feedback_sections(text: str) -> list[dict[str, Any]]:
    lines = (text or "").splitlines()
    headers = [idx for idx, line in enumerate(lines) if line.startswith("## ")]
    sections: list[dict[str, Any]] = []
    for pos, start in enumerate(headers):
        end = headers[pos + 1] if pos + 1 < len(headers) else len(lines)
        title = lines[start][3:].strip()
        body_lines = lines[start + 1:end]
        session_id = ""
        visible_lines: list[str] = []
        for line in body_lines:
            stripped = line.strip()
            if stripped.startswith("<!--") and stripped.endswith("-->") and "session_id:" in stripped:
                session_id = stripped.split("session_id:", 1)[1].split("-->", 1)[0].strip()
                continue
            visible_lines.append(line)
        sections.append({
            "title": title,
            "session_id": session_id,
            "content": "\n".join(visible_lines).strip(),
            "start": start,
            "end": end,
        })
    return sections


def feedback_entry_lines(content: str) -> list[str]:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"- {stamp}"]
    for line in content.strip().splitlines():
        lines.append(f"  {line.rstrip()}")
    return lines


def upsert_feedback_text(text: str, *, session_id: str, user_name: str, content: str) -> str:
    lines = (text or "# WebUI 用户反馈\n").splitlines()
    sections = parse_feedback_sections("\n".join(lines))
    entry = feedback_entry_lines(content)
    for section in sections:
        if section.get("session_id") == session_id:
            insert_at = int(section["end"])
            insert = []
            if insert_at > 0 and lines[insert_at - 1].strip():
                insert.append("")
            insert.extend(entry)
            lines[insert_at:insert_at] = insert
            return "\n".join(lines).rstrip() + "\n"

    if lines and lines[-1].strip():
        lines.append("")
    lines.extend([
        f"## {user_name}",
        f"<!-- session_id: {session_id} -->",
        "",
        *entry,
    ])
    return "\n".join(lines).rstrip() + "\n"


async def read_feedback_text(path: Path) -> str:
    def _read() -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
    return await asyncio.to_thread(_read)


async def write_feedback_text(path: Path, text: str) -> None:
    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    await asyncio.to_thread(_write)


def masked_config(service) -> dict[str, Any]:
    values = {}
    secret_present = {}
    for key, value in service.config.items():
        if key in SECRET_KEYS:
            values[key] = ""
            secret_present[key] = bool(value)
        elif key == "global_model_profiles":
            values[key] = mask_model_profiles(value if isinstance(value, dict) else {})
        else:
            values[key] = value
    return {"values": values, "secret_present": secret_present}


def visible_sessions(request: web.Request) -> list[tuple[str, dict[str, Any]]]:
    service = service_from(request)
    if _is_admin(request):
        return list(service.sessions.items())
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    return [(sid, state) for sid, state in service.sessions.items() if service._user_id_for_session(sid) == user_id]


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "开启", "开", "允许"}


def mask_model_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(profile or {})
    for key in MODEL_SECRET_KEYS:
        if key in data:
            data[key] = MODEL_SECRET_PLACEHOLDER if data.get(key) else ""
    return data


def mask_model_profiles(profiles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(pid): mask_model_profile(profile) for pid, profile in (profiles or {}).items()}


def merge_model_profile_secrets(new_profile: dict[str, Any], old_profile: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(new_profile or {})
    old_profile = old_profile or {}
    for key in MODEL_SECRET_KEYS:
        value = merged.get(key)
        if value in ("", None, MODEL_SECRET_PLACEHOLDER):
            if old_profile.get(key):
                merged[key] = old_profile.get(key)
            else:
                merged.pop(key, None)
    return merged


def resolved_model_summary(service, purpose: str, session_id: str) -> dict[str, Any]:
    profile_id, profile, thinking = service._resolve_llm_profile(purpose, session_id)
    model, api_base, _ = service._llm_profile_model_name(profile, thinking)
    return {
        "profile_id": profile_id,
        "model": model,
        "api_base": api_base,
        "thinking": thinking,
        "configured": service.has_llm_config(purpose, session_id),
    }


def cast_config_value(key: str, value, old_value):
    if key == "allowed_chat_ids":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()]
    if isinstance(old_value, bool):
        return parse_bool(value)
    if isinstance(old_value, int) and not isinstance(old_value, bool):
        return int(value)
    if isinstance(old_value, float):
        return float(value)
    if isinstance(old_value, list):
        if isinstance(value, list):
            return value
        return [part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()]
    return "" if value is None else str(value)


def session_summary(service, session_id: str, state: dict[str, Any]) -> dict[str, Any]:
    last = session_schema.get_last_interaction(state)
    now = time.time()
    return {
        "session_id": session_id,
        "chat_id": service.chat_id_from_session(session_id),
        "character": character_value(state, "custom_character", "") or "",
        "series": character_value(state, "custom_series", "") or "",
        "purity": service._get_purity(session_id),
        "style": service._get_current_style(session_id),
        "location": service._get_session_cfg(session_id, "location", ""),
        "timezone": service._get_session_cfg(session_id, "timezone_offset", ""),
        "last_interaction": last,
        "last_interaction_ago": human_ago(now - last) if last else "无记录",
        "daily_push": f"{len(session_schema.get_daily_triggered_times(state))}/{len(session_schema.get_daily_trigger_times(state))}",
        "photos": len(session_schema.get_sent_photos_history(state)),
        "saved_characters": len(session_schema.get_saved_characters(state)),
        "frozen": session_schema.get_frozen(state),
    }


def human_ago(seconds: float) -> str:
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def serialize_place(place: dict[str, Any] | None) -> dict[str, Any] | None:
    if not place:
        return None
    return {
        "key": place.get("key", ""),
        "label": place.get("label", ""),
        "name": place.get("name", ""),
        "score": round(float(place.get("score", 0) or 0), 2),
        "public": bool(place.get("public")),
        "indoor": bool(place.get("indoor")),
        "views": list(place.get("views") or []),
        "activities": list(place.get("activities") or []),
    }


def serialize_user_place(user_place: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user_place:
        return None
    updated = float(user_place.get("updated_at", 0) or 0)
    return {
        "key": user_place.get("key", ""),
        "label": user_place.get("label", ""),
        "text": user_place.get("text", ""),
        "co_located": bool(user_place.get("co_located")),
        "updated_at": updated,
        "updated_ago": human_ago(time.time() - updated) if updated else "",
    }


def serialize_world_state(world: dict[str, Any]) -> dict[str, Any]:
    if not world:
        return {}
    now = world.get("now")
    return {
        "city": world.get("city", ""),
        "now": now.strftime("%Y-%m-%d %H:%M") if now else "",
        "weekday": world.get("weekday", ""),
        "day_type": world.get("day_type", ""),
        "time_period": world.get("time_period", ""),
        "time_context": {
            "season": (world.get("time_context") or {}).get("season", ""),
            "light_phase": (world.get("time_context") or {}).get("light_phase", ""),
            "light_hint": (world.get("time_context") or {}).get("light_hint", ""),
            "sunrise": ((world.get("time_context") or {}).get("sunrise").strftime("%H:%M") if (world.get("time_context") or {}).get("sunrise") else ""),
            "sunset": ((world.get("time_context") or {}).get("sunset").strftime("%H:%M") if (world.get("time_context") or {}).get("sunset") else ""),
        },
        "weather": world.get("weather", ""),
        "weather_is_bad": bool(world.get("weather_is_bad")),
        "character_place": serialize_place(world.get("character_place")),
        "character_candidates": [serialize_place(item) for item in world.get("character_candidates", [])],
        "next_place": serialize_place(world.get("next_place")),
        "next_time_period": world.get("next_time_period", ""),
        "life_profile": {
            "age_stage": (world.get("life_profile") or {}).get("age_stage", ""),
            "day_anchor": (world.get("life_profile") or {}).get("day_anchor", ""),
        },
        "user_place": serialize_user_place(world.get("user_place")),
        "relation": world.get("relation", ""),
        "constraints": list(world.get("constraints") or []),
        "spatial_override": world.get("spatial_override", ""),
        "catalog_source": world.get("catalog_source", ""),
        "character_place_history": [
            {
                "key": item.get("key", ""),
                "label": item.get("label", ""),
                "source": item.get("source", ""),
                "confidence": round(float(item.get("confidence", 0) or 0), 2),
                "ts": float(item.get("ts", 0) or 0),
                "ago": human_ago(time.time() - float(item.get("ts", 0) or 0)) if item.get("ts") else "",
            }
            for item in (world.get("character_place_history") or [])
            if isinstance(item, dict)
        ],
    }


def build_catalog_preview(service, city: str) -> dict[str, Any]:
    key = service._city_catalog_key(city) if hasattr(service, "_city_catalog_key") else ""
    catalog = getattr(service, "city_place_catalogs", {}).get(key, {}) if key else {}
    places = catalog.get("places") if isinstance(catalog, dict) else {}
    places = places if isinstance(places, dict) else {}
    updated = float(catalog.get("updated_at", 0) or 0) if isinstance(catalog, dict) else 0
    items = []
    for place_key, values in sorted(places.items()):
        meta = PLACE_TYPES.get(place_key, {})
        items.append({
            "key": place_key,
            "label": meta.get("label", place_key),
            "places": [str(item) for item in values if str(item).strip()],
        })
    enabled = service._world_city_places_enabled() if hasattr(service, "_world_city_places_enabled") else bool(service.config.get("world_city_places_enabled", True))
    return {
        "city": city,
        "enabled": enabled,
        "has_catalog": bool(items),
        "updated_at": updated,
        "updated_ago": human_ago(time.time() - updated) if updated else "",
        "items": items,
    }


def build_world_route_preview(service, session_id: str, weather: Any = None) -> dict[str, Any]:
    state = service._get_session_state(session_id)
    summary = session_summary(service, session_id, state)
    enabled = service._world_runtime_enabled() if hasattr(service, "_world_runtime_enabled") else False
    city = service._get_session_cfg(session_id, "location", service.config.get("location", ""))
    now = service._session_now(session_id)
    catalog = build_catalog_preview(service, city)
    payload = {
        "enabled": enabled,
        "session": summary,
        "city": city,
        "timezone": service._get_session_cfg(session_id, "timezone_offset", ""),
        "weather": service._weather_text(weather) if hasattr(service, "_weather_text") else (weather or ""),
        "catalog": catalog,
        "current": {},
        "timeline": [],
    }
    if not enabled or not hasattr(service, "build_world_state"):
        return payload

    current_world = service.build_world_state(session_id, weather=weather, now=now, mode="chat")
    payload["current"] = serialize_world_state(current_world)

    timeline = []
    for index, hour in enumerate(WORLD_TIMELINE_HOURS):
        slot_now = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        next_hour = WORLD_TIMELINE_HOURS[index + 1] if index + 1 < len(WORLD_TIMELINE_HOURS) else 24
        item = serialize_world_state(service.build_world_state(
            session_id, weather=weather, now=slot_now, mode="chat", apply_persisted_place=False))
        item["slot_label"] = f"{hour:02d}:00"
        item["is_current_slot"] = hour <= now.hour < next_hour
        timeline.append(item)
    payload["timeline"] = timeline
    return payload


def serialize_prompt_slots(service, session_id: str, scene: str = "{场景描述}") -> dict[str, Any]:
    state = service._get_session_state(session_id)
    positive, negative = service._build_prompt(scene or "{场景描述}", session_id=session_id)
    slots = None
    cache = getattr(service, "_last_prompt_slots_by_session", {})
    if isinstance(cache, dict):
        slots = cache.get(session_id)
    items = []
    if hasattr(slots, "as_display_items"):
        items = [{"key": key, "value": value} for key, value in slots.as_display_items()]
    prefs = service._prompt_scene_preferences(session_id) if hasattr(service, "_prompt_scene_preferences") else {
        "scene_preference": "",
        "selfie_preference": "",
    }
    return {
        "scene": scene,
        "positive": positive,
        "negative": negative,
        "items": items,
        "editable": {
            "custom_count": character_value(state, "custom_count", ""),
            "custom_positive_prefix": character_value(state, "custom_positive_prefix", ""),
            "custom_default_hair": character_value(state, "custom_default_hair", ""),
            "custom_default_eyes": character_value(state, "custom_default_eyes", ""),
            "custom_current_style": character_value(state, "custom_current_style", ""),
            "dynamic_appearance": session_schema.get_outfit(state),
            "custom_scene_preference": character_value(state, "custom_scene_preference", ""),
            "custom_selfie_preference": character_value(state, "custom_selfie_preference", ""),
        },
        # 只读：当前衣柜按槽位拆分（编辑仍走上面的 dynamic_appearance 扁平框，保存后会自动重新分槽）。
        "wardrobe": service._get_wardrobe(state),
        # 只读：衣橱收藏（角色穿过、可点名复穿的衣服）。
        "closet": session_schema.get_closet(state),
        "effective": {
            "positive_prefix": service._get_session_cfg(session_id, "positive_prefix", ""),
            "default_hair": service._get_session_cfg(session_id, "default_hair", ""),
            "default_eyes": service._get_session_cfg(session_id, "default_eyes", ""),
            "current_style": service._get_current_style(session_id),
            "scene_preference": prefs.get("scene_preference", ""),
            "selfie_preference": prefs.get("selfie_preference", ""),
        },
        "notes": [
            "基础外观只放稳定身体身份特征；1girl/1boy/solo 已迁移到人数槽 custom_count。",
            "场景偏好会注入生图辅助模型，用来影响配图和主动推送的地点、时间与自拍习惯。",
        ],
    }


async def api_status(request: web.Request):
    service = service_from(request)
    config = service.config
    sessions = [session_summary(service, sid, state) for sid, state in visible_sessions(request)]
    chat_profile_id, chat_profile, chat_thinking = service._resolve_llm_profile("chat", "")
    chat_model, chat_api_base, _ = service._llm_profile_model_name(chat_profile, chat_thinking)
    image_profile_id, image_profile, image_thinking = service._resolve_llm_profile("image", "")
    image_model, image_api_base, _ = service._llm_profile_model_name(image_profile, image_thinking)
    vision_profile_id, vision_profile, vision_thinking = service._resolve_llm_profile("vision", "")
    vision_model, vision_api_base, _ = service._llm_profile_model_name(vision_profile, vision_thinking)
    public_host = str(config.get("web_public_host") or config.get("web_host", "127.0.0.1") or "127.0.0.1")
    if public_host in {"0.0.0.0", "::"}:
        public_host = "127.0.0.1"
    port = int(config.get("web_port", 8787) or 8787)
    data = {
        "bot_running": service.is_bot_running,
        "bot_username": service._bot_username,
        "process_id": os.getpid(),
        "process_started_at": service.process_started_at,
        "web_url": f"http://{public_host}:{port}",
        "config_path": str(service.config_path),
        "state_db_path": str(service.app_store.path),
        "launch_script": str(Path.cwd() / "Start-SucyuBot.cmd"),
        "token_configured": bool(config.get("telegram_bot_token")),
        "llm_configured": service.has_llm_config("chat") and service.has_llm_config("image"),
        "chat_llm_configured": service.has_llm_config("chat"),
        "image_llm_configured": service.has_llm_config("image"),
        "vision_llm_configured": service.has_llm_config("vision"),
        "comfyui_url": config.get("comfyui_url", ""),
        "chat_llm_model": chat_model or chat_profile_id,
        "chat_llm_api_base": chat_api_base,
        "image_llm_model": image_model or image_profile_id,
        "image_llm_api_base": image_api_base,
        "vision_llm_model": vision_model or vision_profile_id,
        "vision_llm_api_base": vision_api_base,
        "generating": service._generating,
        "active_pushes": len(service._active_pushes),
        "sessions_count": len(service.sessions),
        "sessions": sessions,
    }
    return json_ok({"status": data})


async def api_config(request: web.Request):
    _require_admin(request)
    return json_ok({"config": masked_config(service_from(request))})


async def api_save_config(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    payload = await request.json()
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        return json_error("配置数据格式不正确")
    for key, value in values.items():
        if key in YAML_ONLY_CONFIG_KEYS:
            continue
        if key in SECRET_KEYS and value in ("", None):
            continue
        old = service.config.get(key)
        service.config[key] = cast_config_value(key, value, old)
    service.save_config()
    return json_ok({"config": masked_config(service)})


async def api_sessions(request: web.Request):
    service = service_from(request)
    sessions = [session_summary(service, sid, state) for sid, state in visible_sessions(request)]
    return json_ok({"sessions": sessions})


async def api_feedback(request: web.Request):
    service = service_from(request)
    sid = feedback_session_for_request(request)
    if sid and not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    path = feedback_file_path(service)
    text = await read_feedback_text(path)
    sections = [
        {
            "session_id": item.get("session_id", ""),
            "user_name": item.get("title", ""),
            "content": item.get("content", ""),
        }
        for item in parse_feedback_sections(text)
        if item.get("session_id")
    ]
    if not _is_admin(request):
        sections = [item for item in sections if item.get("session_id") == sid]
    current_name = feedback_user_name(service, sid) if sid else ""
    return json_ok({
        "sections": sections,
        "current_session_id": sid,
        "current_user_name": current_name,
        "is_admin": _is_admin(request),
    })


async def api_submit_feedback(request: web.Request):
    service = service_from(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return json_error("反馈数据格式不正确")
    sid = feedback_session_for_request(request, payload)
    if not sid:
        return json_error("请先选择会话")
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    content = str(payload.get("content") or "").strip()
    if not content:
        return json_error("反馈内容不能为空")
    if len(content) > FEEDBACK_MAX_LENGTH:
        return json_error(f"反馈内容过长，最多 {FEEDBACK_MAX_LENGTH} 字符")
    user_name = feedback_user_name(service, sid)
    path = feedback_file_path(service)
    lock = getattr(service, "_feedback_file_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        service._feedback_file_lock = lock
    async with lock:
        text = await read_feedback_text(path)
        updated = upsert_feedback_text(text, session_id=sid, user_name=user_name, content=content)
        await write_feedback_text(path, updated)
    return await api_feedback(request)


async def api_session_detail(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    state = service._get_session_state(sid)
    return json_ok({"session": session_summary(service, sid, state), "state": state})


async def api_update_session(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    payload = await request.json()
    state = service._get_session_state(sid)
    allowed = {
        "custom_scheduled_persona", "custom_role_name", "custom_bot_name", "custom_bot_self_name",
        "custom_spatial_relationship", "custom_location", "custom_timezone_offset",
        "custom_count", "custom_positive_prefix",
        "custom_default_hair", "custom_default_eyes", "custom_current_style",
        "custom_scene_preference", "custom_selfie_preference",
        "custom_character", "custom_series", "custom_visual_character", "custom_visual_series", "custom_daily_selfie_limit",
        "custom_character_age_stage", "custom_character_occupation", "custom_character_day_anchor",
    }
    life_profile_keys = {
        "custom_scheduled_persona", "custom_role_name", "custom_bot_name",
        "custom_character", "custom_series", "custom_visual_character", "custom_visual_series", "custom_character_age_stage",
        "custom_character_occupation", "custom_character_day_anchor",
    }
    profile_touched = False
    for key in allowed:
        if key in payload:
            value = "" if payload[key] is None else str(payload[key])
            if key in session_schema.STATE_SCHEMA and session_schema.is_character_config_key(key):
                session_schema.set_character_value(state, key, value)
            else:
                state[key] = value
            if key in life_profile_keys:
                profile_touched = True
    # dynamic_appearance 现走 clothing box（不在 allowed 里直写顶层，避免遗留陈旧顶层键）。
    if "dynamic_appearance" in payload:
        session_schema.set_outfit(state, "" if payload["dynamic_appearance"] is None else str(payload["dynamic_appearance"]))
    if profile_touched:
        state.pop("life_profile", None)
    if "purity" in payload:
        raw = str(payload["purity"]).strip()
        if raw:
            session_schema.set_character_value(state, "purity", max(0, min(10, int(raw))))
            session_schema.set_character_value(state, "purity_user_set", True)
        else:
            session_schema.set_character_value(state, "purity", None)
            session_schema.set_character_value(state, "purity_user_set", False)
    if "custom_allow_llm_change_appearance" in payload:
        value = payload["custom_allow_llm_change_appearance"]
        if value in ("", None, "default"):
            session_schema.set_character_value(state, "custom_allow_llm_change_appearance", None)
        else:
            session_schema.set_character_value(state, "custom_allow_llm_change_appearance", parse_bool(value))
    service._save_session_state(sid, state)
    return json_ok({"session": session_summary(service, sid, state), "state": state})


async def api_delete_session(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if sid not in service.sessions:
        return json_error("会话不存在", status=404)
    if not _session_allowed(request, sid):
        return json_error("无权删除此会话", status=403)
    service.sessions.pop(sid, None)
    service.app_store.delete_session_state(sid)
    return json_ok()


async def api_memories(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    if "character_key" in request.query:
        char = request.query.get("character_key") or ""
    else:
        char = service._memory_character(sid) if hasattr(service, "_memory_character") else ""
    try:
        limit = max(1, min(200, int(request.query.get("limit", "80"))))
    except ValueError:
        limit = 80
    memories = service.memory.list_memories(sid, character=char, limit=limit)
    return json_ok({"memories": memories, "character": char})


async def api_add_memory(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    payload = await request.json()
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return json_error("记忆内容不能为空")
    char = request.query.get("character_key") or (service._memory_character(sid) if hasattr(service, "_memory_character") else "")
    memory_id = service.memory.add_memory(
        sid,
        payload.get("kind") or "manual",
        summary,
        character=char,
        importance=payload.get("importance", 5),
        tags=payload.get("tags") or ["手动"],
        source="webui",
    )
    memories = service.memory.list_memories(sid, character=char, limit=80)
    return json_ok({"id": memory_id, "memories": memories, "character": char})


async def api_update_memory(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    payload = await request.json()
    char = request.query.get("character_key") or (service._memory_character(sid) if hasattr(service, "_memory_character") else "")
    ok = service.memory.edit_memory(
        sid,
        int(request.match_info["memory_id"]),
        character=char,
        summary=payload.get("summary") if "summary" in payload else None,
        kind=payload.get("kind") if "kind" in payload else None,
        importance=payload.get("importance") if "importance" in payload else None,
        tags=payload.get("tags") if "tags" in payload else None,
        source=payload.get("source") if "source" in payload else None,
    )
    if not ok:
        return json_error("记忆不存在或没有可更新字段", status=404)
    memories = service.memory.list_memories(sid, character=char, limit=80)
    return json_ok({"memories": memories, "character": char})


async def api_delete_memory(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    char = request.query.get("character_key") or (service._memory_character(sid) if hasattr(service, "_memory_character") else "")
    ok = service.memory.deactivate_memory(sid, int(request.match_info["memory_id"]), character=char)
    if not ok:
        return json_error("记忆不存在", status=404)
    memories = service.memory.list_memories(sid, character=char, limit=80)
    return json_ok({"memories": memories, "character": char})


async def api_characters(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    state = service._get_session_state(sid)
    if hasattr(service, "_snapshot_character"):
        service._snapshot_character(state)
    characters = dict(session_schema.get_saved_characters(state))
    active_id = active_character_id(state)
    # 如果当前会话已有角色身份但尚未保存进角色池，自动把当前态注入为可编辑条目
    if active_id and active_id not in characters:
        current = service._character_export_payload(state) if hasattr(service, "_character_export_payload") else {}
        if current.get("character") or current.get("bot_name") or current.get("role_name"):
            characters[active_id] = current
    # 始终注入系统默认角色（来自 config 默认值），保证可被选中和编辑，但不可删除
    default_char = service._default_character_payload()
    default_id = default_char.get("id") or default_char.get("bot_name") or "default"
    if default_id and default_id not in characters:
        characters[default_id] = default_char
    return json_ok({
        "active_id": active_id,
        "default_id": default_id,
        "current": service._character_export_payload(state) if hasattr(service, "_character_export_payload") else {},
        "style_pool": service._normalize_style_pool() if hasattr(service, "_normalize_style_pool") else [],
        "characters": characters,
    })


async def api_save_character(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    payload = await request.json()
    if not isinstance(payload, dict):
        return json_error("角色数据必须是 JSON 对象")
    state = service._get_session_state(sid)
    key = str(payload.get("id") or payload.get("character") or payload.get("bot_name") or "").strip()
    if not key:
        return json_error("角色 JSON 必须包含 id 或 character")
    # 默认角色以 config 为存储：仅当该角色不在 saved_characters（用户没创建过同名自定义角色）、
    # 且其 is_default 标记为真时，才走默认路径写回 config。否则走常规 saved_characters 路径。
    default_id = service._default_character_payload().get("id") or ""
    saved = session_schema.get_saved_characters(state)
    is_default_card = key == default_id and saved.get(key, {}).get("is_default") is True
    if is_default_card:
        service._apply_default_character_payload(payload)
        return json_ok({
            "active_id": character_value(state, "custom_character", "") or "",
            "current": service._character_export_payload(state),
            "characters": saved,
            "default": service._default_character_payload(),
        })
    active_id = active_character_id(state)
    force_activate = parse_bool(payload.get("activate")) if "activate" in payload else False
    if hasattr(service, "_apply_character_payload"):
        switching = force_activate and active_id != key
        if switching:
            if hasattr(service, "_save_current_character_context"):
                service._save_current_character_context(state)
            if hasattr(service, "_snapshot_character"):
                service._snapshot_character(state)
            service._apply_character_payload(state, payload)
            if not character_value(state, "custom_character", ""):
                session_schema.set_character_value(state, "custom_character", key)
            has_clothing_context = False
            if hasattr(service, "_restore_character_context"):
                has_clothing_context = service._restore_character_context(sid, state)
            if hasattr(service, "_apply_card_outfit_after_switch"):
                service._apply_card_outfit_after_switch(state, payload, has_clothing_context=has_clothing_context)
        elif force_activate or not active_id or active_id == key:
            service._apply_character_payload(state, payload)
            if not character_value(state, "custom_character", ""):
                session_schema.set_character_value(state, "custom_character", key)
    session_schema.get_saved_characters(state)[key] = {k: v for k, v in payload.items() if k != "id"}
    service._save_session_state(sid, state)
    return json_ok({"active_id": character_value(state, "custom_character", "") or "", "current": service._character_export_payload(state), "characters": session_schema.get_saved_characters(state)})


async def api_delete_character(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    default_id = service._default_character_payload().get("id") or ""
    state = service._get_session_state(sid)
    saved = session_schema.get_saved_characters(state)
    is_default_card = character_id == default_id and saved.get(character_id, {}).get("is_default") is True
    if is_default_card:
        return json_error("系统默认角色不能删除", status=403)
    saved.pop(character_id, None)
    if character_value(state, "custom_character", "") == character_id:
        for key in SESSION_CUSTOM_RESET_KEYS:
            session_schema.set_character_value(state, key, "")
        session_schema.set_outfit(state, "")
        session_schema.set_wardrobe(state, {})
        session_schema.set_closet(state, {})
        session_schema.clear_nudity(state)
        session_schema.set_character_value(state, "persona_user_set", False)
        session_schema.set_character_value(state, "purity", None)
        session_schema.set_character_value(state, "purity_user_set", False)
        if hasattr(service, "_clear_conversation_context"):
            service._clear_conversation_context(state)
    service._save_session_state(sid, state)
    return json_ok({"active_id": character_value(state, "custom_character", "") or "", "characters": saved})


async def api_activate_character(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    state = service._get_session_state(sid)
    saved = session_schema.get_saved_characters(state)
    data = saved.get(character_id)
    if not data:
        return json_error("角色不存在", status=404)
    switching = (data.get("character", "") or "") != (character_value(state, "custom_character", "") or "")
    if switching and hasattr(service, "_save_current_character_context"):
        service._save_current_character_context(state)
    if hasattr(service, "_snapshot_character"):
        service._snapshot_character(state)
    payload = dict(data)
    if not switching:
        payload["role_name"] = character_value(state, "custom_role_name", "") or data.get("role_name", "")
        payload["bot_self_name"] = character_value(state, "custom_bot_self_name", "") or data.get("bot_self_name", "")
        payload["relationship"] = character_value(state, "custom_spatial_relationship", "") or data.get("relationship", "")
    if "style" not in data:
        payload.pop("style", None)
    if data.get("purity") is None or character_value(state, "purity_user_set", False):
        payload.pop("purity", None)
    if hasattr(service, "_apply_character_payload"):
        service._apply_character_payload(state, payload)
    if switching and hasattr(service, "_restore_character_context"):
        has_clothing_context = service._restore_character_context(sid, state)
        if hasattr(service, "_apply_card_outfit_after_switch"):
            service._apply_card_outfit_after_switch(state, payload, has_clothing_context=has_clothing_context)
    state.pop("life_profile", None)
    service._save_session_state(sid, state)
    return json_ok({"active_id": character_value(state, "custom_character", "") or "", "current": service._character_export_payload(state), "characters": session_schema.get_saved_characters(state)})


async def api_diaries(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_key = request.query.get("character_key") or ""
    try:
        limit = max(1, min(100, int(request.query.get("limit", "30"))))
    except ValueError:
        limit = 30
    diaries = service.app_store.recent_diaries(sid, character_key, limit=limit)
    return json_ok({"diaries": diaries, "character_key": character_key})


async def api_diary_detail(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_key = request.query.get("character_key") or ""
    diary_date = request.match_info["diary_date"]
    diary = service.app_store.get_diary(sid, character_key, diary_date)
    if not diary:
        return json_error("日记不存在", status=404)
    return json_ok({"diary": diary, "character_key": character_key})


async def api_save_diary(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    payload = await request.json()
    character_key = request.query.get("character_key") or str(payload.get("character_key") or "").strip()
    diary_date = request.match_info["diary_date"]
    content = str(payload.get("content") or "").strip()
    if not content:
        return json_error("日记内容不能为空")
    service.app_store.upsert_diary(
        sid,
        character_key,
        diary_date,
        content,
        from_message_id=int(payload.get("from_message_id") or 0),
        to_message_id=int(payload.get("to_message_id") or 0),
    )
    diaries = service.app_store.recent_diaries(sid, character_key, limit=30)
    return json_ok({"diaries": diaries, "character_key": character_key})


async def api_delete_diary(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_key = request.query.get("character_key") or ""
    diary_date = request.match_info["diary_date"]
    service.app_store.delete_diary(sid, character_key, diary_date)
    diaries = service.app_store.recent_diaries(sid, character_key, limit=30)
    return json_ok({"diaries": diaries, "character_key": character_key})


async def api_model_profiles(request: web.Request):
    service = service_from(request)
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    if _is_admin(request):
        user_id = request.query.get("user_id") or user_id
    session_id = f"telegram:{user_id}" if user_id and user_id != "admin" else ""
    settings = service.app_store.get_user_model_settings(user_id)
    return json_ok({
        "global_profiles": mask_model_profiles(service._global_model_profiles()),
        "user_profiles": mask_model_profiles(service.app_store.list_model_profiles(user_id)),
        "settings": settings,
        "user_id": user_id,
        "default_chat_model_profile": service.config.get("default_chat_model_profile", ""),
        "default_fast_model_profile": service.config.get("default_fast_model_profile", ""),
        "default_vision_model_profile": service.config.get("default_vision_model_profile", ""),
        "resolved": {
            "chat": resolved_model_summary(service, "chat", session_id),
            "image": resolved_model_summary(service, "image", session_id),
            "vision": resolved_model_summary(service, "vision", session_id),
        },
    })


async def api_save_model_profile(request: web.Request):
    service = service_from(request)
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    if not user_id:
        return json_error("缺少用户身份", status=403)
    profile_id = request.match_info["profile_id"].strip()
    if not profile_id:
        return json_error("profile_id 不能为空")
    payload = await request.json()
    if not isinstance(payload, dict):
        return json_error("模型配置必须是 JSON 对象")
    scope_value = payload.pop("_scope", None)
    if scope_value is None:
        scope_value = payload.pop("scope", None)
    if scope_value is None:
        scope_value = request.query.get("scope") or "user"
    scope = str(scope_value or "user").strip().lower()
    if _is_admin(request) and request.query.get("user_id"):
        user_id = request.query.get("user_id") or user_id
    if scope == "global":
        _require_admin(request)
        profiles = dict(service._global_model_profiles())
        payload = merge_model_profile_secrets(payload, profiles.get(profile_id) or {})
        profiles[profile_id] = payload
        service.config["global_model_profiles"] = profiles
        service.save_config()
        return json_ok({"global_profiles": mask_model_profiles(profiles)})
    current = service.app_store.list_model_profiles(user_id).get(profile_id) or {}
    payload = merge_model_profile_secrets(payload, current)
    service.app_store.upsert_model_profile(user_id, profile_id, payload)
    return json_ok({"user_profiles": mask_model_profiles(service.app_store.list_model_profiles(user_id))})


async def api_update_model_settings(request: web.Request):
    service = service_from(request)
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    if not user_id:
        return json_error("缺少用户身份", status=403)
    if _is_admin(request) and request.query.get("user_id"):
        user_id = request.query.get("user_id") or user_id
    payload = await request.json()
    kwargs: dict[str, Any] = {}
    for key in ("chat_profile_id", "fast_profile_id", "vision_profile_id"):
        if key in payload:
            kwargs[key] = str(payload.get(key) or "")
    settings = service.app_store.update_user_model_settings(user_id, **kwargs)
    return json_ok({"settings": settings})


async def api_prompt_slots(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    scene = request.query.get("scene", "{场景描述}")
    return json_ok({"prompt": serialize_prompt_slots(service, sid, scene=scene)})


async def api_world_route(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    try:
        weather = await service._fetch_weather("", sid)
    except Exception:
        weather = None
    return json_ok({"world": build_world_route_preview(service, sid, weather=weather)})


async def api_world_refresh_places(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    city = service._get_session_cfg(sid, "location", service.config.get("location", ""))
    try:
        catalog = await service._ensure_city_place_catalog(city, force=True)
    except Exception as exc:
        return json_error(str(exc), status=502)
    try:
        weather = await service._fetch_weather("", sid)
    except Exception:
        weather = None
    return json_ok({"catalog": catalog, "world": build_world_route_preview(service, sid, weather=weather)})


async def api_bot_start(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    try:
        await service.start_bot()
    except Exception as exc:
        return json_error(str(exc), status=409)
    return json_ok({"bot_username": service._bot_username})


async def api_bot_stop(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    await service.stop_bot()
    return json_ok()


async def api_service_restart(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    try:
        restart = service.prepare_process_restart()
    except Exception as exc:
        return json_error(f"无法准备重启: {exc}", status=500)
    asyncio.create_task(service.shutdown_for_process_restart())
    return json_ok({"restart": restart})


async def api_service_stop(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    asyncio.create_task(service.shutdown_service())
    return json_ok({"stopping": True})


async def api_admin_llm_usage(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    query = request.query
    now = time.time()
    # 默认最近 24 小时
    try:
        after = float(query.get("after") or 0) or (now - 86400)
    except ValueError:
        after = now - 86400
    try:
        before = float(query.get("before") or 0) or (now + 0.001)
    except ValueError:
        before = now + 0.001
    group_by = [c.strip() for c in (query.get("group_by") or "profile_id,model,purpose,tag").split(",") if c.strip()]
    valid_cols = {"profile_id", "model", "purpose", "tag", "session_id"}
    group_by = [c for c in group_by if c in valid_cols]
    if not group_by:
        group_by = ["profile_id"]
    rows = service.app_store.aggregate_llm_usage(after=after, before=before, group_by=tuple(group_by))
    total = {
        "requests": sum(r.get("requests", 0) or 0 for r in rows),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) or 0 for r in rows),
        "completion_tokens": sum(r.get("completion_tokens", 0) or 0 for r in rows),
        "cached_tokens": sum(r.get("cached_tokens", 0) or 0 for r in rows),
        "total_tokens": sum(r.get("total_tokens", 0) or 0 for r in rows),
    }
    total["cache_hit_rate"] = (
        round(total["cached_tokens"] / total["prompt_tokens"], 4) if total["prompt_tokens"] else 0
    )
    return json_ok({
        "summary": total,
        "groups": rows,
        "time_range": {"after": after, "before": before, "group_by": group_by},
    })


async def api_migrate_visual_tags(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    return json_ok({"migration": service.migrate_visual_identity_tags()})


async def api_cleanup_prompt_prefix(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    apply_changes = parse_bool(payload.get("apply", False)) if isinstance(payload, dict) else False
    cleanup = service.cleanup_prompt_prefix_slots(apply=apply_changes)
    return json_ok({"cleanup": cleanup})


async def api_admin_git_update(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    try:
        result = await service.run_git_update()
    except Exception as exc:
        return json_error(f"Git 更新异常: {exc}", status=502)
    # 拉取成功且有更新 → 自重启
    restart = None
    if result.get("pulled"):
        try:
            restart = service.prepare_process_restart()
            asyncio.create_task(service.shutdown_for_process_restart(delay=3.0))
        except Exception as exc:
            return json_error(f"Git 拉取成功但准备重启失败: {exc}", status=500)
    return json_ok({"result": result, "report": service._format_git_update_report(result), "restart": restart})


FREEZE_INACTIVE_DAYS = 7


async def api_freeze_inactive(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    threshold = time.time() - FREEZE_INACTIVE_DAYS * 86400
    frozen_list = []
    for sid, state in service.sessions.items():
        if session_schema.get_frozen(state):
            continue
        last = session_schema.get_last_interaction(state)
        if last > 0 and last < threshold:
            session_schema.set_frozen(state, True)
            session_schema.set_frozen_at(state, time.time())
            service._mark_dirty(sid)
            frozen_list.append({
                "session_id": sid,
                "chat_id": service.chat_id_from_session(sid),
                "character": character_value(state, "custom_character", "") or "",
                "last_interaction_ago": human_ago(time.time() - last),
            })
    service._flush_sessions(force=True)
    return json_ok({"frozen_count": len(frozen_list), "frozen": frozen_list})


async def api_freeze_session(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权操作此会话", status=403)
    state = service._get_session_state(sid)
    session_schema.set_frozen(state, True)
    session_schema.set_frozen_at(state, time.time())
    service._save_session_state(sid, state)
    return json_ok({"session": session_summary(service, sid, state)})


async def api_unfreeze_session(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权操作此会话", status=403)
    state = service._get_session_state(sid)
    session_schema.set_frozen(state, False)
    session_schema.set_frozen_at(state, 0)
    service._save_session_state(sid, state)
    return json_ok({"session": session_summary(service, sid, state)})


async def api_organize_memories(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权操作此会话", status=403)
    char = request.query.get("character_key") or (service._memory_character(sid) if hasattr(service, "_memory_character") else "")
    if not service.has_llm_config("chat", sid):
        return json_error("聊天模型未配置，无法整理记忆")
    try:
        await service._organize_memories_after_dream(sid, char)
    except Exception as exc:
        return json_error(f"整理记忆失败: {exc}", status=500)
    memories = service.memory.list_memories(sid, character=char, limit=80)
    return json_ok({"memories": memories, "character": char, "message": "记忆整理完成"})


async def api_get_history_summary(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    char = request.query.get("character_key") or (service._memory_character(sid) if hasattr(service, "_memory_character") else "")
    key = char
    summary = ""
    try:
        meta = service.app_store.get_context_meta(sid, key)
        summary = (meta.get("character_history_summary") or "").strip()
    except Exception:
        pass
    if not summary:
        state = service._get_session_state(sid)
        summary = session_schema.get_character_history_summary(state)
    return json_ok({"character_key": key, "summary": summary})


async def api_save_history_summary(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权操作此会话", status=403)
    payload = await request.json()
    summary = str(payload.get("summary") or "").strip()
    char = str(payload.get("character_key") or "").strip()
    if not char:
        char = service._memory_character(sid) if hasattr(service, "_memory_character") else ""
    try:
        service.app_store.upsert_character_history_summary(sid, char, summary)
    except Exception as exc:
        return json_error(f"保存历史提要失败: {exc}", status=500)
    state = service._get_session_state(sid)
    session_schema.set_character_history_summary(state, summary)
    service._save_session_state(sid, state)
    return json_ok({"character_key": char, "summary": summary})


async def api_logs(request: web.Request):
    service = service_from(request)
    log_dir = service._user_log_dir()
    items = []
    if log_dir.exists():
        for path in log_dir.glob("telegram_*.log"):
            chat_id = path.stem[len("telegram_"):]
            try:
                stat = path.stat()
            except OSError:
                continue
            sid = service.session_id_for_chat(chat_id)
            if not _session_allowed(request, sid):
                continue
            state = service.sessions.get(sid, {})
            items.append({
                "chat_id": chat_id,
                "session_id": sid,
                "character": character_value(state, "custom_character", "") or "",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_ago": human_ago(time.time() - stat.st_mtime),
            })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return json_ok({"logs": items, "enabled": service._user_log_enabled(), "dir": str(log_dir)})


async def api_log_detail(request: web.Request):
    service = service_from(request)
    chat_id = request.match_info["chat_id"]
    sid = service.session_id_for_chat(chat_id)
    if not _session_allowed(request, sid):
        return json_error("无权访问此日志", status=403)
    path = service._user_log_path(sid)
    if not path.exists():
        return json_error("日志不存在", status=404)
    try:
        tail = max(1, min(5000, int(request.query.get("tail", "500"))))
    except ValueError:
        tail = 500
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return json_ok({
        "chat_id": chat_id,
        "total_lines": len(lines),
        "shown_lines": min(tail, len(lines)),
        "content": "\n".join(lines[-tail:]),
    })


async def api_log_clear(request: web.Request):
    service = service_from(request)
    chat_id = request.match_info["chat_id"]
    sid = service.session_id_for_chat(chat_id)
    if not _session_allowed(request, sid):
        return json_error("无权清除此日志", status=403)
    path = service._user_log_path(sid)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        return json_error(str(exc), status=500)
    return json_ok()


async def api_llm_debug_log(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    path = service._llm_debug_log_path()
    if not path.exists():
        return json_ok({"content": {}, "updated_at": None})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return json_ok({"content": data.get("entries_by_type", {}), "updated_at": data.get("updated_at")})
    except Exception as exc:
        return json_error(str(exc), status=500)


async def api_system_error_log(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    log_dir = service._user_log_dir()
    error_lines = []
    if log_dir.exists():
        for path in log_dir.glob("*.log"):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines:
                    if "ERROR" in line or "error" in line.lower():
                        error_lines.append({"file": path.name, "line": line})
            except Exception:
                continue
    error_lines.sort(key=lambda x: x["line"], reverse=True)
    return json_ok({"errors": error_lines[:100]})


async def api_test_comfyui(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    try:
        service._ensure_comfy_session()
        async with service.comfy_session.get(f"{service.comfyui_url}/system_stats") as resp:
            if resp.status != 200:
                return json_error(f"ComfyUI HTTP {resp.status}", status=502)
            stats = await resp.json()
        return json_ok({"stats": stats})
    except Exception as exc:
        return json_error(str(exc), status=502)


async def api_test_llm(request: web.Request):
    service = service_from(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    purpose = payload.get("purpose", "chat")
    if purpose not in ("chat", "image"):
        return json_error("purpose 必须是 chat 或 image")
    try:
        text = await service._call_llm("只输出 OK 两个字母。", "ping", temp=0.0, tag=f"gui-test-{purpose}", purpose=purpose)
        return json_ok({"reply": text})
    except Exception as exc:
        return json_error(str(exc), status=502)


async def api_send_message(request: web.Request):
    service = service_from(request)
    if not service.is_bot_running:
        return json_error("机器人尚未启动", status=409)
    payload = await request.json()
    chat_id = str(payload.get("chat_id", "")).strip()
    text = str(payload.get("text", "")).strip()
    if not chat_id or not text:
        return json_error("需要 chat_id 和 text")
    if not _session_allowed(request, service.session_id_for_chat(chat_id)):
        return json_error("无权向此 Chat ID 发送消息", status=403)
    try:
        await service.send_message(chat_id, text)
    except Exception as exc:
        return json_error(str(exc), status=502)
    return json_ok()


async def api_run_command(request: web.Request):
    service = service_from(request)
    if not service.is_bot_running:
        return json_error("机器人尚未启动", status=409)
    payload = await request.json()
    chat_id = str(payload.get("chat_id", "")).strip()
    command = str(payload.get("command", "")).strip().lstrip("/")
    arg = str(payload.get("arg", "")).strip()
    if not chat_id or not command:
        return json_error("需要 chat_id 和 command")
    sid = service.session_id_for_chat(chat_id)
    if not _session_allowed(request, sid):
        return json_error("无权在此 Chat ID 运行命令", status=403)
    try:
        await asyncio.wait_for(service.dispatch_command(chat_id, sid, command, arg), timeout=900)
    except Exception as exc:
        return json_error(str(exc), status=502)
    return json_ok()
