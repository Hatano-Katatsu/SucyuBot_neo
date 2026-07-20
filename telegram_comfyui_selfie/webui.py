from __future__ import annotations

import asyncio
import base64
import copy
import secrets
import os
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import session_schema
from . import appearance as appearance_rules
from .commands import SESSION_CUSTOM_RESET_KEYS
from .command_aliases import COMMAND_ALIAS_GROUPS
from .webui_common import (
    character_value,
    human_ago,
    is_admin as _is_admin,
    json_error,
    json_ok,
    require_admin as _require_admin,
    service_from,
    session_allowed as _session_allowed,
)
from .webui_logs import (
    USER_LOG_LINE_RE,
    api_llm_debug_log,
    api_log_clear,
    api_log_detail,
    api_logs,
    api_system_error_log,
    error_log_paths as _error_log_paths,
    log_chunk_items,
    parse_error_log_line as _parse_error_log_line,
)
from .world_runtime import PLACE_TYPES


SECRET_KEYS = {
    "telegram_bot_token", "llm_api_key", "chat_llm_api_key", "image_llm_api_key",
    "amap_api_key", "google_places_api_key", "tavily_api_key",
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


def active_character_id(state: dict[str, Any]) -> str:
    return (
        character_value(state, "custom_character", "")
        or character_value(state, "custom_bot_name", "")
        or character_value(state, "custom_role_name", "")
        or ""
    ).strip()


def active_context_character_key(service, session_id: str) -> str:
    if hasattr(service, "_context_character_key"):
        try:
            return service._context_character_key(session_id)
        except Exception:
            pass
    if hasattr(service, "_memory_character"):
        try:
            return service._memory_character(session_id)
        except Exception:
            pass
    return ""


def required_character_key_from_request(request: web.Request, payload: dict[str, Any] | None = None) -> str | None:
    value = request.query.get("character_key")
    if value is None and payload is not None:
        value = payload.get("character_key")
    value = str(value or "").strip()
    if not value:
        return None
    # 默认角色卡的 payload id 是 bot_name 回退值（如"蕾伊"），运行态记忆/日记/checkpoint
    # 都写在空串键下——这里统一归一，前端对 is_default 卡也会直接发 __default__ 占位。
    if value == "__default__":
        return ""
    try:
        service = service_from(request)
        sid = request.match_info.get("session_id")
        if sid:
            saved = session_schema.get_saved_characters(service._get_session_state(sid))
            # "default" 是旧前端曾发送的默认角色占位；若用户确实创建了同名自定义角色，
            # 则优先把它当真实角色键。"__default__" 才是无条件保留的系统占位。
            if value == "default":
                entry = saved.get(value)
                return value if isinstance(entry, dict) and entry.get("is_default") is not True else ""
        if sid and hasattr(service, "_default_character_payload"):
            default_id = str(service._default_character_payload().get("id") or "").strip()
            if default_id and value == default_id:
                entry = saved.get(value)
                # 用户创建过同名自定义角色时不映射；否则默认角色 id 一律归一到空串键。
                if not isinstance(entry, dict) or entry.get("is_default") is True:
                    return ""
    except Exception:
        pass
    if value == "default":
        return ""
    return value


def character_operation_lock(service, session_id: str) -> asyncio.Lock:
    if hasattr(service, "character_operation_lock"):
        return service.character_operation_lock(session_id)
    locks = getattr(service, "_character_op_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        service._character_op_locks = locks
    lock = locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[session_id] = lock
    return lock


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
    app.router.add_get("/api/commands", api_commands)
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
    app.router.add_post("/api/sessions/{session_id:.+}/wardrobe", api_update_wardrobe)
    app.router.add_post("/api/sessions/{session_id:.+}/characters/{character_id:[^/]+}/avatar", api_generate_character_avatar)
    app.router.add_get("/api/sessions/{session_id:.+}/characters/{character_id:[^/]+}/avatar-image", api_character_avatar_image)
    app.router.add_get("/api/sessions/{session_id:.+}/characters/{character_id:[^/]+}/checkpoints", api_character_checkpoints)
    app.router.add_get("/api/sessions/{session_id:.+}/characters/{character_id:[^/]+}/checkpoints/{checkpoint_date}", api_export_character_checkpoint)
    app.router.add_get("/api/sessions/{session_id:.+}/characters/{character_id:[^/]+}/checkpoint-current", api_export_character_current_checkpoint)
    app.router.add_delete("/api/sessions/{session_id:.+}/characters/{character_id:.+}", api_delete_character)
    app.router.add_post("/api/sessions/{session_id:.+}/characters/{character_id:.+}/activate", api_activate_character)
    app.router.add_get("/api/sessions/{session_id:.+}/diaries", api_diaries)
    app.router.add_get("/api/sessions/{session_id:.+}/diaries/{diary_date:.+}", api_diary_detail)
    app.router.add_post("/api/sessions/{session_id:.+}/diaries/{diary_date:.+}", api_save_diary)
    app.router.add_delete("/api/sessions/{session_id:.+}/diaries/{diary_date:.+}", api_delete_diary)
    app.router.add_post("/api/sessions/{session_id:.+}/freeze", api_freeze_session)
    app.router.add_post("/api/sessions/{session_id:.+}/unfreeze", api_unfreeze_session)
    app.router.add_post("/api/sessions/{session_id:.+}/organize-memories", api_organize_memories)
    app.router.add_post("/api/sessions/{session_id:.+}/test-push", api_test_push_selected_character)
    app.router.add_get("/api/sessions/{session_id:.+}/history-summary", api_get_history_summary)
    app.router.add_put("/api/sessions/{session_id:.+}/history-summary", api_save_history_summary)
    # 通用会话路由放在最后，且只匹配不含 / 的 session_id（session_id 含 : 但不含 /）
    app.router.add_get("/api/sessions/{session_id:[^/]+}", api_session_detail)
    app.router.add_patch("/api/sessions/{session_id:[^/]+}", api_update_session)
    app.router.add_delete("/api/sessions/{session_id:[^/]+}", api_delete_session)
    app.router.add_get("/api/models", api_model_profiles)
    app.router.add_post("/api/models/{profile_id}", api_save_model_profile)
    app.router.add_delete("/api/models/{profile_id}", api_delete_model_profile)
    app.router.add_patch("/api/models/settings", api_update_model_settings)
    app.router.add_get("/api/prompt-slots/{session_id:.+}", api_prompt_slots)
    app.router.add_post("/api/world/{session_id:.+}/places/refresh", api_world_refresh_places)
    app.router.add_post("/api/world/{session_id:.+}/life-plan", api_world_life_plan_generate)
    app.router.add_post("/api/world/{session_id:.+}/life-plan/goals", api_world_life_plan_goal_create)
    app.router.add_patch("/api/world/{session_id:.+}/life-plan/goals/{kind:[^/]+}/{goal_id:[^/]+}", api_world_life_plan_goal_update)
    app.router.add_delete("/api/world/{session_id:.+}/life-plan/goals/{kind:[^/]+}/{goal_id:[^/]+}", api_world_life_plan_goal_delete)
    app.router.add_get("/api/world/{session_id:.+}", api_world_route)
    app.router.add_post("/api/bot/start", api_bot_start)
    app.router.add_post("/api/bot/stop", api_bot_stop)
    app.router.add_post("/api/service/reload-config", api_service_reload_config)
    app.router.add_post("/api/service/restart", api_service_restart)
    app.router.add_post("/api/service/stop", api_service_stop)
    app.router.add_get("/api/admin/llm-usage", api_admin_llm_usage)
    app.router.add_post("/api/admin/migrate-visual-tags", api_migrate_visual_tags)
    app.router.add_post("/api/admin/cleanup-prompt-prefix", api_cleanup_prompt_prefix)
    app.router.add_post("/api/admin/git-update", api_admin_git_update)
    app.router.add_post("/api/admin/freeze-inactive", api_freeze_inactive)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/logs/llm-debug", api_llm_debug_log)
    app.router.add_get("/api/logs/system-errors", api_system_error_log)
    app.router.add_get("/api/logs/{chat_id:.+}", api_log_detail)
    app.router.add_delete("/api/logs/{chat_id:.+}", api_log_clear)
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


def serialize_life_plan_preview(service, session_id: str) -> dict[str, Any]:
    enabled = service._life_plan_enabled(session_id) if hasattr(service, "_life_plan_enabled") else False
    character_key = active_context_character_key(service, session_id)
    row = None
    if enabled and hasattr(service, "_load_life_plan_row"):
        row = service._load_life_plan_row(session_id, character_key)
    if not row:
        return {
            "enabled": enabled,
            "exists": False,
            "character_key": character_key,
            "updated_at": 0,
            "updated_ago": "",
            "long_goals": [],
            "mid_goals": [],
            "today": {"date": "", "texture": "", "events": []},
        }

    payload = row.get("payload") if isinstance(row, dict) else {}
    if hasattr(service, "_normalize_life_plan_payload"):
        payload = service._normalize_life_plan_payload(payload, session_id=session_id)
    payload = payload if isinstance(payload, dict) else {}
    long_goals = payload.get("long_goals") if isinstance(payload.get("long_goals"), list) else []
    mid_goals = payload.get("mid_goals") if isinstance(payload.get("mid_goals"), list) else []
    today = payload.get("today") if isinstance(payload.get("today"), dict) else {}
    long_by_id = {str(item.get("id") or ""): item for item in long_goals if isinstance(item, dict)}
    mid_by_id = {str(item.get("id") or ""): item for item in mid_goals if isinstance(item, dict)}

    def goal_item(item: dict[str, Any], *, parent: bool = False) -> dict[str, Any]:
        result = {
            "id": str(item.get("id") or ""),
            "text": str(item.get("text") or ""),
            "status": str(item.get("status") or "active"),
            "updated_date": str(item.get("updated_date") or ""),
        }
        if parent:
            parent_id = str(item.get("parent_id") or "")
            result["parent_id"] = parent_id
            result["parent_text"] = str((long_by_id.get(parent_id) or {}).get("text") or "")
            result["parent_dimension"] = str((long_by_id.get(parent_id) or {}).get("dimension") or "")
            result["progress_note"] = str(item.get("progress_note") or "")
        else:
            result["motivation"] = str(item.get("motivation") or "")
            result["dimension"] = str(item.get("dimension") or "")
        return result

    events = []
    for event in today.get("events") or []:
        if not isinstance(event, dict):
            continue
        place_key = str(event.get("place_key") or "")
        related_id = str(event.get("related_mid_id") or "")
        events.append({
            "id": str(event.get("id") or ""),
            "time_hint": str(event.get("time_hint") or ""),
            "text": str(event.get("text") or ""),
            "status": str(event.get("status") or ""),
            "place_key": place_key,
            "place_label": PLACE_TYPES.get(place_key, {}).get("label", place_key),
            "related_mid_id": related_id,
            "related_mid_text": str((mid_by_id.get(related_id) or {}).get("text") or ""),
            "side_note": str(event.get("side_note") or ""),
        })

    updated = float(row.get("updated_at", 0) or 0) if isinstance(row, dict) else 0
    return {
        "enabled": enabled,
        "exists": True,
        "character_key": character_key,
        "updated_at": updated,
        "updated_ago": human_ago(time.time() - updated) if updated else "",
        "long_goals": [goal_item(item) for item in long_goals if isinstance(item, dict)],
        "mid_goals": [goal_item(item, parent=True) for item in mid_goals if isinstance(item, dict)],
        "today": {
            "date": str(today.get("date") or ""),
            "texture": str(today.get("texture") or ""),
            "events": events,
        },
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
        "life_plan": serialize_life_plan_preview(service, session_id),
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
        "public_fallback_outfit": session_schema.get_public_fallback_outfit(state),
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


PUBLIC_FALLBACK_CLOSET_PREFIX = "public fallback "


def _split_public_fallback_closet(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """把系统兜底收藏从普通衣橱里拆出来，避免 WebUI 把内部 key 展示给用户。"""
    public_fallback = dict(session_schema.get_public_fallback_outfit(state))
    visible_closet: dict[str, Any] = {}
    closet = session_schema.get_closet(state)
    for name, entry in (closet or {}).items():
        if not isinstance(entry, dict):
            continue
        key = str(name or "")
        slot = str(entry.get("slot") or "").strip()
        tags = str(entry.get("tags") or "").strip()
        if key.startswith(PUBLIC_FALLBACK_CLOSET_PREFIX):
            if slot and tags and not public_fallback.get(slot):
                public_fallback[slot] = tags
            continue
        visible_closet[key] = entry
    return visible_closet, public_fallback


def _public_fallback_in_current(wardrobe: dict[str, Any], public_fallback: dict[str, Any]) -> bool:
    for slot, tags in (public_fallback or {}).items():
        if not slot or not str(tags or "").strip():
            continue
        if appearance_rules.normalize_appearance_text(wardrobe.get(slot) or "") == appearance_rules.normalize_appearance_text(tags):
            return True
    return False


def _wardrobe_display_names(wardrobe: dict[str, Any], closet: dict[str, Any]) -> dict[str, str]:
    """当前穿搭用英文 tags 做 prompt 真源，但 WebUI 展示优先用衣橱里的中文短名。"""
    display: dict[str, str] = {}
    if not isinstance(wardrobe, dict) or not isinstance(closet, dict):
        return display
    for slot, tags in wardrobe.items():
        slot_text = str(slot or "").strip()
        norm_tags = appearance_rules.normalize_appearance_text(tags or "")
        if not slot_text or not norm_tags:
            continue
        best_name = ""
        best_time = -1.0
        for name, entry in closet.items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("slot") or "").strip() != slot_text:
                continue
            if appearance_rules.normalize_appearance_text(entry.get("tags") or "") != norm_tags:
                continue
            display_name = str(name or "").strip()
            if not display_name or display_name.startswith(PUBLIC_FALLBACK_CLOSET_PREFIX):
                continue
            worn_at = float(entry.get("last_worn") or entry.get("added_at") or 0)
            if worn_at >= best_time:
                best_name = display_name
                best_time = worn_at
        if best_name:
            display[slot_text] = best_name
    return display


def serialize_current_clothing(service, state: dict[str, Any]) -> dict[str, Any]:
    wardrobe = service._get_wardrobe(state)
    item_states = {
        slot: value for slot, value in session_schema.get_wardrobe_item_states(state).items()
        if slot in wardrobe
    }
    closet, public_fallback = _split_public_fallback_closet(state)
    return {
        "dynamic_appearance": session_schema.get_outfit(state),
        "wardrobe": wardrobe,
        "wardrobe_display": _wardrobe_display_names(wardrobe, closet),
        "wardrobe_item_states": item_states,
        "public_fallback_outfit": public_fallback,
        "public_fallback_in_current": _public_fallback_in_current(wardrobe, public_fallback),
        "closet": closet,
        "nudity": session_schema.get_nudity(state),
    }


def _apply_wardrobe_direct(service, sid: str, state: dict[str, Any], wardrobe: dict[str, Any]) -> str:
    session_schema.set_wardrobe(state, wardrobe)
    session_schema.prune_wardrobe_item_states(state, wardrobe)
    rendered = appearance_rules.render_wardrobe(wardrobe)
    session_schema.set_outfit(state, rendered)
    if rendered.strip():
        session_schema.clear_nudity(state)
    service._save_session_state(sid, state)
    return rendered


async def api_update_wardrobe(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return json_error("衣柜操作必须是 JSON 对象")

    # 前端 data-* 属性用连字符命名，统一成下划线再分发。
    action = str(payload.get("action") or "apply").strip().replace("-", "_")
    state = service._get_session_state(sid)
    result = ""

    if action in {"apply", "replace"}:
        description = str(payload.get("description") or "").strip()
        if not description:
            return json_error("请输入要修改的穿搭")
        result = await service._apply_wardrobe(sid, description, replace=(action == "replace"))
        state = service._get_session_state(sid)
    elif action == "save_closet":
        description = str(payload.get("description") or "").strip()
        if not description:
            return json_error("请输入要收藏的衣物")
        change = await service._classify_wardrobe_items(state, description)
        names = change.get("names") if isinstance(change.get("names"), dict) else {}
        closet = session_schema.get_closet(state)
        now = time.time()
        added = [
            slot for slot in appearance_rules.WARDROBE_CLOTHING_SLOTS
            if appearance_rules.normalize_appearance_text(change.get(slot) or "")
        ]
        for slot in added:
            tags = appearance_rules.normalize_appearance_text(change.get(slot) or "")
            name = service._wardrobe_closet_display_name(description, slot, tags, names, added)
            closet = appearance_rules.closet_add(closet, name, slot, tags, now=now, worn=False)
        if not added:
            return json_error("没识别出可收藏的衣物（发型/瞳色/配饰不进衣橱）")
        session_schema.set_closet(state, closet)
        service._save_session_state(sid, state)
        result = session_schema.get_outfit(state)
    elif action == "closet_edit":
        name = str(payload.get("name") or "").strip()
        closet = dict(session_schema.get_closet(state))
        entry = closet.get(name)
        if not isinstance(entry, dict):
            return json_error("衣橱里没有这件衣服", status=404)
        new_name = str(payload.get("new_name") or "").strip() or name
        tags_raw = str(payload.get("tags") or "").strip()
        new_tags = appearance_rules.normalize_appearance_text(tags_raw) if tags_raw else str(entry.get("tags") or "").strip()
        if not new_tags:
            return json_error("衣物标签不能为空")
        if new_name != name and new_name in closet:
            return json_error("衣橱里已有同名衣物")
        slot = str(entry.get("slot") or "").strip()
        old_tags = str(entry.get("tags") or "")
        closet.pop(name, None)
        closet[new_name] = dict(entry, tags=new_tags)
        session_schema.set_closet(state, closet)
        wardrobe = dict(service._get_wardrobe(state))
        if slot and appearance_rules.normalize_appearance_text(wardrobe.get(slot) or "") == appearance_rules.normalize_appearance_text(old_tags):
            # 这件正穿在身上 → 同步更新当前穿搭标签。
            wardrobe[slot] = new_tags
            session_schema.clear_wardrobe_item_states(state, [slot])
            result = _apply_wardrobe_direct(service, sid, state, wardrobe)
        else:
            service._save_session_state(sid, state)
            result = session_schema.get_outfit(state)
    elif action == "closet_delete":
        name = str(payload.get("name") or "").strip()
        closet = dict(session_schema.get_closet(state))
        if name not in closet:
            return json_error("衣橱里没有这件衣服", status=404)
        closet.pop(name, None)
        session_schema.set_closet(state, closet)
        service._save_session_state(sid, state)
        result = session_schema.get_outfit(state)
    elif action == "clear":
        result = await service._apply_wardrobe(sid, "reset")
        state = service._get_session_state(sid)
    elif action == "set_item_state":
        slot = str(payload.get("slot") or "").strip()
        item_state = str(payload.get("state") or "").strip()
        if slot not in appearance_rules.WARDROBE_CLOTHING_SLOTS:
            return json_error("未知的衣物槽位")
        wardrobe = service._get_wardrobe(state)
        if not str(wardrobe.get(slot) or "").strip():
            return json_error("这个槽位当前没有穿着")
        session_schema.set_wardrobe(state, wardrobe)
        session_schema.set_wardrobe_item_state(state, slot, item_state)
        if session_schema.get_wardrobe_item_states(state):
            session_schema.clear_nudity(state)
        service._save_session_state(sid, state)
        result = session_schema.get_outfit(state)
    elif action == "clear_item_states":
        session_schema.clear_wardrobe_item_states(state)
        session_schema.clear_nudity(state)
        service._save_session_state(sid, state)
        result = session_schema.get_outfit(state)
    elif action == "wear_closet":
        name = str(payload.get("name") or "").strip()
        closet = session_schema.get_closet(state)
        entry = closet.get(name) if isinstance(closet, dict) else None
        if not isinstance(entry, dict):
            return json_error("衣橱里没有这件衣服", status=404)
        slot = str(entry.get("slot") or "").strip()
        tags = str(entry.get("tags") or "").strip()
        if slot not in appearance_rules.WARDROBE_CLOTHING_SLOTS or not tags:
            return json_error("这件收藏缺少可复穿的槽位或标签")
        wardrobe = appearance_rules.apply_wardrobe_change(service._get_wardrobe(state), {slot: tags})
        closet = appearance_rules.closet_add(closet, name, slot, tags, now=time.time())
        session_schema.set_closet(state, closet)
        session_schema.clear_wardrobe_item_states(state, [slot])
        result = _apply_wardrobe_direct(service, sid, state, wardrobe)
    elif action == "remove_slot":
        slot = str(payload.get("slot") or "").strip()
        removable = set(appearance_rules.WARDROBE_RENDER_ORDER)
        if slot not in removable:
            return json_error("未知的衣柜槽位")
        wardrobe = dict(service._get_wardrobe(state))
        wardrobe.pop(slot, None)
        session_schema.clear_wardrobe_item_states(state, [slot])
        result = _apply_wardrobe_direct(service, sid, state, wardrobe)
    elif action == "stash_public_fallback":
        closet, public_fallback = _split_public_fallback_closet(state)
        if not public_fallback:
            return json_error("当前没有公开场合兜底")
        wardrobe = dict(service._get_wardrobe(state))
        changed = False
        for slot, tags in public_fallback.items():
            if appearance_rules.normalize_appearance_text(wardrobe.get(slot) or "") == appearance_rules.normalize_appearance_text(tags):
                wardrobe.pop(slot, None)
                session_schema.clear_wardrobe_item_states(state, [slot])
                changed = True
        if not changed:
            return json_error("当前穿搭里没有这套公开兜底")
        session_schema.set_public_fallback_outfit(state, public_fallback)
        session_schema.set_closet(state, {**closet, **{
            name: entry for name, entry in session_schema.get_closet(state).items()
            if str(name or "").startswith(PUBLIC_FALLBACK_CLOSET_PREFIX)
        }})
        result = _apply_wardrobe_direct(service, sid, state, wardrobe)
    elif action == "clear_public_fallback":
        session_schema.clear_public_fallback_outfit(state)
        service._save_session_state(sid, state)
        result = session_schema.get_outfit(state)
    else:
        return json_error("未知的衣柜操作")

    if hasattr(service, "_snapshot_character"):
        service._snapshot_character(state)
    return json_ok({
        "result": result,
        "current": service._character_export_payload(state) if hasattr(service, "_character_export_payload") else {},
        "current_clothing": serialize_current_clothing(service, state),
    })


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
    # 修改全局作息时间或推送频率后，需要重新生成所有会话今天的推送时间列表
    global_schedule_keys = {
        "workday_wake_time", "workday_sleep_time",
        "weekend_wake_time", "weekend_sleep_time",
        "daily_selfie_limit",
    }
    schedule_changed = False
    try:
        for key, value in values.items():
            if key in YAML_ONLY_CONFIG_KEYS:
                continue
            if key in SECRET_KEYS and value in ("", None):
                continue
            old = service.config.get(key)
            service.config[key] = cast_config_value(key, value, old)
            if key in global_schedule_keys:
                schedule_changed = True
        service.save_config()
    except (TypeError, ValueError) as exc:
        return json_error(f"配置字段 {key} 的值无效: {exc}")
    if schedule_changed:
        for sid in list(service.sessions.keys()):
            try:
                s = service._get_session_state(sid)
                session_schema.set_daily_trigger_date(s, "")
                service._save_session_state(sid, s)
            except Exception:
                pass
    return json_ok({"config": masked_config(service)})


async def api_commands(request: web.Request):
    commands = [canonical for canonical, _aliases in COMMAND_ALIAS_GROUPS]
    return json_ok({"commands": commands})


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
        "custom_workday_wake_time", "custom_workday_sleep_time",
        "custom_weekend_wake_time", "custom_weekend_sleep_time",
    }
    life_profile_keys = {
        "custom_scheduled_persona", "custom_role_name", "custom_bot_name",
        "custom_character", "custom_series", "custom_visual_character", "custom_visual_series", "custom_character_age_stage",
        "custom_character_occupation", "custom_character_day_anchor",
    }
    # 修改作息时间或推送频率后，需要重新生成今天的推送时间列表
    schedule_keys = {
        "custom_workday_wake_time", "custom_workday_sleep_time",
        "custom_weekend_wake_time", "custom_weekend_sleep_time",
        "custom_daily_selfie_limit",
    }
    profile_touched = False
    schedule_touched = False
    for key in allowed:
        if key in payload:
            value = "" if payload[key] is None else str(payload[key])
            if key in session_schema.STATE_SCHEMA and session_schema.is_character_config_key(key):
                session_schema.set_character_value(state, key, value)
            else:
                state[key] = value
            if key in life_profile_keys:
                profile_touched = True
            if key in schedule_keys:
                schedule_touched = True
    # dynamic_appearance 现走 clothing box（不在 allowed 里直写顶层，避免遗留陈旧顶层键）。
    if "dynamic_appearance" in payload:
        session_schema.set_outfit(state, "" if payload["dynamic_appearance"] is None else str(payload["dynamic_appearance"]))
    if profile_touched:
        state.pop("life_profile", None)
    if schedule_touched:
        session_schema.set_daily_trigger_date(state, "")
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
    char = required_character_key_from_request(request)
    if char is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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
    char = required_character_key_from_request(request, payload)
    if char is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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
    char = required_character_key_from_request(request, payload)
    if char is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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
    char = required_character_key_from_request(request)
    if char is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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
    elif default_id and isinstance(characters.get(default_id), dict) and characters[default_id].get("is_default") is True:
        # 默认角色生成头像后会在会话角色池留一条带头像元数据的记录；展示字段仍以实时
        # config 为准，只从该记录继承头像，避免后续编辑默认配置后 UI 继续显示旧快照。
        avatar_meta = {
            key: characters[default_id].get(key)
            for key in ("avatar_path", "avatar_updated_at")
            if characters[default_id].get(key) not in (None, "")
        }
        characters[default_id] = {**default_char, **avatar_meta}
    if (
        not character_value(state, "custom_character", "")
        and not character_value(state, "persona_user_set", False)
    ):
        active_id = default_id
    checkpoints: dict[str, list[dict[str, Any]]] = {}
    if hasattr(service, "list_character_checkpoints"):
        for cid in characters:
            try:
                key = service._web_character_checkpoint_key(sid, cid) if hasattr(service, "_web_character_checkpoint_key") else cid
                checkpoints[cid] = service.list_character_checkpoints(sid, key)
            except Exception:
                checkpoints[cid] = []
    return json_ok({
        "active_id": active_id,
        "default_id": default_id,
        "current": service._character_export_payload(state) if hasattr(service, "_character_export_payload") else {},
        "current_clothing": serialize_current_clothing(service, state),
        "style_pool": service._normalize_style_pool() if hasattr(service, "_normalize_style_pool") else [],
        "characters": characters,
        "checkpoints": checkpoints,
    })


async def api_save_character(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    payload = await request.json()
    if not isinstance(payload, dict):
        return json_error("角色数据必须是 JSON 对象")
    # activate/导入会切换活动角色，纳入角色操作锁，与头像/推送/Telegram 消息处理互斥。
    async with character_operation_lock(service, sid):
        return await _save_character_locked(service, sid, payload, request)


async def _save_character_locked(service, sid: str, payload: dict[str, Any], request: web.Request):
    if hasattr(service, "is_character_checkpoint_payload") and service.is_character_checkpoint_payload(payload):
        import_mode = request.query.get("import_mode") or payload.get("import_mode") or payload.get("_import_mode") or "basic"
        try:
            result = service.import_character_checkpoint(sid, payload, mode=import_mode)
        except Exception as exc:
            return json_error(f"检查点导入失败：{exc}")
        state = service._get_session_state(sid)
        return json_ok({
            "active_id": character_value(state, "custom_character", "") or "",
            "current": service._character_export_payload(state),
            "characters": session_schema.get_saved_characters(state),
            "import_result": result,
        })
    state = service._get_session_state(sid)
    key = str(payload.get("id") or payload.get("character") or payload.get("bot_name") or "").strip()
    if not key:
        return json_error("角色 JSON 必须包含 id 或 character")
    if key == "__default__":
        return json_error("__default__ 是系统保留角色键，请使用其他角色名")
    # 默认角色以 config 为存储：仅当该角色不在 saved_characters（用户没创建过同名自定义角色）、
    # 且其 is_default 标记为真时，才走默认路径写回 config。否则走常规 saved_characters 路径。
    default_id = service._default_character_payload().get("id") or ""
    saved = session_schema.get_saved_characters(state)
    existing = saved.get(key)
    is_default_card = (
        key == default_id
        and payload.get("is_default") is True
        and (not isinstance(existing, dict) or existing.get("is_default") is True)
    )
    if is_default_card:
        service._apply_default_character_payload(payload)
        return json_ok({
            "active_id": character_value(state, "custom_character", "") or "",
            "current": service._character_export_payload(state),
            "characters": saved,
            "default": service._default_character_payload(),
        })
    # 自定义角色卡的 character 字段必须与存档键一致，防止 id≠character 键分裂。
    payload["character"] = key
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


def _safe_avatar_part(value: str) -> str:
    text = str(value or "").strip().replace("..", "_")
    return re.sub(r"[\s/\\:*?\"<>|]+", "_", text).strip("._") or "unknown"


def _avatar_file_path(service, session_id: str, character_id: str) -> Path:
    session_part = _safe_avatar_part(session_id)
    character_part = _safe_avatar_part(character_id)
    return service.state_path.parent / "avatars" / session_part / f"{character_part}.png"


def _avatar_public_marker(service, session_id: str, character_id: str) -> str:
    try:
        rel = _avatar_file_path(service, session_id, character_id).relative_to(service.state_path.parent)
        return rel.as_posix()
    except Exception:
        return f"avatars/{_safe_avatar_part(session_id)}/{_safe_avatar_part(character_id)}.png"


def _character_for_avatar(service, state: dict[str, Any], session_id: str, character_id: str) -> dict[str, Any]:
    saved = session_schema.get_saved_characters(state)
    saved_entry = saved.get(character_id)
    default_char = service._default_character_payload()
    default_id = default_char.get("id") or default_char.get("bot_name") or "default"
    if isinstance(saved_entry, dict) and saved_entry.get("is_default") is not True:
        return dict(saved_entry)
    if character_id in ("", "__default__"):
        return dict(default_char)
    active_id = active_character_id(state)
    if character_id == active_id:
        current = service._character_export_payload(state) if hasattr(service, "_character_export_payload") else {}
        if current:
            return dict(current)
    if character_id == default_id:
        avatar_meta = {
            key: saved_entry.get(key)
            for key in ("avatar_path", "avatar_updated_at")
            if isinstance(saved_entry, dict) and saved_entry.get(key) not in (None, "")
        }
        return {**default_char, **avatar_meta}
    return {}


def _character_payload_for_operation(service, state: dict[str, Any], session_id: str, character_id: str) -> dict[str, Any]:
    payload = _character_for_avatar(service, state, session_id, character_id)
    if not payload:
        return {}
    payload.setdefault("id", character_id)
    if not payload.get("character") and not payload.get("is_default"):
        payload["character"] = character_id
    return payload


def _selected_character_is_active(service, state: dict[str, Any], session_id: str, character_id: str, payload: dict[str, Any]) -> bool:
    active_key = active_context_character_key(service, session_id)
    if active_key and character_id == active_key:
        return True
    current_character = character_value(state, "custom_character", "")
    if current_character and current_character == (payload.get("character") or character_id):
        return True
    try:
        default_id = str(service._default_character_payload().get("id") or "").strip()
    except Exception:
        default_id = ""
    return bool(
        payload.get("is_default")
        and not current_character
        and not character_value(state, "persona_user_set", False)
        and character_id in ("", "__default__", default_id)
    )


def _switch_state_to_selected_character(service, session_id: str, state: dict[str, Any], character_id: str, payload: dict[str, Any]) -> None:
    if hasattr(service, "_save_current_character_context"):
        service._save_current_character_context(state)
    if hasattr(service, "_snapshot_character"):
        service._snapshot_character(state)
    next_payload = dict(payload)
    if "style" not in payload:
        next_payload.pop("style", None)
    if "purity" not in next_payload:
        next_payload["purity"] = None
    if payload.get("is_default"):
        # 系统默认角色由 config 实时提供，不能把默认值写成会话 custom_* 覆盖。
        for key in SESSION_CUSTOM_RESET_KEYS:
            session_schema.set_character_value(state, key, "")
        state.pop("custom_daily_selfie_limit", None)
        session_schema.set_character_value(state, "custom_allow_llm_change_appearance", None)
        session_schema.set_character_value(state, "persona_user_set", False)
        session_schema.set_character_value(state, "purity", None)
        session_schema.set_character_value(state, "purity_user_set", False)
    else:
        # 本函数只在切换角色时调用：包括 None 在内都应用目标卡自己的 purity，
        # 避免沿用上一个角色的会话覆盖。
        if hasattr(service, "_apply_character_payload"):
            service._apply_character_payload(state, next_payload)
        if not character_value(state, "custom_character", ""):
            session_schema.set_character_value(state, "custom_character", character_id)
    has_clothing_context = False
    if hasattr(service, "_restore_character_context"):
        has_clothing_context = service._restore_character_context(session_id, state)
    if hasattr(service, "_apply_card_outfit_after_switch"):
        service._apply_card_outfit_after_switch(state, next_payload, has_clothing_context=has_clothing_context)
    state.pop("life_profile", None)
    service._save_session_state(session_id, state)


def _merge_character_containers(dst_state: dict[str, Any], src_state: dict[str, Any]) -> None:
    dst_contexts = session_schema.get_character_contexts(dst_state)
    dst_contexts.clear()
    dst_contexts.update(copy.deepcopy(session_schema.get_character_contexts(src_state)))
    dst_saved = session_schema.get_saved_characters(dst_state)
    dst_saved.clear()
    dst_saved.update(copy.deepcopy(session_schema.get_saved_characters(src_state)))


def _avatar_scene_from_character(character: dict[str, Any]) -> tuple[str, str]:
    name = character.get("character") or character.get("bot_name") or "the character"
    role = character.get("role_name") or ""
    series = character.get("series") or ""
    persona = compact_text_for_avatar(character.get("persona") or "", 180)
    relationship = compact_text_for_avatar(character.get("relationship") or "", 120)
    scene_parts = [
        "single character avatar portrait",
        "upper body, shoulders visible, centered composition",
        "looking at viewer, calm natural expression",
        "clean simple background, no text, no logo, no UI",
        f"character name/reference: {name}",
    ]
    if role:
        scene_parts.append(f"role/type: {role}")
    if series:
        scene_parts.append(f"series/source: {series}")
    if persona:
        scene_parts.append(f"personality mood: {persona}")
    if relationship:
        scene_parts.append(f"relationship tone: {relationship}")
    appearance_parts = [
        character.get("count") or "",
        character.get("visual_character") or "",
        character.get("visual_series") or "",
        character.get("appearance") or "",
        character.get("outfit") or "",
        character.get("style") or "",
    ]
    one_shot = ", ".join(str(part).strip() for part in appearance_parts if str(part or "").strip())
    return ", ".join(scene_parts), one_shot


def compact_text_for_avatar(value: Any, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_chars].rstrip() + ("..." if len(text) > max_chars else "")


async def api_generate_character_avatar(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    async with character_operation_lock(service, sid):
        return await _generate_character_avatar_locked(service, sid, character_id)


async def _generate_character_avatar_locked(service, sid: str, character_id: str):
    state = service._get_session_state(sid)
    character = _character_for_avatar(service, state, sid, character_id)
    if not character:
        return json_error("角色不存在", status=404)
    scene, one_shot = _avatar_scene_from_character(character)
    avatar_state = copy.deepcopy(state)
    payload = dict(character)
    payload.setdefault("id", character_id)
    payload.setdefault("character", character.get("character") or character_id)
    if hasattr(service, "_apply_character_payload"):
        service._apply_character_payload(avatar_state, payload)
    original_session_state = service.sessions.get(sid)
    missing = object()
    session_cache_snapshots: dict[str, tuple[Any, bool, Any]] = {}
    for cache_name in ("_last_prompt_slots_by_session", "_last_generated_nltag_by_session"):
        cache = getattr(service, cache_name, missing)
        if isinstance(cache, dict):
            session_cache_snapshots[cache_name] = (cache, sid in cache, copy.deepcopy(cache.get(sid)))
        else:
            session_cache_snapshots[cache_name] = (cache, False, None)
    global_cache_snapshots: dict[str, Any] = {}
    for cache_name in ("_last_prompt_slots", "_last_generated_nltag"):
        old_value = getattr(service, cache_name, missing)
        global_cache_snapshots[cache_name] = missing if old_value is missing else copy.deepcopy(old_value)
    service.sessions[sid] = avatar_state
    try:
        ok, images, err = await service._do_generate(
            scene,
            session_id=sid,
            one_shot_appearance=one_shot,
            device_in_frame=False,
            orientation="2:3",
        )
    except Exception as exc:
        service._ulog(sid, "ERROR", f"CHARACTER_AVATAR_FAILED character={character_id} error={exc}")
        return json_error(f"头像生成失败: {exc}", status=502)
    finally:
        if original_session_state is not None:
            service.sessions[sid] = original_session_state
        else:
            service.sessions.pop(sid, None)
        # 头像链路使用临时角色态，会同时覆盖会话级和 legacy 全局展示缓存；恢复生成前快照，
        # 既保住活动角色最近一次提示词/照片历史，也不误删其他会话的缓存。
        for cache_name, (original_cache, had_sid, old_value) in session_cache_snapshots.items():
            current_cache = getattr(service, cache_name, None)
            if isinstance(original_cache, dict):
                target_cache = current_cache if isinstance(current_cache, dict) else original_cache
                if had_sid:
                    target_cache[sid] = old_value
                else:
                    target_cache.pop(sid, None)
                setattr(service, cache_name, target_cache)
            elif isinstance(current_cache, dict):
                current_cache.pop(sid, None)
                if original_cache is missing and not current_cache:
                    delattr(service, cache_name)
            elif original_cache is missing:
                if hasattr(service, cache_name):
                    delattr(service, cache_name)
            else:
                setattr(service, cache_name, original_cache)
        for cache_name, old_value in global_cache_snapshots.items():
            if old_value is missing:
                if hasattr(service, cache_name):
                    delattr(service, cache_name)
            else:
                setattr(service, cache_name, old_value)
    if not ok or not images:
        service._ulog(sid, "ERROR", f"CHARACTER_AVATAR_FAILED character={character_id} error={err}")
        return json_error(f"头像生成失败: {err or '无图片'}", status=502)
    path = _avatar_file_path(service, sid, character_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(images[0])
    except Exception as exc:
        service._ulog(sid, "ERROR", f"CHARACTER_AVATAR_SAVE_FAILED character={character_id} error={exc}")
        return json_error(f"头像保存失败: {exc}", status=500)
    marker = _avatar_public_marker(service, sid, character_id)
    updated_at = time.time()
    saved = session_schema.get_saved_characters(state)
    stored = dict(saved.get(character_id) or character)
    stored["avatar_path"] = marker
    stored["avatar_updated_at"] = updated_at
    saved[character_id] = stored
    service._save_session_state(sid, state)
    return json_ok({
        "character_id": character_id,
        "avatar_path": marker,
        "avatar_updated_at": updated_at,
        "characters": saved,
    })


async def api_character_avatar_image(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    path = _avatar_file_path(service, sid, character_id)
    if not path.exists():
        return json_error("头像不存在", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache, must-revalidate"})


async def api_character_checkpoints(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    if not hasattr(service, "list_character_checkpoints"):
        return json_error("当前服务不支持角色检查点", status=404)
    key = service._web_character_checkpoint_key(sid, character_id) if hasattr(service, "_web_character_checkpoint_key") else character_id
    return json_ok({"checkpoints": service.list_character_checkpoints(sid, key), "character_id": character_id})


def _checkpoint_filename(character_id: str, checkpoint_date: str, suffix: str = "") -> str:
    safe_char = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in str(character_id or "character"))
    suffix_part = f"-{suffix}" if suffix else ""
    return f"{safe_char}-{checkpoint_date}{suffix_part}.json"


async def api_export_character_checkpoint(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    if not hasattr(service, "read_character_checkpoint"):
        return json_error("当前服务不支持角色检查点", status=404)
    character_id = request.match_info["character_id"]
    checkpoint_date = request.match_info["checkpoint_date"]
    key = service._web_character_checkpoint_key(sid, character_id) if hasattr(service, "_web_character_checkpoint_key") else character_id
    try:
        checkpoint = service.read_character_checkpoint(sid, key, checkpoint_date)
    except FileNotFoundError:
        return json_error("检查点不存在", status=404)
    except Exception as exc:
        return json_error(f"检查点读取失败：{exc}")
    return json_ok({
        "checkpoint": checkpoint,
        "filename": _checkpoint_filename(character_id, checkpoint.get("checkpoint_date") or checkpoint_date),
    })


async def api_export_character_current_checkpoint(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    if not hasattr(service, "export_current_character_checkpoint"):
        return json_error("当前服务不支持角色检查点", status=404)
    character_id = request.match_info["character_id"]
    key = service._web_character_checkpoint_key(sid, character_id) if hasattr(service, "_web_character_checkpoint_key") else character_id
    try:
        checkpoint = service.export_current_character_checkpoint(sid, key)
    except Exception as exc:
        return json_error(f"当前状态导出失败：{exc}")
    return json_ok({
        "checkpoint": checkpoint,
        "filename": _checkpoint_filename(character_id, checkpoint.get("checkpoint_date") or "current", "current"),
    })


async def api_delete_character(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    default_id = service._default_character_payload().get("id") or ""
    state = service._get_session_state(sid)
    saved = session_schema.get_saved_characters(state)
    existing = saved.get(character_id)
    is_default_card = character_id == default_id and (
        not isinstance(existing, dict) or existing.get("is_default") is True
    )
    if is_default_card:
        return json_error("系统默认角色不能删除", status=403)
    saved.pop(character_id, None)
    character_key = service._web_character_checkpoint_key(sid, character_id) if hasattr(service, "_web_character_checkpoint_key") else character_id
    if hasattr(service, "memory") and hasattr(service.memory, "delete_character_memories"):
        service.memory.delete_character_memories(sid, character_key)
    if hasattr(service, "app_store") and hasattr(service.app_store, "delete_character_runtime_data"):
        service.app_store.delete_character_runtime_data(sid, character_key)
    if character_value(state, "custom_character", "") == character_id:
        for key in SESSION_CUSTOM_RESET_KEYS:
            session_schema.set_character_value(state, key, "")
        session_schema.set_outfit(state, "")
        session_schema.set_wardrobe(state, {})
        session_schema.set_closet(state, {})
        session_schema.clear_public_fallback_outfit(state)
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
    async with character_operation_lock(service, sid):
        return await _activate_character_locked(service, sid, character_id)


async def _activate_character_locked(service, sid: str, character_id: str):
    state = service._get_session_state(sid)
    data = _character_payload_for_operation(service, state, sid, character_id)
    if not data:
        return json_error("角色不存在", status=404)
    already_active = _selected_character_is_active(service, state, sid, character_id, data)
    if not already_active:
        _switch_state_to_selected_character(service, sid, state, character_id, data)
    elif not data.get("is_default"):
        payload = dict(data)
        payload["role_name"] = character_value(state, "custom_role_name", "") or data.get("role_name", "")
        payload["bot_self_name"] = character_value(state, "custom_bot_self_name", "") or data.get("bot_self_name", "")
        payload["relationship"] = character_value(state, "custom_spatial_relationship", "") or data.get("relationship", "")
        if "style" not in data:
            payload.pop("style", None)
        if character_value(state, "purity_user_set", False):
            payload.pop("purity", None)
        if hasattr(service, "_apply_character_payload"):
            service._apply_character_payload(state, payload)
        state.pop("life_profile", None)
        service._save_session_state(sid, state)
    default_id = str(service._default_character_payload().get("id") or "").strip()
    active_id = character_value(state, "custom_character", "") or (default_id if data.get("is_default") else "")
    return json_ok({"active_id": active_id, "current": service._character_export_payload(state), "characters": session_schema.get_saved_characters(state)})


async def api_diaries(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_key = required_character_key_from_request(request)
    if character_key is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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
    character_key = required_character_key_from_request(request)
    if character_key is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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
    character_key = required_character_key_from_request(request, payload)
    if character_key is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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
    character_key = required_character_key_from_request(request)
    if character_key is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
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


async def api_delete_model_profile(request: web.Request):
    service = service_from(request)
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    if _is_admin(request) and request.query.get("user_id"):
        user_id = request.query.get("user_id") or user_id
    profile_id = request.match_info["profile_id"].strip()
    scope = str(request.query.get("scope") or "user").lower()
    if scope == "global":
        _require_admin(request)
        profiles = dict(service._global_model_profiles())
        if profile_id not in profiles:
            return json_error("模型 profile 不存在", status=404)
        profiles.pop(profile_id, None)
        service.config["global_model_profiles"] = profiles
        service.save_config()
        return json_ok({"global_profiles": mask_model_profiles(profiles)})
    if not service.app_store.delete_model_profile(user_id, profile_id):
        return json_error("模型 profile 不存在", status=404)
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


async def api_world_life_plan_generate(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    if not hasattr(service, "ensure_life_plan_for_today"):
        return json_error("生活线功能不可用", status=409)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    instruction = str(payload.get("instruction") or "").strip() if isinstance(payload, dict) else ""
    regenerate_goals = bool(isinstance(payload, dict) and payload.get("regenerate_goals"))
    async with character_operation_lock(service, sid):
        if (instruction or regenerate_goals) and hasattr(service, "regenerate_life_plan_goals"):
            result = await service.regenerate_life_plan_goals(
                sid,
                instruction=instruction,
                reason="web-instruction" if instruction else "web-goal-regenerate",
            )
        else:
            result = await service.ensure_life_plan_for_today(sid, force=True, reason="web")
        try:
            weather = await service._fetch_weather("", sid)
        except Exception:
            weather = None
        return json_ok({
            "result": result,
            "world": build_world_route_preview(service, sid, weather=weather),
            "life_plan": serialize_life_plan_preview(service, sid),
        })


async def _world_life_plan_response(service, sid: str, extra: dict[str, Any] | None = None):
    try:
        weather = await service._fetch_weather("", sid)
    except Exception:
        weather = None
    payload = {
        "world": build_world_route_preview(service, sid, weather=weather),
        "life_plan": serialize_life_plan_preview(service, sid),
    }
    if extra:
        payload.update(extra)
    return json_ok(payload)


async def api_world_life_plan_goal_create(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    if not hasattr(service, "upsert_life_plan_goal"):
        return json_error("生活线功能不可用", status=409)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    kind = str(payload.get("kind") or "").strip()
    if not kind:
        return json_error("缺少目标类型")
    try:
        row = service.upsert_life_plan_goal(sid, kind, payload)
    except ValueError as exc:
        return json_error(str(exc))
    return await _world_life_plan_response(service, sid, {"life_plan_row": row})


async def api_world_life_plan_goal_update(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    if not hasattr(service, "upsert_life_plan_goal"):
        return json_error("生活线功能不可用", status=409)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    payload["id"] = request.match_info["goal_id"]
    try:
        row = service.upsert_life_plan_goal(sid, request.match_info["kind"], payload)
    except ValueError as exc:
        return json_error(str(exc))
    return await _world_life_plan_response(service, sid, {"life_plan_row": row})


async def api_world_life_plan_goal_delete(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    if not hasattr(service, "delete_life_plan_goal"):
        return json_error("生活线功能不可用", status=409)
    try:
        row = service.delete_life_plan_goal(sid, request.match_info["kind"], request.match_info["goal_id"])
    except KeyError:
        return json_error("目标不存在", status=404)
    except ValueError as exc:
        return json_error(str(exc))
    return await _world_life_plan_response(service, sid, {"life_plan_row": row})


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


async def api_service_reload_config(request: web.Request):
    _require_admin(request)
    service = service_from(request)
    try:
        config = service.reload_config_from_disk()
    except Exception as exc:
        return json_error(f"配置文件重新载入失败: {exc}", status=500)
    return json_ok({
        "config": masked_config(service),
        "config_path": str(service.config_path),
        "loaded_keys": len(config),
    })


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
    char = required_character_key_from_request(request)
    if char is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
    if not service.has_llm_config("chat", sid):
        return json_error("聊天模型未配置，无法整理记忆")
    try:
        result = await service._organize_memories_after_dream(sid, char)
    except Exception as exc:
        return json_error(f"整理记忆失败: {exc}", status=500)
    memories = service.memory.list_memories(sid, character=char, limit=80)
    status = (result or {}).get("status") if isinstance(result, dict) else "ok"
    message = "记忆整理完成"
    if status == "no_op":
        message = "记忆整理完成：模型未给出需要执行的操作"
    elif status == "skipped":
        message = f"记忆整理跳过：{(result or {}).get('reason') or '无可整理内容'}"
    elif status in {"failed", "partial_failed"}:
        message = "记忆整理存在失败，详情已写入错误日志"
    return json_ok({"memories": memories, "character": char, "result": result, "message": message})


async def api_test_push_selected_character(request: web.Request):
    service = service_from(request)
    if not service.is_bot_running:
        return json_error("机器人尚未启动", status=409)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权操作此会话", status=403)
    payload = await request.json()
    char = required_character_key_from_request(request, payload)
    if char is None:
        return json_error("缺少 character_key，手动推送必须指定目标角色")
    mode = str(payload.get("mode") or payload.get("arg") or "normal").strip() or "normal"
    async with character_operation_lock(service, sid):
        return await _test_push_selected_character_locked(service, sid, char, mode)


async def _test_push_selected_character_locked(service, sid: str, char: str, mode: str):
    state = service._get_session_state(sid)
    character_payload = _character_payload_for_operation(service, state, sid, char)
    if not character_payload:
        return json_error("角色不存在", status=404)
    already_active = _selected_character_is_active(service, state, sid, char, character_payload)
    original_snapshot = copy.deepcopy(state)
    restored = already_active
    try:
        if not already_active:
            _switch_state_to_selected_character(service, sid, state, char, character_payload)
        now = service._session_now(sid)
        ok = await service._sched_fire(
            sid,
            now,
            mode_override=mode,
            skip_active_check=True,
            character_lock_held=True,
        )
        if not already_active:
            target_state = service._get_session_state(sid)
            if hasattr(service, "_save_current_character_context"):
                service._save_current_character_context(target_state)
            if hasattr(service, "_snapshot_character"):
                service._snapshot_character(target_state)
            _merge_character_containers(original_snapshot, target_state)
            service.sessions[sid] = original_snapshot
            service._save_session_state(sid, original_snapshot)
            restored = True
        message = "手动推送已发送" if ok else "手动推送未发送，详情请查看日志"
        return json_ok({"triggered": bool(ok), "character_key": char, "mode": mode, "message": message})
    except Exception as exc:
        service._ulog(sid, "ERROR", f"MANUAL_PUSH_FAILED character={char} mode={mode} error={exc}")
        return json_error(f"手动推送失败: {exc}", status=502)
    finally:
        if not restored:
            target_state = service.sessions.get(sid)
            if isinstance(target_state, dict):
                try:
                    if hasattr(service, "_save_current_character_context"):
                        service._save_current_character_context(target_state)
                    if hasattr(service, "_snapshot_character"):
                        service._snapshot_character(target_state)
                    _merge_character_containers(original_snapshot, target_state)
                except Exception:
                    pass
            service.sessions[sid] = original_snapshot
            service._save_session_state(sid, original_snapshot)


async def api_get_history_summary(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not _session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    char = required_character_key_from_request(request)
    if char is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
    key = char
    summary = ""
    try:
        meta = service.app_store.get_context_meta(sid, key)
        summary = (meta.get("character_history_summary") or "").strip()
    except Exception:
        pass
    if not summary and key == active_context_character_key(service, sid):
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
    char = required_character_key_from_request(request, payload)
    if char is None:
        return json_error("缺少 character_key，角色页操作必须指定目标角色")
    try:
        service.app_store.upsert_character_history_summary(sid, char, summary)
    except Exception as exc:
        return json_error(f"保存历史提要失败: {exc}", status=500)
    if char == active_context_character_key(service, sid):
        state = service._get_session_state(sid)
        session_schema.set_character_history_summary(state, summary)
        service._save_session_state(sid, state)
    return json_ok({"character_key": char, "summary": summary})


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
    if purpose not in ("chat", "image", "vision"):
        return json_error("purpose 必须是 chat、image 或 vision")
    try:
        if purpose == "vision":
            # 32x32 红色 PNG，用于测试视觉模型是否可用。
            # 部分视觉模型（如 qwen-vl）要求图片宽高均 > 10px，1x1 占位图会被 provider 拒绝。
            image_b64 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAANUlEQVR4nO3NMQEAMAjEwKe68K8CMZUQFracgKSmO5feaT0OFhwgB8gBcoAcIAfIAXKAHIR8qZcBlCKtHn4AAAAASUVORK5CYII="
            image_bytes = base64.b64decode(image_b64)
            description = await service._describe_image_for_chat(
                "", image_bytes, "image/png", source_label="测试图片"
            )
            return json_ok({"reply": description})
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
