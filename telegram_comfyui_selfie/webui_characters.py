from __future__ import annotations

import asyncio
import copy
from typing import Any

from aiohttp import web

from . import appearance as appearance_rules
from . import session_schema
from .commands import SESSION_CUSTOM_RESET_KEYS
from .deletion_runtime import (
    DeletionBusyError,
    DeletionForbiddenError,
    DeletionNotFoundError,
)
from .webui_common import (
    character_value,
    json_error,
    json_ok,
    parse_bool,
    service_from,
    session_allowed,
)


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


async def api_characters(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not session_allowed(request, sid):
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
    if not session_allowed(request, sid):
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


async def api_delete_character(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_id = request.match_info["character_id"]
    async with character_operation_lock(service, sid):
        try:
            result = await service.delete_character(sid, character_id)
        except DeletionForbiddenError as exc:
            return json_error(str(exc), status=403)
        except DeletionNotFoundError as exc:
            return json_error(str(exc), status=404)
        except DeletionBusyError as exc:
            return json_error(str(exc), status=409)
    return json_ok({
        "active_id": result["active_id"],
        "characters": result["characters"],
        "deleted": result["deleted"],
    })


async def api_activate_character(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not session_allowed(request, sid):
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
