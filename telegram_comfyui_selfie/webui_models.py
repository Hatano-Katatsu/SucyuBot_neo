from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

from aiohttp import web

from . import session_schema
from .webui_common import (
    is_admin,
    json_error,
    json_ok,
    parse_bool,
    require_admin,
    service_from,
)


SECRET_KEYS = {
    "telegram_bot_token", "llm_api_key", "chat_llm_api_key", "image_llm_api_key",
    "amap_api_key", "google_places_api_key", "tavily_api_key",
}
MODEL_SECRET_PLACEHOLDER = "********"
MODEL_SECRET_KEYS = {"api_key", "api_key_no_think"}
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
NUMERIC_CONFIG_KEYS = {
    "cfg", "default_purity", "height", "life_plan_max_events", "life_plan_max_long",
    "life_plan_max_mid", "timezone_offset", "turbo_strength", "width",
}
NUMERIC_CONFIG_SUFFIXES = (
    "_bytes", "_cfg", "_chars", "_count", "_days", "_hour", "_hours", "_limit",
    "_minutes", "_offset", "_penalty", "_per_type", "_port", "_purity", "_rounds",
    "_seconds", "_steps", "_strength", "_temperature", "_tokens", "_top_p",
)
CONFIG_ORDERED_NUMBER_PAIRS = (
    ("post_chat_push_delay_min_minutes", "post_chat_push_delay_max_minutes"),
    ("checkpoint_soft_limit_chars", "checkpoint_hard_limit_chars"),
    ("world_character_place_strong_hours", "world_character_place_ttl_hours"),
)


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


def _is_numeric_config_key(key: str) -> bool:
    return (
        key in NUMERIC_CONFIG_KEYS
        or "_temperature_" in key
        or key.endswith(NUMERIC_CONFIG_SUFFIXES)
    )


def _optional_finite_config_number(config: dict[str, Any], key: str) -> float | None:
    value = config.get(key)
    if value in ("", None):
        return None
    if isinstance(value, bool):
        raise ValueError(f"配置字段 {key} 必须是有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置字段 {key} 必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"配置字段 {key} 必须是有限数值")
    return number


def validate_config_candidate(config: dict[str, Any]) -> None:
    for key in config:
        if _is_numeric_config_key(str(key)):
            _optional_finite_config_number(config, str(key))
    for lower_key, upper_key in CONFIG_ORDERED_NUMBER_PAIRS:
        lower = _optional_finite_config_number(config, lower_key)
        upper = _optional_finite_config_number(config, upper_key)
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(f"配置字段 {lower_key} 不能大于 {upper_key}")


def prepare_config_candidate(current: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(current)
    for key, value in values.items():
        if key in YAML_ONLY_CONFIG_KEYS:
            continue
        if key in SECRET_KEYS and value in ("", None):
            continue
        try:
            candidate[key] = cast_config_value(key, value, candidate.get(key))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置字段 {key} 的值无效: {exc}") from exc
    validate_config_candidate(candidate)
    return candidate


def config_operation_lock(service) -> asyncio.Lock:
    if hasattr(service, "config_update_lock"):
        return service.config_update_lock()
    lock = getattr(service, "_web_config_update_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        service._web_config_update_lock = lock
    return lock


def _replace_config_sync(service, candidate: dict[str, Any]) -> None:
    if hasattr(service, "replace_config_and_save"):
        service.replace_config_and_save(candidate)
        return
    previous = service.config
    service.config = copy.deepcopy(candidate)
    try:
        service.save_config()
    except Exception:
        service.config = previous
        raise


async def _replace_config(service, candidate: dict[str, Any]) -> None:
    await asyncio.to_thread(_replace_config_sync, service, candidate)


async def api_config(request: web.Request):
    require_admin(request)
    return json_ok({"config": masked_config(service_from(request))})


async def api_save_config(request: web.Request):
    require_admin(request)
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
    schedule_changed = bool(global_schedule_keys.intersection(values))
    async with config_operation_lock(service):
        try:
            candidate = prepare_config_candidate(service.config, values)
        except (TypeError, ValueError) as exc:
            return json_error(str(exc))
        try:
            await _replace_config(service, candidate)
        except Exception as exc:
            return json_error(f"配置保存失败: {exc}", status=500)
    if schedule_changed:
        for sid in list(service.sessions.keys()):
            try:
                s = service._get_session_state(sid)
                session_schema.set_daily_trigger_date(s, "")
                service._save_session_state(sid, s)
            except Exception:
                pass
    return json_ok({"config": masked_config(service)})


async def api_model_profiles(request: web.Request):
    service = service_from(request)
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    if is_admin(request):
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
    if is_admin(request) and request.query.get("user_id"):
        user_id = request.query.get("user_id") or user_id
    if scope == "global":
        require_admin(request)
        async with config_operation_lock(service):
            profiles = copy.deepcopy(service._global_model_profiles())
            payload = merge_model_profile_secrets(payload, profiles.get(profile_id) or {})
            profiles[profile_id] = payload
            candidate = copy.deepcopy(service.config)
            candidate["global_model_profiles"] = profiles
            try:
                validate_config_candidate(candidate)
                await _replace_config(service, candidate)
            except Exception as exc:
                return json_error(f"模型 profile 保存失败: {exc}", status=500)
        return json_ok({"global_profiles": mask_model_profiles(profiles)})
    current = service.app_store.list_model_profiles(user_id).get(profile_id) or {}
    payload = merge_model_profile_secrets(payload, current)
    service.app_store.upsert_model_profile(user_id, profile_id, payload)
    return json_ok({"user_profiles": mask_model_profiles(service.app_store.list_model_profiles(user_id))})


async def api_delete_model_profile(request: web.Request):
    service = service_from(request)
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    if is_admin(request) and request.query.get("user_id"):
        user_id = request.query.get("user_id") or user_id
    profile_id = request.match_info["profile_id"].strip()
    scope = str(request.query.get("scope") or "user").lower()
    if scope == "global":
        require_admin(request)
        async with config_operation_lock(service):
            profiles = copy.deepcopy(service._global_model_profiles())
            if profile_id not in profiles:
                return json_error("模型 profile 不存在", status=404)
            profiles.pop(profile_id, None)
            candidate = copy.deepcopy(service.config)
            candidate["global_model_profiles"] = profiles
            try:
                validate_config_candidate(candidate)
                await _replace_config(service, candidate)
            except Exception as exc:
                return json_error(f"模型 profile 删除失败: {exc}", status=500)
        return json_ok({"global_profiles": mask_model_profiles(profiles)})
    if not service.app_store.delete_model_profile(user_id, profile_id):
        return json_error("模型 profile 不存在", status=404)
    return json_ok({"user_profiles": mask_model_profiles(service.app_store.list_model_profiles(user_id))})


async def api_update_model_settings(request: web.Request):
    service = service_from(request)
    user_id = (request.get("web_auth") or {}).get("user_id", "")
    if not user_id:
        return json_error("缺少用户身份", status=403)
    if is_admin(request) and request.query.get("user_id"):
        user_id = request.query.get("user_id") or user_id
    payload = await request.json()
    kwargs: dict[str, Any] = {}
    for key in ("chat_profile_id", "fast_profile_id", "vision_profile_id"):
        if key in payload:
            kwargs[key] = str(payload.get(key) or "")
    settings = service.app_store.update_user_model_settings(user_id, **kwargs)
    return json_ok({"settings": settings})
