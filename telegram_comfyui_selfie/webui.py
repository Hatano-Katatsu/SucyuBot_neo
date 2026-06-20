from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .world_runtime import PLACE_TYPES


SECRET_KEYS = {"telegram_bot_token", "llm_api_key", "chat_llm_api_key", "image_llm_api_key"}
WORLD_TIMELINE_HOURS = (6, 8, 11, 13, 16, 18, 20, 23)


def create_web_app(service) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app["service"] = service
    static_dir = Path(__file__).with_name("static")

    app.router.add_get("/", index)
    app.router.add_static("/static/", static_dir)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/config", api_save_config)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_get("/api/sessions/{session_id:.+}", api_session_detail)
    app.router.add_patch("/api/sessions/{session_id:.+}", api_update_session)
    app.router.add_delete("/api/sessions/{session_id:.+}", api_delete_session)
    app.router.add_get("/api/prompt-slots/{session_id:.+}", api_prompt_slots)
    app.router.add_post("/api/world/{session_id:.+}/places/refresh", api_world_refresh_places)
    app.router.add_get("/api/world/{session_id:.+}", api_world_route)
    app.router.add_post("/api/bot/start", api_bot_start)
    app.router.add_post("/api/bot/stop", api_bot_stop)
    app.router.add_post("/api/service/restart", api_service_restart)
    app.router.add_post("/api/admin/migrate-visual-tags", api_migrate_visual_tags)
    app.router.add_post("/api/admin/cleanup-prompt-prefix", api_cleanup_prompt_prefix)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/logs/{chat_id:.+}", api_log_detail)
    app.router.add_delete("/api/logs/{chat_id:.+}", api_log_clear)
    app.router.add_post("/api/actions/test-comfyui", api_test_comfyui)
    app.router.add_post("/api/actions/test-llm", api_test_llm)
    app.router.add_post("/api/actions/send-message", api_send_message)
    app.router.add_post("/api/actions/run-command", api_run_command)
    return app


async def index(request: web.Request):
    return web.FileResponse(Path(__file__).with_name("static") / "index.html")


def service_from(request: web.Request):
    return request.app["service"]


def json_ok(data: dict[str, Any] | None = None):
    payload = {"ok": True}
    if data:
        payload.update(data)
    return web.json_response(payload)


def json_error(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": message}, status=status)


def masked_config(service) -> dict[str, Any]:
    values = {}
    secret_present = {}
    for key, value in service.config.items():
        if key in SECRET_KEYS:
            values[key] = ""
            secret_present[key] = bool(value)
        else:
            values[key] = value
    return {"values": values, "secret_present": secret_present}


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "开启", "开", "允许"}


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
    last = state.get("last_interaction", 0)
    now = time.time()
    return {
        "session_id": session_id,
        "chat_id": service.chat_id_from_session(session_id),
        "character": state.get("custom_character") or "",
        "series": state.get("custom_series") or "",
        "purity": service._get_purity(session_id),
        "style": service._get_current_style(session_id),
        "location": service._get_session_cfg(session_id, "location", ""),
        "timezone": service._get_session_cfg(session_id, "timezone_offset", ""),
        "last_interaction": last,
        "last_interaction_ago": human_ago(now - last) if last else "无记录",
        "daily_push": f"{len(state.get('daily_triggered_times', []))}/{len(state.get('daily_trigger_times', []))}",
        "photos": len(state.get("sent_photos_history", [])),
        "saved_characters": len(state.get("saved_characters", {})),
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
        item = serialize_world_state(service.build_world_state(session_id, weather=weather, now=slot_now, mode="chat"))
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
            "custom_count": state.get("custom_count", ""),
            "custom_positive_prefix": state.get("custom_positive_prefix", ""),
            "custom_default_hair": state.get("custom_default_hair", ""),
            "custom_default_eyes": state.get("custom_default_eyes", ""),
            "custom_current_style": state.get("custom_current_style", ""),
            "dynamic_appearance": state.get("dynamic_appearance", ""),
            "custom_scene_preference": state.get("custom_scene_preference", ""),
            "custom_selfie_preference": state.get("custom_selfie_preference", ""),
        },
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
    sessions = [session_summary(service, sid, state) for sid, state in service.sessions.items()]
    data = {
        "bot_running": service.is_bot_running,
        "bot_username": service._bot_username,
        "process_id": os.getpid(),
        "process_started_at": service.process_started_at,
        "web_url": f"http://{config.get('web_host', '127.0.0.1')}:{config.get('web_port', 8787)}",
        "config_path": str(service.config_path),
        "state_path": str(service.state_path),
        "launch_script": str(Path.cwd() / "Start-SucyuBot.cmd"),
        "token_configured": bool(config.get("telegram_bot_token")),
        "llm_configured": service.has_llm_config("chat") and service.has_llm_config("image"),
        "chat_llm_configured": service.has_llm_config("chat"),
        "image_llm_configured": service.has_llm_config("image"),
        "comfyui_url": config.get("comfyui_url", ""),
        "chat_llm_model": service._get_llm_value("chat", "model", ""),
        "chat_llm_api_base": service._get_llm_value("chat", "api_base", ""),
        "image_llm_model": service._get_llm_value("image", "model", ""),
        "image_llm_api_base": service._get_llm_value("image", "api_base", ""),
        "generating": service._generating,
        "active_pushes": len(service._active_pushes),
        "sessions_count": len(service.sessions),
        "sessions": sessions,
    }
    return json_ok({"status": data})


async def api_config(request: web.Request):
    return json_ok({"config": masked_config(service_from(request))})


async def api_save_config(request: web.Request):
    service = service_from(request)
    payload = await request.json()
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        return json_error("配置数据格式不正确")
    for key, value in values.items():
        if key in SECRET_KEYS and value in ("", None):
            continue
        old = service.config.get(key)
        service.config[key] = cast_config_value(key, value, old)
    service.save_config()
    return json_ok({"config": masked_config(service)})


async def api_sessions(request: web.Request):
    service = service_from(request)
    sessions = [session_summary(service, sid, state) for sid, state in service.sessions.items()]
    return json_ok({"sessions": sessions})


async def api_session_detail(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if sid not in service.sessions:
        return json_error("会话不存在", status=404)
    return json_ok({"session": session_summary(service, sid, service.sessions[sid]), "state": service._get_session_state(sid)})


async def api_update_session(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if sid not in service.sessions:
        return json_error("会话不存在", status=404)
    payload = await request.json()
    state = service._get_session_state(sid)
    allowed = {
        "custom_scheduled_persona", "custom_role_name", "custom_bot_name", "custom_bot_self_name",
        "custom_spatial_relationship", "custom_location", "custom_timezone_offset",
        "custom_count", "custom_positive_prefix",
        "custom_default_hair", "custom_default_eyes", "custom_current_style", "dynamic_appearance",
        "custom_scene_preference", "custom_selfie_preference",
        "custom_character", "custom_series", "custom_visual_character", "custom_visual_series", "custom_daily_selfie_limit",
        "custom_character_age_stage", "custom_character_day_anchor",
    }
    life_profile_keys = {
        "custom_scheduled_persona", "custom_role_name", "custom_bot_name",
        "custom_character", "custom_series", "custom_visual_character", "custom_visual_series", "custom_character_age_stage",
        "custom_character_day_anchor",
    }
    profile_touched = False
    for key in allowed:
        if key in payload:
            state[key] = "" if payload[key] is None else str(payload[key])
            if key in life_profile_keys:
                profile_touched = True
    if profile_touched:
        state.pop("life_profile", None)
    if "purity" in payload:
        raw = str(payload["purity"]).strip()
        if raw:
            state["purity"] = max(0, min(10, int(raw)))
            state["purity_user_set"] = True
        else:
            state["purity"] = None
            state["purity_user_set"] = False
    if "custom_allow_llm_change_appearance" in payload:
        value = payload["custom_allow_llm_change_appearance"]
        if value in ("", None, "default"):
            state["custom_allow_llm_change_appearance"] = None
        else:
            state["custom_allow_llm_change_appearance"] = parse_bool(value)
    service._save_session_state(sid, state)
    return json_ok({"session": session_summary(service, sid, state), "state": state})


async def api_delete_session(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    service.sessions.pop(sid, None)
    service._write_state()
    return json_ok()


async def api_prompt_slots(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if sid not in service.sessions:
        return json_error("会话不存在", status=404)
    scene = request.query.get("scene", "{场景描述}")
    return json_ok({"prompt": serialize_prompt_slots(service, sid, scene=scene)})


async def api_world_route(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if sid not in service.sessions:
        return json_error("会话不存在", status=404)
    try:
        weather = await service._fetch_weather("", sid)
    except Exception:
        weather = None
    return json_ok({"world": build_world_route_preview(service, sid, weather=weather)})


async def api_world_refresh_places(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if sid not in service.sessions:
        return json_error("会话不存在", status=404)
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
    service = service_from(request)
    try:
        await service.start_bot()
    except Exception as exc:
        return json_error(str(exc), status=409)
    return json_ok({"bot_username": service._bot_username})


async def api_bot_stop(request: web.Request):
    service = service_from(request)
    await service.stop_bot()
    return json_ok()


async def api_service_restart(request: web.Request):
    service = service_from(request)
    try:
        restart = service.prepare_process_restart()
    except Exception as exc:
        return json_error(f"无法准备重启: {exc}", status=500)
    asyncio.create_task(service.shutdown_for_process_restart())
    return json_ok({"restart": restart})


async def api_migrate_visual_tags(request: web.Request):
    service = service_from(request)
    return json_ok({"migration": service.migrate_visual_identity_tags()})


async def api_cleanup_prompt_prefix(request: web.Request):
    service = service_from(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    apply_changes = parse_bool(payload.get("apply", False)) if isinstance(payload, dict) else False
    cleanup = service.cleanup_prompt_prefix_slots(apply=apply_changes)
    return json_ok({"cleanup": cleanup})


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
            state = service.sessions.get(sid, {})
            items.append({
                "chat_id": chat_id,
                "session_id": sid,
                "character": state.get("custom_character") or "",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_ago": human_ago(time.time() - stat.st_mtime),
            })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return json_ok({"logs": items, "enabled": service._user_log_enabled(), "dir": str(log_dir)})


async def api_log_detail(request: web.Request):
    service = service_from(request)
    chat_id = request.match_info["chat_id"]
    path = service._user_log_path(service.session_id_for_chat(chat_id))
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
    path = service._user_log_path(service.session_id_for_chat(chat_id))
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        return json_error(str(exc), status=500)
    return json_ok()


async def api_test_comfyui(request: web.Request):
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
    try:
        await asyncio.wait_for(service.dispatch_command(chat_id, sid, command, arg), timeout=900)
    except Exception as exc:
        return json_error(str(exc), status=502)
    return json_ok()
