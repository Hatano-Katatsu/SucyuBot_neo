from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from . import session_schema
from .defaults import DEFAULT_CONFIG, WEEKDAY_NAMES
from .generation import (
    ANIMATOOL_NLTAG_FIELDS,
    _infer_prompt_view,
    _strip_non_mirror_camera_artifacts,
    public_outfit_guard_context,
)
from .world_runtime import PLACE_TYPES

logger = logging.getLogger(__name__)

VALID_VIEWS = {"selfie", "mirror", "pov", "third", "portrait"}
DEVICE_FREE_VIEWS = {"selfie", "portrait", "pov"}

# AnimaTool turbo knowledge/schema 缓存（按 comfyui_url 分键）
_animatool_turbo_knowledge_cache: dict[str, tuple[dict[str, Any], float]] = {}
_animatool_turbo_schema_cache: dict[str, tuple[dict[str, Any], float]] = {}
_ANIMATOOL_KNOWLEDGE_TTL = 300.0


def _sanitize_nltag_for_view(text: str, view: str) -> str:
    if view not in DEVICE_FREE_VIEWS:
        return str(text or "").strip()
    return _strip_non_mirror_camera_artifacts(str(text or "")).strip()


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
        + "你的 scene → tags/nltag（英文自然语言），new_appearance_tags → appearance（danbooru 标签）。"
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


# 明确要求“自拍/对镜/录像/设备入画”的信号。这里故意比 DEVICE_CONTEXT_ZH 更窄，
# 因为“拍一张照片”并不等于“画面里要保留自拍/镜子/手机”。
SELF_CAMERA_CONTEXT_ZH = frozenset({
    "自拍", "对镜", "对着镜子", "镜前", "镜中", "前摄", "前置",
    "举着手机", "拿着手机", "手机自拍", "录像", "录视频", "拍视频",
    "拍进画面", "拍进图里", "把手机拍进去", "把镜子拍进去", "边做边拍", "边做边录",
})


def _detect_self_camera_context(*sources: str) -> bool:
    combined = " ".join(s for s in sources if s)
    if not combined:
        return False
    return any(kw in combined for kw in SELF_CAMERA_CONTEXT_ZH)


PORTRAIT_REQUEST_ZH = frozenset({
    "帮我拍", "帮她拍", "帮忙拍", "替我拍", "替她拍", "给我拍", "给她拍",
    "让我拍", "让你拍", "请你拍", "请人拍", "摆拍",
    "拍一张照片", "拍张照片", "来一张照片", "再拍一张", "再来一张",
})


def _detect_portrait_request(*sources: str) -> bool:
    combined = " ".join(s for s in sources if s)
    if not combined:
        return False
    return any(kw in combined for kw in PORTRAIT_REQUEST_ZH)


CLOSE_INTERACTION_CONTEXT_ZH = frozenset({
    "靠过来", "靠近", "凑近", "贴近", "贴着", "依偎", "搂着", "抱着", "拥着",
    "牵着", "挽着", "递给你", "递到你", "递向你", "喂你", "掖好", "掖住",
    "俯身", "俯下身", "拍了拍", "戳了戳", "摸了摸", "扶住你", "靠在你身边",
})


def _detect_close_interaction_context(*sources: str) -> bool:
    combined = " ".join(s for s in sources if s)
    if not combined:
        return False
    return any(kw in combined for kw in CLOSE_INTERACTION_CONTEXT_ZH)


# 明确的"角色此刻裸体/正在脱光"信号。clothing_off 是唯一没有确定性兜底的判定项——
# 规划器一漏填，持久穿搭就原样画回来（"脱不掉衣服"bug）。强裸体词只覆盖明确性行为
# 或明确脱光/裸体；半脱/滑落另由 _infer_clothing_off_fallback 返回 topless 等更窄提示。
NUDITY_CONTEXT_ZH = frozenset({
    # 明确性行为（隐含裸体）
    "做爱", "性爱", "交合", "交媾", "插入", "抽插", "进入她", "进入体内",
    "在她体内", "顶入", "挺入", "骑乘", "后入", "内射", "中出", "射进",
    "口交", "肉棒", "阴茎", "龟头",
    # 明确裸体 / 脱光
    "裸体", "全裸", "赤裸", "赤身", "裸身", "裸着", "光着身", "光溜溜",
    "脱光", "一丝不挂", "没穿衣服", "衣服都脱", "衣服脱了", "脱了衣服",
    "褪去衣物", "褪下衣物", "褪下衣服", "脱掉衣服", "脱下衣服",
    "宽衣解带", "扒光", "扒掉衣服",
})

_PARTIAL_TOPLESS_RE = re.compile(
    r"(?:衣襟|领口|襟口|上衣|睡衣|寝衣|衬衫|衣物|衣服).{0,8}(?:敞开|滑落|褪下|褪到|落到|滑到)|"
    r"(?:敞开|拉开|扯开).{0,8}(?:衣襟|领口|胸口|上衣|睡衣|寝衣|衬衫)|"
    r"(?:露出|裸露).{0,8}(?:胸|乳|肩膀|锁骨)|"
    r"(?:敞胸|半裸|上身裸|上半身裸|衣不蔽体)",
)
_PARTIAL_BOTTOMLESS_RE = re.compile(
    r"(?:裙子|裙摆|短裙|内裤|裤子|下装).{0,8}(?:脱下|褪下|滑落|褪到|落到|滑到|掀起)|"
    r"(?:露出|裸露).{0,8}(?:大腿根|臀|下身|私处)",
)


def _combined_context(*sources: str) -> str:
    return " ".join(str(s) for s in sources if s)


def _detect_nudity_context(*sources: str) -> bool:
    combined = _combined_context(*sources)
    if not combined:
        return False
    return any(kw in combined for kw in NUDITY_CONTEXT_ZH)


def _infer_clothing_off_fallback(*sources: str) -> str:
    """从中文上下文给 clothing_off 的确定性兜底。

    返回空表示不确定；强裸体返回 completely nude；半脱只返回局部裸露词，避免把
    "脱了外套"、"领口敞开"之类误判成全裸。
    """
    combined = _combined_context(*sources)
    if not combined:
        return ""
    if _detect_nudity_context(combined):
        return "completely nude"
    if _PARTIAL_TOPLESS_RE.search(combined):
        return "topless"
    if _PARTIAL_BOTTOMLESS_RE.search(combined):
        return "bottomless"
    return ""

# 持久裸体态的 TTL 兜底：超过这个时长没有任何新裸体信号，就当她已经穿回衣服了。
# 主要靠换装(change_appearance)和 /新场景 主动解除，TTL 只是防"永远卡裸体"的保险。
NUDITY_PERSIST_TTL_SECONDS = 3 * 3600


def normalize_view(view: str | None) -> str:
    view = (view or "").strip().lower()
    return view if view in VALID_VIEWS else ""


def scene_implies_mirror_selfie(text: str) -> bool:
    lowered = (text or "").lower()
    return "mirror selfie" in lowered or "mirror reflection" in lowered or "对镜" in lowered or "镜子" in lowered


def _resolve_roleplay_view(
    *,
    requested_view: str,
    planned_view: str,
    default_view: str,
    derived_co_located: bool,
    two_person: bool,
    free_composition: bool,
    scene: str,
    intent: str,
    mood: str,
    prompt: str,
    dialog_context: str = "",
) -> str:
    """对 LLM 返回的 view 做最后一层业务裁决。"""
    final_view = requested_view or planned_view or default_view
    mirror_scene = scene_implies_mirror_selfie(scene)
    explicit_self_camera = mirror_scene or _detect_self_camera_context(
        intent, mood, prompt, scene, dialog_context
    )
    if mirror_scene and not two_person:
        final_view = "mirror"
    if free_composition:
        return final_view
    if (
        derived_co_located
        and not explicit_self_camera
        and _detect_portrait_request(intent, mood, prompt, scene, dialog_context)
    ):
        return "portrait"
    close_interaction = two_person or _detect_close_interaction_context(
        intent, mood, prompt, scene, dialog_context
    )
    if derived_co_located and not explicit_self_camera and final_view in {"selfie", "mirror", ""}:
        return "pov" if close_interaction else "third"
    if two_person and not explicit_self_camera and final_view in {"selfie", "mirror"}:
        return "pov"
    return final_view


USER_VISUAL_SUBJECT_RE = re.compile(
    r"^\s*[（(]?\s*你(?=[^，。；;,.]{0,40}(?:窝|坐|站|躺|靠|倚|跪|趴|蜷|穿|披|侧卧|斜倚|窝在|坐在|躺在|靠在|倚在|穿着))"
)


def normalize_scene_visual_subject(scene: str) -> str:
    scene = (scene or "").strip()
    if not scene:
        return scene
    return USER_VISUAL_SUBJECT_RE.sub(lambda m: m.group(0).replace("你", "角色", 1), scene, count=1)


_PLACE_SCENE_ANCHORS: dict[str, tuple[str, ...]] = {
    "home": ("home", "living room", "bedroom", "kitchen", "sofa"),
    "company": ("office", "workplace", "desk", "meeting room"),
    "school": ("school", "classroom", "campus", "library"),
    "park": ("park", "bench", "lawn", "trees"),
    "mall": ("shopping mall", "mall", "store", "atrium"),
    "street": ("street", "sidewalk", "road", "crosswalk"),
    "cafe": ("cafe", "coffee shop", "café", "coffee table"),
    "restaurant": ("restaurant", "dining table", "booth", "table", "counter"),
    "transit": ("station", "train", "subway", "platform", "bus stop"),
    "convenience": ("convenience store", "store shelf", "checkout counter"),
    "cinema": ("cinema", "movie theater", "theater lobby"),
    "hotel": ("hotel", "hotel room", "hotel corridor"),
    "hospital": ("hospital", "clinic", "waiting area"),
    "gym": ("gym", "fitness room", "treadmill"),
    "factory": ("factory", "workshop", "production line"),
    "farm": ("farm", "field", "greenhouse"),
    "construction": ("construction site", "scaffold", "worksite"),
    "museum": ("museum", "exhibition hall", "gallery"),
    "landmark": ("landmark", "tourist spot", "viewpoint"),
    "temple": ("temple", "shrine", "torii"),
    "library": ("library", "bookshelf", "reading room"),
    "zoo": ("zoo", "aquarium", "animal exhibit"),
    "amusement": ("amusement park", "carousel", "ferris wheel"),
    "bar": ("bar", "bar counter", "booth"),
    "ktv": ("karaoke room", "ktv", "private booth"),
    "stadium": ("stadium", "arena", "bleachers"),
    "supermarket": ("supermarket", "grocery aisle", "shopping cart"),
    "bookstore": ("bookstore", "book shelf", "reading corner"),
    "beach": ("beach", "shore", "seaside"),
    "salon": ("salon", "beauty salon", "mirror station"),
}


def _scene_has_place_anchor(scene: str, place_key: str) -> bool:
    lowered = (scene or "").lower()
    return any(anchor in lowered for anchor in _PLACE_SCENE_ANCHORS.get(place_key, ()))


def _place_scene_anchor_phrase(place_key: str, place_label: str = "", place_name: str = "") -> str:
    if place_key == "restaurant":
        detail = place_name or place_label or "the current restaurant"
        return f"inside {detail}, at the restaurant table or booth"
    examples = PLACE_TYPES.get(place_key, {}).get("examples") or []
    example = str(examples[0]) if examples else ""
    label = place_name or place_label or PLACE_TYPES.get(place_key, {}).get("label") or place_key
    return f"inside the current {place_key} setting ({label}{', ' + example if example else ''})"


def _normalize_image_plan_scene(parsed: dict[str, Any], fallback_scene: str, strong_pin: dict[str, Any] | None) -> str:
    # 业务规划层只承认 scene；tags 只是兼容旧污染返回的止血兜底，避免把有效画面描述丢掉。
    raw_scene = (parsed.get("scene") or parsed.get("tags") or fallback_scene or "").strip()
    scene = normalize_scene_visual_subject(raw_scene)
    if strong_pin:
        place_key = str(strong_pin.get("key") or "").strip().lower()
        if place_key and not _scene_has_place_anchor(scene, place_key):
            anchor = _place_scene_anchor_phrase(
                place_key,
                str(strong_pin.get("label") or ""),
                str(strong_pin.get("name") or ""),
            )
            scene = f"{anchor}, {scene}" if scene else anchor
    return scene


def _compact_context_text(text: Any, max_chars: int = 0) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if max_chars > 0 and len(compact) > max_chars:
        compact = compact[: max_chars - 3].rstrip() + "..."
    return compact


def _compact_photo_source_intent(source_description: Any, max_chars: int = 180) -> str:
    text = _compact_context_text(source_description)
    if not text:
        return ""
    chunks = [part.strip() for part in re.split(r"[；;]\s*", text) if part.strip()]
    keep: list[str] = []
    saw_labeled = False
    for chunk in chunks:
        match = re.match(r"^(意图|情绪/关系推进|必须包含|原始草案/上下文)\s*[:：]\s*(.+)$", chunk)
        if not match:
            continue
        saw_labeled = True
        label, value = match.group(1), match.group(2).strip()
        if label == "原始草案/上下文" or not value:
            continue
        keep.append(f"{label}: {value}")
    result = "；".join(keep)
    if not result and not saw_labeled and "原始草案/上下文" not in text:
        result = f"意图: {text}"
    return _compact_context_text(result, max_chars=max_chars)


_SPATIAL_HINT_RE = re.compile(
    r"坐|站|躺|跪|趴|蹲|靠|倚|抱|搂|贴|牵|握|脚边|腿上|膝上|怀里|身后|背后|面前|旁边|身边|"
    r"肩膀|肩头|胸口|锁骨|腰|后背|背部|背向|背对|腿|脚|手臂|手指|手心|手掌|双手|一只手|手里|手边|手腕|"
    r"仰头|抬头|低头|俯身|转身|侧身|吹头发|"
    r"\b(?:sit|sitting|stand|standing|lie|lying|kneel|kneeling|crouch|lean|leaning|hug|embrace|hold|"
    r"beside|behind|in front of|at .* feet|on .* lap|lap|shoulder|chest|back|feet|hands?)\b",
    re.IGNORECASE,
)

_SPATIAL_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "feet": ("脚边", "脚下", "feet", "foot"),
    "lap": ("腿上", "膝上", "lap"),
    "behind": ("身后", "背后", "behind"),
    "in_front": ("面前", "跟前", "in front"),
    "beside": ("旁边", "身边", "beside", "next to"),
    "arms": ("怀里", "抱", "搂", "arms", "embrace", "hug"),
    "shoulder": ("肩膀", "肩头", "shoulder"),
    "chest": ("胸口", "锁骨", "chest", "collarbone"),
    "back": ("后背", "背向", "back toward", "back to"),
    "sit": ("坐", "sit", "sitting", "seated"),
    "stand": ("站", "stand", "standing"),
    "lie": ("躺", "lie", "lying"),
    "kneel": ("跪", "kneel", "kneeling"),
    "lean": ("靠", "倚", "lean", "leaning"),
    "look_up": ("仰头", "抬头", "look up", "looking up"),
    "bend": ("俯身", "bend", "bending", "lean over"),
}


def _spatial_concepts(text: str) -> set[str]:
    lowered = (text or "").lower()
    concepts = set()
    for concept, aliases in _SPATIAL_CONCEPT_ALIASES.items():
        if any(alias.lower() in lowered for alias in aliases):
            concepts.add(concept)
    return concepts


def _looks_like_spatial_context(text: Any) -> bool:
    return bool(_SPATIAL_HINT_RE.search(str(text or "")))


def format_dialog_context(service: Any, state: dict[str, Any], session_id: str = "", limit: int = 12) -> str | None:
    lines = []
    for msg in service._active_chat_history(state, limit):
        if msg.get("role") not in ("user", "assistant"):
            continue
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        role = "用户" if msg.get("role") == "user" else "角色"
        lines.append(f"{role}: {content}")

    return "\n".join(lines) if lines else None


def format_planning_continuity_context(
    service: Any,
    state: dict[str, Any],
    session_id: str = "",
    limit: int = 8,
    max_lines: int = 4,
    max_chars: int = 140,
) -> str | None:
    lines = []
    for msg in service._active_chat_history(state, limit):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _compact_context_text(msg.get("content"), max_chars=max_chars)
        if not content:
            continue
        role_label = "用户" if role == "user" else "角色"
        lines.append(f"{role_label}: {content}")
    if max_lines > 0:
        lines = lines[-max_lines:]
    return "\n".join(lines) if lines else None


def format_planning_spatial_context(
    service: Any,
    state: dict[str, Any],
    session_id: str = "",
    *,
    intent: str = "",
    must_include: str = "",
    prompt: str = "",
    limit: int = 8,
    max_lines: int = 5,
    max_chars: int = 260,
) -> str | None:
    """保留生图最容易被瘦身截掉的身体站位与相对位置线索。"""
    lines: list[str] = []
    seen: set[str] = set()

    def add(label: str, text: Any):
        compact = _compact_context_text(text, max_chars=max_chars)
        if not compact or not _looks_like_spatial_context(compact):
            return
        key = compact.lower()
        if key in seen:
            return
        seen.add(key)
        lines.append(f"{label}: {compact}")

    add("图片意图", intent)
    add("必须包含", must_include)
    add("画面草案", prompt)
    history_lines: list[str] = []
    for msg in service._active_chat_history(state, limit):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        compact = _compact_context_text(msg.get("content"), max_chars=max_chars)
        if not compact or not _looks_like_spatial_context(compact):
            continue
        role_label = "用户" if role == "user" else "角色"
        key = compact.lower()
        if key in seen:
            continue
        seen.add(key)
        history_lines.append(f"{role_label}: {compact}")
    if max_lines > 0:
        history_lines = history_lines[-max_lines:]
    lines.extend(history_lines)
    return "\n".join(lines) if lines else None


def _primary_spatial_constraint(spatial_context: str | None) -> str:
    if not spatial_context:
        return ""
    for line in (spatial_context or "").splitlines():
        text = re.sub(r"^\s*[^:：]{1,12}[:：]\s*", "", line).strip()
        if text and _looks_like_spatial_context(text):
            return text
    return ""


def _merge_spatial_constraint_into_scene(scene: str, spatial_context: str | None) -> str:
    constraint = _primary_spatial_constraint(spatial_context)
    if not constraint:
        return scene
    needed = _spatial_concepts(constraint)
    if not needed:
        return scene
    present = _spatial_concepts(scene)
    # 坐/站/躺这类泛动作可能自然变体很多；只要所有已识别概念都在 scene 中出现，就不重复追加。
    if needed and needed.issubset(present):
        return scene
    guard = f"空间/身体关系硬约束: {constraint}"
    return f"{scene}。{guard}" if scene else guard


def format_sent_photo_context(service: Any, state: dict[str, Any], session_id: str = "", limit: int = 5) -> str | None:
    reset_time = session_schema.get_short_context_reset_time(state)
    photos = [
        photo for photo in session_schema.get_sent_photos_history(state)
        if service._within(photo.get("timestamp", 0), since=reset_time)
    ][-limit:]
    if not photos:
        return None
    lines = []
    tz = service._session_tz(session_id)
    for photo in photos:
        ts = photo.get("timestamp", 0)
        stamp = datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "未知时间"
        visual = (photo.get("nltag") or photo.get("scene") or "").strip()
        view = (photo.get("view") or "").strip() or "未知视角"
        source_intent = (photo.get("source_intent") or "").strip() or _compact_photo_source_intent(photo.get("source_description"))
        line = f"[{stamp}] {view}: {visual or '未记录'}"
        if source_intent:
            line += f"；{source_intent}"
        lines.append(line)
    return "\n".join(lines)


def format_recent_photo_dedup_context(
    service: Any,
    state: dict[str, Any],
    session_id: str = "",
    limit: int = 3,
    max_chars: int = 140,
) -> str | None:
    reset_time = session_schema.get_short_context_reset_time(state)
    photos = [
        photo for photo in session_schema.get_sent_photos_history(state)
        if service._within(photo.get("timestamp", 0), since=reset_time)
    ][-limit:]
    if not photos:
        return None
    lines = []
    tz = service._session_tz(session_id)
    for photo in photos:
        ts = photo.get("timestamp", 0)
        stamp = datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "未知时间"
        view = (photo.get("view") or "").strip() or "未知视角"
        scene = _compact_context_text(photo.get("nltag") or photo.get("scene"), max_chars=max_chars) or "未记录"
        source_intent = _compact_context_text(
            (photo.get("source_intent") or "").strip() or _compact_photo_source_intent(photo.get("source_description")),
            max_chars=max_chars,
        )
        line = f"[{stamp}] {view}: {scene}"
        if source_intent:
            line += f"；{source_intent}"
        lines.append(line)
    return "\n".join(lines)


async def plan_roleplay_image(
    service: Any,
    session_id: str,
    *,
    mode: str = "chat",
    intent: str = "",
    mood: str = "",
    must_include: str = "",
    prompt: str = "",
    view: str = "",
    weather_data: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    free_composition = (mode or "").strip().lower() == "illustration"
    requested_view = normalize_view(view)
    fallback_scene = (prompt or intent or must_include).strip()
    fallback_intimate_hint = _detect_intimate_context(intent, mood, prompt)
    fallback_device_hint = _detect_device_context(intent, mood, prompt)
    fallback_clothing_off = _infer_clothing_off_fallback(intent, mood, prompt)
    needs_caption = mode not in ("chat", "illustration")
    state = service._get_session_state(session_id)
    persisted_co_located = session_schema.get_user_co_located(state)
    if now is None:
        now = service._session_now(session_id)
    if not service.has_llm_config("image"):
        fallback_view = _resolve_roleplay_view(
            requested_view=requested_view,
            planned_view="",
            default_view="pov" if (fallback_intimate_hint or persisted_co_located) else "selfie",
            derived_co_located=persisted_co_located,
            two_person=fallback_intimate_hint,
            free_composition=free_composition,
            scene=fallback_scene,
            intent=intent,
            mood=mood,
            prompt=prompt,
        )
        if fallback_view == "portrait":
            fallback_device_hint = False
        return {
            "scene": fallback_scene,
            "view": fallback_view,
            "new_appearance_tags": None,
            "clothing_off": fallback_clothing_off,
            "is_intimate": fallback_intimate_hint,
            "partner_in_frame": False,
            "device_in_frame": fallback_device_hint,
            "caption": "",
        }

    if weather_data is None:
        weather_data = await service._fetch_weather(session_id=session_id)
    weather = f"{weather_data['desc']} {weather_data['temp']} C" if weather_data else "未知"
    time_ctx = service._get_time_context(session_id, now=now, weather=weather_data)
    time_period = time_ctx.get("period") or service._get_time_period(now.hour)
    time_light = service._format_time_context(session_id, now=now, weather=weather_data)
    light_guard = service._format_light_guard(session_id, now=now, weather=weather_data)
    weekday = WEEKDAY_NAMES[now.weekday()]
    safety = service._get_effective_safety(session_id)
    purity = service._get_purity(session_id)
    dynamic = service._effective_dynamic_appearance(session_id) if hasattr(service, "_effective_dynamic_appearance") else (session_schema.get_outfit(state) or service.config.get("dynamic_appearance", ""))
    visible_appearance = (
        service._effective_visual_prompt_tags(session_id)
        if hasattr(service, "_effective_visual_prompt_tags")
        else ""
    )
    prompt_prefs = service._prompt_scene_preferences(session_id) if hasattr(service, "_prompt_scene_preferences") else {}
    spatial = service._get_session_cfg(session_id, "spatial_relationship", DEFAULT_CONFIG["spatial_relationship"])
    spatial_line = f"默认物理空间关系: {spatial}\n" if str(spatial).strip() else ""
    if hasattr(service, "_session_role_identity"):
        role_name, bot_name, bot_self_name = service._session_role_identity(session_id)
    else:
        bot_name = service._get_session_cfg(session_id, "bot_name", "蕾伊")
        bot_self_name = service._get_session_cfg(session_id, "bot_self_name", "我")
        role_name = service._get_session_cfg(session_id, "role_name", "魅魔")
    is_push = mode in ("normal", "morning", "ntr")
    push_transition_decision: dict[str, Any] = {}
    push_transition_context = ""
    push_advance_context = ""
    life_push_context = ""
    if is_push and hasattr(service, "_push_scene_transition_decision"):
        try:
            push_transition_decision = service._push_scene_transition_decision(
                state,
                session_id,
                now=now,
                mode=mode,
            )
            if push_transition_decision.get("should_transition") and hasattr(service, "_format_push_scene_transition_context"):
                push_transition_context = service._format_push_scene_transition_context(
                    state,
                    session_id,
                    now=now,
                    mode=mode,
                )
            elif push_transition_decision.get("should_advance_beat") and hasattr(service, "_format_push_scene_advance_context"):
                push_advance_context = service._format_push_scene_advance_context(
                    state,
                    session_id,
                    now=now,
                    mode=mode,
                )
        except Exception:
            logger.debug("push scene transition decision failed", exc_info=True)
    if is_push and hasattr(service, "_life_plan_push_context"):
        try:
            life_push_context = service._life_plan_push_context(session_id, now=now)
        except Exception:
            logger.debug("life plan push context failed", exc_info=True)
    hard_scene_transition = bool(push_transition_decision.get("should_transition"))
    if hard_scene_transition:
        session_schema.clear_nudity(state)
    continuity_context = format_planning_continuity_context(service, state, session_id)
    if hard_scene_transition or push_transition_decision.get("drop_continuity"):
        continuity_context = None
    spatial_context = format_planning_spatial_context(
        service,
        state,
        session_id,
        intent=intent,
        must_include=must_include,
        prompt=prompt,
    ) if not hard_scene_transition else None
    photo_context = None if hard_scene_transition else format_recent_photo_dedup_context(service, state, session_id)
    memory_query = "\n".join(part for part in (intent, mood, must_include, prompt, continuity_context or "") if part)
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
                now=now,
                apply_persisted_place=not hard_scene_transition,
            )
        except Exception:
            logger.debug("world context build failed for image planning", exc_info=True)

    # 角色地点权威来源：按 authority 分档处理。
    # - strong（刚确认/高置信/轮次未过期）：钉死本次画面地点，防瞬移。
    # - weak（仍新鲜但已陈旧：时间过半/多轮未提及/低置信）：不锁死，改作"参考 + 历史轨迹线索"，
    #   允许规划器结合对话重新判断并回写刷新，避免陈旧 pin 把角色卡死在某地。
    # - None（超硬 TTL）：完全交规划器自行判断。
    pinned_place = service._active_character_place(state) if hasattr(service, "_active_character_place") else None
    if hard_scene_transition:
        strong_pin = None
        weak_pin = pinned_place
    else:
        strong_pin = pinned_place if (pinned_place and pinned_place.get("authority") == "strong") else None
        weak_pin = pinned_place if (pinned_place and pinned_place.get("authority") == "weak") else None
    # 最近位置轨迹（用于 weak / 冷启动时给规划器一条动线连续性线索）。
    location_trail = ""
    history = session_schema.get_character_place_history(state) if isinstance(state, dict) else []
    if history and not strong_pin:
        recent = history[-3:]
        location_trail = "、".join(item.get("label", item.get("key", "?")) for item in recent if isinstance(item, dict))

    intimate_hint = _detect_intimate_context(intent, mood, prompt, continuity_context or "")
    device_hint = _detect_device_context(intent, mood, prompt, continuity_context or "")
    clothing_off_hint = _infer_clothing_off_fallback(intent, mood, prompt, continuity_context or "")
    user_gender = service._get_user_gender(session_id) if hasattr(service, "_get_user_gender") else "male"
    user_g_zh = "女性" if user_gender == "female" else "男性"

    spatial_hint = service._get_session_cfg(session_id, "spatial_relationship", DEFAULT_CONFIG["spatial_relationship"])
    spatial_label = f"默认物理空间设定（{spatial_hint}）" if str(spatial_hint).strip() else "默认无固定空间设定"

    scene_boundary_rules = (
        "Scene boundary: write scene as environment, camera framing, action, lighting, mood, and spatial context. "
        "Do not restate stable character appearance that is already in persona/current visible appearance, such as hair color, eye color, body traits, species traits, or permanent accessories. "
        "Current visible appearance is authoritative; if short continuity or recent photo memory conflicts with it, treat the older visual detail as outdated. "
        "Only mention clothing/accessories in scene when they are a deliberate one-shot visual change for this image; put one-shot visual tags in new_appearance_tags.\n"
    )
    stable_mode_rules = (
        "通用模式要求:\n"
        "morning: 必须使用 pov，刚睡醒、厨房或卧室早安场景。\n"
        f"normal: 根据{spatial_label}和近期对话判断，身处同一空间用 pov，异地或上班时段用 selfie/mirror。\n"
        "ntr: 用户长时间未互动的冷落惩罚推送，强烈 NTR 危机感，通常 portrait（他人帮角色拍）、selfie 或分屏。\n"
        "推送转场通用规则: 最近对话/照片只能作为情绪、约定和避免重复的参考；判定应转场时，不要把上一幕地点、姿势或话题强行续写成此刻仍在发生。\n"
    )
    stable_world_rules = (
        "通用世界/地点判断规则: 位置优先级为用户本轮声明 > 工具/系统记录 > 时钟动线推断；用户文字明确说的位置始终优先。"
        "地点参考是 weak，不要强锁；不要无理由瞬移，也不要把角色死钉在已陈旧地点。"
        "用户位置状态可继承，但最近对话明确离开或抵达时必须覆盖旧记录。\n"
        "通用自然光规则: 当前不是黄昏/落日/暮色时，不要把“晚上见”“稍后”“接下来傍晚”提前画成当前画面；"
        "不得写夕阳、落日、晚霞、黄昏、暮色、路灯刚亮、twilight、sunset、dusk、evening sky、streetlights turning on。"
        "如需表达等待晚上，只写动作、表情、手机消息或约定感，当前环境仍按现在的自然光。\n"
    )
    stable_spatial_rules = (
        "空间/身体关系规则: 如果用户本轮意图、聊天草案或短期连续性里出现坐/站/躺/跪/脚边/腿上/身后/怀里/肩膀/背向/俯身等身体站位，"
        "这些属于硬约束；scene 的主句必须明确角色相对用户、镜头和道具的位置，不要只写表情或氛围，也不要改成相反站位。\n"
    )
    stable_intimacy_rules = (
        "场景类型自判: 只要角色与用户有贴身性接触（性交、骑乘、交合、爱抚、拥抱贴身、亲吻、前戏，"
        "或任何用户身体会与角色贴合入画的性暗示情形），都判为亲密场景 is_intimate=true；纯日常、无身体接触才是 false。"
        "性事刚结束的事后温存、同床共枕、相拥而眠、躺在对方身边、爱抚余韵等画面，只要用户的身体仍与角色同框贴近，"
        "同样判为 is_intimate=true（不要因为‘性行为已结束’就当成日常）。请在 JSON 里输出 is_intimate 布尔值。"
        + ("系统初步判断本次可能属于亲密场景，请重点确认。" if intimate_hint else "")
    )
    if free_composition:
        interaction_rules = (
            "\n【自由配图模式的亲密构图】若 is_intimate=true，仍要避免把用户误画成抢主体的完整第二人；"
            f"用户身体默认只作为{user_g_zh}局部、手臂、胸腹、背或腿入画。"
            "但用户本次明确指定的视角、机位、远近、局部特写、手机/相机/镜子入画优先，不要硬改成 POV。"
            "new_appearance_tags 仍只填临时外观变化，不要把情绪或动作写进去。"
        )
    else:
        interaction_rules = (
            "\n【以下亲密交互规则仅当你判定 is_intimate=true 时适用；若判定为日常/非性场景，请完全忽略本段，按通用规则写】:\n"
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
    subject_rules = (
        "\n画面主体规则: 图片主体默认必须是角色，不要把“你/用户”写成画面中被观看的主角。"
        "用户只能作为视角来源、互动对象或少量局部元素出现；只有用户明确要求双人同框时，才允许用户作为第二主体。"
        "如果草案写成“你坐着/你躺着/你穿着”，必须改写为“角色坐着/她躺着/角色穿着”。"
        "角色名只用于台词称呼；默认或原创角色不要把名字当作画面标签，画面描述应依靠角色类型和外貌特征。既有作品角色可以保留角色名和作品名。"
    )
    if not free_composition:
        subject_rules += (
            "\n单人构图硬规则: 当视角是 selfie 前摄自拍或 mirror 对镜自拍时，画面里【只能有角色一个人】，"
            "scene 绝不能写入第二个人（用户、伴侣、他、她、对方、男人、女人）或对方的完整身体、面部。"
            "如果此刻用户/伴侣的身体会和角色同框入画（如躺在身边、被搂着、贴身依偎），默认不要用 selfie/mirror，"
            "改用 pov，并且只把对方写成画面边缘的身体局部（手、手臂、胸膛、腿等），不要写成完整的第二个人。"
            "唯一例外是用户明确要求拍照/录像/对镜（device_in_frame=true）：此时可保留其要求的 selfie/mirror 视角，"
            "但对方依然只画身体局部，不得写成完整的第二个主角。\n"
        )
    field_rules = (
        "partner_in_frame: 当画面里会出现用户/伴侣的身体（哪怕只是局部）时置 true；纯角色单人时 false。\n"
        "device_in_frame: 仅当用户明确要求把手机/相机/镜子作为拍照、录像、对镜的道具拍进画面时置 true；否则 false。"
    )
    single_frame_rules = (
        "\n单帧构图硬规则: scene 必须是【单一冻结瞬间】——只描写一个时间点的一个场景动作，"
        "严禁分格、分镜、四宫格、漫画分格、拼贴画、多面板。"
        "如果角色有连续动作（如转身→走开→回头），只选取其中最具表现力的一帧，不要把多帧塞进同一张图。"
        "不要在 scene 里写叙事推进或时间线（先…然后…最后…），只写此刻定格的画面。"
    )
    if free_composition:
        view_rules = (
            "\n视角规则: 优先服从用户本次给出的视角、机位、远近和构图；未指定时才根据上下文选择 pov/third/selfie/mirror/portrait。"
            "允许部位特写、超近景、远景、低机位、俯拍、侧后方、环境或道具承接；不要为了自拍偏好强行改写。"
            "手部规则: 用户明确要求手部或局部特写时保留；否则仍避免复杂手势和多余手臂。"
        )
    else:
        view_rules = (
            "\n视角规则: 身处同一空间或用户明确要靠近互动时优先 pov；"
            "异地、展示穿搭或回复照片请求时优先 selfie/mirror；需要叙事全景时用 third。"
            "取景物理规则: view=selfie 是前摄自拍（角色伸手举着手机自拍），但画面中【不得出现手机本体、手机屏幕 UI、相机、镜子或拿手机的手】，只靠伸手取景和看向镜头表现自拍；"
            "view=portrait 是别人（用户或他人）帮角色拍的照片：角色看向镜头、为镜头摆姿势，拍摄者在画面外，画面里只有角色一个人，同样不得出现手机、相机、镜子。"
            "portrait 只在两种情况下用：①用户与角色【同处一地】且角色明确说想让用户/他人帮忙拍一张照片；②NTR 场景（他人给角色拍照）。其余展示穿搭/异地/回复照片仍用 selfie。"
            "只有 view=mirror 的对镜自拍才允许镜子和手机同时可见，并且只画镜中反射，不要画镜外前景人物。"
            "selfie/portrait/pov 的 scene 不要写手机屏幕、消息界面、聊天窗口、倒计时界面；如需表达等回复，只写表情、姿态和氛围。"
            "手部规则: 避免复杂手势，除非对镜自拍需要一只手拿手机，否则尽量让手自然或在画面外，严禁三只手/多余手臂。"
        )
    json_contract_rules = (
        "必须输出严格 JSON: {\"scene\":\"...\",\"view\":\"selfie|mirror|pov|third|portrait\",\"aspect_ratio\":\"2:3|3:2\",\"caption\":\"...\",\"new_appearance_tags\":\"...\",\"clothing_off\":\"...\",\"character_location\":\"...\",\"user_location\":\"...\",\"is_intimate\":false,\"partner_in_frame\":false,\"device_in_frame\":false}。"
        "aspect_ratio 选画幅（重要）：只允许 2:3（竖版，832x1216）或 3:2（横版，1216x832）。"
        "默认用 2:3 竖版；当场景以横向元素为主（如地平线、宽阔街景、双人并排、横向躺卧、风景全景）时用 3:2。"
        "近景人像、自拍、特写、站姿、行走、坐姿等纵向构图一律用 2:3。"
        "character_location 填角色此刻所在场所的英文枚举（取值同 user_location，但不含 with_user/unknown）：若上面给出了角色地点约束，必须填那个枚举值；没有约束时按动线与对话自行判断。"
        "is_intimate 是布尔值，按上面的场景类型自判规则给出。"
        "partner_in_frame、device_in_frame 都是布尔值，按上面单人构图硬规则里的定义给出。"
        "user_location 填你判断的用户此刻所在场所（关键）：与角色同处填 with_user，完全无法判断填 unknown，"
        "否则取其一: home/company/school/park/mall/street/cafe/restaurant/transit/convenience/cinema/hotel/hospital/gym/factory/farm/construction/"
        "museum/landmark/temple/library/zoo/amusement/bar/ktv/stadium/supermarket/bookstore/beach/salon。"
        "系统会从 user_location 自动推导用户是否与角色同处（co_located），你不需要单独输出 co_located。"
        "new_appearance_tags 只填这张图需要额外强调的一次性服装、配饰、临时发型或发色瞳色变化，英文标签逗号分隔；"
        "这些标签只用于本次生图，不会写入长期外型。不要把姿势、表情、动作、场景、灯光写进去。没有一次性外观补充时留空。"
        "clothing_off 填这张图里【应当从角色当前着装中去掉/已脱下/未穿】的服装或配饰（英文标签逗号分隔，如 'cardigan, jacket'），"
        "或填裸露状态词 'nude'/'topless'/'bottomless'/'completely nude' 表示相应程度的裸体；"
        "只要叙事表明此刻角色脱了某件、在试穿前脱下原装、正在裸体或性爱中褪去衣物，就据对话如实填写。"
        "性爱/裸体场景必须按程度填裸露词（如 'completely nude'），不能留空——留空会让角色被原来的衣服画回去。"
        "填了裸露/脱衣后，scene 里不要再把已脱下的衣服写成穿着或贴身（如『湿裙子贴着胸口』），改写裸露肌肤或仅用床单/泡沫等遮挡。"
        "这是一次性的、只影响本图，绝不写入长期衣柜（事后会自动恢复原着装）。没有脱衣/裸露时留空。"
    )
    if is_push:
        role_context = (
            f"你是角色扮演推送图片导演。当前推送模式: {mode}。\n"
            "主动推送时把画面写成角色日常动线里的自然片段，不要无理由瞬移到用户身边；短期连续性上下文可用于承接，但必须先服从场景转换判定。\n"
            f"角色身份: 当前角色是「{bot_name}」（{role_name}），优先使用「{bot_self_name}」作为自称；不要写成其他默认角色。\n"
        )
        temporal = time_period
    else:
        if free_composition:
            role_context = (
                f"你是角色扮演配图导演，负责把短期连续性、用户在 /配图 后输入的画面要求和世界/角色状态整合成最终画面。\n"
                f"角色身份: 当前角色是「{bot_name}」（{role_name}），优先使用「{bot_self_name}」作为自称；不要写成其他默认角色。\n"
                "优先级: 用户本次 /配图 后输入的场景、视角、机位、远近、焦段、构图、部位特写或道具要求最高；"
                "短期连续性、最近已发图片摘要、世界状态和记忆用于补全人物、情绪、地点和连续性；"
                "slot/外观/偏好只作为参考，不能覆盖用户本次明确要求。\n"
                "自由构图: 不强制自拍、不强制看镜头、不强制 portrait/pov；允许低机位、俯拍、远景、极近特写、部位特写、背影、环境承接、道具或手机/相机入画。"
                "若用户没有指定构图，再根据当前聊天场景自然选择。"
            )
        else:
            role_context = (
                f"你是角色扮演图片导演，负责把聊天模型给出的图片意图整合成最终画面。\n"
                f"角色身份: 当前角色是「{bot_name}」（{role_name}），优先使用「{bot_self_name}」作为自称；不要写成其他默认角色。\n"
            )
        temporal = f"{time_period}, {weekday}"
    stable_front = (
        scene_boundary_rules
        + stable_mode_rules
        + stable_world_rules
        + stable_spatial_rules
        + stable_intimacy_rules
        + interaction_rules
        + subject_rules
        + field_rules
        + single_frame_rules
        + view_rules
        + json_contract_rules
    )
    if needs_caption:
        stable_front += (
            "\ncaption 填一句简短的中文台词（纯文本，角色口吻），本图的配文。"
            "scene 描述本次画面的英文自然语言场景（不要中文）。"
        )
    else:
        stable_front += (
            "\n聊天模型已经给出文字回复，这张图只配画面、不需要任何台词或配文，不要输出 caption 字段。"
        )
    system = stable_front + "\n\n" + role_context + "\n" + service._get_effective_persona(session_id) + "\n\n"
    system += (
        f"当前可见外貌: {visible_appearance or '无'}\n"
        f"当前附加外貌: {dynamic or '无'}\n"
        f"用户画面偏好: 场景偏好={prompt_prefs.get('scene_preference') or '无'}；自拍偏好={prompt_prefs.get('selfie_preference') or '无'}。\n"
        f"角色性观念: {service._purity_directive(purity)}\n"
        f"当前场合: {temporal}, {safety.get('context', '')}。\n"
        f"季节与自然光: {time_light}。\n"
        f"{light_guard}\n"
        f"{spatial_line}"
        "你要综合用户最近的话、聊天模型的意图、最近已发图片摘要、时间天气、外貌和安全约束，"
        "输出适合发给用户的一张图。不要输出英文画图标签。\n"
        "公开场合必须穿着得体；私密场合可以更放松。避免和最近照片重复。"
    )
    if push_transition_context:
        system += "\n" + push_transition_context
    if push_advance_context:
        system += "\n" + push_advance_context
    if life_push_context:
        system += "\n" + life_push_context
    public_outfit_context = public_outfit_guard_context(
        service,
        session_id,
        dynamic,
        "\n".join(part for part in (intent, mood, must_include, prompt) if part),
    )
    if public_outfit_context:
        system += "\n" + public_outfit_context
    if world_context:
        # 与聊天侧同构：进行中的对话已经确立了地点时，动线只作背景，避免配图把角色按现实时段“传送”
        # （家→商场这类漂移）；只有冷启动/无对话时才用动线引导地点。
        space_judgement = (
            "“用户位置/空间关系判断”只是基于历史消息的参考，不是硬性指令："
            "请结合“默认物理空间关系”设定、最近对话内容、角色此刻所在地点与当前时段，"
            "自行判断【此刻用户是否和角色在同一空间】，并据此决定视角——"
            "判断同处则优先 pov 或近距离 third 同框互动；判断异地、独处或仅线上联系才用 selfie/mirror。"
        )
        if continuity_context and not hard_scene_transition:
            system += (
                f"\n{world_context}\n"
                "以上“角色当前所在/接下来动线”只是日常背景参考。当前正在进行的对话场景优先级最高："
                "如果对话里角色已经处在某个地点（在家、商场、车站等），或刚说过自己在哪，就保持那个地点不变，"
                "不要因为动线显示的时间点不同，就擅自把角色挪到别处（严禁无理由瞬移）。\n"
                + space_judgement
            )
        elif hard_scene_transition:
            system += (
                f"\n{world_context}\n"
                "以上世界状态用于确定转场后的当前落点；旧短期连续性若只是在描述上一幕地点或动作，不得覆盖当前动线。"
                "可以保留情绪、关系张力和未完成约定，但画面应落在自然推进后的此刻。\n"
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
    # 用户位置持久状态注入：引导规划器输出准确的 user_location（co_located 由代码推导）。
    user_place = service._active_user_place(state) if hasattr(service, "_active_user_place") else None
    if user_place:
        if user_place.get("co_located"):
            system += (
                '\n用户位置状态（系统记录，基于此前对话/生图判断）: 用户此刻与角色在同一空间，user_location 应填 with_user。'
                '除非最近对话明确表明用户已经离开（如「我走了」「到公司了」「我回家了」），否则应维持同处。'
            )
        else:
            up_label = user_place.get("label") or user_place.get("key") or "未知"
            system += (
                f'\n用户位置状态（系统记录，基于此前对话/生图判断）: 用户此刻在「{up_label}」，与角色异地。'
                '除非最近对话明确表明用户已来到角色身边（如「我到了」「开门」「我来找你」），否则 user_location 应填对应地点枚举。'
            )
    if is_push:
        user = (
            f"当前时段: {time_period}，星期: {weekday}，天气: {weather}，推送模式: {mode}。"
        )
    else:
        user = (
            f"当前天气: {weather}\n"
            f"图片意图: {intent or '未提供'}\n"
            f"情绪/关系推进: {mood or '未提供'}\n"
            f"必须包含: {must_include or '无'}\n"
            f"聊天模型画面草案: {prompt or '无'}\n"
            f"用户指定视角: {requested_view or '未指定'}"
        )
    if continuity_context:
        user += f"\n\n短期连续性:\n{continuity_context}"
    if spatial_context:
        user += f"\n\n空间/身体关系硬约束（scene 主句必须保留，不要改成相反站位）:\n{spatial_context}"
    if photo_context:
        user += f"\n\n最近已发图片摘要:\n{photo_context}"
    if memory_context:
        user += f"\n\n长期记忆:\n{memory_context}"

    try:
        text = await service._call_llm(
            system,
            user,
            temp=float(service._get_llm_value("image", "temperature_scene", "0.95")),
            tag="roleplay-image-plan",
            purpose="image",
            session_id=session_id,
        )
        parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
    except Exception as exc:
        logger.error("roleplay image planning failed: %s", exc)
        fallback_view = _resolve_roleplay_view(
            requested_view=requested_view,
            planned_view="",
            default_view="pov" if (intimate_hint or persisted_co_located) else "selfie",
            derived_co_located=persisted_co_located,
            two_person=intimate_hint,
            free_composition=free_composition,
            scene=fallback_scene,
            intent=intent,
            mood=mood,
            prompt=prompt,
            dialog_context=continuity_context or "",
        )
        if fallback_view == "portrait":
            device_hint = False
        return {
            "scene": fallback_scene,
            "view": fallback_view,
            "aspect_ratio": "2:3",
            "new_appearance_tags": None,
            "clothing_off": clothing_off_hint,
            "is_intimate": intimate_hint,
            "partner_in_frame": False,
            "device_in_frame": device_hint,
            "caption": "",
        }

    scene = _normalize_image_plan_scene(parsed, fallback_scene, strong_pin)
    if not free_composition:
        scene = _merge_spatial_constraint_into_scene(scene, spatial_context)
    planned_view = normalize_view(parsed.get("view"))
    # co_located 从 user_location 推导，不靠 LLM 单独判断。
    raw_user_loc = (parsed.get("user_location") or "").strip().lower()
    derived_co_located = raw_user_loc in ("with_user", "with_character", "together")
    if not derived_co_located and raw_user_loc in ("", "unknown") and persisted_co_located:
        derived_co_located = True
    # user_location 匹配角色地点 → 也算同处（用 build_world_state 拿角色地点，有时钟动线兜底）
    if not derived_co_located and raw_user_loc and raw_user_loc != "unknown":
        try:
            world = service.build_world_state(session_id, now=now, mode="image") if hasattr(service, "build_world_state") else {}
            char_key = (world.get("character_place") or {}).get("key", "")
            if char_key and raw_user_loc == char_key:
                derived_co_located = True
        except Exception:
            logger.debug("build_world_state for co_located derivation failed", exc_info=True)
    # 持久化 user_location（co_located 由代码推导，不需要持久化）
    if hasattr(service, "_apply_llm_user_location"):
        try:
            service._apply_llm_user_location(
                session_id,
                user_location=raw_user_loc,
                co_located=derived_co_located,
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
    if two_person or derived_co_located:
        default_view = "pov"
    final_view = _resolve_roleplay_view(
        requested_view=requested_view,
        planned_view=planned_view,
        default_view=default_view,
        derived_co_located=derived_co_located,
        two_person=two_person,
        free_composition=free_composition,
        scene=scene,
        intent=intent,
        mood=mood,
        prompt=prompt,
        dialog_context=continuity_context or "",
    )
    if final_view == "portrait":
        device_in_frame = False
    # clothing_off 兜底：规划器漏填、但对话/意图有明确裸体/性爱信号时，强制本图裸体——
    # 否则持久穿搭会原样画回来（"脱不掉衣服"bug）。只在留空时兜底，不覆盖规划器的显式判断。
    clothing_off = (parsed.get("clothing_off") or "").strip()
    if not clothing_off and clothing_off_hint:
        clothing_off = clothing_off_hint
    # 持久裸体态（根治）：一旦剧情脱光，后续每张图自动续上裸体，直到换装/新场景/超 TTL。
    # 续上（规划器本图没判脱衣，但新鲜期内仍处裸体）：
    if not clothing_off and session_schema.get_nudity(state) and service._within(
        session_schema.get_nudity_at(state), NUDITY_PERSIST_TTL_SECONDS
    ):
        clothing_off = session_schema.get_nudity(state)
    # 确立/刷新：本图全裸 → 记成持久裸体态（带时间戳供 TTL 老化）。
    if "nude" in clothing_off.lower():
        session_schema.set_nudity(state, "completely nude", at=time.time())
    # aspect_ratio 校验：只允许 2:3 和 3:2，默认 2:3
    raw_ar = (parsed.get("aspect_ratio") or "").strip()
    aspect_ratio = "3:2" if raw_ar == "3:2" else "2:3"
    return {
        "scene": scene,
        "view": final_view,
        "aspect_ratio": aspect_ratio,
        "caption": (parsed.get("caption") or "").strip(),
        "new_appearance_tags": (parsed.get("new_appearance_tags") or "").strip(),
        "clothing_off": clothing_off,
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

    # 槽位信息：AnimaTool 走自然语言 nltag/tags，不再使用 neg 字段。
    prompt_view = _infer_prompt_view(slots.scene or "")
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
        "view": prompt_view,
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

    # 时间、天气与光线：在最终写 tags 的这步重新注入。scene 经过多次 LLM 改写后时段/光线易丢，
    # 而这步是决定最终 tags 的唯一出口——必须让它确保画面光线与当前时段一致。
    time_period = time_light = light_guard = weather_text = ""
    if session_id:
        try:
            cached_weather = None
            cached = getattr(service, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                cached_weather = cached.get("data")
            if cached_weather is not None:
                if hasattr(service, "_weather_text"):
                    weather_text = service._weather_text(cached_weather)
                elif isinstance(cached_weather, dict):
                    weather_text = f"{cached_weather.get('desc', '未知')} {cached_weather.get('temp', '?')} C"
                else:
                    weather_text = str(cached_weather)
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
        "- scene → tags/nltag（改写成 3-5 句完整英文，把末尾的逗号标签堆融进句子，不要保留 Danbooru 逗号串）\n\n"
        "## nltag 规则（重要）\n"
        "不要输出 neg 或 negative 字段；它们对 AnimaTool 无意义。\n"
        "selfie/portrait/pov 场景中，仍要在自然语言描述里避免手机、相机、UI 界面、取景框、快门按钮等拍摄设备元素；"
        "mirror 场景允许镜子和镜中反射，但不要写手机 UI。\n\n"
        "## 时间与光线（重要，必须体现）\n"
        f"当前天气: {weather_text or '未知'}；当前时段: {time_period or '未知'}；光线参考: {time_light or '未知'}\n"
        f"{light_guard}\n"
        "tags 必须自然体现当前天气。晴/少云可体现为清晰天光或柔和云影；雨、雪、雾、雷雨、大风等可见天气必须写进环境、窗外、地面、伞、衣物湿痕或空气质感中。"
        "不要把雨天写成晴朗阳光，也不要在室内完全抹掉窗外天气。\n"
        "tags 必须自然体现当前时段与光线（如黄昏金色斜光、夜晚人工灯光、正午自然光）；"
        "室内场景也要让窗外天光/室内灯光与当前时段一致。"
        "绝不要画出与当前时段矛盾的光线（如深夜出现正午阳光、白天出现夕阳）。\n\n"
        "## 单帧构图（重要，必须遵守）\n"
        "画面必须是【单一冻结瞬间】的单帧构图，严禁分格、分镜、四宫格、漫画分格、拼贴画、多面板。\n"
        "scene / tags 只描写一个时间点的一个场景，不要包含多个时间线、多个动作阶段或叙事推进。\n"
        "如果角色有连续动作（如转身→走开），只选取其中一帧，不要把多帧塞进同一张图。\n\n"
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

    nltag_field = ""
    for field in ANIMATOOL_NLTAG_FIELDS:
        if field in content_required and field in properties:
            nltag_field = field
            break
    if not nltag_field:
        for field in ANIMATOOL_NLTAG_FIELDS:
            if field in properties:
                nltag_field = field
                break
    raw_nltag = next((str(parsed.get(field) or "").strip() for field in ANIMATOOL_NLTAG_FIELDS if str(parsed.get(field) or "").strip()), "")
    if nltag_field:
        nltag_value = raw_nltag or slots.scene or ""
        view_for_nltag = prompt_view or _infer_prompt_view(nltag_value)
        nltag_value = _sanitize_nltag_for_view(nltag_value, view_for_nltag)
        if not nltag_value and slots.scene:
            nltag_value = _sanitize_nltag_for_view(slots.scene, prompt_view or _infer_prompt_view(slots.scene))
        parsed[nltag_field] = nltag_value
        for field in ANIMATOOL_NLTAG_FIELDS:
            if field != nltag_field:
                parsed.pop(field, None)

    for field in ANIMATOOL_NLTAG_FIELDS:
        if str(parsed.get(field) or "").strip():
            break

    # character/series 对 OC 必须为空
    if not slots.character:
        parsed.pop("character", None)
    if not slots.series:
        parsed.pop("series", None)

    # 清理空值和超参数泄漏
    cleaned = {k: v for k, v in parsed.items() if v not in (None, "") and k not in _HYPER_KEYS}

    logger.info("ANIMATOOL_SLOTS_LLM: %s", json.dumps(cleaned, ensure_ascii=False)[:600])
    return cleaned
