from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from .defaults import DEFAULT_CONFIG, WEEKDAY_NAMES

logger = logging.getLogger(__name__)

VALID_VIEWS = {"selfie", "mirror", "pov", "third"}

# AnimaTool turbo knowledge/schema 缓存（按 comfyui_url 分键）
_animatool_turbo_knowledge_cache: dict[str, tuple[dict[str, Any], float]] = {}
_animatool_turbo_schema_cache: dict[str, tuple[dict[str, Any], float]] = {}
_ANIMATOOL_KNOWLEDGE_TTL = 300.0


async def _fetch_animatool_turbo_knowledge(service: Any, ttl: float = _ANIMATOOL_KNOWLEDGE_TTL) -> dict[str, Any]:
    """从 AnimaTool 动态获取 turbo 画图知识规范。"""
    url = str(service.config.get("comfyui_url", "http://127.0.0.1:8188")).rstrip("/")
    now = time.monotonic()
    cached = _animatool_turbo_knowledge_cache.get(url)
    if cached and (now - cached[1]) < ttl:
        return cached[0]
    knowledge: dict[str, Any] = {}
    try:
        from .generation import ensure_comfy_session

        ensure_comfy_session(service)
        async with service.comfy_session.get(
            f"{url}/anima/knowledge_turbo", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                knowledge = await resp.json(content_type=None) or {}
    except Exception as exc:
        logger.debug("fetch animatool turbo knowledge failed: %s", exc)
    _animatool_turbo_knowledge_cache[url] = (knowledge, now)
    return knowledge


async def _fetch_animatool_turbo_schema(service: Any, ttl: float = _ANIMATOOL_KNOWLEDGE_TTL) -> dict[str, Any]:
    """从 AnimaTool 动态获取 turbo 接口 JSON schema。"""
    url = str(service.config.get("comfyui_url", "http://127.0.0.1:8188")).rstrip("/")
    now = time.monotonic()
    cached = _animatool_turbo_schema_cache.get(url)
    if cached and (now - cached[1]) < ttl:
        return cached[0]
    schema: dict[str, Any] = {}
    try:
        from .generation import ensure_comfy_session

        ensure_comfy_session(service)
        async with service.comfy_session.get(
            f"{url}/anima/schema_turbo", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                schema = await resp.json(content_type=None) or {}
    except Exception as exc:
        logger.debug("fetch animatool turbo schema failed: %s", exc)
    _animatool_turbo_schema_cache[url] = (schema, now)
    return schema


def _build_animatool_turbo_hint(knowledge: dict[str, Any], schema: dict[str, Any]) -> str:
    """根据动态获取的 knowledge/schema 生成给 image planner 的追加规则。"""
    params = schema.get("parameters", {}) if isinstance(schema, dict) else {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    required = params.get("required", []) if isinstance(params, dict) else []

    # 过滤掉固定超参数
    _HYPER_KEYS = {"steps", "cfg", "width", "height", "batch_size", "filename_prefix", "seed", "aspect_ratio"}
    content_fields = [k for k in properties if k not in _HYPER_KEYS]
    content_required = [k for k in required if k not in _HYPER_KEYS]

    # knowledge 关键段落直接注入
    knowledge_sections = []
    for key in ("turbo_expert", "turbo_examples"):
        val = str(knowledge.get(key, "")).strip()
        if val:
            if len(val) > 3000:
                val = val[:3000] + "\n...（截断）"
            knowledge_sections.append(val)
    knowledge_text = "\n\n".join(knowledge_sections) if knowledge_sections else ""

    # schema 内容字段定义
    field_hint = "\n".join(
        f"- {name}: {properties[name].get('description', '')}"
        for name in content_fields
        if name in properties
    )

    return (
        "\n【AnimaTool Turbo】（由 /anima/knowledge_turbo + /anima/schema_turbo 动态获取）\n"
        "以下规则覆盖通用画面描述规则。\n"
        + (f"\n{knowledge_text}\n" if knowledge_text else "")
        + ("\n内容字段:\n" + field_hint + "\n" if field_hint else "")
        + ("必填: " + ", ".join(content_required) + "\n" if content_required else "")
        + "你的 scene → tags（英文自然语言），new_appearance_tags → appearance（danbooru 标签）。"
        + " 角色身份 → character/series（仅已知公开角色；OC 留空）。"
    )

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


# 用户明确要“把设备拍进画面”（性爱/亲密时拍照、录像、对镜）的信号。命中则放行手机/镜子、
# 不再强制把亲密场景掰成 POV。规划器 LLM 的 device_in_frame 为主判，这里是确定性兜底。
DEVICE_CONTEXT_ZH = frozenset({
    "拍照", "拍下来", "拍张", "拍一张", "拍张照", "录像", "录视频", "录下来",
    "录制", "摄像", "边做边拍", "边做边录", "拍片", "拍成视频", "录成视频",
    "性爱录像", "做爱录像", "性爱视频", "做爱视频", "手机拍", "相机拍", "拍我们",
})


def _detect_device_context(*sources: str) -> bool:
    combined = " ".join(s for s in sources if s)
    if not combined:
        return False
    return any(kw in combined for kw in DEVICE_CONTEXT_ZH)


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
    for msg in state.get("recent_message_history", []):
        if service._within(msg.get("time", 0), 3 * 3600):
            dt = datetime.fromtimestamp(msg["time"], service._session_tz(session_id))
            recent.append(f"[{dt.strftime('%H:%M')}] 用户: {msg.get('text', '')}")
    if recent:
        lines.append("近 3 小时用户发言:\n" + "\n".join(recent))
    return "\n".join(lines) if lines else None


def format_sent_photo_context(service: Any, state: dict[str, Any], session_id: str = "", limit: int = 5) -> str | None:
    reset_time = float(state.get("short_context_reset_time", 0) or 0)
    photos = [
        photo for photo in state.get("sent_photos_history", [])
        if service._within(photo.get("timestamp", 0), since=reset_time)
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
    fallback_intimate_hint = _detect_intimate_context(intent, mood, prompt)
    fallback_device_hint = _detect_device_context(intent, mood, prompt)
    if not service.has_llm_config("image"):
        fallback_view = requested_view
        if fallback_intimate_hint and not fallback_device_hint and fallback_view in {"selfie", "mirror"}:
            fallback_view = "pov"
        return {
            "scene": fallback_scene,
            "view": fallback_view,
            "new_appearance_tags": None,
            "is_intimate": fallback_intimate_hint,
            "partner_in_frame": False,
            "device_in_frame": fallback_device_hint,
        }

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
    dynamic = service._effective_dynamic_appearance(session_id) if hasattr(service, "_effective_dynamic_appearance") else (state.get("dynamic_appearance") or service.config.get("dynamic_appearance", ""))
    prompt_prefs = service._prompt_scene_preferences(session_id) if hasattr(service, "_prompt_scene_preferences") else {}
    spatial = service._get_session_cfg(session_id, "spatial_relationship", DEFAULT_CONFIG["spatial_relationship"])
    spatial_line = f"默认物理空间关系: {spatial}\n" if str(spatial).strip() else ""
    if hasattr(service, "_session_role_identity"):
        role_name, bot_name, bot_self_name = service._session_role_identity(session_id)
    else:
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

    # 角色地点权威来源：按 authority 分档处理。
    # - strong（刚确认/高置信/轮次未过期）：钉死本次画面地点，防瞬移。
    # - weak（仍新鲜但已陈旧：时间过半/多轮未提及/低置信）：不锁死，改作"参考 + 历史轨迹线索"，
    #   允许规划器结合对话重新判断并回写刷新，避免陈旧 pin 把角色卡死在某地。
    # - None（超硬 TTL）：完全交规划器自行判断。
    pinned_place = service._active_character_place(state) if hasattr(service, "_active_character_place") else None
    strong_pin = pinned_place if (pinned_place and pinned_place.get("authority") == "strong") else None
    weak_pin = pinned_place if (pinned_place and pinned_place.get("authority") == "weak") else None
    # 最近位置轨迹（用于 weak / 冷启动时给规划器一条动线连续性线索）。
    location_trail = ""
    history = state.get("character_place_history", []) if isinstance(state, dict) else []
    if history and not strong_pin:
        recent = history[-3:]
        location_trail = "、".join(item.get("label", item.get("key", "?")) for item in recent if isinstance(item, dict))

    intimate_hint = _detect_intimate_context(intent, mood, prompt, dialog_context or "")
    device_hint = _detect_device_context(intent, mood, prompt, dialog_context or "")
    user_gender = service._get_user_gender(session_id) if hasattr(service, "_get_user_gender") else "male"
    user_g_zh = "女性" if user_gender == "female" else "男性"

    system = (
        f"{service._get_effective_persona(session_id)}\n\n"
        "Scene boundary: write scene as environment, camera framing, action, lighting, mood, and spatial context. "
        "Do not restate stable character appearance that is already in persona/current appearance/photo memory, such as hair color, eye color, body traits, species traits, or permanent accessories. "
        "Only mention clothing/accessories in scene when they are a deliberate one-shot visual change for this image; put one-shot visual tags in new_appearance_tags.\n"
        "你是角色扮演图片导演，负责把聊天模型给出的图片意图整合成最终画面。\n"
        f"角色身份: 当前角色是「{bot_name}」（{role_name}），优先使用「{bot_self_name}」作为自称；不要写成其他默认角色。\n"
        f"当前附加外貌: {dynamic or '无'}\n"
        f"用户画面偏好: 场景偏好={prompt_prefs.get('scene_preference') or '无'}；自拍偏好={prompt_prefs.get('selfie_preference') or '无'}。\n"
        f"角色性观念: {service._purity_directive(purity)}\n"
        f"当前场合: {time_period}, {weekday}, {safety.get('context', '')}。\n"
        f"季节与自然光: {time_light}。\n"
        f"{light_guard}\n"
        f"{spatial_line}"
        "你要综合用户最近的话、聊天模型的意图、最近发过的照片、时间天气、外貌和安全约束，"
        "输出适合发给用户的一张图。不要输出英文画图标签。\n"
        "公开场合必须穿着得体；私密场合可以更放松。避免和最近照片重复。"
    )
    if world_context:
        # 与聊天侧同构：进行中的对话已经确立了地点时，动线只作背景，避免配图把角色按现实时段“传送”
        # （家→商场这类漂移）；只有冷启动/无对话时才用动线引导地点。
        space_judgement = (
            "“用户位置/空间关系判断”只是基于历史消息的参考，不是硬性指令："
            "请结合“默认物理空间关系”设定、最近对话内容、角色此刻所在地点与当前时段，"
            "自行判断【此刻用户是否和角色在同一空间】，并据此决定视角——"
            "判断同处则优先 pov 或近距离 third 同框互动；判断异地、独处或仅线上联系才用 selfie/mirror。"
        )
        if dialog_context:
            system += (
                f"\n{world_context}\n"
                "以上“角色当前所在/接下来动线”只是日常背景参考。当前正在进行的对话场景优先级最高："
                "如果对话里角色已经处在某个地点（在家、商场、车站等），或刚说过自己在哪，就保持那个地点不变，"
                "不要因为动线显示的时间点不同，就擅自把角色挪到别处（严禁无理由瞬移）。\n"
                + space_judgement
            )
        else:
            system += (
                f"\n{world_context}\n"
                "其中“角色当前所在/接下来动线”按现实时间天气推断，应当遵守，角色不要无理由瞬移。"
                + space_judgement
            )
    if strong_pin:
        system += (
            f"\n地点锁定（最高优先，覆盖上面动线背景）: 角色此刻所在地点已确定为「{strong_pin['label']}」"
            f"（枚举值 {strong_pin['key']}）。本次画面必须就发生在这个地点，只描写该地点内的动作、姿态、光线、道具和氛围，"
            "不要把角色画到别的场所（严禁瞬移）；character_location 字段必须等于这个枚举值。"
        )
    elif weak_pin:
        system += (
            f"\n地点参考（较弱，角色此前提到在「{weak_pin['label']}」，但已过去一段对话/时间，可能已变动）: "
            "请结合最近对话判断此刻是否仍在该地点；若对话明确指向别处，以对话为准。"
            "不要无理由瞬移，也不要把角色死钉在该地点。"
        )
    if location_trail:
        system += f"\n近期位置轨迹（连续性参考，按时间正序）: {location_trail}。可据此判断动线方向。"
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
        "- 自拍物理规则不变: 默认不得出现手机、相机、镜子或拿手机的手；"
        "但若用户明确要求在亲密时拍照/录像/对镜（device_in_frame=true），则按其要求放行对应的 selfie/mirror 视角与手机/镜子入画。\n"
        "- new_appearance_tags 仍只填临时外观变化，不要把情绪或动作写进去。"
    )
    system += (
        "\n画面主体规则: 图片主体默认必须是角色，不要把“你/用户”写成画面中被观看的主角。"
        "用户只能作为视角来源、互动对象或少量局部元素出现；只有用户明确要求双人同框时，才允许用户作为第二主体。"
        "如果草案写成“你坐着/你躺着/你穿着”，必须改写为“角色坐着/她躺着/角色穿着”。"
        "角色名只用于台词称呼；默认或原创角色不要把名字当作画面标签，画面描述应依靠角色类型和外貌特征。既有作品角色可以保留角色名和作品名。"
    )
    system += (
        "\n单人构图硬规则: 当视角是 selfie（别人帮角色拍的照片）或 mirror 对镜自拍时，画面里【只能有角色一个人】，"
        "scene 绝不能写入第二个人（用户、伴侣、他、她、对方、男人、女人）或对方的完整身体、面部。"
        "如果此刻用户/伴侣的身体会和角色同框入画（如躺在身边、被搂着、贴身依偎），默认不要用 selfie/mirror，"
        "改用 pov，并且只把对方写成画面边缘的身体局部（手、手臂、胸膛、腿等），不要写成完整的第二个人。"
        "唯一例外是用户明确要求拍照/录像/对镜（device_in_frame=true）：此时可保留其要求的 selfie/mirror 视角，"
        "但对方依然只画身体局部，不得写成完整的第二个主角。\n"
        "partner_in_frame: 当画面里会出现用户/伴侣的身体（哪怕只是局部）时置 true；纯角色单人时 false。\n"
        "device_in_frame: 仅当用户明确要求把手机/相机/镜子作为拍照、录像、对镜的道具拍进画面时置 true；否则 false。"
    )
    system += (
        "\n视角规则: 身处同一空间或用户明确要靠近互动时优先 pov；"
        "异地、展示穿搭或回复照片请求时优先 selfie/mirror；需要叙事全景时用 third。"
        "取景物理规则: view=selfie 是别人帮角色拍的照片（第三者在画面外拍摄，角色看向镜头），不是前摄自拍，画面中不得出现手机、相机、镜子或拿手机的手，也不要写成自拍；"
        "只有 view=mirror 的对镜自拍才允许镜子和手机同时可见，并且只画镜中反射，不要画镜外前景人物。"
        "selfie/pov 的 scene 不要写手机屏幕、消息界面、聊天窗口、倒计时界面；如需表达等回复，只写表情、姿态和氛围。"
        "手部规则: 避免复杂手势，除非对镜自拍需要一只手拿手机，否则尽量让手自然或在画面外，严禁三只手/多余手臂。"
        "必须输出严格 JSON: {\"scene\":\"...\",\"view\":\"selfie|mirror|pov|third\",\"new_appearance_tags\":\"...\",\"clothing_off\":\"...\",\"character_location\":\"...\",\"user_location\":\"...\",\"co_located\":true,\"is_intimate\":false,\"partner_in_frame\":false,\"device_in_frame\":false}。"
        "character_location 填角色此刻所在场所的英文枚举（取值同 user_location，但不含 with_user/unknown）：若上面给出了角色地点约束，必须填那个枚举值；没有约束时按动线与对话自行判断。"
        "is_intimate 是布尔值，按上面的场景类型自判规则给出。"
        "partner_in_frame、device_in_frame 都是布尔值，按上面单人构图硬规则里的定义给出。"
        "co_located 是布尔值，表示你判断此刻用户是否和角色在同一空间。"
        "user_location 填你判断的用户此刻所在场所：与角色同处填 with_user，完全无法判断填 unknown，"
        "否则取其一: home/company/school/park/mall/street/cafe/restaurant/transit/convenience/cinema/hotel/hospital/gym/factory/farm/construction/"
        "museum/landmark/temple/library/zoo/amusement/bar/ktv/stadium/supermarket/bookstore/beach/salon。"
        "聊天模型已经给出文字回复，这张图只配画面、不需要任何台词或配文，不要输出 caption 字段。"
        "new_appearance_tags 只填这张图需要额外强调的一次性服装、配饰、临时发型或发色瞳色变化，英文标签逗号分隔；"
        "这些标签只用于本次生图，不会写入长期外型。不要把姿势、表情、动作、场景、灯光写进去。没有一次性外观补充时留空。"
        "clothing_off 填这张图里【应当从角色当前着装中去掉/已脱下/未穿】的服装或配饰（英文标签逗号分隔，如 'cardigan, jacket'），"
        "或填裸露状态词 'nude'/'topless'/'bottomless'/'completely nude' 表示相应程度的裸体；"
        "只要叙事表明此刻角色脱了某件、在试穿前脱下原装、正在裸体或性爱中褪去衣物，就据对话如实填写。"
        "这是一次性的、只影响本图，绝不写入长期衣柜（事后会自动恢复原着装）。没有脱衣/裸露时留空。"
    )

    if str(service.config.get("image_backend", "native") or "native").lower() == "animatool":
        try:
            knowledge = await _fetch_animatool_turbo_knowledge(service)
            schema = await _fetch_animatool_turbo_schema(service)
        except Exception:
            knowledge = {}
            schema = {}
        turbo_hint = _build_animatool_turbo_hint(knowledge, schema)
        if turbo_hint:
            system += turbo_hint

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
        fallback_view = requested_view
        if intimate_hint and not device_hint and fallback_view in {"selfie", "mirror"}:
            fallback_view = "pov"
        return {
            "scene": fallback_scene,
            "view": fallback_view,
            "new_appearance_tags": None,
            "is_intimate": intimate_hint,
            "partner_in_frame": False,
            "device_in_frame": device_hint,
        }

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
    # 角色地点回写：strong pin（已锁死）不回写，避免规划器二次发挥覆盖对话确立的位置；
    # 冷启动（无 pin）或 weak pin（已陈旧）时，允许规划器重新判断并刷新——冷启动写入、weak 同地只是
    # 重置新鲜度/轮次（去重不新增轨迹），weak 异地则更新到本轮判断，让陈旧 pin 不再卡死画面。
    if not strong_pin and hasattr(service, "_set_character_place"):
        char_loc = (parsed.get("character_location") or "").strip().lower()
        if char_loc and char_loc not in ("with_user", "unknown"):
            service._set_character_place(session_id, char_loc, char_loc, 0.6, source="image")
    # LLM 自判优先，关键词检测作 OR 兜底（尤其 LLM 漏判时）。
    is_intimate = bool(parsed.get("is_intimate")) or intimate_hint
    partner_in_frame = bool(parsed.get("partner_in_frame"))
    # 设备入画：规划器主判 + 中文关键词兜底。命中则放行手机/镜子、不再强制把画面掰成 POV。
    device_in_frame = bool(parsed.get("device_in_frame")) or device_hint
    two_person = is_intimate or partner_in_frame
    default_view = "selfie"
    if two_person or bool(parsed.get("co_located")):
        default_view = "pov"
    final_view = requested_view or planned_view or default_view
    if scene_implies_mirror_selfie(scene) and (device_in_frame or not two_person):
        final_view = "mirror"
    # 亲密/伴侣同框画面里前摄自拍、对镜自拍物理上讲不通（自拍框 + 第二人会画出断臂/双人）：硬性改 POV。
    # 例外：用户明确要拍照/录像/对镜（device_in_frame）时尊重其 selfie/mirror 视角。
    if two_person and not device_in_frame and final_view in {"selfie", "mirror"}:
        final_view = "pov"
    return {
        "scene": scene,
        "view": final_view,
        "new_appearance_tags": (parsed.get("new_appearance_tags") or "").strip(),
        "clothing_off": (parsed.get("clothing_off") or "").strip(),
        "is_intimate": is_intimate,
        "partner_in_frame": partner_in_frame,
        "device_in_frame": device_in_frame,
    }


async def plan_animatool_slots(
    service: Any,
    session_id: str,
    slots: "PromptSlots",
    *,
    intent: str = "",
    mood: str = "",
) -> dict[str, Any] | None:
    """把已算好的 PromptSlots 槽位交给 LLM，让它根据 AnimaTool schema 直出最终 JSON。

    返回 dict（可直接 POST /anima/generate_turbo + seed/steps/cfg/filename_prefix），
    失败时返回 None，调用方应回退到旧逻辑。
    """
    if not service.has_llm_config("image"):
        return None

    try:
        knowledge = await _fetch_animatool_turbo_knowledge(service)
        schema = await _fetch_animatool_turbo_schema(service)
    except Exception:
        logger.debug("fetch animatool schema/knowledge failed", exc_info=True)
        return None

    params = schema.get("parameters", {}) if isinstance(schema, dict) else {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    required = params.get("required", []) if isinstance(params, dict) else []
    if not properties:
        return None

    # 过滤掉固定超参数，只列出内容字段
    _HYPER_KEYS = {"steps", "cfg", "width", "height", "batch_size", "filename_prefix", "seed", "aspect_ratio"}
    content_fields = [k for k in properties if k not in _HYPER_KEYS]
    content_required = [k for k in required if k not in _HYPER_KEYS]

    # knowledge 注入：把整个 knowledge 对象的关键字段原样注入
    knowledge_sections = []
    for key in ("turbo_expert", "turbo_examples", "artist_list"):
        val = str(knowledge.get(key, "")).strip()
        if val:
            # 截断过长内容
            if len(val) > 4000:
                val = val[:4000] + "\n...（截断）"
            knowledge_sections.append(f"### {key}\n{val}")
    knowledge_text = "\n\n".join(knowledge_sections) if knowledge_sections else "（未获取到 knowledge）"

    # schema 内容字段定义
    schema_text = json.dumps(
        {k: properties[k] for k in content_fields},
        ensure_ascii=False, indent=2,
    ) if content_fields else "（无内容字段）"

    # 槽位信息
    slot_info = {
        "quality": slots.quality or "",
        "count": slots.count or "",
        "character": slots.character or "",
        "series": slots.series or "",
        "effective_appearance": slots.effective_appearance or "",
        "style_artist": slots.style_artist or "",
        "style_general": slots.style_general or "",
        "scene": slots.scene or "",
        "one_shot_appearance": slots.one_shot_appearance or "",
        "negative": slots.negative or "",
    }
    slot_text = "\n".join(f"- {k}: {v}" for k, v in slot_info.items() if v)

    state = service._get_session_state(session_id) if session_id else {}
    # 评级与旧管线一致：纯良度越低越露骨。用 effective safety（含时段调整）映射 Anima 四级评级。
    # 旧 purity_map 把低纯良度错映成 "safe"，导致默认出图变 SFW——这里按 level 正向分档修正。
    if session_id:
        safety = service._get_effective_safety(session_id)
        level = int(safety.get("level", service._get_purity(session_id)))
    else:
        level = 1
    if level <= 0:
        safety_tag = "explicit"
    elif level <= 2:
        safety_tag = "nsfw"
    elif level <= 5:
        safety_tag = "sensitive"
    else:
        safety_tag = "safe"

    # 时间与光线：在最终写 tags 的这步重新注入。scene 经过多次 LLM 改写后时段/光线易丢，
    # 而这步是决定最终 tags 的唯一出口——必须让它确保画面光线与当前时段一致。
    time_period = time_light = light_guard = ""
    if session_id:
        try:
            time_period = service._get_time_context(session_id).get("period") or ""
            time_light = service._format_time_context(session_id) or ""
            light_guard = service._format_light_guard(session_id) or ""
        except Exception:
            logger.debug("time/light context for animatool tags failed", exc_info=True)

    system = (
        "你是 AnimaTool Turbo 的专用提示词工程师。\n"
        "用户给你已计算好的提示词槽位，你需要把它们映射到 AnimaTool turbo API 的 JSON 字段中。\n"
        "steps/cfg/width/height/batch_size/filename_prefix/seed/aspect_ratio 由系统注入，不要输出。\n\n"
        f"## Knowledge\n{knowledge_text}\n\n"
        f"## Schema 内容字段\n{schema_text}\n\n"
        f"## 必填字段: {', '.join(content_required) if content_required else '（未指定）'}\n\n"
        "## 槽位→字段\n"
        "- quality → quality_meta_year_safe（末尾追加 safe/sensitive/nsfw/explicit）\n"
        "- count → count\n"
        "- character → character（仅已知公开角色；OC 留空）\n"
        "- series → series（仅已知公开角色；OC 留空）\n"
        "- effective_appearance + one_shot_appearance → appearance\n"
        "- style_artist → artist（@ 开头，为空留空）\n"
        "- style_general → style\n"
        "- scene → tags（改写成 3-5 句完整英文，把末尾的逗号标签堆融进句子，不要保留 Danbooru 逗号串）\n"
        "- negative → neg\n\n"
        "## 时间与光线（重要，必须体现）\n"
        f"当前时段: {time_period or '未知'}；光线参考: {time_light or '未知'}\n"
        f"{light_guard}\n"
        "tags 必须自然体现当前时段与光线（如黄昏金色斜光、夜晚人工灯光、正午自然光）；"
        "室内场景也要让窗外天光/室内灯光与当前时段一致。"
        "绝不要画出与当前时段矛盾的光线（如深夜出现正午阳光、白天出现夕阳）。\n\n"
        "只输出 JSON，不要 ```json``` 包装。\n"
    )

    user = f"## 槽位\n{slot_text}\n\n安全等级: {safety_tag}"
    if intent:
        user += f"\n用户意图: {intent}"
    if mood:
        user += f"\n情绪: {mood}"

    try:
        text = await service._call_llm(
            system,
            user,
            temp=float(service._get_llm_value("image", "temperature_scene", "0.95")),
            tag="animatool-slots-plan",
            purpose="image",
            session_id=session_id,
        )
        parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
    except Exception as exc:
        logger.error("animatool slots planning failed: %s", exc)
        return None

    if not isinstance(parsed, dict):
        return None

    # 后处理：确保必填字段存在
    if "quality_meta_year_safe" in properties:
        q = parsed.get("quality_meta_year_safe", "")
        if not q:
            parsed["quality_meta_year_safe"] = f"masterpiece, best quality, highres, newest, year 2025, {safety_tag}"
        elif safety_tag not in str(q).lower():
            parsed["quality_meta_year_safe"] = f"{q}, {safety_tag}"

    if "count" in properties and not parsed.get("count"):
        parsed["count"] = slots.count or "1girl"

    # neg：executor 不兜底默认负向，必须给全。补 Anima 质量负向 + 按评级联动（见 turbo_expert）。
    neg = str(parsed.get("neg") or slots.negative or "").strip()
    neg_low = neg.lower()

    def _add_neg(*tags: str):
        nonlocal neg, neg_low
        for t in tags:
            if t.lower() not in neg_low:
                neg = f"{neg}, {t}" if neg else t
                neg_low = neg.lower()

    _add_neg("worst quality", "low quality", "score_1", "score_2", "score_3")
    if safety_tag in ("safe", "sensitive"):
        _add_neg("nsfw", "explicit")
    else:
        # nsfw/explicit：先剔除 LLM 照搬 turbo_expert 误加的 "uncensored"
        # （它进负向 = 排斥无码、反而促成打码，与露骨内容矛盾），再补齐"要无码"的负向。
        neg = ", ".join(t for t in (p.strip() for p in neg.split(",")) if t and t.lower() != "uncensored")
        neg_low = neg.lower()
        _add_neg("safe", "sensitive", "censored", "mosaic censoring", "pixel censoring", "bar censoring")
    parsed["neg"] = neg

    # character/series 对 OC 必须为空
    if not slots.character:
        parsed.pop("character", None)
    if not slots.series:
        parsed.pop("series", None)

    # 清理空值和超参数泄漏
    cleaned = {k: v for k, v in parsed.items() if v not in (None, "") and k not in _HYPER_KEYS}

    logger.info("ANIMATOOL_SLOTS_LLM: %s", json.dumps(cleaned, ensure_ascii=False)[:600])
    return cleaned
