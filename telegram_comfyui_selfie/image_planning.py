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

INTIMATE_CONTEXT_ZH = frozenset({
    "交合", "做爱", "插入", "进入她", "抽插", "骑乘", "后入", "结合",
    "融为一体", "裸体相拥", "赤裸相拥", "缠绵", "交缠", "交媾",
    "在她体内", "进入体内", "律动", "进出", "顶入", "挺入", "侵入",
    "侵占", "占有她", "要了她", "亲密交互", "交合中", "结合中",
    # 事后/余韵/同床共枕等贴身画面（用户身体仍与角色同框）：同样按亲密处理，避免被当成单人自拍。
    "事后", "高潮", "余韵", "云雨", "鱼水之欢", "欢爱", "内射", "中出",
    "精液", "射进", "射在", "同床", "共枕", "事后温存", "相拥而眠",
})


def _detect_intimate_context(*sources: str) -> bool:
    combined = " ".join(s for s in sources if s)
    if not combined:
        return False
    return any(kw in combined for kw in INTIMATE_CONTEXT_ZH)


def normalize_view(view: str | None) -> str:
    view = (view or "").strip().lower()
    return view if view in VALID_VIEWS else ""


def scene_implies_mirror_selfie(text: str) -> bool:
    lowered = (text or "").lower()
    return "mirror selfie" in lowered or "mirror reflection" in lowered or "对镜" in lowered or "镜子" in lowered


USER_VISUAL_SUBJECT_RE = re.compile(
    r"^\s*[（(]?\s*你(?=[^，。；;,.]{0,40}(?:窝|坐|站|躺|靠|倚|跪|趴|蜷|穿|披|侧卧|斜倚|窝在|坐在|躺在|靠在|倚在|穿着))"
)


def normalize_scene_visual_subject(scene: str) -> str:
    scene = (scene or "").strip()
    if not scene:
        return scene
    return USER_VISUAL_SUBJECT_RE.sub(lambda m: m.group(0).replace("你", "角色", 1), scene, count=1)


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
        return {"scene": fallback_scene, "view": requested_view, "new_appearance_tags": None, "is_intimate": False}

    state = service._get_session_state(session_id)
    now = service._session_now(session_id)
    weather_data = await service._fetch_weather(session_id=session_id)
    weather = f"{weather_data['desc']} {weather_data['temp']} C" if weather_data else "未知"
    time_ctx = service._get_time_context(session_id, now=now, weather=weather_data)
    time_period = time_ctx.get("period") or service._get_time_period(now.hour)
    time_light = service._format_time_context(session_id, now=now, weather=weather_data)
    light_guard = service._format_light_guard(session_id, now=now, weather=weather_data)
    weekday = WEEKDAY_NAMES[now.weekday()]
    safety = service._get_effective_safety(session_id)
    purity = service._get_purity(session_id)
    dynamic = state.get("dynamic_appearance") or service.config.get("dynamic_appearance", "")
    prompt_prefs = service._prompt_scene_preferences(session_id) if hasattr(service, "_prompt_scene_preferences") else {}
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
    world_query = "\n".join(part for part in (intent, mood, must_include, prompt) if part)
    world_context = ""
    if hasattr(service, "_format_world_context"):
        try:
            world_context = service._format_world_context(
                session_id,
                world_query,
                weather=weather_data,
                mode="image",
            )
        except Exception:
            logger.debug("world context build failed for image planning", exc_info=True)

    intimate_hint = _detect_intimate_context(intent, mood, prompt, dialog_context or "")
    user_gender = service._get_user_gender(session_id) if hasattr(service, "_get_user_gender") else "male"
    user_g_zh = "女性" if user_gender == "female" else "男性"

    system = (
        f"{service._get_effective_persona(session_id)}\n\n"
        "Scene boundary: write scene as environment, camera framing, action, lighting, mood, and spatial context. "
        "Do not restate stable character appearance that is already in persona/current appearance/photo memory, such as hair color, eye color, body traits, species traits, or permanent accessories. "
        "Only mention clothing/accessories in scene when they are a deliberate one-shot visual change for this image; put one-shot visual tags in new_appearance_tags.\n"
        "你是角色扮演图片导演，负责把聊天模型给出的图片意图整合成最终画面。\n"
        f"角色身份: 角色名参考「{bot_name}」，角色类型「{role_name}」，优先使用「{bot_self_name}」作为自称。\n"
        f"当前附加外貌: {dynamic or '无'}\n"
        f"用户画面偏好: 场景偏好={prompt_prefs.get('scene_preference') or '无'}；自拍偏好={prompt_prefs.get('selfie_preference') or '无'}。\n"
        f"角色性观念: {service._purity_directive(purity)}\n"
        f"当前场合: {time_period}, {weekday}, {safety.get('context', '')}。\n"
        f"季节与自然光: {time_light}。\n"
        f"{light_guard}\n"
        f"默认物理空间关系: {spatial}\n"
        "你要综合用户最近的话、聊天模型的意图、最近发过的照片、时间天气、外貌和安全约束，"
        "输出适合发给用户的一张图。不要输出英文画图标签。\n"
        "公开场合必须穿着得体；私密场合可以更放松。避免和最近照片重复。"
    )
    if world_context:
        system += (
            f"\n{world_context}\n"
            "其中“角色当前所在/接下来动线”按现实时间天气推断，应当遵守，角色不要无理由瞬移。"
            "但“用户位置/空间关系判断”只是基于历史消息的参考，不是硬性指令："
            "请结合“默认物理空间关系”设定、最近对话内容、角色此刻所在地点与当前时段，"
            "自行判断【此刻用户是否和角色在同一空间】，并据此决定视角——"
            "判断同处则优先 pov 或近距离 third 同框互动；判断异地、独处或仅线上联系才用 selfie/mirror。"
        )
    if quirk:
        system += f"\n角色专属画面修补规则: {quirk}"
    system += (
        "\n场景类型自判: 只要角色与用户有贴身性接触（性交、骑乘、交合、爱抚、拥抱贴身、亲吻、前戏，"
        "或任何用户身体会与角色贴合入画的性暗示情形），都判为亲密场景 is_intimate=true；纯日常、无身体接触才是 false。"
        "性事刚结束的事后温存、同床共枕、相拥而眠、躺在对方身边、爱抚余韵等画面，只要用户的身体仍与角色同框贴近，"
        "同样判为 is_intimate=true（不要因为‘性行为已结束’就当成日常）。"
        "请在 JSON 里输出 is_intimate 布尔值。"
        + ("系统初步判断本次可能属于亲密场景，请重点确认。" if intimate_hint else "")
        + "\n【以下亲密交互规则仅当你判定 is_intimate=true 时适用；若判定为日常/非性场景，请完全忽略本段，按通用规则写】:\n"
        "- 视角固定为 pov（用户第一人称视角），严禁 selfie 或 mirror，不需要第三人称全景。\n"
        f"- 用户身体归属（关键，针对双人误画）: 画面焦点永远是角色（一名女性）。用户作为亲密伴侣入画时，只画用户的【{user_g_zh}】身体局部（手、手臂、胸膛或胸部、腹、背、腿），"
        "绝不能把用户写成有完整面部、发型、表情、迷离眼神的第二个主角，更不能让用户喧宾夺主。\n"
        f"凡“你的手/你的胸/你的背/你的腿”等用户身体部位，scene 要写成可见的{user_g_zh}身体局部，不要写成“另一个角色/她/第二个人”。"
        "除非用户明确要求双人同框，画面里被完整刻画的人物只有角色一名。\n"
        f"- 只画用户身体局部（手/臂/胸/腹/背/腿），不要画完整的{user_g_zh}全身或面部。\n"
        "- 人物优先: 重点在角色的表情（迷离、红晕、咬唇）、身体反应（汗水、潮红、轻颤）和互动姿态，弱化环境背景。\n"
        "- 场景精简: 环境灯光压到最短；构图近距离特写或半身近景。\n"
        "- 自拍物理规则不变: 不得出现手机、相机、镜子或拿手机的手。\n"
        "- new_appearance_tags 仍只填临时外观变化，不要把情绪或动作写进去。"
    )
    system += (
        "\n画面主体规则: 图片主体默认必须是角色，不要把“你/用户”写成画面中被观看的主角。"
        "用户只能作为视角来源、互动对象或少量局部元素出现；只有用户明确要求双人同框时，才允许用户作为第二主体。"
        "如果草案写成“你坐着/你躺着/你穿着”，必须改写为“角色坐着/她躺着/角色穿着”。"
        "角色名只用于台词称呼；默认或原创角色不要把名字当作画面标签，画面描述应依靠角色类型和外貌特征。既有作品角色可以保留角色名和作品名。"
    )
    system += (
        "\n单人构图硬规则: 当视角是 selfie 前摄自拍或 mirror 对镜自拍时，画面里【只能有角色一个人】，"
        "scene 绝不能写入第二个人（用户、伴侣、他、她、对方、男人、女人）或对方的完整身体、面部。"
        "如果此刻用户/伴侣的身体会和角色同框入画（如躺在身边、被搂着、贴身依偎），就不要用 selfie/mirror，"
        "必须改用 pov，并且只把对方写成画面边缘的身体局部（手、手臂、胸膛、腿等），不要写成完整的第二个人。"
    )
    system += (
        "\n视角规则: 身处同一空间或用户明确要靠近互动时优先 pov；"
        "异地、展示穿搭或回复照片请求时优先 selfie/mirror；需要叙事全景时用 third。"
        "自拍物理规则: view=selfie 是前摄自拍，画面中不得出现手机、相机、镜子或拿手机的手；"
        "只有 view=mirror 的对镜自拍才允许镜子和手机同时可见，并且只画镜中反射，不要画镜外前景人物。"
        "selfie/pov 的 scene 不要写手机屏幕、消息界面、聊天窗口、倒计时界面；如需表达等回复，只写表情、姿态和氛围。"
        "手部规则: 避免复杂手势，除非对镜自拍需要一只手拿手机，否则尽量让手自然或在画面外，严禁三只手/多余手臂。"
        "必须输出严格 JSON: {\"scene\":\"...\",\"view\":\"selfie|mirror|pov|third\",\"new_appearance_tags\":\"...\",\"user_location\":\"...\",\"co_located\":true,\"is_intimate\":false}。"
        "is_intimate 是布尔值，按上面的场景类型自判规则给出。"
        "co_located 是布尔值，表示你判断此刻用户是否和角色在同一空间。"
        "user_location 填你判断的用户此刻所在场所：与角色同处填 with_user，完全无法判断填 unknown，"
        "否则取其一: home/company/school/park/mall/street/cafe/restaurant/transit/convenience/cinema/hotel/hospital/gym/factory/farm/construction。"
        "聊天模型已经给出文字回复，这张图只配画面、不需要任何台词或配文，不要输出 caption 字段。"
        "new_appearance_tags 只填这张图需要额外强调的一次性服装、配饰、临时发型或发色瞳色变化，英文标签逗号分隔；"
        "这些标签只用于本次生图，不会写入长期外型。不要把姿势、表情、动作、场景、灯光写进去。没有一次性外观补充时留空。"
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
        return {"scene": fallback_scene, "view": requested_view, "new_appearance_tags": None, "is_intimate": False}

    scene = normalize_scene_visual_subject((parsed.get("scene") or fallback_scene).strip())
    planned_view = normalize_view(parsed.get("view"))
    # 把 LLM 这次对“用户位置/是否同处”的判断持久化，供下次生图参考与 Web 显示（自带连续性迟滞）。
    if hasattr(service, "_apply_llm_user_location"):
        try:
            service._apply_llm_user_location(
                session_id,
                user_location=parsed.get("user_location") or "",
                co_located=bool(parsed.get("co_located")),
                now=now,
            )
        except Exception:
            logger.debug("persist llm user location failed", exc_info=True)
    # LLM 自判优先，关键词检测作 OR 兜底（尤其 LLM 漏判时）。
    is_intimate = bool(parsed.get("is_intimate")) or intimate_hint
    default_view = "selfie"
    if is_intimate or bool(parsed.get("co_located")):
        default_view = "pov"
    final_view = requested_view or planned_view or default_view
    if scene_implies_mirror_selfie(scene) and not is_intimate:
        final_view = "mirror"
    # 亲密/事后贴身画面里前摄自拍、对镜自拍物理上讲不通（自拍框 + 第二人同框会画出断臂/双人）：硬性改 POV。
    if is_intimate and final_view in {"selfie", "mirror"}:
        final_view = "pov"
    return {
        "scene": scene,
        "view": final_view,
        "new_appearance_tags": (parsed.get("new_appearance_tags") or "").strip(),
        "is_intimate": is_intimate,
    }
