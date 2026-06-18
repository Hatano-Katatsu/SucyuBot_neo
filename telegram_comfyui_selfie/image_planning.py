from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

from .defaults import DEFAULT_CONFIG, WEEKDAY_NAMES

logger = logging.getLogger(__name__)

VALID_VIEWS = {"selfie", "mirror", "pov", "third"}


def normalize_view(view: str | None) -> str:
    view = (view or "").strip().lower()
    return view if view in VALID_VIEWS else ""


def format_dialog_context(service: Any, state: dict[str, Any], session_id: str = "", limit: int = 12) -> str | None:
    lines = []
    history = state.get("chat_history", [])
    try:
        start = int(state.get("short_context_start", 0) or 0)
    except Exception:
        start = 0
    if start < 0 or start > len(history):
        start = 0
    for msg in history[start:][-limit:]:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        role = "用户" if msg.get("role") == "user" else "角色"
        lines.append(f"{role}: {content}")

    recent = []
    now_ts = time.time()
    for msg in state.get("recent_message_history", []):
        if now_ts - msg.get("time", 0) < 3 * 3600:
            dt = datetime.fromtimestamp(msg["time"], service._session_tz(session_id))
            recent.append(f"[{dt.strftime('%H:%M')}] 用户: {msg.get('text', '')}")
    if recent:
        lines.append("近 3 小时用户发言:\n" + "\n".join(recent))
    return "\n".join(lines) if lines else None


def format_sent_photo_context(service: Any, state: dict[str, Any], session_id: str = "", limit: int = 5) -> str | None:
    reset_time = float(state.get("short_context_reset_time", 0) or 0)
    photos = [
        photo for photo in state.get("sent_photos_history", [])
        if not reset_time or photo.get("timestamp", 0) >= reset_time
    ][-limit:]
    if not photos:
        return None
    lines = []
    tz = service._session_tz(session_id)
    for photo in photos:
        ts = photo.get("timestamp", 0)
        stamp = datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "未知时间"
        scene = (photo.get("scene") or "").strip()
        source = (photo.get("source_description") or "").strip()
        view = (photo.get("view") or "").strip() or "未知视角"
        appearance = (photo.get("appearance") or "").strip()
        parts = [f"[{stamp}] {view}: {scene}"]
        if source and source != scene:
            parts.append(f"原始描述: {source}")
        if appearance:
            parts.append(f"外貌: {appearance}")
        lines.append("；".join(parts))
    return "\n".join(lines)


async def plan_roleplay_image(
    service: Any,
    session_id: str,
    *,
    intent: str = "",
    mood: str = "",
    must_include: str = "",
    prompt: str = "",
    view: str = "",
) -> dict[str, Any]:
    requested_view = normalize_view(view)
    fallback_scene = (prompt or intent or must_include).strip()
    if not service.has_llm_config("image"):
        return {"scene": fallback_scene, "caption": "", "view": requested_view, "new_appearance_tags": None}

    state = service._get_session_state(session_id)
    now = service._session_now(session_id)
    weather_data = await service._fetch_weather(session_id=session_id)
    weather = f"{weather_data['desc']} {weather_data['temp']} C" if weather_data else "未知"
    time_period = service._get_time_period(now.hour)
    weekday = WEEKDAY_NAMES[now.weekday()]
    safety = service._get_effective_safety(session_id)
    purity = service._get_purity(session_id)
    dynamic = state.get("dynamic_appearance") or service.config.get("dynamic_appearance", "")
    quirk = service._get_session_cfg(session_id, "character_quirk_rule", "")
    spatial = service._get_session_cfg(session_id, "spatial_relationship", DEFAULT_CONFIG["spatial_relationship"])
    bot_name = service._get_session_cfg(session_id, "bot_name", "蕾伊")
    bot_self_name = service._get_session_cfg(session_id, "bot_self_name", "我")
    role_name = service._get_session_cfg(session_id, "role_name", "魅魔")
    dialog_context = format_dialog_context(service, state, session_id)
    photo_context = format_sent_photo_context(service, state, session_id)
    memory_query = "\n".join(part for part in (intent, mood, must_include, prompt, dialog_context or "") if part)
    memory_context = ""
    if hasattr(service, "_long_term_memory_context"):
        memory_context = service._long_term_memory_context(session_id, memory_query, limit=8)

    system = (
        f"{service._get_effective_persona(session_id)}\n\n"
        "你是角色扮演图片导演，负责把聊天模型给出的图片意图整合成最终画面。\n"
        f"角色身份: 角色名参考「{bot_name}」，角色类型「{role_name}」，优先使用「{bot_self_name}」作为自称。\n"
        f"当前附加外貌: {dynamic or '无'}\n"
        f"角色性观念: {service._purity_directive(purity)}\n"
        f"当前场合: {time_period}, {weekday}, {safety.get('context', '')}。\n"
        f"默认物理空间关系: {spatial}\n"
        "你要综合用户最近的话、聊天模型的意图、最近发过的照片、时间天气、外貌和安全约束，"
        "输出适合发给用户的一张图。不要输出英文画图标签。\n"
        "公开场合必须穿着得体；私密场合可以更放松。避免和最近照片重复。"
    )
    if quirk:
        system += f"\n角色专属画面修补规则: {quirk}"
    system += (
        "\n视角规则: 身处同一空间或用户明确要靠近互动时优先 pov；"
        "异地、展示穿搭或回复照片请求时优先 selfie/mirror；需要叙事全景时用 third。"
        "必须输出严格 JSON: {\"scene\":\"...\",\"caption\":\"...\",\"view\":\"selfie|mirror|pov|third\",\"new_appearance_tags\":\"...\"}。"
        "new_appearance_tags 没有变化时留空。"
    )

    user = (
        f"当前天气: {weather}\n"
        f"图片意图: {intent or '未提供'}\n"
        f"情绪/关系推进: {mood or '未提供'}\n"
        f"必须包含: {must_include or '无'}\n"
        f"聊天模型画面草案: {prompt or '无'}\n"
        f"用户指定视角: {requested_view or '未指定'}"
    )
    if dialog_context:
        user += f"\n\n对话上下文:\n{dialog_context}"
    if photo_context:
        user += f"\n\n最近发过的照片:\n{photo_context}"
    if memory_context:
        user += f"\n\n长期记忆:\n{memory_context}"

    try:
        text = await service._call_llm(
            system,
            user,
            temp=float(service._get_llm_value("image", "temperature_scene", "0.95")),
            tag="roleplay-image-plan",
            purpose="image",
        )
        parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
    except Exception as exc:
        logger.error("roleplay image planning failed: %s", exc)
        return {"scene": fallback_scene, "caption": "", "view": requested_view, "new_appearance_tags": None}

    scene = (parsed.get("scene") or fallback_scene).strip()
    planned_view = normalize_view(parsed.get("view"))
    final_view = requested_view or planned_view or "selfie"
    return {
        "scene": scene,
        "caption": (parsed.get("caption") or "").strip(),
        "view": final_view,
        "new_appearance_tags": (parsed.get("new_appearance_tags") or "").strip(),
    }
