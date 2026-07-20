from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from . import session_schema
from .appearance import (
    WARDROBE_CLOTHING_SLOTS,
    WARDROBE_RENDER_ORDER,
    closet_add,
    infer_gender_from_count,
    infer_gender_from_prefix,
    inject_appearance,
    normalize_appearance_text,
    render_wardrobe,
    seed_wardrobe_from_text,
)
from .defaults import DEFAULT_CONFIG
from .http_limits import read_limited_bytes, read_limited_json, response_limit

logger = logging.getLogger(__name__)


PHONE_TERMS = ("phone", "smartphone", "cellphone", "mobile phone", "手机")
MIRROR_TERMS = ("mirror", "mirror reflection", "mirror selfie", "镜子", "对镜")
ORIGINAL_SERIES_MARKERS = {"oc", "original", "original character", "原创", "原创角色", "自设", "自创", "原创oc", "无", "none", "-"}
NON_LATIN_IDENTITY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
EMPTY_IDENTITY_MARKERS = {"", "unknown", "none", "n/a", "na", "null", "-"}
VISIBLE_PHONE_NEGATIVES = (
    "holding phone", "visible phone", "smartphone",
    "viewfinder", "phone screen", "camera UI", "shutter button",
)
BOTTOM_EXPOSURE_NEGATIVES = ("no panties", "no underwear", "bottomless", "crotchless")
ANIMATOOL_NLTAG_FIELDS = ("nltag", "nl_tag", "nl_tags", "tags")
ANIMATOOL_NEGATIVE_FIELDS = ("neg", "negative", "negative_prompt")
VALID_VIEWS = {"selfie", "mirror", "pov", "third", "portrait"}

ANIMATOOL_PHONE_GUARD_TERMS = (
    "holding phone", "visible phone", "phone", "smartphone", "cellphone", "mobile phone",
    "phone in hand", "hand holding phone", "viewfinder", "phone screen", "camera ui",
    "camera interface", "shutter button", "two phones", "multiple phones",
)
ANIMATOOL_MIRROR_GUARD_TERMS = (
    "mirror", "mirror reflection", "mirror selfie", "multiple reflections",
)
ANIMATOOL_EXTRA_PERSON_GUARD_TERMS = (
    "2girls", "multiple girls", "extra girls", "2boys", "multiple boys", "extra boys",
    "multiple characters", "full second person", "second body", "duplicate body", "extra face",
    "unrelated extra person", "foreground person", "person outside mirror",
    "male", "boy", "man", "1boy",
)
ANIMATOOL_PANEL_GUARD_TERMS = (
    "split screen", "grid", "multiple panels", "collage",
)


def _remember_generated_nltag(service: Any, session_id: str, nltag: str):
    text = str(nltag or "").strip()
    if not text:
        return
    try:
        service._last_generated_nltag = text
        if session_id:
            cache = getattr(service, "_last_generated_nltag_by_session", None)
            if not isinstance(cache, dict):
                cache = {}
            cache[session_id] = text
            service._last_generated_nltag_by_session = cache
    except Exception:
        logger.debug("failed to store generated nltag", exc_info=True)


def _payload_nltag(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    for field in ANIMATOOL_NLTAG_FIELDS:
        text = str(payload.get(field) or "").strip()
        if text:
            return text
    return ""


def _preferred_animatool_nltag_field(properties: dict[str, Any], required: set[str] | None = None) -> str:
    required = required or set()
    for field in ANIMATOOL_NLTAG_FIELDS:
        if field in required and field in properties:
            return field
    for field in ANIMATOOL_NLTAG_FIELDS:
        if field in properties:
            return field
    return ""


@dataclass
class PromptSlots:
    raw_scene: str = ""
    scene: str = ""
    quality: str = ""
    count: str = ""
    identity: str = ""
    character: str = ""  # 角色名视觉 tag（AnimaTool character 字段）
    series: str = ""     # 作品名视觉 tag（AnimaTool series 字段）
    base_appearance: str = ""
    effective_appearance: str = ""
    style_artist: str = ""
    style_general: str = ""
    safety: str = ""
    one_shot_appearance: str = ""
    negative: str = ""
    positive: str = ""
    session_id: str = ""

    def compact(self, limit: int = 420) -> str:
        parts = []
        for key, value in self.as_log_items():
            text = re.sub(r"\s+", " ", value or "").strip()
            if not text:
                text = "-"
            if len(text) > limit:
                text = text[:limit].rstrip() + "..."
            parts.append(f"{key}={text}")
        return " | ".join(parts)

    def pretty(self, limit: int = 900) -> str:
        blocks = []
        for key, value in self.as_display_items():
            text = (value or "").strip() or "（空）"
            if len(text) > limit:
                text = text[:limit].rstrip() + "..."
            blocks.append(f"[{key}]\n{text}")
        return "\n\n".join(blocks)

    def as_log_items(self) -> list[tuple[str, str]]:
        return [
            ("quality", self.quality),
            ("count", self.count),
            ("identity", self.identity),
            ("base_appearance", self.base_appearance),
            ("effective_appearance", self.effective_appearance),
            ("style", ", ".join(x for x in (self.style_artist, self.style_general) if x)),
            ("safety", self.safety),
            ("scene", self.scene),
            ("one_shot_appearance", self.one_shot_appearance),
            ("negative", self.negative),
        ]

    def as_display_items(self) -> list[tuple[str, str]]:
        return [
            ("quality", self.quality),
            ("count", self.count),
            ("identity", self.identity),
            ("base_appearance", self.base_appearance),
            ("effective_appearance", self.effective_appearance),
            ("style_artist", self.style_artist),
            ("style_general", self.style_general),
            ("safety", self.safety),
            ("scene", self.scene),
            ("one_shot_appearance", self.one_shot_appearance),
            ("negative", self.negative),
            ("positive_final", self.positive),
        ]

    def render_positive(self) -> str:
        """按槽位顺序渲染最终正向提示词。"""
        appearance = self.effective_appearance or self.base_appearance
        modules = [
            self.quality,
            self.count,
            self.identity,
            self.style_artist,
            appearance,
            self.style_general,
            self.safety,
            self.scene,
            self.one_shot_appearance,
        ]
        return _dedupe_prompt_modules(modules)

    def quality_for_schema(self) -> str:
        """AnimaTool schema 把质量词与安全评级放在同一字段时使用。"""
        return _dedupe_prompt_modules([self.quality, self.safety])


@dataclass(frozen=True)
class AnimaToolGuardContract:
    """不能交给 LLM 自由删改的 AnimaTool 安全与构图终裁项。"""

    phone: tuple[str, ...] = ()
    mirror: tuple[str, ...] = ()
    extra_people: tuple[str, ...] = ()
    panels: tuple[str, ...] = ()
    public_exposure: tuple[str, ...] = ()

    def negative_terms(self) -> tuple[str, ...]:
        seen: set[str] = set()
        terms: list[str] = []
        for group in (
            self.phone,
            self.mirror,
            self.extra_people,
            self.panels,
            self.public_exposure,
        ):
            for term in group:
                key = _tag_key(term)
                if key and key not in seen:
                    seen.add(key)
                    terms.append(term)
        return tuple(terms)

    def nltag_constraint(self) -> str:
        """无 neg 字段的工作流以单句自然语言保留同一份终裁语义。"""
        clauses: list[str] = []
        if self.phone:
            forbidden_phone = {
                _tag_key(term)
                for term in ANIMATOOL_PHONE_GUARD_TERMS
                if term not in {"two phones", "multiple phones"}
            }
            if any(_tag_key(term) in forbidden_phone for term in self.phone):
                clauses.append("no phone, camera interface, viewfinder, or shutter control is visible")
            else:
                clauses.append("no duplicate phone is visible")
        if self.mirror:
            forbidden_mirror = {
                _tag_key(term)
                for term in ANIMATOOL_MIRROR_GUARD_TERMS
                if term != "multiple reflections"
            }
            if any(_tag_key(term) in forbidden_mirror for term in self.mirror):
                clauses.append("no mirror or reflected duplicate is visible")
            else:
                clauses.append("there is at most one intended reflection")
        if self.extra_people:
            clauses.append("no unrelated extra person, duplicate body, or extra face is visible")
        if self.panels:
            clauses.append("the image is one undivided single frame, never a grid, collage, split screen, or multiple panels")
        if self.public_exposure:
            clauses.append("the specified outfit fully covers intimate areas with no unintended nudity or exposure")
        if not clauses:
            return ""
        return "Deterministic rendering constraints: " + "; ".join(clauses) + "."

HAIR_COLOR_WORDS = (
    "blonde", "blond", "golden", "silver", "white", "black", "brown", "red",
    "pink", "blue", "purple", "green", "grey", "gray", "orange", "ginger", "platinum",
)
EYE_COLOR_WORDS = HAIR_COLOR_WORDS + ("amber", "hazel", "aqua", "violet")

QUALITY_SLOT_TAGS = (
    "masterpiece", "best quality", "absurdres", "highres", "score_9", "score_8", "score_7",
    "anime coloring", "clean lineart", "soft cel shading", "detailed illustration",
)
COUNT_SLOT_TAGS = ("1girl", "1boy", "solo")


@dataclass
class PromptPrefixParts:
    base: str = ""
    quality: str = ""
    count: str = ""
    style: str = ""


def _tag_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("_", " ").strip().lower())


QUALITY_SLOT_KEYS = {_tag_key(tag) for tag in QUALITY_SLOT_TAGS}
COUNT_SLOT_KEYS = {_tag_key(tag) for tag in COUNT_SLOT_TAGS}


def _split_tags(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,\n]+", str(text or "")) if part.strip()]


def _join_unique_tags(tags: list[str]) -> str:
    kept: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        key = _tag_key(tag)
        if key and key not in seen:
            kept.append(tag.strip())
            seen.add(key)
    return ", ".join(kept)


def _dedupe_prompt_modules(modules: list[str]) -> str:
    seen: set[str] = set()
    deduped: list[str] = []
    for module in modules:
        kept: list[str] = []
        for tag in _split_tags(module):
            key = _tag_key(tag)
            if key and key not in seen:
                kept.append(tag)
                seen.add(key)
        if kept:
            deduped.append(", ".join(kept))
    return ", ".join(deduped)


def _is_style_tag(tag: str) -> bool:
    low = _tag_key(tag)
    return tag.strip().startswith("@") or low.startswith("artist:") or low.startswith("art by ")


def _split_prompt_prefix(prefix: str) -> PromptPrefixParts:
    quality: list[str] = []
    count: list[str] = []
    style: list[str] = []
    base: list[str] = []
    for tag in _split_tags(prefix):
        key = _tag_key(tag)
        if key in QUALITY_SLOT_KEYS:
            quality.append(tag)
        elif key in COUNT_SLOT_KEYS:
            count.append(tag)
        elif _is_style_tag(tag):
            style.append(tag)
        else:
            base.append(tag)
    return PromptPrefixParts(
        base=_join_unique_tags(base),
        quality=_join_unique_tags(quality),
        count=_join_unique_tags(count),
        style=_join_unique_tags(style),
    )


def _hair_colors_in_text(text: str) -> set[str]:
    normalized = _tag_key(text)
    return {color for color in HAIR_COLOR_WORDS if re.search(rf"\b{re.escape(color)}\b", normalized)}


def _eye_colors_in_text(text: str) -> set[str]:
    normalized = _tag_key(text)
    return {color for color in EYE_COLOR_WORDS if re.search(rf"\b{re.escape(color)}\b", normalized)}


def _explicit_appearance_override(service: Any, state: dict[str, Any]) -> str:
    if not state:
        return ""
    parts: list[str] = []
    dynamic_slots = service._parse_appearance(session_schema.get_outfit(state))
    for key in ("hair", "eyes", "outfit", "accessory", "other"):
        parts.extend(dynamic_slots.get(key, []))
    for key in ("custom_default_hair", "custom_default_eyes"):
        raw = (state.get(key) or "").strip()
        if raw:
            parts.append(raw)
    return normalize_appearance_text(", ".join(parts))


def _explicit_hair_override(service: Any, state: dict[str, Any], char: str = "") -> list[str]:
    dynamic_hair = service._parse_appearance(session_schema.get_outfit(state)).get("hair", [])
    if dynamic_hair:
        return dynamic_hair
    custom_hair = (state.get("custom_default_hair") or "").strip()
    if custom_hair:
        return service._parse_appearance(custom_hair).get("hair", [])
    if char:
        return service._parse_appearance(char).get("hair", [])
    return []


def _explicit_eye_override(service: Any, state: dict[str, Any], char: str = "") -> list[str]:
    dynamic_eyes = service._parse_appearance(session_schema.get_outfit(state)).get("eyes", [])
    if dynamic_eyes:
        return dynamic_eyes
    custom_eyes = (state.get("custom_default_eyes") or "").strip()
    if custom_eyes:
        return service._parse_appearance(custom_eyes).get("eyes", [])
    if char:
        return service._parse_appearance(char).get("eyes", [])
    return []


def _explicit_outfit_override(service: Any, state: dict[str, Any]) -> list[str]:
    return service._parse_appearance(session_schema.get_outfit(state)).get("outfit", [])


def _strip_conflicting_scene_hair(scene_desc: str, hair_override: list[str]) -> str:
    override_text = ", ".join(hair_override)
    override_colors = _hair_colors_in_text(override_text)
    if not override_colors:
        return scene_desc
    color_alt = "|".join(re.escape(c) for c in sorted(HAIR_COLOR_WORDS, key=len, reverse=True))
    hair_shape = r"(?:long|short|shoulder-length|waist-length|messy|loose|flowing|straight|wavy|curly|silky|tousled|disheveled)\s+"

    def replace_colored_hair(match: re.Match[str]) -> str:
        phrase = match.group(0)
        colors = _hair_colors_in_text(phrase)
        if colors and colors.isdisjoint(override_colors):
            return "hair"
        return phrase

    text = re.sub(
        rf"\b(?:(?:deep|dark|light|pale|bright|soft)\s+)?(?:{color_alt})\s+(?:{hair_shape}){{0,3}}hair\b",
        replace_colored_hair,
        scene_desc,
        flags=re.IGNORECASE,
    )
    if any("bun" in _tag_key(tag) or "updo" in _tag_key(tag) for tag in hair_override):
        text = re.sub(
            rf"\b(?:{hair_shape}){{1,4}}hair\s+(?:falls?|spills?|falling|spilling|cascades?|cascading|flows?|flowing|hangs?|hanging|spread)[^,.;]*",
            "hair",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bhair\s+(?:falls?|spills?|falling|spilling|cascades?|cascading|flows?|flowing|hangs?|hanging|spread)[^,.;]*",
            "hair",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    return text


def _strip_conflicting_scene_eyes(scene_desc: str, eye_override: list[str]) -> str:
    override_colors = _eye_colors_in_text(", ".join(eye_override))
    if not override_colors:
        return scene_desc
    color_alt = "|".join(re.escape(c) for c in sorted(EYE_COLOR_WORDS, key=len, reverse=True))

    def replace_colored_eyes(match: re.Match[str]) -> str:
        phrase = match.group(0)
        colors = _eye_colors_in_text(phrase)
        if colors and colors.isdisjoint(override_colors):
            return "eyes"
        return phrase

    text = re.sub(
        rf"\b(?:(?:deep|dark|light|pale|bright|soft)\s+)?(?:{color_alt})\s+(?:eyes|pupils|irises)\b",
        replace_colored_eyes,
        scene_desc,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    return text


def _strip_conflicting_scene_outfit(scene_desc: str, outfit_override: list[str], outfit_kw: list[str]) -> str:
    if not outfit_override:
        return scene_desc
    keywords = [re.escape(k) for k in outfit_kw if re.fullmatch(r"[A-Za-z][A-Za-z -]*", k or "")]
    if not keywords:
        return scene_desc
    outfit_alt = "|".join(sorted(keywords, key=len, reverse=True))
    color_alt = "black|white|blue|red|pink|purple|green|yellow|brown|gray|grey|dark|light"
    # 单件衣物短语：前导读（冠词/物主代词/颜色/修饰词）+ 可选修饰词/颜色 + 衣物关键词，
    # 允许 "and" 连接另一件（"a white camisole and navy pleated skirt"）。衣物本身由当前衣柜标签
    # 承载，场景里只删不重述。裸名词形态（pattern 3）必须带前导读，否则 "moon-white nightgown"
    # 这类连字符复合词里的关键词会被单独挖掉。
    head = rf"(?:(?:a|an|the|her|his|their)\s+|(?:{color_alt})\s+|(?:[a-z]+\s+))"
    core = rf"(?:[a-z]+\s+){{0,2}}?(?:{color_alt}\s+)?(?:[a-z]+\s+){{0,2}}?(?:{outfit_alt})\b"
    garment = rf"{head}{core}"
    garment_loose = rf"(?:{head})?{core}"
    garment_phrase = rf"{garment}(?:\s+and\s+{garment})*"
    garment_phrase_loose = rf"{garment_loose}(?:\s+and\s+{garment})*"
    # 衣物做主语时的状态谓语尾巴（"rides up slightly as she shifts"）随衣物一起删；
    # 只收衣物状态动词，保留 sit/stand 等人物姿态谓语，避免把角色动作吃掉。
    state_tail = (
        rf"(?:\s+(?:rides?|slips?|slides?|pools?|gathers?|bunches?|clings?|hugs?|hangs?|drapes?|falls?|"
        rf"fits?|flares?|billows?|rises?|shifts?|opens?|splits?|slipping|hanging|pooling|riding|"
        rf"gathered|bunched|clinging|hugging|draped)\b[^,.;]*)?"
    )
    # 直接删除场景里的衣物描述（连同前导动词/介词/冠词），不再替换成 "the current outfit" 占位语——
    # 占位语对生图模型不可渲染；旧实现句中替换还会产生 "her the current outfit" 破句，
    # 且贪婪尾巴会把 "tying a bento box" 这类角色动作一起吃掉。
    patterns = [
        # 动词短语："wearing a red dress" / "dressed in ..."
        rf"\b(?:wears?|wearing|dressed\s+in)\s+{garment_phrase_loose}{state_tail}",
        # 介词短语："in a white camisole" / "with a red dress"
        rf"\b(?:in|with)\s+{garment_phrase_loose}{state_tail}",
        # 裸名词短语："her light dress (rides up ...)" / "a white camisole"
        rf"(?<![A-Za-z-]){garment_phrase}{state_tail}",
    ]
    text = scene_desc
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    # 清理悬空冠词/物主代词与多余标点空白（"her ."、"a ,"、"under her, ."）。
    text = re.sub(r"\b(?:a|an|the|her|his|their)\s+(?=[,.;]|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*,+", ", ", text)
    text = re.sub(r"\s*,\s*(?=[.!?;])", "", text)
    text = re.sub(r"([.!?])\s*,\s*", r"\1 ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,")


def _strip_conflicting_scene_appearance(
    service: Any,
    state: dict[str, Any],
    char: str,
    scene_desc: str,
) -> str:
    scene_desc = _strip_conflicting_scene_hair(scene_desc, _explicit_hair_override(service, state, char))
    scene_desc = _strip_conflicting_scene_eyes(scene_desc, _explicit_eye_override(service, state, char))
    scene_desc = _strip_conflicting_scene_outfit(scene_desc, _explicit_outfit_override(service, state), service._outfit_kw)
    return scene_desc


PUBLIC_MODEST_OUTFIT_TAG = "modest casual clothes"
PUBLIC_FALLBACK_OUTFIT_CHANGE = {
    "top": "plain white crew-neck t-shirt",
    "bottom": "dark blue jeans",
}
PUBLIC_FALLBACK_OUTFIT_TAG = ", ".join(PUBLIC_FALLBACK_OUTFIT_CHANGE.values())
PUBLIC_PRIVATE_OUTFIT_TERMS = (
    "lingerie", "underwear", "panties", "g-string", "thong",
    "nightgown", "nightdress", "negligee", "sleepwear", "pajamas",
    "babydoll", "chemise", "slip dress",
)
PUBLIC_SWIMWEAR_TERMS = ("bikini", "swimsuit", "school swimsuit")
PUBLIC_EXPOSURE_NEGATIVE_GUARDS = (
    "lingerie", "underwear", "nightgown", "nightdress", "negligee", "sleepwear",
    "revealing clothes", "cleavage", "underboob", "sideboob", "see-through clothing",
    "nipples", "pussy", "nude", "naked", "topless", "bottomless",
)
PUBLIC_SCENE_ANCHOR_RE = re.compile(
    r"\b("
    r"school|campus|university|classroom|library|office|workplace|company|meeting room|"
    r"mall|shopping mall|street|sidewalk|cafe|coffee shop|restaurant|station|subway|train|"
    r"bus stop|convenience store|supermarket|bookstore|museum|hospital|gym|park|stadium"
    r")\b|学校|校园|大学|教室|图书馆|办公室|公司|商场|街|咖啡店|餐厅|车站|地铁|便利店|超市|书店|博物馆|医院|健身房|公园",
    re.IGNORECASE,
)
PRIVATE_SCENE_ANCHOR_RE = re.compile(
    r"\b(home|bedroom|living room|kitchen|bathroom|hotel room|private room|sofa|bed)\b|家中|卧室|客厅|厨房|浴室|酒店房间|私人房间",
    re.IGNORECASE,
)
PUBLIC_SWIMWEAR_CONTEXT_RE = re.compile(
    r"\b(beach|shore|seaside|ocean|sea|pool|swimming pool|swim|water park|onsen|hot spring)\b|海边|海滩|泳池|游泳|水上乐园|温泉",
    re.IGNORECASE,
)
PUBLIC_SPORT_UNDERWEAR_CONTEXT_RE = re.compile(
    r"\b(gym|fitness|workout|yoga|sports?|training|dance studio)\b|健身房|运动|训练|瑜伽|舞蹈室",
    re.IGNORECASE,
)
PUBLIC_EXPLICIT_PLAY_RE = re.compile(
    r"\b(exhibitionism|public play|public sex|exposure play|humiliation play|bdsm|roleplay|punishment)\b|"
    r"露出|公开play|户外play|公开调教|羞耻play|惩罚play|过激play|故意穿|故意露|大胆露出",
    re.IGNORECASE,
)


def _world_character_place_key(service: Any, session_id: str) -> str:
    if not session_id or not hasattr(service, "build_world_state"):
        return ""
    try:
        world = service.build_world_state(session_id, user_text="", mode="image")
        place = world.get("character_place") or {}
        return str(place.get("key") or "").strip().lower()
    except Exception:
        logger.debug("world place key detection failed", exc_info=True)
        return ""


def _allows_public_private_outfit(scene_desc: str, is_intimate: bool = False, clothing_off: str = "") -> bool:
    if is_intimate:
        return True
    text = f"{scene_desc}\n{clothing_off}"
    if PUBLIC_EXPLICIT_PLAY_RE.search(text):
        return True
    low = _tag_key(clothing_off)
    return any(term in low for term in ("nude", "topless", "bottomless", "no underwear", "no panties", "completely nude"))


def _allows_public_swimwear(scene_desc: str, place_key: str = "") -> bool:
    return place_key == "beach" or bool(PUBLIC_SWIMWEAR_CONTEXT_RE.search(scene_desc or ""))


def _is_public_private_outfit_tag(
    tag: str,
    *,
    allow_swimwear: bool = False,
    allow_sport_underwear: bool = False,
) -> bool:
    low = _tag_key(tag)
    if not low:
        return False
    if low in {"nipples", "nipple", "pussy", "vagina", "nude", "naked", "topless", "bottomless"}:
        return True
    if allow_swimwear and any(term in low for term in PUBLIC_SWIMWEAR_TERMS):
        return False
    if allow_sport_underwear and "sports bra" in low:
        return False
    if "bikini" in low and any(term in low for term in ("armor", "armour", "costume")):
        return False
    if re.search(r"\bbra\b", low):
        return True
    if any(term in low for term in PUBLIC_PRIVATE_OUTFIT_TERMS):
        return True
    if any(term in low for term in PUBLIC_SWIMWEAR_TERMS):
        return True
    if "camisole" in low and any(term in low for term in ("lace", "night", "sleep", "lingerie")):
        return True
    if "robe" in low and any(term in low for term in ("bath", "sleep", "night", "lace")):
        return True
    if any(term in low for term in ("see through", "see-through", "transparent")):
        return True
    return False


def _remove_public_private_outfit_tags(
    text: str,
    candidate_tags: list[str] | None = None,
    *,
    allow_swimwear: bool = False,
    allow_sport_underwear: bool = False,
) -> tuple[str, list[str]]:
    candidates = None if candidate_tags is None else {_tag_key(tag) for tag in candidate_tags if _tag_key(tag)}
    removed: list[str] = []
    kept: list[str] = []
    for tag in _split_tags(text):
        if candidates is not None and _tag_key(tag) not in candidates:
            kept.append(tag)
            continue
        if _is_public_private_outfit_tag(
            tag,
            allow_swimwear=allow_swimwear,
            allow_sport_underwear=allow_sport_underwear,
        ):
            removed.append(tag)
        else:
            kept.append(tag)
    return normalize_appearance_text(", ".join(kept)), removed


def _appearance_has_public_body_cover(service: Any, text: str) -> bool:
    wardrobe = seed_wardrobe_from_text(
        text,
        getattr(service, "_outfit_kw", []),
        getattr(service, "_accessory_kw", []),
    )
    return bool(wardrobe.get("dress") or (wardrobe.get("top") and wardrobe.get("bottom")))


def _ensure_public_fallback_outfit(service: Any, state: dict[str, Any], session_id: str) -> str:
    fallback_tags = render_wardrobe(PUBLIC_FALLBACK_OUTFIT_CHANGE) or PUBLIC_FALLBACK_OUTFIT_TAG
    if not session_id or not isinstance(state, dict):
        return fallback_tags

    existing = session_schema.get_public_fallback_outfit(state)
    if render_wardrobe(existing) == fallback_tags:
        return fallback_tags

    closet = session_schema.get_closet(state)
    now = time.time()
    for slot, tags in PUBLIC_FALLBACK_OUTFIT_CHANGE.items():
        closet = closet_add(closet, f"public fallback {slot}", slot, tags, now=now)
    session_schema.set_closet(state, closet)
    session_schema.set_public_fallback_outfit(state, dict(PUBLIC_FALLBACK_OUTFIT_CHANGE))
    if hasattr(service, "_save_session_state"):
        service._save_session_state(session_id, state)
    if hasattr(service, "_ulog"):
        service._ulog(
            session_id,
            "WARDROBE",
            f'public fallback outfit stored -> "{fallback_tags[:140]}"',
        )
    return fallback_tags


def _public_render_context(service: Any, state: dict[str, Any], session_id: str, scene_desc: str) -> bool:
    scene = str(scene_desc or "")
    scene_is_public = bool(PUBLIC_SCENE_ANCHOR_RE.search(scene))
    if scene_is_public:
        return True
    if PRIVATE_SCENE_ANCHOR_RE.search(scene):
        return False
    if not session_id or not hasattr(service, "build_world_state"):
        return False
    try:
        world = service.build_world_state(session_id, user_text="", mode="image")
        place = world.get("character_place") or {}
        return bool(place.get("public"))
    except Exception:
        logger.debug("public render context detection failed", exc_info=True)
        return False


def _guard_public_outfit(
    service: Any,
    state: dict[str, Any],
    session_id: str,
    scene_desc: str,
    effective_appearance: str,
    one_shot_appearance: str,
    negative: str,
    current_outfit_tags: list[str] | None = None,
    is_intimate: bool = False,
    clothing_off: str = "",
) -> tuple[str, str, str, list[str]]:
    """公开场合里把睡衣/内衣类持久穿搭降成仅本图的得体日常穿搭。"""
    if not _public_render_context(service, state, session_id, scene_desc):
        return effective_appearance, one_shot_appearance, negative, []
    if _allows_public_private_outfit(scene_desc, is_intimate=is_intimate, clothing_off=clothing_off):
        return effective_appearance, one_shot_appearance, negative, []
    place_key = _world_character_place_key(service, session_id)
    allow_swimwear = _allows_public_swimwear(scene_desc, place_key)
    allow_sport_underwear = bool(PUBLIC_SPORT_UNDERWEAR_CONTEXT_RE.search(scene_desc or ""))
    effective_clean, removed_effective = _remove_public_private_outfit_tags(
        effective_appearance,
        current_outfit_tags,
        allow_swimwear=allow_swimwear,
        allow_sport_underwear=allow_sport_underwear,
    )
    one_shot_clean, removed_one_shot = _remove_public_private_outfit_tags(
        one_shot_appearance,
        allow_swimwear=allow_swimwear,
        allow_sport_underwear=allow_sport_underwear,
    )
    removed = removed_effective + removed_one_shot
    if not removed:
        return effective_appearance, one_shot_appearance, negative, []

    combined = _dedupe_prompt_modules([effective_clean, one_shot_clean])
    if not _appearance_has_public_body_cover(service, combined):
        fallback_outfit = _ensure_public_fallback_outfit(service, state, session_id)
        effective_clean = _dedupe_prompt_modules([effective_clean, fallback_outfit])
    negative = _append_negatives(negative, *removed, *PUBLIC_EXPOSURE_NEGATIVE_GUARDS, "revealing public outfit")
    return effective_clean, one_shot_clean, negative, removed


def public_outfit_guard_context(service: Any, session_id: str, dynamic_appearance: str, scene_desc: str = "") -> str:
    state = service._get_session_state(session_id) if session_id else {}
    if not _public_render_context(service, state, session_id, scene_desc):
        return ""
    if _allows_public_private_outfit(scene_desc):
        return ""
    place_key = _world_character_place_key(service, session_id)
    _, removed = _remove_public_private_outfit_tags(
        dynamic_appearance,
        allow_swimwear=_allows_public_swimwear(scene_desc, place_key),
        allow_sport_underwear=bool(PUBLIC_SPORT_UNDERWEAR_CONTEXT_RE.search(scene_desc or "")),
    )
    if not removed:
        return ""
    preview = ", ".join(removed[:4])
    return (
        "公开场合穿搭约束: 当前附加外貌里含睡衣/内衣/泳装类暴露项"
        f"（{preview}），但本轮世界地点属于公开场合。"
        "本图不要直接使用这些项；请改写为得体日常外出穿搭（例如 modest casual clothes、外套/校内日常服），"
        "new_appearance_tags 只写本图需要的得体替代穿搭。"
    )


_FULL_NUDE_RE = re.compile(
    r"\b(nude|naked|fully undressed|completely undressed|stark naked|no clothes|nothing on)\b",
    re.IGNORECASE,
)
# 性爱场景关键词（判定 is_sex_scene / 二人称身体保留都要用，提前到模块级共享）。
SEX_SCENE_KEYWORDS = [
    "sex", "make love", "penetration", "penetrating", "vaginal", "missionary", "doggystyle",
    "cowgirl", "girl on top", "straddling", "straddle", "riding", "grinding", "thrust",
    "thrusting", "squelch", "impaled", "insertion", "humping", "creampie", "naked together",
    "fellatio", "blowjob", "oral", "cunnilingus", "handjob", "paizuri", "footjob",
    "fingering", "orgasm", "climax", "mating press", "squirting",
]
# 性爱场景里明确提到性器/体液时，把对应 danbooru tag 补进最终正向——
# 自然语言长句里的提及会被生图模型稀释，tag 级补强才能真正画出来。
_EXPLICIT_SEXUAL_TAG_MAP = (
    (re.compile(r"\b(?:penis|cock|dick|erection|shaft|glans|phallus)\b", re.IGNORECASE), "penis"),
    (re.compile(r"\b(?:testicles|balls|scrotum)\b", re.IGNORECASE), "testicles"),
    (re.compile(r"\b(?:pussy|vagina|vaginal|clitoris|clit|labia|cervix|womb|uterus)\b", re.IGNORECASE), "pussy"),
    (re.compile(r"\b(?:anus|anal|asshole|butthole|rectum)\b", re.IGNORECASE), "anus"),
    (re.compile(r"\b(?:cum|semen|creampie|ejaculat\w*|sperm)\b", re.IGNORECASE), "cum"),
    (re.compile(r"\b(?:fellatio|blowjob)\b", re.IGNORECASE), "fellatio"),
    (re.compile(r"\b(?:orgasm|climax|cumming)\b", re.IGNORECASE), "orgasm"),
    (re.compile(r"\b(?:pussy juice|love juice|wetness)\b", re.IGNORECASE), "pussy juice"),
    (re.compile(r"\b(?:squirt\w*|squirting)\b", re.IGNORECASE), "squirting"),
    # 交合的委婉说法（point of union / joined / intercourse / penetration）：补 "sex" tag，
    # 否则翻译层把性器委婉化后，"清晰可见的交合" 不一定画得出来。
    (re.compile(r"\b(?:point of union|join(?:ed|ing|s)?|intercourse|copulation|lovemaking|mating|coupling|penetrat\w+|impaled)\b", re.IGNORECASE), "sex"),
)


def _explicit_sexual_scene_tags(scene_desc: str) -> list[str]:
    """扫描最终英文场景，返回被明确提到的性器/体液 tag（未提到不返回）。"""
    tags: list[str] = []
    for rx, tag in _EXPLICIT_SEXUAL_TAG_MAP:
        if rx.search(scene_desc or "") and tag not in tags:
            tags.append(tag)
    return tags
_NUDE_STATE_WORDS = (
    "topless", "bottomless", "barefoot", "no panties", "no underwear", "no bra",
    "exposed breasts", "exposed nipples", "bare shoulders",
)
_BOTTOM_CLOTHING_OFF_WORDS = (
    "bottomless", "no panties", "no underwear", "panties", "panty",
    "g-string", "thong", "knickers", "briefs", "underwear",
)
_WARDROBE_STATE_PREFIX = {
    "half_off": "half-removed",
    "damaged": "torn",
}
_WARDROBE_UPPER_COVER_SLOTS = ("dress", "top", "bra")
_WARDROBE_LOWER_COVER_SLOTS = ("dress", "bottom", "panties")
_WARDROBE_EXPOSURE_NEGATIVES = (
    "nude", "naked", "nudity", "topless", "bottomless", "completely nude",
    "revealing clothes", "nipples", "nipple", "pussy", "vagina",
)


def _removable_appearance_tags(service: Any, appearance: str) -> list[str]:
    text = str(appearance or "").strip()
    if not text:
        return []
    try:
        parsed = service._parse_appearance(text)
    except Exception:
        parsed = {}
    removable: list[str] = []
    if not isinstance(parsed, dict):
        return removable
    for key in ("outfit", "accessory"):
        for tag in parsed.get(key, []) or []:
            tag = str(tag or "").strip()
            if tag and tag not in removable:
                removable.append(tag)
    return removable


def _apply_clothing_off(service: Any, clothing_off: str, effective_appearance: str, neg: str, worn_tags: list[str]) -> tuple[str, str]:
    """按规划器的一次性"脱衣/裸露"指令，从本次渲染的外观里剥离服装。

    只改这张图的 effective_appearance（持久的衣柜/dynamic_appearance 不动），实现"场景内
    暂时脱衣、场景结束随规划器重新判断而自动复原"。这是修复"叙事脱了衣服但图里还在"的核心：
    让规划器的逐图判断覆盖陈旧的持久着装，而不是反过来。

    worn_tags 是"当前所穿"的服饰标签（来自 dynamic_appearance/一次性外观）——按它做标签级匹配，
    不依赖关键词分类，避免漏掉未登记在 outfit_keywords 里的衣物（如 cardigan）。
    """
    raw = (clothing_off or "").strip()
    if not raw:
        return effective_appearance, neg
    raw_lower = raw.lower()
    appearance = effective_appearance
    worn = [w for w in (worn_tags or []) if w and w.strip()]
    if _FULL_NUDE_RE.search(raw):
        for tag in worn:
            appearance = service._remove_tag(appearance, tag)
        if "nude" not in appearance.lower():
            appearance = f"{appearance}, completely nude" if appearance.strip() else "completely nude"
        # 把刚脱掉的衣物压进负向：scene 里若仍残留"湿裙子贴着胸口"之类描述，靠它抵消，避免衣服被画回去。
        if worn:
            neg = _append_negatives(neg, *worn)
    else:
        extra: list[str] = []
        for tok in [t.strip() for t in re.split(r"[,;]+", raw) if t.strip()]:
            tl = tok.lower()
            if tl in _NUDE_STATE_WORDS:
                extra.append(tok)
                continue
            # 双向子串匹配，容忍 "cardigan" 对 "cotton knit cardigan"
            for tag in worn:
                tagl = tag.lower()
                if tl and (tl in tagl or tagl in tl):
                    appearance = service._remove_tag(appearance, tag)
        for w in extra:
            if w.lower() not in appearance.lower():
                appearance = f"{appearance}, {w}" if appearance.strip() else w
    appearance = normalize_appearance_text(appearance)
    # 这张图既然要露，负向别再压制裸体（仅去裸体相关词，不动评级词，避免和评级系统打架）
    neg = _remove_negatives(neg, "nude", "naked", "nudity", "topless", "bottomless", "completely nude", "revealing clothes")
    if _FULL_NUDE_RE.search(raw) or any(word in raw_lower for word in _BOTTOM_CLOTHING_OFF_WORDS):
        neg = _remove_negatives(neg, *BOTTOM_EXPOSURE_NEGATIVES)
    return appearance, neg


def _prefix_wardrobe_tags(tags: str, prefix: str) -> str:
    return normalize_appearance_text(", ".join(f"{prefix} {tag}" for tag in _split_tags(tags) if tag))


def _append_unique_appearance_tags(appearance: str, tags: list[str] | tuple[str, ...]) -> str:
    text = appearance
    lower = f", {text.lower()},"
    for tag in tags:
        tag = str(tag or "").strip()
        if not tag:
            continue
        needle = f", {tag.lower()},"
        if needle in lower:
            continue
        text = f"{text}, {tag}" if text.strip() else tag
        lower = f", {text.lower()},"
    return normalize_appearance_text(text)


def _wardrobe_state_exposure_tags(wardrobe: dict[str, Any], states: dict[str, str]) -> list[str]:
    worn_slots = {
        slot for slot in WARDROBE_CLOTHING_SLOTS
        if str((wardrobe or {}).get(slot) or "").strip()
    }
    if not worn_slots or not states:
        return []

    tags: list[str] = []
    if states.get("bra") in {"half_off", "damaged", "removed"}:
        tags.append("nipples")
    if states.get("panties") in {"half_off", "damaged", "removed"}:
        tags.append("pussy")

    def has_normal_cover(slots: tuple[str, ...]) -> bool:
        return any(slot in worn_slots and states.get(slot) not in {"half_off", "damaged", "removed"} for slot in slots)

    upper_touched = any(states.get(slot) in {"half_off", "damaged", "removed"} for slot in _WARDROBE_UPPER_COVER_SLOTS)
    lower_touched = any(states.get(slot) in {"half_off", "damaged", "removed"} for slot in _WARDROBE_LOWER_COVER_SLOTS)
    if upper_touched and not has_normal_cover(_WARDROBE_UPPER_COVER_SLOTS):
        tags.append("nipples")
    if lower_touched and not has_normal_cover(_WARDROBE_LOWER_COVER_SLOTS):
        tags.append("pussy")

    all_removed = all(states.get(slot) == "removed" for slot in worn_slots)
    if all_removed:
        tags.append("nude")

    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag not in seen:
            out.append(tag)
            seen.add(tag)
    return out


def _apply_wardrobe_item_states(
    service: Any,
    state: dict[str, Any],
    appearance: str,
) -> tuple[str, str, list[str], list[str]]:
    """把衣柜部件状态渲染到本次 prompt，不改写 wardrobe 本体。"""
    try:
        wardrobe = service._get_wardrobe(state)
    except Exception:
        wardrobe = session_schema.get_wardrobe(state)
    states = {
        slot: value
        for slot, value in session_schema.get_wardrobe_item_states(state).items()
        if str((wardrobe or {}).get(slot) or "").strip()
    }
    if not states:
        return appearance, render_wardrobe(wardrobe), [], []

    rendered_parts: list[str] = []
    removed_tags: list[str] = []
    text = appearance
    for slot in WARDROBE_RENDER_ORDER:
        tags = str((wardrobe or {}).get(slot) or "").strip()
        if not tags:
            continue
        item_state = states.get(slot)
        if item_state:
            original_tags = _split_tags(tags)
            for tag in original_tags:
                text = service._remove_tag(text, tag)
            if item_state == "removed":
                removed_tags.extend(original_tags)
                continue
            prefixed = _prefix_wardrobe_tags(tags, _WARDROBE_STATE_PREFIX[item_state])
            if prefixed:
                rendered_parts.append(prefixed)
                text = _append_unique_appearance_tags(text, _split_tags(prefixed))
        else:
            rendered_parts.append(tags)

    exposure_tags = _wardrobe_state_exposure_tags(wardrobe, states)
    text = _append_unique_appearance_tags(text, exposure_tags)
    return normalize_appearance_text(text), normalize_appearance_text(", ".join(rendered_parts)), removed_tags, exposure_tags


def _free_wardrobe_state_exposure_negatives(neg: str, exposure_tags: list[str], removed_tags: list[str]) -> str:
    if removed_tags:
        neg = _append_negatives(neg, *removed_tags)
    if not exposure_tags:
        return neg
    neg = _remove_negatives(neg, *_WARDROBE_EXPOSURE_NEGATIVES)
    if any(tag in {"pussy", "nude"} for tag in exposure_tags):
        neg = _remove_negatives(neg, *BOTTOM_EXPOSURE_NEGATIVES)
    return neg


def _strip_conflicting_scene_light(service: Any, session_id: str, scene_desc: str) -> str:
    if not session_id or not hasattr(service, "_get_time_context"):
        return scene_desc
    try:
        phase = (service._get_time_context(session_id).get("light_phase") or "").strip()
    except Exception:
        return scene_desc
    if phase not in {"日间自然光", "朝阳", "黎明"}:
        return scene_desc

    replacements = [
        (r"\bevening\s+twilight\b", "afternoon daylight"),
        (r"\btwilight\s+sky\b", "daytime sky"),
        (r"\b(?:twilight|dusk|sunset|sundown|evening\s+sky)\b", "daylight"),
        (r"\bevening\s+(?:sunlight|light|glow)\b", "afternoon sunlight"),
        (r"\bevening\b", "afternoon"),
        (r"\borange[-\s]pink\s+clouds\b", "daytime clouds"),
        (r"\borange[-\s]red\s+(?:sky|clouds|glow|light)\b", "daytime sky"),
        (r"\bglowing\s+sky\b", "daytime sky"),
        (r"\bearly\s+streetlights?\b", "street details"),
        (r"\bstreetlights?\s+(?:just\s+)?(?:flickers?|turns?)\s+on\b", "streetlights remain off"),
        (r"\bthe\s+warm\s+yellow\s+light\s+of\s+the\s+streetlamp\s+just\s+flickers\s+on\b", "daylight on the street"),
        (r"傍晚|黄昏|暮色|夕阳|落日|晚霞|路灯刚亮|街灯初亮", "白天自然光"),
    ]
    text = scene_desc
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    return text


def _resolve_negative_conflicts(positive: str, negative: str) -> str:
    """删掉与正向直接打架的负向词，避免同一标签被一边推一边压。

    1) 任何与正向完全相同的负向 token 直接删（例如角色是金发，正向有 blonde hair，
       默认负向里那条防杂色发的 blonde hair 就该让位）。
    2) 角色自己的发色不再被压：负向里的 "<颜色> hair" 若该颜色出现在正向的发型描述里，删掉。
    """
    pos_tokens = {_tag_key(t) for t in positive.split(",") if t.strip()}
    pos_hair_colors = set()
    for tok in pos_tokens:
        if "hair" in tok:
            pos_hair_colors.update(_hair_colors_in_text(tok))
    kept = []
    for tok in [x.strip() for x in negative.split(",") if x.strip()]:
        low = _tag_key(tok)
        if low in pos_tokens:
            continue
        match = re.fullmatch(r"(\w+)\s+hair", low)
        if match and match.group(1) in pos_hair_colors:
            continue
        kept.append(tok)
    return ", ".join(kept)


def _append_negatives(negative: str, *terms: str) -> str:
    seen = {item.strip().lower() for item in negative.split(",") if item.strip()}
    additions = []
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen:
            additions.append(term.strip())
            seen.add(key)
    if additions:
        negative = f"{negative}, {', '.join(additions)}" if negative else ", ".join(additions)
    return negative


def _remove_negatives(negative: str, *terms: str) -> str:
    banned = {term.strip().lower() for term in terms if term.strip()}
    kept = []
    for item in [part.strip() for part in negative.split(",") if part.strip()]:
        if item.lower() not in banned:
            kept.append(item)
    return ", ".join(kept)


def _infer_prompt_view(scene_desc: str) -> str:
    text = scene_desc.strip().lower()
    if "mirror reflection" in text or "mirror selfie" in text:
        return "mirror"
    if text.startswith("a front-camera selfie") or text.startswith("a selfie of"):
        return "selfie"
    if text.startswith("a photo of"):
        return "portrait"
    if text.startswith("first-person pov"):
        return "pov"
    return ""


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _strip_non_mirror_camera_artifacts(scene_desc: str) -> str:
    text = scene_desc
    protected = {
        "__OFF_FRAME_PHONE_CAMERA__": "off-frame front-facing phone camera",
        "__NO_VISIBLE_PHONE__": "no visible phone",
    }
    for token, phrase in protected.items():
        text = re.sub(re.escape(phrase), token, text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bgazing\s+(?:at|toward|towards|into)\s+(?:a\s+|the\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\s+screen\s+with\b",
        "gazing toward the viewer with",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:gazing|staring|looking|glancing|peeking|checking)\s+(?:at|toward|towards|into)\s+(?:a\s+|the\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\s+screen\b",
        "looking toward the viewer",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:the\s+)?(?:smartphone|phone|cellphone|mobile phone)\s+screen\s+(?:is\s+)?(?:lit\s+)?(?:showing|displaying|with)\s+[^,.;]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    phrase_patterns = [
        r"\b(?:she|he|the\s+character|the\s+woman|the\s+girl|the\s+man|the\s+boy)\s+(?:holds?|holding|grips?|gripping|checks?|checking|scrolls?\s+through|scrolling\s+through|plays?\s+with|using|uses?|shakes?|shaking|types?\s+on|typing\s+on|texts?\s+on|texting\s+on)\s+(?:a\s+|an\s+|one\s+|her\s+|his\s+|the\s+)?(?:smartphone|phone|cellphone|mobile phone)(?:\s+in\s+(?:her|his|both|one)\s+hands?)?(?:\s+with\s+[^,.;]*)?",
        r"\b(?:as|while)\s+(?:she|he|the\s+character|the\s+woman|the\s+girl|the\s+man|the\s+boy)\s+(?:types?|typing|texts?|texting)\s+(?:a\s+)?message\b",
        r"\b(?:typing|texting)\s+(?:a\s+)?message\b",
        r"\b(?:while\s+)?(?:the\s+)?(?:other|another|one)\s+(?:hand\s+)?(?:is\s+)?(?:idly\s+|casually\s+)?(?:holds?|holding|grips?|gripping|checks?|checking|scrolls?\s+through|scrolling\s+through|plays?\s+with|using)\s+(?:a\s+|an\s+|one\s+|her\s+|his\s+|the\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:one|another|the other)\s+hand\s+(?:is\s+)?(?:on|near|around)\s+(?:a\s+|one\s+|her\s+|his\s+|the\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\bholding\s+(?:a\s+|one\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\b(?:\s+in\s+(?:her|his|both|one)\s+hands?)?",
        r"\b(?:smartphone|phone|cellphone|mobile phone)\s+in\s+(?:her\s+)?hand\b",
        r"\bvisible\s+(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:smartphone|phone|cellphone|mobile phone)\s+screen\b",
        r"\b(?:message|chat)\s+interface(?:\s+countdown\s+prompt)?\b",
        r"\bcountdown\s+prompt\b",
        r"\bcountdown\b",
        r"\bwith\s+(?:a\s+|one\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\b(?:\s+in\s+(?:her|his|both|one)\s+hands?)?",
        r"\bmirror\s+selfie\b",
        r"\bmirror\s+reflection\b",
        r"\bin\s+(?:a\s+)?mirror\b",
    ]
    for pattern in phrase_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:smartphone|phone|cellphone|mobile phone)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmirror\b", "", text, flags=re.IGNORECASE)
    text = text.replace("手机", "").replace("镜子", "").replace("对镜", "")
    text = re.sub(
        r"\b(?:while\s+)?(?:the\s+)?(?:other|another|one)\s+(?:hand\s+)?(?:is\s+)?(?:idly\s+|casually\s+)?(?:holds?|holding|grips?|gripping|checks?|checking|scrolls?\s+through|scrolling\s+through|plays?\s+with|using)\s+(?:a|an|one|her|his|the)?\s*(?=[,.;]|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(?:with|holding|holds?|using|uses?|gripping|grips?)\s+(?:a|an|one|her|his|the)\s*(?=[,.;]|$)", "", text, flags=re.IGNORECASE)
    # 手机短语被删后留下的孤儿介词片段（"in her hand." / "in both hands,"）：只清标点之后或句首、
    # 且后面不再跟实质内容的（hands 后紧跟标点/结尾）。像 ", with her hands full of flour," 这种
    # 合法从句 hands 后还有内容，不能误删。
    text = re.sub(r"(?<=[,.;])\s*(?:in|with)\s+(?:her|his|both|one|the other)\s+hands?\s*,?\s*(?=[,.;]|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(?:in|with)\s+(?:her|his|both|one|the other)\s+hands?\s*,?\s*(?=[,.;]|$)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*,\s*,+", ", ", text)
    text = re.sub(r"\s*,\s*(?=[.!?;])", "", text)
    text = re.sub(r"([.!?])\s*,\s*", r"\1 ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"(^|,\s*)(?:and\s*)?(?=,|$)", "", text, flags=re.IGNORECASE)
    for token, phrase in protected.items():
        text = text.replace(token, phrase)
    return text.strip(" ,")


SECOND_PERSON_VISUAL_SUBJECT_RE = re.compile(
    r"^(?P<prefix>\s*(?:(?:first-person\s+pov|pov)[^,]*,\s*)?"
    r"(?:looking\s+at\s+a\s+(?:woman|girl|man|boy),\s*)?"
    r"(?:solo,\s*)?)(?:you|user)\b",
    re.IGNORECASE,
)

SECOND_PERSON_SUBJECT_ACTION_RE = re.compile(
    r"\b(?:you|user)\s+(?=(?:are|lounge|sit|stand|lie|lean|kneel|crouch|wear|curl|rest|pose|look|wait|hold|twirl|play|smile|sleep)\b)",
    re.IGNORECASE,
)

# 场景里出现“与角色性别相反的第二个人（伴侣）”的信号。scene 到这一步已译成英文，故只匹配英文。
# 用于兜底：当本该是单人图却混进了伴侣（多为用户被写成第三人称 him/he），改走亲密/伴侣局部路径。
MALE_PARTNER_RE = re.compile(
    r"\b(?:he|him|his|himself|boyfriend|husband|male partner|a man|the man|another man)\b",
    re.IGNORECASE,
)
FEMALE_PARTNER_RE = re.compile(
    r"\b(?:she|her|hers|herself|girlfriend|wife|female partner|a woman|the woman|another woman)\b",
    re.IGNORECASE,
)
USER_BODY_PART_RE = re.compile(
    r"\b(?:your|user'?s|partner'?s|male|female)\s+"
    r"(?:hands?|arms?|torso|chest|body|legs?|feet|thighs?|shoulders?|back|abdomen|belly|waist|lap)\b|"
    r"\b(?:hands?|arms?|torso|chest|legs?|feet|thighs?|shoulders?|back|lap)\s+"
    r"(?:visible|at the edge of frame|in frame)\b",
    re.IGNORECASE,
)

# 角色处于相机背后 / 背对相机的构图信号。
# 所有“相机相对”机位（pov/selfie/portrait/mirror）都隐含同一前提：角色在相机前方且面向相机。
# 命中这些信号说明角色跑到了相机背后或背对相机（如从背后环抱正对屏幕的用户），POV 会自相矛盾
# （第一人称看不到她），必须退回 third（旁观机位，对角色朝向零约束）。
# 只匹配无歧义的“角色在相机背后/背对”措辞——刻意不含裸 behind/背后（会误伤“背后的窗户/街景”）。
POV_FACING_BREAK_RE = re.compile(
    r"\bfrom behind\b|\bback[- ]?hug\b|\bfacing away\b|\bback turned\b|"
    r"\bback to the (?:camera|viewer|user)\b|\bseen from behind\b|\brear view\b|"
    r"\bhugs?\s+[^,.;]*?\bfrom behind\b|\barms?\s+around\s+[^,.;]*?\bfrom behind\b",
    re.IGNORECASE,
)
POV_FACING_BREAK_ZH = (
    "从背后", "背后抱", "背后环", "背后搂", "背后贴", "背后靠",
    "身后抱", "身后环", "身后搂", "背对", "背朝", "背向", "背靠着你",
)


def _scene_breaks_pov_facing(*sources: str) -> bool:
    combined = " ".join(str(s) for s in sources if s)
    if not combined:
        return False
    if POV_FACING_BREAK_RE.search(combined):
        return True
    return any(kw in combined for kw in POV_FACING_BREAK_ZH)


# 规划器/翻译层误写进 scene 的第一人称取景措辞：当画面已被判为角色背对相机、退回 third 时，
# 这些“第一人称/用户视角”短语是自相矛盾的谎言，需连同其后的冠词一起清掉，避免留下悬空 "of a"。
FIRST_PERSON_LIE_RE = re.compile(
    r"\bfirst[- ]person\s+pov[^,.;]*?(?:looking toward the character)?\s*[,;]?\s*|"
    r"\bfirst[- ]person\s+(?:view|point of view|perspective)\s+of\s+(?:a|an|the)\s+|"
    r"\bfrom\s+(?:a\s+)?first[- ]person\s+(?:viewpoint|point of view|perspective)\s*[,;]?\s*|"
    r"\bfrom\s+the\s+user'?s\s+(?:viewpoint|point of view|perspective)[^,.;]*?[,;]?\s*",
    re.IGNORECASE,
)
# 设备入画（用户要求拍照/录像）的英文兜底：scene 到这步已译成英文。命中则放行手机/相机，不抹设备。
# 只匹配【无歧义的拍摄意图】词；故意不含 "holding a phone/smartphone"——那既可能是误泄漏的手机
# （应被清掉），区分不了意图。真正的拍摄意图主要由规划器 device_in_frame 与中文关键词（基于用户原话）给出。
DEVICE_SCENE_RE = re.compile(
    r"\b(?:recording|filming|sex tape|on camera|camcorder|video camera)\b",
    re.IGNORECASE,
)
# 自拍/对镜取景措辞：伴侣同框时必须清掉，避免“自拍框 + 画面里有第二人”自相矛盾。
SELF_CAMERA_FRAMING_RE = re.compile(
    r"\bA front-camera selfie of a (?:woman|man|girl|boy)\b|"  # 旧措辞兼容
    r"\bA selfie of a (?:woman|man|girl|boy)\b|"
    r"\bone arm extended toward the viewer\b|"
    r"\bA photo of a (?:woman|man|girl|boy)\b|"  # portrait 取景：别人帮拍
    r"\bposing for the camera\b|"
    r"\btaken by someone else just out of frame\b|"
    r"\bA mirror reflection of a (?:woman|man|girl|boy)\b|"
    r"\bshot by an off-frame front-facing phone camera\b|"
    r"\bno visible phone\b|"
    r"\bonly mirror reflection is visible\b|"
    r"\bno foreground person\b|"
    r"\bsingle reflected body\b",
    re.IGNORECASE,
)


def _normalize_second_person_visual_subject(scene_desc: str, keep_user_body: bool = False) -> str:
    text = (scene_desc or "").strip()
    if not text:
        return text
    if keep_user_body:
        # 伴侣/性爱场景：用户的身体与动作是合法入画内容，任何二人称改写都会破坏伴侣归属——
        # "straddles your waist" 会变成跨坐在自己身上，"Both of you are naked" 会变成
        # "Both of the character is naked" 破句。用户只做画面边缘局部由 build_prompt 的
        # 伴侣局部规则保证，不需要在这里改写。
        return text
    text = SECOND_PERSON_VISUAL_SUBJECT_RE.sub(lambda m: f"{m.group('prefix')}The character", text, count=1)
    text = SECOND_PERSON_SUBJECT_ACTION_RE.sub("the character ", text)
    # 单人场景：用户不该被画进去，"your waist" 之类归到角色自己身体。
    text = re.sub(
        r"\byour\s+(hair|face|body|shoulder|shoulders|chest|waist|leg|legs|shirt|dress|clothes|outfit|hand|hands|arm|arms|eyes|mouth)\b",
        r"the character's \1",
        text,
        flags=re.IGNORECASE,
    )

    verb_fixes = {
        "are": "is",
        "lounge": "lounges",
        "sit": "sits",
        "stand": "stands",
        "lie": "lies",
        "lean": "leans",
        "kneel": "kneels",
        "crouch": "crouches",
        "wear": "wears",
        "curl": "curls",
        "rest": "rests",
        "pose": "poses",
        "look": "looks",
        "wait": "waits",
        "hold": "holds",
        "twirl": "twirls",
        "play": "plays",
        "smile": "smiles",
        "sleep": "sleeps",
    }
    for base, fixed in verb_fixes.items():
        text = re.sub(rf"\b(The character|the character)\s+{base}\b", rf"\1 {fixed}", text, flags=re.IGNORECASE)
    return text


def _clean_visual_identity_tag(value: Any) -> str:
    text = str(value or "").strip().strip("`\"' ")
    if not text:
        return ""
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\([^()]*[\u3040-\u30ff\u3400-\u9fff][^()]*\)", "", text).strip()
    low = text.lower()
    if low in EMPTY_IDENTITY_MARKERS:
        return ""
    if re.search(r"[A-Za-z]", text) and not NON_LATIN_IDENTITY_RE.search(text):
        return text.strip(" ,;/|")
    for part in re.split(r"\s*(?:/|\||;|,|，|、)\s*", text):
        part = part.strip(" `\"'")
        if re.search(r"[A-Za-z]", part) and not NON_LATIN_IDENTITY_RE.search(part):
            return part.strip(" ,;/|")
    return ""


def _appearance_identity_fallback(prefix: str) -> tuple[str, str]:
    for tag in [part.strip() for part in (prefix or "").split(",") if part.strip()]:
        clean = _clean_visual_identity_tag(tag)
        if not clean:
            continue
        match = re.search(r"\(([^)]*[A-Za-z][^)]*)\)", clean)
        if match:
            return clean, _clean_visual_identity_tag(match.group(1))
    return "", ""


def _visual_character_identity(state: dict[str, Any]) -> tuple[str, str]:
    raw_character = (state.get("custom_character") or "").strip()
    series = (state.get("custom_series") or "").strip()
    series_key = re.sub(r"\s+", " ", series.lower()).strip()
    if series_key in ORIGINAL_SERIES_MARKERS:
        return "", ""
    visual_character = _clean_visual_identity_tag(state.get("custom_visual_character"))
    visual_series = _clean_visual_identity_tag(state.get("custom_visual_series"))
    if visual_character and not visual_series:
        match = re.search(r"\(([^)]*[A-Za-z][^)]*)\)", visual_character)
        if match:
            visual_series = _clean_visual_identity_tag(match.group(1))
    if visual_character and visual_series:
        return visual_character, visual_series
    if series:
        fallback_source = ", ".join(
            part for part in [
                state.get("custom_positive_prefix") or "",
                session_schema.get_outfit(state),
            ] if part
        )
        fallback_character, fallback_series = _appearance_identity_fallback(fallback_source)
        if fallback_character and fallback_series:
            return fallback_character, fallback_series
    character = _clean_visual_identity_tag(raw_character)
    clean_series = _clean_visual_identity_tag(series)
    if character and clean_series:
        return character, clean_series
    return "", ""


def _strip_non_visual_role_names(service: Any, state: dict[str, Any], session_id: str, scene_desc: str) -> str:
    character, series = _visual_character_identity(state)
    if character and series:
        text = scene_desc
        replacements = {
            (state.get("custom_character") or "").strip(): character,
            (state.get("custom_series") or "").strip(): series,
        }
        for raw, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            if raw and raw != replacement:
                text = text.replace(f"{raw}\u7684", f"{replacement}'s ")
                text = text.replace(raw, replacement)
        return text

    names = {
        (state.get("custom_character") or "").strip(),
        (service.config.get("bot_name") or "").strip(),
    }
    if session_id:
        names.add((service._get_session_cfg(session_id, "bot_name", "") or "").strip())
    names.discard("")

    aliases: set[str] = set()
    default_role_name = "\u857e\u4f0a"
    if default_role_name in names:
        aliases.update({"Rey", "Rei", "Lei"})

    text = scene_desc
    for name in sorted(names, key=len, reverse=True):
        if not name:
            continue
        if name.isascii():
            aliases.add(name)
        else:
            text = text.replace(f"{name}\u7684", "\u89d2\u8272\u7684")
            text = text.replace(name, "\u89d2\u8272")

    for alias in sorted(aliases, key=len, reverse=True):
        escaped = re.escape(alias)
        text = re.sub(rf"\b{escaped}'s\b", "the character's", text, flags=re.IGNORECASE)
        text = re.sub(rf"\b{escaped}\b", "the character", text, flags=re.IGNORECASE)
    return text


def view_opener(view: str, gender: str = "girl") -> str:
    subj = "man" if gender == "boy" else "woman"
    count = "1boy" if gender == "boy" else "1girl"
    return {
        "selfie": f"A selfie of a {subj}, solo, upper body framing, looking at viewer, one arm extended toward the viewer",
        "mirror": f"A mirror reflection of a {subj}, solo, single reflected body, only mirror reflection is visible, no foreground person, holding one smartphone with one hand, looking at viewer through the mirror",
        "pov": "First-person POV from the user's viewpoint, looking toward the character",
        "third": f"{count}, solo",
        # 别人（用户/他人）帮角色拍的照片：角色看向镜头为镜头摆姿势，拍摄者在画面外，画面里只有角色，不出现手机/相机。
        "portrait": f"A photo of a {subj}, solo, upper body framing, looking at viewer, posing for the camera, taken by someone else just out of frame",
    }.get(view, "")


def build_prompt(
    service: Any,
    scene_desc: str,
    is_ntr: bool = False,
    session_id: str = "",
    one_shot_appearance: str = "",
    is_intimate: bool = False,
    partner_in_frame: bool = False,
    device_in_frame: bool = False,
    clothing_off: str = "",
    view: str = "",
    ignore_wardrobe_item_states: bool = False,
) -> tuple[str, str]:
    raw_scene_desc = scene_desc
    state = service._get_session_state(session_id) if session_id else {}
    # 伴侣/性爱信号要在二人称归一化之前判："straddles your waist" 里的用户身体是合法入画内容，
    # 一旦被改写成 "the character's waist"，后面的伴侣局部检测就再也匹配不到。
    pre_scene_lower = (scene_desc or "").lower()
    keep_user_body = (
        partner_in_frame
        or is_intimate
        or any(k in pre_scene_lower for k in SEX_SCENE_KEYWORDS)
        or bool(USER_BODY_PART_RE.search(scene_desc or ""))
    )
    scene_desc = _normalize_second_person_visual_subject(scene_desc, keep_user_body=keep_user_body)
    scene_desc = _strip_non_visual_role_names(service, state, session_id, scene_desc)

    purity = service._get_purity(session_id) if session_id else 1
    safety = service._get_effective_safety(session_id) if session_id else {"tag": None, "level": 1}
    current_style = service._get_current_style(session_id)
    if service._is_character_set(session_id):
        # 兜底：角色态但身体特征被清空（半重置残留）时回退全局 positive_prefix。
        base_char = state.get("custom_positive_prefix", "") or service.config.get("positive_prefix", "")
    else:
        base_char = service._get_session_cfg(session_id, "positive_prefix", "")
    prefix_parts = _split_prompt_prefix(base_char)
    char = prefix_parts.base
    char = inject_appearance(service, char, session_id)
    # 衣柜部件状态只在后面合并完 appearance_override 后应用一次（见 effective_appearance 处）——
    # 重复应用会在已渲染的 "half-removed white camisole" 里再做子串删除，留下裸 "half-removed," 碎片。
    wardrobe_state_worn_src = ""
    wardrobe_state_removed_tags: list[str] = []
    wardrobe_state_exposure_tags: list[str] = []
    scene_desc = _strip_conflicting_scene_appearance(service, state, char, scene_desc)
    scene_desc = _strip_conflicting_scene_light(service, session_id, scene_desc)
    if service._parse_appearance(scene_desc).get("outfit"):
        for old in service._parse_appearance(char).get("outfit", []):
            char = service._remove_tag(char, old)
    # 角色性别先算出来（人数槽与“第二人”检测都要用）。
    persisted_count = (state.get("custom_count") or "").strip() if session_id else ""
    gender_from_count = infer_gender_from_count(persisted_count) if persisted_count else ""
    male = (
        (gender_from_count == "boy")
        or (not gender_from_count and "1boy" in {_tag_key(tag) for tag in _split_tags(prefix_parts.count)})
        or (not gender_from_count and infer_gender_from_prefix(char) == "boy")
    )

    scene_lower = scene_desc.lower()
    sex_keywords = SEX_SCENE_KEYWORDS
    is_ntr_scene = is_ntr or any(k in scene_lower for k in ["ntr", "netorare", "cuckold", "split screen"])
    # 第二人/设备：规划器主判（partner_in_frame / device_in_frame）+ 确定性正则兜底（覆盖无规划器的
    # /自拍、调度推送等路径）。场景里混进与角色性别相反的伴侣，但本该是单人图，是 1girl/solo 与
    # “画面里有第二人”的硬矛盾，最易画出断臂/双人；非 NTR 时按亲密/伴侣场景处理，让伴侣只入局部。
    partner_re = FEMALE_PARTNER_RE if male else MALE_PARTNER_RE
    # 性爱场景明确提到的性器/体液 tag：男性性器被提到意味着伴侣必然在场，并入第二人信号。
    explicit_sex_tags = _explicit_sexual_scene_tags(scene_desc)
    scene_has_partner = (
        partner_in_frame
        or bool(partner_re.search(scene_desc))
        or bool(USER_BODY_PART_RE.search(scene_desc))
        or any(tag in {"penis", "testicles"} for tag in explicit_sex_tags)
    )
    device_present = device_in_frame or bool(DEVICE_SCENE_RE.search(scene_desc))
    scene_has_sex_keyword = any(k in scene_lower for k in sex_keywords)
    is_partner_scene = scene_has_partner
    is_sex_scene = is_intimate or scene_has_sex_keyword or bool(explicit_sex_tags)

    quality = "masterpiece, best quality, highres, absurdres, newest, year 2025, anime coloring, clean lineart, soft cel shading, detailed illustration"
    safety_tag = str(safety.get("tag") or "").strip()
    count = "1boy, solo" if male else "1girl, solo"
    if is_ntr or is_partner_scene:
        count = re.sub(r"\bsolo\b,?\s*", "", count).strip(", ")
    character, series = _visual_character_identity(state)
    artist = current_style if current_style.startswith("@") else ""
    legacy_style = prefix_parts.style
    style_general = current_style if current_style and not current_style.startswith("@") else ""

    neg = service.config.get("negative_prompt", DEFAULT_CONFIG["negative_prompt"])
    neg = _append_negatives(neg, "extra hands", "poorly drawn hands", "extra digits",
                            "split screen", "grid", "multiple panels", "collage",
                            *BOTTOM_EXPOSURE_NEGATIVES)
    if state.get("custom_positive_prefix"):
        strip = {"clothes", "clothing"}
        if male:
            strip |= {"male", "boy", "man", "1boy"}
        kept = []
        for tok in [t.strip() for t in neg.split(",") if t.strip()]:
            low = tok.lower()
            if low in strip:
                continue
            # 角色态下发色由角色 prefix 决定，去掉所有 "<颜色> hair" 守卫（它们本是给默认黑发角色用的）
            hair_color = re.fullmatch(r"(\w+)\s+hair", low)
            if hair_color and hair_color.group(1) in HAIR_COLOR_WORDS:
                continue
            kept.append(tok)
        neg = ", ".join(kept)
    if is_ntr:
        neg = ", ".join(t for t in [x.strip() for x in neg.split(",")] if t.lower() not in {"male", "boy", "man", "1boy"})
    elif not male and "male" not in neg.lower():
        neg += ", male, boy, man"

    prompt_view = (view or "").strip().lower()
    if prompt_view not in VALID_VIEWS:
        prompt_view = _infer_prompt_view(scene_desc)
    # 角色背对相机/在相机背后（从背后环抱面向屏幕的用户等）：POV 看不到她，这类同框场景应走
    # 第三人称双人取景，而非贴身 POV。此处与规划器的几何闸门同源，覆盖无规划器/规划器漏判路径。
    partner_behind = is_partner_scene and not is_ntr_scene and _scene_breaks_pov_facing(scene_desc)
    if is_ntr_scene:
        # NTR 推送：移除 solo、允许伴侣（第三人）完整入画、不强制 POV
        scene_desc = re.sub(r"\bsolo\b,?\s*", "", scene_desc)
        if is_sex_scene:
            # 性爱 NTR：伴侣完整或局部入画，放行 device
            scene_desc += ", third person fully visible in frame, intimate interaction"
            neg = _remove_negatives(
                neg, "male", "boy", "man", "1boy",
                "2girls", "multiple girls", "extra girls",
                "multiple characters", "second body", "duplicate body",
            )
        else:
            # 非性爱 NTR：伴侣入画、移除单人限制
            scene_desc += ", another person visible in frame"
            neg = _remove_negatives(
                neg, "male", "boy", "man", "1boy",
                "2girls", "multiple girls", "extra girls",
                "multiple characters",
            )
        if device_present:
            neg = _remove_negatives(
                neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone",
                "visible phone", "phone in hand", "hand holding phone",
                "mirror", "mirror reflection", "mirror selfie",
                *VISIBLE_PHONE_NEGATIVES,
            )
        else:
            neg = _append_negatives(neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone")
    elif is_sex_scene or is_partner_scene:
        if not device_present:
            # 伴侣/性爱/日常同框画面不能是单人自拍取景：先清掉自拍/对镜的相机取景措辞，
            # 否则会出现“自拍框 + 画面里有第二人”的矛盾（断臂/双人的主因）。
            # 用户明确要拍照/录像（device_present）时跳过：保留其自拍/对镜取景与设备。
            scene_desc = SELF_CAMERA_FRAMING_RE.sub("", scene_desc)
            framing_tags = ["selfie", "holding phone", "arm extended", "mirror selfie", "phone"]
            if is_partner_scene:
                framing_tags.append("solo")
            for tag in framing_tags:
                scene_desc = re.sub(r"\b" + re.escape(tag) + r"\b", "", scene_desc, flags=re.IGNORECASE)
            # 删词可能留下悬空冠词（"A phone and tea set" → "A and tea set"，孤立的 "A" 会被画成文字）：
            # 冠词紧接 and/逗号/句尾时一并清掉。
            scene_desc = re.sub(r"\b(?:a|an|the)\s+and\b", "and", scene_desc, flags=re.IGNORECASE)
            scene_desc = re.sub(r"\b(?:a|an|the)\s+(?=[,.;]|$)", "", scene_desc, flags=re.IGNORECASE)
            scene_desc = re.sub(r"\s*,\s*,+", ", ", scene_desc)
            scene_desc = re.sub(r"\s+([,.;])", r"\1", scene_desc)
            scene_desc = re.sub(r"\s{2,}", " ", scene_desc).strip(" ,")
            if partner_behind:
                # 退回第三人称双人取景：清掉误写的第一人称谎言（连同其后冠词，避免悬空 "of a"），
                # 前置双人主体（角色为焦点，伴侣为完整第二人），不再补 POV 取景。
                scene_desc = FIRST_PERSON_LIE_RE.sub("", scene_desc)
                scene_desc = re.sub(r"\s*,\s*,+", ", ", scene_desc)
                scene_desc = re.sub(r"\s+([,.;])", r"\1", scene_desc)
                scene_desc = re.sub(r"\s{2,}", " ", scene_desc).strip(" ,")
                subjects = "1boy, 1girl" if male else "1girl, 1boy"
                if not re.search(r"\b1boy\b", scene_desc, re.IGNORECASE):
                    scene_desc = f"{subjects}, {scene_desc}".strip(", ")
            # 取景清空后若已无 POV/对视开头，补一个 POV 取景，确保是“贴身视角”而非无主语近景。
            # 外部显式指定 third/portrait/selfie/mirror 时尊重该视角，不再强制 POV。
            elif view not in {"third", "portrait", "selfie", "mirror"} and not re.search(r"first-person pov|looking at a (?:woman|man)", scene_desc, re.IGNORECASE):
                scene_desc = f"{view_opener('pov', 'boy' if male else 'girl')}, {scene_desc}".strip(", ")
            if is_partner_scene:
                scene_desc = re.sub(r"\bsolo\b,?\s*", "", scene_desc)
        else:
            # 设备入画：只有画面里确实有第二身体时才去掉 solo；设备本身不等于伴侣入画。
            if is_partner_scene:
                scene_desc = re.sub(r"\bsolo\b,?\s*", "", scene_desc)
        user_gender = service._get_user_gender(session_id) if session_id and hasattr(service, "_get_user_gender") else "male"
        if not is_partner_scene:
            if is_sex_scene:
                if explicit_sex_tags:
                    scene_desc += ", off-frame partner, no visible second person, character full body in frame"
                else:
                    scene_desc += ", off-frame partner, no visible second person, intimate close-up"
                neg = _append_negatives(neg, "full second person", "second body", "duplicate body", "extra face", "unrelated extra person")
        elif user_gender == "female":
            # 用户是女性（百合/女用户）：只有明确入画时才画女性局部，并放开“双女”负向。
            if is_sex_scene:
                # 不强制 intimate close-up：交合类动作需要角色全身/大半身在画面里，特写会裁掉身体和交合处。
                scene_desc += ", partner's hands or arms visible only as required by the pose, character full body in frame"
            elif partner_behind:
                # 第三人称双人：伴侣是完整第二人（角色在其身后），不压成画面边缘局部。
                scene_desc += ", female partner fully in frame, everyday close interaction"
            else:
                scene_desc += ", partial female hands or feet visible only as required by the pose, everyday close interaction"
            neg = _remove_negatives(neg, "2girls", "multiple girls", "extra girls", "multiple characters", "second body", "duplicate body")
        elif user_gender == "male":
            if is_sex_scene:
                scene_desc += ", partial male body visible, male hands, male torso, character full body in frame"
            elif partner_behind:
                # 第三人称双人：伴侣是完整第二人（角色从背后环抱他），不压成画面边缘局部。
                scene_desc += ", male partner fully in frame, everyday close interaction"
            else:
                partner_part = "partial male hands visible"
                if re.search(r"\b(?:feet|foot)\b|脚", scene_lower):
                    partner_part = "partial male feet visible at the edge of frame"
                elif re.search(r"\b(?:lap|thighs?)\b|腿上|膝上", scene_lower):
                    partner_part = "partial male thighs visible at the edge of frame"
                elif re.search(r"\bshoulder\b|肩", scene_lower):
                    partner_part = "partial male shoulder visible at the edge of frame"
                elif re.search(r"\b(?:chest|collarbone)\b|胸口|锁骨", scene_lower):
                    partner_part = "partial male chest edge visible"
                scene_desc += f", {partner_part}, everyday close interaction"
            neg = _remove_negatives(neg, "male", "boy", "man", "1boy")
        else:
            if is_sex_scene:
                if any(tag in {"penis", "testicles"} for tag in explicit_sex_tags):
                    scene_desc += ", partial male body visible, male torso, male hands, character full body in frame"
                else:
                    scene_desc += ", partner's hands or arms visible only as required by the pose, character full body in frame"
            elif partner_behind:
                scene_desc += ", partner fully in frame, everyday close interaction"
            else:
                scene_desc += ", partner's hands or feet visible only as required by the pose, everyday close interaction"
            neg = _remove_negatives(
                neg,
                "male", "boy", "man", "1boy",
                "2girls", "multiple girls", "extra girls",
                "multiple characters", "second body", "duplicate body",
            )
        # 背对相机的双人第三人称需要完整的第二人，不能压“完整第二人/第三人称”负向。
        if is_partner_scene and not is_sex_scene and not partner_behind:
            neg = _append_negatives(neg, "full second person", "extra face", "unrelated extra person")
        if device_present:
            # 用户要把手机/相机/镜子拍进画面：放开手机与对镜负向，让设备能渲染出来。
            neg = _remove_negatives(
                neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone",
                "visible phone", "phone in hand", "hand holding phone", "mirror", "mirror reflection", "mirror selfie",
                *VISIBLE_PHONE_NEGATIVES,
            )
        elif partner_behind:
            # 退回第三人称双人：压手机/自拍，但绝不压 “third-person perspective”（那正是这里要的机位）。
            neg = _append_negatives(neg, "selfie", "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "arm extended")
        else:
            neg = _append_negatives(neg, "selfie", "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "arm extended", "third-person perspective")
    else:
        has_phone = _contains_any(scene_desc, PHONE_TERMS)
        has_mirror = _contains_any(scene_desc, MIRROR_TERMS)
        if prompt_view == "mirror" or ("mirror selfie" in scene_desc.lower() and has_phone):
            neg = _remove_negatives(neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "visible phone", "phone in hand")
            scene_desc += ", mirror reflection, single reflected body, only mirror reflection is visible, no foreground person"
            neg = _append_negatives(neg, "foreground person", "person outside mirror", "second body", "duplicate body", "multiple reflections", "two phones", "multiple phones")
        elif prompt_view in {"selfie", "pov", "portrait"}:
            scene_desc = _strip_non_mirror_camera_artifacts(scene_desc)
            # selfie 是真·前摄自拍、portrait 是别人帮角色拍的照片：两者画面里都不该出现手机本体或手机 UI。
            # 关键：不再往正向里写 “front-facing phone camera” 之类诱导词（那正是手机 UI 框的来源），
            # 只补“看向镜头”的取景线索，手机/UI 的抑制全部交给下面的负向。
            if prompt_view in {"selfie", "portrait"} and "looking at viewer" not in scene_desc.lower():
                scene_desc += ", looking at viewer"
            neg = _append_negatives(
                neg,
                *VISIBLE_PHONE_NEGATIVES,
                "mirror", "mirror reflection", "mirror selfie",
            )
        elif not has_phone and not has_mirror and not is_ntr_scene:
            neg = _append_negatives(neg, *VISIBLE_PHONE_NEGATIVES)

    # 性爱场景明确提到性器/体液时，补 tag 级正向——自然语言长句里的提及会被生图模型稀释。
    if is_sex_scene and explicit_sex_tags:
        missing_sex_tags = [
            tag for tag in explicit_sex_tags
            if not re.search(rf"\b{re.escape(tag)}\b", scene_desc, re.IGNORECASE)
        ]
        if missing_sex_tags:
            scene_desc += ", " + ", ".join(missing_sex_tags)

    effective = safety.get("level", purity)
    if purity <= 7:
        if effective > 5 and not is_sex_scene:
            neg += ", nsfw, explicit, naked, nude, sex"
    elif purity <= 9:
        neg += ", nsfw, explicit, naked, nude, sex, suggestive, lewd, ecchi, revealing clothes"
    else:
        neg += ", nsfw, explicit, naked, nude, sex, suggestive, lewd, ecchi, cleavage, bikini, lingerie, underwear"

    appearance_override = _explicit_appearance_override(service, state)
    identity = ", ".join(part for part in (character, series) if part)
    effective_appearance = char
    if appearance_override:
        effective_appearance = f"{effective_appearance}, {appearance_override}" if effective_appearance else appearance_override
    if session_id and not ignore_wardrobe_item_states and session_schema.get_wardrobe_item_states(state):
        # 全链路唯一一次部件状态渲染：char + override 合并后的完整文本上应用，
        # 原始标签全部移除（remove_tag 全局替换）再追加一次带前缀标签，不会产生重复或碎片。
        effective_appearance, wardrobe_state_worn_src, wardrobe_state_removed_tags, wardrobe_state_exposure_tags = _apply_wardrobe_item_states(
            service, state, effective_appearance
        )
    one_shot_effective = (one_shot_appearance or "").strip()
    # 当前衣柜标签单独传给公开场合兜底，避免误删角色 base 里的标志性暴露服装/装甲/原皮造型。
    worn_src = wardrobe_state_worn_src or (
        service._effective_dynamic_appearance(session_id) if session_id else session_schema.get_outfit(state)
    )
    current_outfit_tags = _removable_appearance_tags(service, worn_src)
    public_ctx = (
        purity > 2
        and
        _public_render_context(service, state, session_id, scene_desc)
        and not _allows_public_private_outfit(scene_desc, is_intimate=is_sex_scene, clothing_off=clothing_off)
    )
    guarded_outfit_tags = list(current_outfit_tags)
    if public_ctx:
        guarded_outfit_tags.extend(wardrobe_state_exposure_tags)
    if purity > 2:
        effective_appearance, one_shot_effective, neg, _public_outfit_removed = _guard_public_outfit(
            service,
            state,
            session_id,
            scene_desc,
            effective_appearance,
            one_shot_effective,
            neg,
            current_outfit_tags=guarded_outfit_tags,
            is_intimate=is_sex_scene,
            clothing_off=clothing_off,
        )
    # 一次性脱衣/裸露：让规划器的逐图判断剥离本次着装（不落盘），覆盖陈旧持久态。
    # "当前所穿"标签取自生效的 dynamic_appearance + 本次一次性外观，按标签匹配剥离。
    worn_tags = list(current_outfit_tags)
    if one_shot_effective:
        worn_tags += _removable_appearance_tags(service, one_shot_effective)
    effective_appearance, neg = _apply_clothing_off(service, clothing_off, effective_appearance, neg, worn_tags)
    if public_ctx:
        # 公开场景不能因为半脱状态放开裸体负向；被门控剥离的暴露词继续由安全护栏压制。
        neg = _append_negatives(neg, *wardrobe_state_removed_tags, *PUBLIC_EXPOSURE_NEGATIVE_GUARDS)
    else:
        neg = _free_wardrobe_state_exposure_negatives(neg, wardrobe_state_exposure_tags, wardrobe_state_removed_tags)
    slots = PromptSlots(
        raw_scene=raw_scene_desc,
        scene=scene_desc,
        quality=quality,
        count=count,
        identity=identity,
        character=character,
        series=series,
        base_appearance=prefix_parts.base,
        effective_appearance=effective_appearance,
        style_artist=", ".join(part for part in (artist, legacy_style) if part),
        style_general=style_general,
        safety=safety_tag,
        one_shot_appearance=one_shot_effective,
        negative=neg,
        session_id=session_id,
    )
    positive = slots.render_positive()
    if service._parse_appearance(positive).get("outfit"):
        neg = _remove_negatives(neg, "clothes", "clothing")
    neg = _resolve_negative_conflicts(positive, neg)
    slots.negative = neg
    slots.positive = positive
    try:
        service._last_prompt_slots = slots
        _remember_generated_nltag(service, session_id, slots.scene)
        if session_id:
            cache = getattr(service, "_last_prompt_slots_by_session", None)
            if not isinstance(cache, dict):
                cache = {}
            cache[session_id] = slots
            service._last_prompt_slots_by_session = cache
    except Exception:
        logger.debug("failed to store prompt slots", exc_info=True)
    return positive, neg


def _replace_workflow_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    """只替换已解析工作流中的字符串值，绝不对序列化后的 JSON 做文本手术。"""
    if isinstance(value, dict):
        return {key: _replace_workflow_placeholders(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_workflow_placeholders(item, replacements) for item in value]
    if isinstance(value, str):
        rendered = value
        for placeholder, replacement in replacements.items():
            rendered = rendered.replace(placeholder, replacement)
        return rendered
    return value


def build_workflow(service: Any, positive: str, negative: str, seed: int) -> dict[str, Any]:
    wf_file = service.config.get("comfyui_workflow_file", "")
    if wf_file:
        try:
            raw = Path(wf_file).read_text(encoding="utf-8")
            wf = json.loads(raw)
            if not isinstance(wf, dict):
                raise ValueError("工作流根节点必须是 JSON 对象")
            replacements = {
                "{{positive}}": str(positive),
                "{{negative}}": str(negative),
                "{{seed}}": str(seed),
                "{{width}}": str(int(service.config.get("width", "1024"))),
                "{{height}}": str(int(service.config.get("height", "1024"))),
                "{{steps}}": str(int(service.config.get("steps", "30"))),
                "{{cfg}}": str(float(service.config.get("cfg", "4"))),
                "{{sampler}}": str(service.config.get("sampler", "er_sde")),
                "{{scheduler}}": str(service.config.get("scheduler", "simple")),
            }
            return _replace_workflow_placeholders(wf, replacements)
        except Exception as exc:
            raise RuntimeError(f"自定义 ComfyUI 工作流加载失败: {wf_file}: {exc}") from exc
    return build_anima_workflow(service, positive, negative, seed)


def _configured_output_nodes(service: Any) -> set[str] | None:
    raw = service.config.get("comfyui_output_nodes", service.config.get("comfyui_output_node", ""))
    if isinstance(raw, (list, tuple, set)):
        nodes = {str(item).strip() for item in raw if str(item).strip()}
    else:
        nodes = {part.strip() for part in re.split(r"[,;\s]+", str(raw or "")) if part.strip()}
    return nodes or None


def _collect_output_images(outputs: Any, output_nodes: set[str] | None = None) -> list[dict[str, Any]]:
    """从任意 SaveImage/PreviewImage 输出节点收集图片，保持节点与图片原顺序。"""
    if not isinstance(outputs, dict):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id, payload in outputs.items():
        if output_nodes is not None and str(node_id) not in output_nodes:
            continue
        images = payload.get("images") if isinstance(payload, dict) else None
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict) or not str(image.get("filename") or "").strip():
                continue
            key = (
                str(image.get("filename") or ""),
                str(image.get("subfolder") or ""),
                str(image.get("type") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(image)
    return result


def build_anima_workflow(service: Any, positive: str, negative: str, seed: int) -> dict[str, Any]:
    w = int(service.config.get("width", "1024"))
    h = int(service.config.get("height", "1024"))
    steps = int(service.config.get("steps", "30"))
    cfg = float(service.config.get("cfg", "4"))
    sampler = service.config.get("sampler", "er_sde")
    scheduler = service.config.get("scheduler", "simple")
    unet = service.config.get("unet_model", "anima-preview3-base.safetensors")
    clip = service.config.get("clip_model", "qwen_3_06b_base.safetensors")
    vae = service.config.get("vae_model", "qwen_image_vae.safetensors")
    wf = {
        "46": {"inputs": {"filename_prefix": "Anima", "images": ["63", 0]}, "class_type": "SaveImage"},
        "61": {"inputs": {"clip_name": clip, "type": "stable_diffusion", "device": "default"}, "class_type": "CLIPLoader"},
        "62": {"inputs": {"vae_name": vae}, "class_type": "VAELoader"},
        "63": {"inputs": {"samples": ["66", 0], "vae": ["62", 0]}, "class_type": "VAEDecode"},
        "64": {"inputs": {"width": w, "height": h, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "68": {"inputs": {"unet_name": unet, "weight_dtype": "default"}, "class_type": "UNETLoader"},
    }
    model_src, clip_src = ["68", 0], ["61", 0]
    if service.config.get("turbo_mode", False):
        strength = float(service.config.get("turbo_strength", "0.6"))
        wf["69"] = {"inputs": {"model": ["68", 0], "clip": ["61", 0], "lora_name": service.config.get("turbo_lora_model", "anima-turbo-lora-v0.2.safetensors"), "strength_model": strength, "strength_clip": strength}, "class_type": "LoraLoader"}
        model_src, clip_src = ["69", 0], ["69", 1]
    wf["65"] = {"inputs": {"text": negative, "clip": clip_src}, "class_type": "CLIPTextEncode"}
    wf["67"] = {"inputs": {"text": positive, "clip": clip_src}, "class_type": "CLIPTextEncode"}
    wf["66"] = {"inputs": {"seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler, "scheduler": scheduler, "denoise": 1, "model": model_src, "positive": ["67", 0], "negative": ["65", 0], "latent_image": ["64", 0]}, "class_type": "KSampler"}
    return wf


# AnimaTool 画图工作流注册表：每种工作流有独立的 schema / knowledge / generate 端点。
# turbo_v1 / aesthetic_v1 使用新模型共享 knowledge（/anima/knowledge_new_models），
# 且 schema 中包含 neg（反词）字段；turbo0.2 沿用旧 turbo 端点，不支持 neg；
# base 使用通用 Anima 端点，schema 字段最多，支持 neg。
ANIMATOOL_WORKFLOWS: dict[str, dict[str, Any]] = {
    "turbo_v1": {
        "label": "Turbo v1.0",
        "schema_path": "/anima/schema_turbo_v1",
        "knowledge_path": "/anima/knowledge_new_models",
        "generate_path": "/anima/generate_turbo_v1",
        "knowledge_keys": ("new_models_expert", "new_models_examples", "artist_list"),
        "supports_neg": True,
    },
    "aesthetic_v1": {
        "label": "Aesthetic v1.0",
        "schema_path": "/anima/schema_aesthetic_v1",
        "knowledge_path": "/anima/knowledge_new_models",
        "generate_path": "/anima/generate_aesthetic_v1",
        "knowledge_keys": ("new_models_expert", "new_models_examples", "artist_list"),
        "supports_neg": True,
    },
    "turbo0.2": {
        "label": "Turbo v0.2",
        "schema_path": "/anima/schema_turbo",
        "knowledge_path": "/anima/knowledge_turbo",
        "generate_path": "/anima/generate_turbo",
        "knowledge_keys": ("turbo_expert", "turbo_examples", "artist_list"),
        "supports_neg": False,
    },
    "base": {
        "label": "Base (Anima)",
        "schema_path": "/anima/schema",
        "knowledge_path": "/anima/knowledge",
        "generate_path": "/anima/generate",
        "knowledge_keys": ("anima_expert", "prompt_examples", "artist_list"),
        "supports_neg": True,
    },
}
DEFAULT_ANIMATOOL_WORKFLOW = "turbo_v1"


def _get_animatool_workflow(service: Any) -> str:
    """读取并校验当前 AnimaTool 画图工作流配置，非法值回退默认。"""
    raw = str(service.config.get("animatool_workflow", DEFAULT_ANIMATOOL_WORKFLOW) or DEFAULT_ANIMATOOL_WORKFLOW).strip().lower()
    if raw not in ANIMATOOL_WORKFLOWS:
        return DEFAULT_ANIMATOOL_WORKFLOW
    return raw


def _workflow_supports_neg(service: Any) -> bool:
    """当前工作流是否支持反词（neg）字段。"""
    return bool(ANIMATOOL_WORKFLOWS.get(_get_animatool_workflow(service), {}).get("supports_neg"))


def _guard_terms_from_negative(negative: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    allowed = {_tag_key(term) for term in candidates}
    seen: set[str] = set()
    matched: list[str] = []
    for term in _split_tags(negative):
        key = _tag_key(term)
        if key in allowed and key not in seen:
            seen.add(key)
            matched.append(term)
    return tuple(matched)


def _build_animatool_guard_contract(slots: PromptSlots | None) -> AnimaToolGuardContract:
    """从 native 已终裁的 negative 中提取不可被 AnimaTool LLM 删除的子集。"""
    negative = str(slots.negative or "") if isinstance(slots, PromptSlots) else ""
    return AnimaToolGuardContract(
        phone=_guard_terms_from_negative(negative, ANIMATOOL_PHONE_GUARD_TERMS),
        mirror=_guard_terms_from_negative(negative, ANIMATOOL_MIRROR_GUARD_TERMS),
        extra_people=_guard_terms_from_negative(negative, ANIMATOOL_EXTRA_PERSON_GUARD_TERMS),
        panels=_guard_terms_from_negative(negative, ANIMATOOL_PANEL_GUARD_TERMS),
        public_exposure=_guard_terms_from_negative(
            negative,
            (*PUBLIC_EXPOSURE_NEGATIVE_GUARDS, *BOTTOM_EXPOSURE_NEGATIVES, "revealing public outfit"),
        ),
    )


def _append_animatool_nltag_constraint(text: Any, constraint: str) -> str:
    value = str(text or "").strip()
    guard = str(constraint or "").strip()
    if not guard or guard.lower() in value.lower():
        return value
    if value and value[-1] not in ".!?":
        value += "."
    return f"{value} {guard}".strip()


def _apply_animatool_guard_contract(
    payload: dict[str, Any],
    schema: dict[str, Any],
    slots: PromptSlots | None,
    workflow: str,
) -> dict[str, Any]:
    """按实时 schema 映射终裁项；LLM 返回值只能补充，不能覆盖或删除。"""
    result = dict(payload or {})
    guards = _build_animatool_guard_contract(slots)
    negative_terms = guards.negative_terms()
    if not negative_terms:
        return result

    params = schema.get("parameters", {}) if isinstance(schema, dict) else {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    required = set(params.get("required", []) if isinstance(params, dict) else [])
    negative_field = next((field for field in ANIMATOOL_NEGATIVE_FIELDS if field in properties), "")
    if not properties and bool(ANIMATOOL_WORKFLOWS.get(workflow, {}).get("supports_neg")):
        negative_field = next((field for field in ANIMATOOL_NEGATIVE_FIELDS if field in result), "neg")
    if negative_field:
        result[negative_field] = _append_negatives(
            str(result.get(negative_field) or ""),
            *negative_terms,
        )
        for field in ANIMATOOL_NEGATIVE_FIELDS:
            if field != negative_field:
                result.pop(field, None)
        return result

    for field in ANIMATOOL_NEGATIVE_FIELDS:
        result.pop(field, None)

    nltag_field = _preferred_animatool_nltag_field(properties, required) if properties else ""
    if not nltag_field:
        nltag_field = next((field for field in ANIMATOOL_NLTAG_FIELDS if field in result), "")
    if not nltag_field and (not properties or "positive" in properties):
        nltag_field = "positive" if "positive" in result or properties else "tags"
    if nltag_field:
        result[nltag_field] = _append_animatool_nltag_constraint(
            result.get(nltag_field),
            guards.nltag_constraint(),
        )
    return result


# AnimaTool schema 缓存（按 comfyui_url + workflow 分键，避免不同工作流互相覆盖）
_animatool_turbo_schema_cache: dict[str, tuple[dict[str, Any], float]] = {}
_ANIMATOOL_SCHEMA_TTL = 300.0


async def _fetch_animatool_turbo_schema(service: Any, ttl: float = _ANIMATOOL_SCHEMA_TTL, workflow: str | None = None) -> dict[str, Any]:
    """从 AnimaTool 动态获取当前工作流对应接口的 JSON schema，带缓存。"""
    url = str(service.comfyui_url).rstrip("/")
    wf = workflow or _get_animatool_workflow(service)
    cache_key = f"{url}|{wf}"
    now = time.monotonic()
    cached = _animatool_turbo_schema_cache.get(cache_key)
    if cached and (now - cached[1]) < ttl:
        return cached[0]
    schema_path = ANIMATOOL_WORKFLOWS.get(wf, {}).get("schema_path", "/anima/schema_turbo")
    schema: dict[str, Any] = {}
    try:
        ensure_comfy_session(service)
        async with service.comfy_session.get(f"{url}{schema_path}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                schema = await read_limited_json(
                    resp,
                    response_limit(service.config, "comfy_json"),
                    label="AnimaTool schema 响应",
                ) or {}
    except Exception as exc:
        logger.debug("fetch animatool schema (%s) failed: %s", wf, exc)
    _animatool_turbo_schema_cache[cache_key] = (schema, now)
    return schema


def _schema_type_convert(name: str, value: Any, prop: dict[str, Any]) -> Any:
    """按 schema 属性将值转为正确类型。"""
    if value is None:
        return None
    schema_type = prop.get("type")
    if schema_type == "integer":
        try:
            v = int(float(value))
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            if minimum is not None:
                v = max(int(minimum), v)
            if maximum is not None:
                v = min(int(maximum), v)
            return v
        except (TypeError, ValueError):
            return prop.get("default")
    if schema_type == "number":
        try:
            v = float(value)
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            if minimum is not None:
                v = max(float(minimum), v)
            if maximum is not None:
                v = min(float(maximum), v)
            return v
        except (TypeError, ValueError):
            return prop.get("default")
    if schema_type == "string":
        v = str(value)
        enum = prop.get("enum")
        if enum and v not in enum:
            # 不在枚举中时使用默认值
            default = prop.get("default")
            return default if default is not None else v
        return v
    if schema_type == "boolean":
        return bool(value)
    return value


def _animatool_safety_tag(slots: PromptSlots | None) -> str:
    """从 PromptSlots 提取安全等级标签，空值兜底 safe。"""
    if isinstance(slots, PromptSlots):
        for tag in re.split(r"[,\s]+", str(slots.safety or "").strip()):
            if tag.strip().lower() in ("safe", "sensitive", "nsfw", "explicit"):
                return tag.strip().lower()
    return "safe"


def _build_animatool_quality_meta(slots: PromptSlots | None, workflow: str) -> str:
    """按工作流格式构造 quality_meta_year_safe。

    turbo_v1/aesthetic_v1 简化格式：masterpiece, best quality, <safety>
    turbo0.2/base 完整格式：masterpiece, best quality, highres, newest, year 2025, <safety>
    """
    safety = _animatool_safety_tag(slots)
    if workflow in ("turbo_v1", "aesthetic_v1"):
        return f"masterpiece, best quality, {safety}"
    return f"masterpiece, best quality, highres, newest, year 2025, {safety}"


def _build_animatool_neg(slots: PromptSlots | None, workflow: str) -> str:
    """按工作流格式构造 neg 反词，安全等级按四档对齐。"""
    safety = _animatool_safety_tag(slots)
    common_neg = "bad anatomy, bad hands, bad feet, extra fingers, missing fingers, text, watermark, logo"
    base_extra = "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, extra toes"
    if safety == "safe":
        safety_neg = "nsfw, explicit, sensitive, naked, nude, sex"
    elif safety == "sensitive":
        safety_neg = "nsfw, explicit, naked, nude, sex"
    elif safety == "nsfw":
        safety_neg = "safe, sensitive, censored, mosaic"
    else:  # explicit
        safety_neg = "safe, sensitive, censored, mosaic"
    if workflow == "base":
        negative = f"{base_extra}, {common_neg}, {safety_neg}"
    else:
        negative = f"{common_neg}, {safety_neg}"
    return _append_negatives(
        negative,
        *_build_animatool_guard_contract(slots).negative_terms(),
    )


# AnimaTool 采样步数默认值：turbo 工作流 12 步，非 turbo 工作流 40 步。
# 均可在配置 animatool_turbo_steps 中覆盖。
_TURBO_WORKFLOWS = frozenset({"turbo_v1", "turbo0.2"})
_TURBO_DEFAULT_STEPS = 12
_NON_TURBO_DEFAULT_STEPS = 40


def _animatool_steps(service: Any, workflow: str) -> int:
    """返回采样步数，从配置 animatool_turbo_steps 读取。

    turbo_v1 / turbo0.2 默认 12；aesthetic_v1 / base 默认 40。
    配置 animatool_turbo_steps 非空时覆盖所有工作流的默认值。
    """
    default = _TURBO_DEFAULT_STEPS if workflow in _TURBO_WORKFLOWS else _NON_TURBO_DEFAULT_STEPS
    raw = service.config.get("animatool_turbo_steps", "")
    if not raw or not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _sanitize_filename_segment(text: str) -> str:
    """把角色名清理为可安全用于文件名的片段。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    # 去掉括号及括号内内容（如 "shiroko (blue archive)" → "shiroko"）
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", raw).strip()
    # 只保留字母、数字、中文、下划线、连字符，其他字符替换为下划线
    cleaned = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", cleaned, flags=re.UNICODE)
    # 合并连续下划线并去首尾
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned


def _animatool_filename_prefix(service: Any, slots: PromptSlots | None, workflow: str) -> str:
    """构造输出文件名前缀：base_prefix + 角色名。"""
    base = service.config.get("animatool_filename_prefix", "sucyubot_turbo")
    char_name = ""
    session_id = ""
    if isinstance(slots, PromptSlots):
        char_name = slots.character or slots.identity or ""
        session_id = slots.session_id or ""
    if not char_name and session_id:
        # OC 没有视觉 identity 标签：回退到会话内当前角色名，而不是全局默认 bot_name——
        # 否则所有 OC 生成的图片文件名都错变成全局默认角色（如"蕾伊"）。
        if hasattr(service, "_session_role_identity"):
            try:
                _, bot_name, _ = service._session_role_identity(session_id)
                char_name = bot_name or ""
            except Exception:
                char_name = ""
        if not char_name:
            char_name = service._get_session_cfg(session_id, "bot_name", "") or ""
    if not char_name:
        char_name = service.config.get("bot_name", "") or ""
    segment = _sanitize_filename_segment(char_name)
    if segment:
        return f"{base}_{segment}"
    return base


def _build_animatool_turbo_payload(
    service: Any,
    slots: PromptSlots | None,
    positive: str,
    negative: str,
    seed: int,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """根据 AnimaTool schema 字段构建请求体；schema 为空时按原来的字段映射兜底。"""
    workflow = _get_animatool_workflow(service)
    params = schema.get("parameters", {}) if isinstance(schema, dict) else {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    required = set(params.get("required", []) if isinstance(params, dict) else [])

    # 槽位到 schema 候选字段的映射（按优先级）
    # AnimaTool 规范：
    # - tags 是英文自然语言场景描述，对应项目里的 scene；
    # - appearance 是逗号分隔的英文 danbooru 标签，对应 effective_appearance + one_shot_appearance；
    # - positive 字段会覆盖结构化字段，只在 schema 不支持 tags 时才发送。
    # quality_meta_year_safe / neg 不走 slot_candidates——它们按工作流 schema 格式构造，
    # 不直接复制项目内部的 quality/negative 全量标签（含 highres/anime coloring/no panties 等）。
    slot_candidates: dict[str, list[str]] = {
        "count": ["count"],
        "character": ["character"],
        "series": ["series"],
        "style_artist": ["artist"],
        "style_general": ["style"],
        "effective_appearance": ["appearance"],
        "scene": list(ANIMATOOL_NLTAG_FIELDS),
        "one_shot_appearance": ["appearance"],
        "positive": ["positive"],
    }

    payload: dict[str, Any] = {
        "filename_prefix": _animatool_filename_prefix(service, slots, workflow),
        "seed": seed,
        "steps": _animatool_steps(service, workflow),
        "cfg": float(service.config.get("animatool_turbo_cfg", "1.0") or 1.0),
    }

    aspect = _aspect_ratio_from_dimensions(service)
    if aspect:
        payload["aspect_ratio"] = aspect

    if "width" in properties:
        try:
            payload["width"] = int(service.config.get("width", "1024") or 1024)
        except Exception:
            pass
    if "height" in properties:
        try:
            payload["height"] = int(service.config.get("height", "1024") or 1024)
        except Exception:
            pass
    if "batch_size" in properties:
        try:
            payload["batch_size"] = max(1, int(service.config.get("batch_size", "1") or 1))
        except Exception:
            payload["batch_size"] = 1

    if isinstance(slots, PromptSlots):
        # 从槽位填充 schema 支持的字段
        for slot_name, schema_names in slot_candidates.items():
            for field_name in schema_names:
                if field_name not in properties or field_name in payload:
                    continue
                value = getattr(slots, slot_name, None)
                if value in (None, ""):
                    continue
                prop = properties[field_name]
                if field_name in ("character", "series") and not value:
                    continue
                # character/series 为空串时跳过，避免污染 schema
                if field_name in ANIMATOOL_NLTAG_FIELDS and not value:
                    # 自然语言 tags/nltag 必填时，后面兜底
                    continue
                # count 只取人数标签，去掉 solo 等非人数标签
                if field_name == "count":
                    count_tags = [t.strip() for t in re.split(r"[,\s]+", str(value)) if t.strip()]
                    value = next((t for t in count_tags if t.lower() in ("1girl", "2girls", "1boy", "1other")), "")
                    if not value:
                        continue
                payload[field_name] = _schema_type_convert(field_name, value, prop)
        # quality_meta_year_safe：按工作流 schema 格式构造，不复制槽位全量质量标签
        if "quality_meta_year_safe" in properties and "quality_meta_year_safe" not in payload:
            payload["quality_meta_year_safe"] = _build_animatool_quality_meta(slots, workflow)
        # 反词字段完全以实时 schema 为准，注册表只在 schema 不可用时兜底。
        negative_field = next((field for field in ANIMATOOL_NEGATIVE_FIELDS if field in properties), "")
        if negative_field and negative_field not in payload:
            payload[negative_field] = _build_animatool_neg(slots, workflow)
        # 一次性外观补充追加到 appearance（不覆盖有效外貌，只追加）
        one_shot = (getattr(slots, "one_shot_appearance", None) or "").strip()
        if one_shot and "appearance" in properties and "appearance" in payload:
            existing = str(payload["appearance"]).strip()
            if existing:
                combined = f"{existing}, {one_shot}"
            else:
                combined = one_shot
            payload["appearance"] = _schema_type_convert("appearance", combined, properties["appearance"])
    else:
        # 无槽位时，优先把自然语言正面提示词放进 nltag/tags。
        nltag_field = _preferred_animatool_nltag_field(properties, required)
        if nltag_field:
            payload[nltag_field] = positive
        elif "positive" in properties:
            payload["positive"] = positive
        # 无槽位时也按工作流格式构造 quality_meta_year_safe / neg
        if "quality_meta_year_safe" in properties and "quality_meta_year_safe" not in payload:
            payload["quality_meta_year_safe"] = _build_animatool_quality_meta(slots, workflow)
        negative_field = next((field for field in ANIMATOOL_NEGATIVE_FIELDS if field in properties), "")
        if negative_field and negative_field not in payload:
            payload[negative_field] = _build_animatool_neg(slots, workflow)

    # 必填字段兜底
    if "quality_meta_year_safe" in required:
        if "quality_meta_year_safe" not in payload or not payload["quality_meta_year_safe"]:
            payload["quality_meta_year_safe"] = _build_animatool_quality_meta(slots, workflow)
    if "count" in required:
        if "count" not in payload or not payload["count"]:
            payload["count"] = "1girl"
    nltag_field = _preferred_animatool_nltag_field(properties, required)
    if nltag_field in required:
        if nltag_field not in payload or not payload[nltag_field]:
            # tags/nltag 兜底：依次用场景、自然语言正面提示词
            tags_value = (
                getattr(slots, "scene", "")
                or positive
                or ""
            )
            payload[nltag_field] = tags_value

    # 如果 schema 支持自然语言 tags/nltag 且已提供，就不要再发送 positive（positive 会覆盖结构化字段）
    if nltag_field and nltag_field in payload and payload[nltag_field]:
        payload.pop("positive", None)

    payload = _apply_animatool_guard_contract(payload, schema, slots, workflow)

    # 最终按 schema 类型转换并过滤掉 None/空串
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        if v in (None, ""):
            continue
        if k in properties:
            cleaned[k] = _schema_type_convert(k, v, properties[k])
        else:
            cleaned[k] = v
    return cleaned


async def _do_generate_animatool(
    service: Any,
    scene_desc: str,
    session_id: str,
    seed: int,
    orientation: str = "",
) -> tuple[bool, list[bytes], str]:
    """AnimaTool 生图：把槽位交给 LLM 直出 animatool JSON，失败回退旧逻辑。"""
    from .image_planning import plan_animatool_slots

    slots = getattr(service, "_last_prompt_slots", None)

    # 尝试新流程：LLM 直出 animatool JSON
    llm_payload = None
    if isinstance(slots, PromptSlots):
        llm_payload = await plan_animatool_slots(
            service, session_id, slots,
        )

    if llm_payload:
        # 补充固定超参数
        wf = _get_animatool_workflow(service)
        llm_payload["seed"] = seed
        llm_payload["filename_prefix"] = _animatool_filename_prefix(service, slots, wf)
        llm_payload["steps"] = _animatool_steps(service, wf)
        llm_payload["cfg"] = float(service.config.get("animatool_turbo_cfg", "1.0") or 1.0)
        aspect = _aspect_ratio_from_dimensions(service, orientation)
        if aspect:
            llm_payload["aspect_ratio"] = aspect
        # 去掉 schema 不支持的内容字段（超参数保留）
        schema = await _fetch_animatool_turbo_schema(service)
        props = {}
        if isinstance(schema, dict):
            params = schema.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
        if props:
            hyper_keys = {"seed", "filename_prefix", "steps", "cfg", "aspect_ratio", "width", "height", "batch_size"}
            llm_payload = {k: v for k, v in llm_payload.items() if k in props or k in hyper_keys}
        llm_payload = _apply_animatool_guard_contract(llm_payload, schema, slots, wf)
        _remember_generated_nltag(service, session_id, _payload_nltag(llm_payload))
        return await _post_animatool(service, session_id, slots, seed, llm_payload)

    # 回退：旧逻辑
    logger.info("animatool slots LLM failed, falling back to legacy payload builder")
    return await submit_animatool_turbo(service, slots.positive if isinstance(slots, PromptSlots) else "", slots.negative if isinstance(slots, PromptSlots) else "", seed)


async def _post_animatool(
    service: Any,
    session_id: str,
    slots: Any,
    seed: int,
    payload: dict[str, Any],
) -> tuple[bool, list[bytes], str]:
    """POST 当前工作流的 /anima/generate_* 并下载图片。"""
    payload = dict(payload or {})
    wf = _get_animatool_workflow(service)
    generate_path = ANIMATOOL_WORKFLOWS.get(wf, {}).get("generate_path", "/anima/generate_turbo")
    try:
        _remember_generated_nltag(service, session_id, _payload_nltag(payload))
        if hasattr(service, "_ulog") and isinstance(slots, PromptSlots):
            service._ulog(
                session_id,
                "ANIMATOOL_TURBO_PAYLOAD",
                f"seed={seed} workflow={wf} payload={json.dumps(payload, ensure_ascii=False)}",
            )
        async with service.comfy_session.post(f"{service.comfyui_url}{generate_path}", json=payload) as resp:
            data = await read_limited_json(
                resp,
                response_limit(service.config, "comfy_json"),
                label=f"AnimaTool {wf} 生成响应",
            )
            if resp.status >= 400:
                return False, [], f"AnimaTool {wf} failed: {resp.status} {data}"
        images = data.get("images", []) if isinstance(data, dict) else []
        result: list[bytes] = []
        for img in images:
            filename = img.get("filename")
            if not filename:
                continue
            params = {"filename": filename, "type": img.get("type", "output")}
            if img.get("subfolder"):
                params["subfolder"] = img.get("subfolder")
            async with service.comfy_session.get(f"{service.comfyui_url}/view", params=params) as view_resp:
                if view_resp.status == 200:
                    result.append(await read_limited_bytes(
                        view_resp,
                        response_limit(service.config, "generated_image"),
                        label="AnimaTool 图片响应",
                    ))
        if not result:
            return False, [], f"AnimaTool {wf} returned no images: {data}"
        return True, result, ""
    except Exception as exc:
        return False, [], f"AnimaTool {wf} exception: {exc}"


async def submit_animatool_turbo(service: Any, positive: str, negative: str, seed: int) -> tuple[bool, list[bytes], str]:
    slots = getattr(service, "_last_prompt_slots", None)
    wf = _get_animatool_workflow(service)
    generate_path = ANIMATOOL_WORKFLOWS.get(wf, {}).get("generate_path", "/anima/generate_turbo")
    schema = await _fetch_animatool_turbo_schema(service)
    if not schema:
        # schema 获取失败时回退到原来的硬编码字段，但尽量去掉 schema 中不存在的字段
        logger.warning("animatool %s schema not available, falling back to hardcoded fields", wf)
        payload = {
            "filename_prefix": _animatool_filename_prefix(service, slots, wf),
            "seed": seed,
            "steps": _animatool_steps(service, wf),
            "cfg": float(service.config.get("animatool_turbo_cfg", "1.0") or 1.0),
        }
        aspect = _aspect_ratio_from_dimensions(service)
        if aspect:
            payload["aspect_ratio"] = aspect
        if isinstance(slots, PromptSlots):
            appearance = slots.effective_appearance
            one_shot = (slots.one_shot_appearance or "").strip()
            if one_shot and appearance:
                appearance = f"{appearance}, {one_shot}"
            elif one_shot:
                appearance = one_shot
            # count 只取人数标签
            count_tags = [t.strip() for t in re.split(r"[,\s]+", str(slots.count or "")) if t.strip()]
            count_value = next((t for t in count_tags if t.lower() in ("1girl", "2girls", "1boy", "1other")), "1girl")
            payload.update({
                "quality_meta_year_safe": _build_animatool_quality_meta(slots, wf),
                "count": count_value,
                "character": slots.character or slots.identity,
                "series": slots.series,
                "artist": slots.style_artist,
                "appearance": appearance,
                "tags": slots.scene or "",
            })
            # 工作流支持反词时按 schema 格式构造 neg
            if _workflow_supports_neg(service):
                payload["neg"] = _build_animatool_neg(slots, wf)
        else:
            payload["tags"] = positive
            payload["quality_meta_year_safe"] = _build_animatool_quality_meta(slots, wf)
            if _workflow_supports_neg(service):
                payload["neg"] = _build_animatool_neg(slots, wf)
        cleaned = {k: v for k, v in payload.items() if v not in (None, "")}
    else:
        cleaned = _build_animatool_turbo_payload(service, slots, positive, negative, seed, schema)
    cleaned = _apply_animatool_guard_contract(cleaned, schema, slots, wf)
    _remember_generated_nltag(service, getattr(slots, "session_id", "") if isinstance(slots, PromptSlots) else "", _payload_nltag(cleaned))
    try:
        if hasattr(service, "_ulog") and isinstance(slots, PromptSlots):
            service._ulog(
                getattr(slots, "session_id", ""),
                "ANIMATOOL_TURBO_PAYLOAD",
                f"seed={seed} workflow={wf} payload={json.dumps(cleaned, ensure_ascii=False)}",
            )
        async with service.comfy_session.post(f"{service.comfyui_url}{generate_path}", json=cleaned) as resp:
            data = await read_limited_json(
                resp,
                response_limit(service.config, "comfy_json"),
                label=f"AnimaTool {wf} 生成响应",
            )
            if resp.status >= 400:
                return False, [], f"AnimaTool {wf} failed: {resp.status} {data}"
        images = data.get("images", []) if isinstance(data, dict) else []
        result: list[bytes] = []
        for img in images:
            filename = img.get("filename")
            if not filename:
                continue
            params = {"filename": filename, "type": img.get("type", "output")}
            if img.get("subfolder"):
                params["subfolder"] = img.get("subfolder")
            async with service.comfy_session.get(f"{service.comfyui_url}/view", params=params) as view_resp:
                if view_resp.status == 200:
                    result.append(await read_limited_bytes(
                        view_resp,
                        response_limit(service.config, "generated_image"),
                        label="AnimaTool 图片响应",
                    ))
        if not result:
            return False, [], f"AnimaTool {wf} returned no images: {data}"
        return True, result, ""
    except Exception as exc:
        return False, [], f"AnimaTool {wf} exception: {exc}"


def _aspect_ratio_from_dimensions(service: Any, orientation: str = "") -> str:
    """从全局 width/height 推算画幅比例。

    只允许 2:3（竖版）和 3:2（横版），模拟真实相机画幅。
    orientation 可传入 "2:3" 或 "3:2" 直接指定，跳过 width/height 推算。
    """
    if orientation in ("2:3", "3:2"):
        return orientation
    try:
        w = int(service.config.get("width", "832") or 832)
        h = int(service.config.get("height", "1216") or 1216)
    except Exception:
        return "2:3"
    return "3:2" if w > h else "2:3"

def ensure_comfy_session(service: Any):
    if service.comfy_session is None or service.comfy_session.closed:
        service.comfy_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600), trust_env=True)


async def do_generate(
    service: Any,
    scene_desc: str,
    is_ntr: bool = False,
    session_id: str = "",
    one_shot_appearance: str = "",
    is_intimate: bool = False,
    partner_in_frame: bool = False,
    device_in_frame: bool = False,
    clothing_off: str = "",
    orientation: str = "",
    view: str = "",
    ignore_wardrobe_item_states: bool = False,
) -> tuple[bool, list[bytes], str]:
    async with service._gen_lock:
        service._generating = True
        try:
            return await do_generate_locked(
                service, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
                is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
                clothing_off=clothing_off, orientation=orientation, view=view,
                ignore_wardrobe_item_states=ignore_wardrobe_item_states,
            )
        finally:
            service._generating = False


async def do_generate_locked(
    service: Any,
    scene_desc: str,
    is_ntr: bool = False,
    session_id: str = "",
    one_shot_appearance: str = "",
    is_intimate: bool = False,
    partner_in_frame: bool = False,
    device_in_frame: bool = False,
    clothing_off: str = "",
    orientation: str = "",
    view: str = "",
    ignore_wardrobe_item_states: bool = False,
) -> tuple[bool, list[bytes], str]:
    ensure_comfy_session(service)
    positive, negative = build_prompt(
        service, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
        is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
        clothing_off=clothing_off, view=view,
        ignore_wardrobe_item_states=ignore_wardrobe_item_states,
    )
    seed = random.randint(0, 2**63 - 1)
    if session_id and hasattr(service, "_ulog"):
        slots = getattr(service, "_last_prompt_slots", None)
        if isinstance(slots, PromptSlots):
            service._ulog(
                session_id,
                "PROMPT_SLOTS",
                f"seed={seed} {slots.compact()}",
            )
        service._ulog(
            session_id,
            "PROMPT",
            f"seed={seed} scene={scene_desc} positive={positive} negative={negative}",
        )
    if str(service.config.get("image_backend", "native") or "native").lower() == "animatool":
        return await _do_generate_animatool(service, scene_desc, session_id, seed, orientation=orientation)
    workflow = build_workflow(service, positive, negative, seed)
    try:
        async with service.comfy_session.post(f"{service.comfyui_url}/prompt", json={"prompt": workflow}) as resp:
            data = await read_limited_json(
                resp,
                response_limit(service.config, "comfy_json"),
                label="ComfyUI prompt 响应",
            )
        if "prompt_id" not in data:
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(data)
            return False, [], f"ComfyUI submit failed: {msg}"
        prompt_id = data["prompt_id"]
        for _ in range(int(600 / 1.5)):
            await asyncio.sleep(1.5)
            async with service.comfy_session.get(f"{service.comfyui_url}/history/{prompt_id}") as resp:
                history = await read_limited_json(
                    resp,
                    response_limit(service.config, "comfy_json"),
                    label="ComfyUI history 响应",
                )
            if prompt_id not in history:
                continue
            outputs = history[prompt_id].get("outputs", {})
            images = _collect_output_images(outputs, _configured_output_nodes(service))
            if not images:
                continue
            result = []
            for img in images:
                params = {"filename": img["filename"]}
                if img.get("subfolder"):
                    params["subfolder"] = img.get("subfolder")
                async with service.comfy_session.get(f"{service.comfyui_url}/view", params=params) as resp:
                    if resp.status == 200:
                        result.append(await read_limited_bytes(
                            resp,
                            response_limit(service.config, "generated_image"),
                            label="ComfyUI 图片响应",
                        ))
            return True, result, ""
        return False, [], "timeout"
    except Exception as exc:
        return False, [], f"exception: {exc}"
