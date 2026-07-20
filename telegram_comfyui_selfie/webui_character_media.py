from __future__ import annotations

import copy
import re
import time
from typing import Any

from aiohttp import web

from . import session_schema
from .character_artifacts import (
    avatar_file_path as _avatar_file_path,
    avatar_public_marker as _avatar_public_marker,
    safe_avatar_part as _safe_avatar_part,
)
from .webui_characters import _character_for_avatar, character_operation_lock
from .webui_common import json_error, json_ok, service_from, session_allowed


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
    if not session_allowed(request, sid):
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
    if not session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    path = _avatar_file_path(service, sid, character_id)
    if not path.exists():
        return json_error("头像不存在", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache, must-revalidate"})


async def api_character_checkpoints(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not session_allowed(request, sid):
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
    if not session_allowed(request, sid):
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
    if not session_allowed(request, sid):
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
