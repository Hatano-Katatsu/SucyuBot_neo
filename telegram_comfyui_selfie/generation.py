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

from .appearance import infer_gender_from_count, infer_gender_from_prefix, inject_appearance, normalize_appearance_text
from .defaults import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


PHONE_TERMS = ("phone", "smartphone", "cellphone", "mobile phone", "手机")
MIRROR_TERMS = ("mirror", "mirror reflection", "mirror selfie", "镜子", "对镜")
ORIGINAL_SERIES_MARKERS = {"oc", "original", "original character", "原创", "原创角色", "自设", "自创", "原创oc", "无", "none", "-"}
NON_LATIN_IDENTITY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
EMPTY_IDENTITY_MARKERS = {"", "unknown", "none", "n/a", "na", "null", "-"}
VISIBLE_PHONE_NEGATIVES = (
    "holding phone", "visible phone", "phone in hand", "hand holding phone",
    "phone visible in frame", "visible smartphone", "smartphone in hand",
    "camera frame", "phone interface", "viewfinder", "screen border", "UI overlay",
    "camera UI", "phone screen", "selfie frame", "record button", "shutter button",
)


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
    one_shot_appearance: str = ""
    negative: str = ""
    positive: str = ""

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
            self.scene,
            self.one_shot_appearance,
        ]
        return _dedupe_prompt_modules(modules)

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
    dynamic_slots = service._parse_appearance(state.get("dynamic_appearance", "") or "")
    for key in ("hair", "eyes", "outfit", "accessory", "other"):
        parts.extend(dynamic_slots.get(key, []))
    for key in ("custom_default_hair", "custom_default_eyes"):
        raw = (state.get(key) or "").strip()
        if raw:
            parts.append(raw)
    return normalize_appearance_text(", ".join(parts))


def _explicit_hair_override(service: Any, state: dict[str, Any], char: str = "") -> list[str]:
    dynamic_hair = service._parse_appearance(state.get("dynamic_appearance", "") or "").get("hair", [])
    if dynamic_hair:
        return dynamic_hair
    custom_hair = (state.get("custom_default_hair") or "").strip()
    if custom_hair:
        return service._parse_appearance(custom_hair).get("hair", [])
    if char:
        return service._parse_appearance(char).get("hair", [])
    return []


def _explicit_eye_override(service: Any, state: dict[str, Any], char: str = "") -> list[str]:
    dynamic_eyes = service._parse_appearance(state.get("dynamic_appearance", "") or "").get("eyes", [])
    if dynamic_eyes:
        return dynamic_eyes
    custom_eyes = (state.get("custom_default_eyes") or "").strip()
    if custom_eyes:
        return service._parse_appearance(custom_eyes).get("eyes", [])
    if char:
        return service._parse_appearance(char).get("eyes", [])
    return []


def _explicit_outfit_override(service: Any, state: dict[str, Any]) -> list[str]:
    return service._parse_appearance(state.get("dynamic_appearance", "") or "").get("outfit", [])


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
    patterns = [
        rf"\b(?:wears?|wearing|dressed\s+in)\s+[^,.;]*(?:{outfit_alt})[^,.;]*",
        rf"\b(?:black|white|blue|red|pink|purple|green|yellow|brown|gray|grey|dark|light)\s+[^,.;]*(?:{outfit_alt})[^,.;]*",
    ]
    text = scene_desc
    for pattern in patterns:
        text = re.sub(pattern, "wearing the current outfit", text, flags=re.IGNORECASE)
    text = re.sub(r"(wearing the current outfit)(?:\s*,\s*\1)+", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    return text


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
        r"\b(?:while\s+)?(?:the\s+)?(?:other|another|one)\s+(?:hand\s+)?(?:is\s+)?(?:idly\s+|casually\s+)?(?:holds?|holding|grips?|gripping|checks?|checking|scrolls?\s+through|scrolling\s+through|plays?\s+with|using)\s+(?:a\s+|an\s+|one\s+|her\s+|his\s+|the\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:one|another|the other)\s+hand\s+(?:is\s+)?(?:on|near|around)\s+(?:a\s+|one\s+|her\s+|his\s+|the\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\bholding\s+(?:a\s+|one\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:smartphone|phone|cellphone|mobile phone)\s+in\s+(?:her\s+)?hand\b",
        r"\bvisible\s+(?:smartphone|phone|cellphone|mobile phone)\b",
        r"\b(?:smartphone|phone|cellphone|mobile phone)\s+screen\b",
        r"\b(?:message|chat)\s+interface(?:\s+countdown\s+prompt)?\b",
        r"\bcountdown\s+prompt\b",
        r"\bcountdown\b",
        r"\bwith\s+(?:a\s+|one\s+|her\s+)?(?:smartphone|phone|cellphone|mobile phone)\b",
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
    text = re.sub(r"\s*,\s*,+", ", ", text)
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
# 设备入画（用户要求拍照/录像）的英文兜底：scene 到这步已译成英文。命中则放行手机/相机，不抹设备。
# 只匹配【无歧义的拍摄意图】词；故意不含 "holding a phone/smartphone"——那既可能是误泄漏的手机
# （应被清掉），区分不了意图。真正的拍摄意图主要由规划器 device_in_frame 与中文关键词（基于用户原话）给出。
DEVICE_SCENE_RE = re.compile(
    r"\b(?:recording|filming|sex tape|on camera|camcorder|video camera)\b",
    re.IGNORECASE,
)
# 自拍/对镜取景措辞：伴侣同框时必须清掉，避免“自拍框 + 画面里有第二人”自相矛盾。
SELF_CAMERA_FRAMING_RE = re.compile(
    r"\bA front-camera selfie of a (?:woman|man|girl|boy)\b|"
    r"\bA selfie of a (?:woman|man|girl|boy)\b|"
    r"\bA mirror reflection of a (?:woman|man|girl|boy)\b|"
    r"\bshot by an off-frame front-facing phone camera\b|"
    r"\bno visible phone\b|"
    r"\bonly mirror reflection is visible\b|"
    r"\bno foreground person\b|"
    r"\bsingle reflected body\b",
    re.IGNORECASE,
)


def _normalize_second_person_visual_subject(scene_desc: str) -> str:
    text = (scene_desc or "").strip()
    if not text:
        return text

    text = SECOND_PERSON_VISUAL_SUBJECT_RE.sub(lambda m: f"{m.group('prefix')}The character", text, count=1)
    text = SECOND_PERSON_SUBJECT_ACTION_RE.sub("the character ", text)
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
                state.get("dynamic_appearance") or "",
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
        "selfie": f"A front-camera selfie of a {subj}, solo, upper body framing, looking at viewer, shot by an off-frame front-facing phone camera, no visible phone",
        "mirror": f"A mirror reflection of a {subj}, solo, single reflected body, only mirror reflection is visible, no foreground person, holding one smartphone with one hand, looking at viewer through the mirror",
        "pov": f"First-person POV, looking at a {subj}, solo, eye contact with the viewer",
        "third": f"{count}, solo",
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
) -> tuple[str, str]:
    raw_scene_desc = scene_desc
    state = service._get_session_state(session_id) if session_id else {}
    scene_desc = _normalize_second_person_visual_subject(scene_desc)
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
    sex_keywords = [
        "sex", "make love", "penetration", "penetrating", "vaginal", "missionary", "doggystyle",
        "cowgirl", "girl on top", "straddling", "straddle", "riding", "grinding", "thrust",
        "thrusting", "squelch", "impaled", "insertion", "humping", "creampie", "naked together",
    ]
    is_ntr_scene = is_ntr or any(k in scene_lower for k in ["ntr", "netorare", "cuckold", "split screen"])
    # 第二人/设备：规划器主判（partner_in_frame / device_in_frame）+ 确定性正则兜底（覆盖无规划器的
    # /自拍、调度推送等路径）。场景里混进与角色性别相反的伴侣，但本该是单人图，是 1girl/solo 与
    # “画面里有第二人”的硬矛盾，最易画出断臂/双人；非 NTR 时按亲密/伴侣场景处理，让伴侣只入局部。
    partner_re = FEMALE_PARTNER_RE if male else MALE_PARTNER_RE
    scene_has_partner = partner_in_frame or bool(partner_re.search(scene_desc))
    device_present = device_in_frame or bool(DEVICE_SCENE_RE.search(scene_desc))
    is_sex_scene = (
        is_intimate
        or any(k in scene_lower for k in sex_keywords)
        or (scene_has_partner and not is_ntr_scene)
    )

    quality = "masterpiece, best quality, absurdres, score_9, score_8, anime coloring, clean lineart, soft cel shading, detailed illustration"
    if safety.get("tag"):
        quality += f", {safety['tag']}"
    count = "1boy, solo" if male else "1girl, solo"
    if is_ntr or is_sex_scene:
        count = re.sub(r"\bsolo\b,?\s*", "", count).strip(", ")
    character, series = _visual_character_identity(state)
    artist = current_style if current_style.startswith("@") else ""
    legacy_style = prefix_parts.style
    style_general = current_style if current_style and not current_style.startswith("@") else ""

    neg = service.config.get("negative_prompt", DEFAULT_CONFIG["negative_prompt"])
    neg = _append_negatives(
        neg,
        "extra hands", "three hands", "three arms", "extra arms", "duplicate hands", "duplicate arms",
        "malformed hands", "poorly drawn hands", "extra digits", "duplicated limbs",
    )
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
    if "2girls" not in neg.lower():
        neg += ", 2girls, multiple girls, extra girls"
    if is_ntr:
        neg = ", ".join(t for t in [x.strip() for x in neg.split(",")] if t.lower() not in {"male", "boy", "man", "1boy"})
    elif not male and "male" not in neg.lower():
        neg += ", male, boy, man"

    prompt_view = _infer_prompt_view(scene_desc)
    if is_sex_scene and not is_ntr_scene:
        if not device_present:
            # 伴侣/性爱/事后贴身画面不能是单人自拍取景：先清掉自拍/对镜的相机取景措辞，
            # 否则会出现“自拍框 + 画面里有第二人”的矛盾（断臂/双人的主因）。
            # 用户明确要拍照/录像（device_present）时跳过：保留其自拍/对镜取景与设备。
            scene_desc = SELF_CAMERA_FRAMING_RE.sub("", scene_desc)
            for tag in ["selfie", "solo", "holding phone", "arm extended", "mirror selfie", "phone"]:
                scene_desc = re.sub(r"\b" + re.escape(tag) + r"\b", "", scene_desc, flags=re.IGNORECASE)
            scene_desc = re.sub(r"\s*,\s*,+", ", ", scene_desc)
            scene_desc = re.sub(r"\s+([,.;])", r"\1", scene_desc)
            scene_desc = re.sub(r"\s{2,}", " ", scene_desc).strip(" ,")
            # 取景清空后若已无 POV/对视开头，补一个 POV 取景，确保是“贴身视角”而非无主语近景。
            if not re.search(r"first-person pov|looking at a (?:woman|man)", scene_desc, re.IGNORECASE):
                scene_desc = f"{view_opener('pov', 'boy' if male else 'girl')}, {scene_desc}".strip(", ")
                scene_desc = re.sub(r"\bsolo\b,?\s*", "", scene_desc)
        else:
            # 设备入画：仍要去掉 solo（画面里有两个身体），但保留自拍/对镜取景与设备。
            scene_desc = re.sub(r"\bsolo\b,?\s*", "", scene_desc)
        user_gender = service._get_user_gender(session_id) if session_id and hasattr(service, "_get_user_gender") else "male"
        if user_gender == "female":
            # 用户是女性（百合/女用户）：伴侣画成女性局部，放开“双女”负向，但仍保留 male 负向。
            scene_desc += ", partner's hands, partner's arms, intimate close-up"
            neg = _remove_negatives(neg, "2girls", "multiple girls", "extra girls", "multiple characters", "second body", "duplicate body")
        else:
            scene_desc += ", partial male body visible, male hands, male torso, intimate close-up"
            neg = _remove_negatives(neg, "male")
        if device_present:
            # 用户要把手机/相机/镜子拍进画面：放开手机与对镜负向，让设备能渲染出来。
            neg = _remove_negatives(
                neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone",
                "visible phone", "phone in hand", "hand holding phone", "mirror", "mirror reflection", "mirror selfie",
                *VISIBLE_PHONE_NEGATIVES,
            )
        else:
            neg = _append_negatives(neg, "selfie", "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "arm extended", "third-person perspective")
    else:
        has_phone = _contains_any(scene_desc, PHONE_TERMS)
        has_mirror = _contains_any(scene_desc, MIRROR_TERMS)
        if prompt_view == "mirror" or ("mirror selfie" in scene_desc.lower() and has_phone):
            neg = _remove_negatives(neg, "holding phone", "phone", "cellphone", "mobile phone", "smartphone", "visible phone", "phone in hand")
            scene_desc += ", mirror reflection, single reflected body, only mirror reflection is visible, no foreground person"
            neg = _append_negatives(neg, "foreground person", "person outside mirror", "second body", "duplicate body", "multiple reflections", "two phones", "multiple phones")
        elif prompt_view in {"selfie", "pov"}:
            scene_desc = _strip_non_mirror_camera_artifacts(scene_desc)
            if prompt_view == "selfie" and "off-frame front-facing phone camera" not in scene_desc.lower():
                scene_desc += ", shot by an off-frame front-facing phone camera, no visible phone"
            neg = _append_negatives(
                neg,
                *VISIBLE_PHONE_NEGATIVES,
                "mirror", "mirror reflection", "mirror selfie",
            )
        elif not has_phone and not has_mirror and not is_ntr_scene:
            neg = _append_negatives(neg, *VISIBLE_PHONE_NEGATIVES)

    effective = safety.get("level", purity)
    if purity <= 2:
        neg = ", ".join(t for t in [x.strip() for x in neg.split(",")] if t.lower() not in {"child", "loli", "censor bar", "mosaic", "pixelated"})
    elif purity <= 7:
        if effective > 5:
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
        one_shot_appearance=(one_shot_appearance or "").strip(),
        negative=neg,
    )
    positive = slots.render_positive()
    if service._parse_appearance(positive).get("outfit"):
        neg = _remove_negatives(neg, "clothes", "clothing")
    neg = _resolve_negative_conflicts(positive, neg)
    slots.negative = neg
    slots.positive = positive
    try:
        service._last_prompt_slots = slots
        if session_id:
            cache = getattr(service, "_last_prompt_slots_by_session", None)
            if not isinstance(cache, dict):
                cache = {}
            cache[session_id] = slots
            service._last_prompt_slots_by_session = cache
    except Exception:
        logger.debug("failed to store prompt slots", exc_info=True)
    return positive, neg


def build_workflow(service: Any, positive: str, negative: str, seed: int) -> dict[str, Any]:
    wf_file = service.config.get("comfyui_workflow_file", "")
    if wf_file:
        try:
            raw = Path(wf_file).read_text(encoding="utf-8")
            wf = json.loads(raw)
            replacements = {
                "{{positive}}": positive,
                "{{negative}}": negative,
                "{{seed}}": str(seed),
                "{{width}}": str(int(service.config.get("width", "1024"))),
                "{{height}}": str(int(service.config.get("height", "1024"))),
                "{{steps}}": str(int(service.config.get("steps", "30"))),
                "{{cfg}}": str(float(service.config.get("cfg", "4"))),
                "{{sampler}}": service.config.get("sampler", "er_sde"),
                "{{scheduler}}": service.config.get("scheduler", "simple"),
            }
            wf_text = json.dumps(wf)
            for old, new in replacements.items():
                wf_text = wf_text.replace(old, new)
            return json.loads(wf_text)
        except Exception as exc:
            logger.error("自定义工作流加载失败，回退内置工作流: %s", exc)
    return build_anima_workflow(service, positive, negative, seed)


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


# AnimaTool turbo schema 缓存（按 comfyui_url 分键，避免多个服务互相覆盖）
_animatool_turbo_schema_cache: dict[str, tuple[dict[str, Any], float]] = {}
_ANIMATOOL_SCHEMA_TTL = 300.0


async def _fetch_animatool_turbo_schema(service: Any, ttl: float = _ANIMATOOL_SCHEMA_TTL) -> dict[str, Any]:
    """从 AnimaTool 动态获取 turbo 接口的 JSON schema，带缓存。"""
    url = str(service.comfyui_url).rstrip("/")
    now = time.monotonic()
    cached = _animatool_turbo_schema_cache.get(url)
    if cached and (now - cached[1]) < ttl:
        return cached[0]
    schema: dict[str, Any] = {}
    try:
        ensure_comfy_session(service)
        async with service.comfy_session.get(f"{url}/anima/schema_turbo", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                schema = await resp.json(content_type=None) or {}
    except Exception as exc:
        logger.debug("fetch animatool turbo schema failed: %s", exc)
    _animatool_turbo_schema_cache[url] = (schema, now)
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


def _build_animatool_turbo_payload(
    service: Any,
    slots: PromptSlots | None,
    positive: str,
    negative: str,
    seed: int,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """根据 AnimaTool turbo schema 字段构建请求体；schema 为空时按原来的字段映射兜底。"""
    params = schema.get("parameters", {}) if isinstance(schema, dict) else {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    required = set(params.get("required", []) if isinstance(params, dict) else [])

    # 槽位到 schema 候选字段的映射（按优先级）
    # AnimaTool turbo 规范：
    # - tags 是英文自然语言场景描述，对应项目里的 scene；
    # - appearance 是逗号分隔的英文 danbooru 标签，对应 effective_appearance + one_shot_appearance；
    # - positive 字段会覆盖结构化字段，只在 schema 不支持 tags 时才发送。
    slot_candidates: dict[str, list[str]] = {
        "quality": ["quality_meta_year_safe"],
        "count": ["count"],
        "character": ["character"],
        "series": ["series"],
        "style_artist": ["artist"],
        "style_general": ["style"],
        "effective_appearance": ["appearance"],
        "scene": ["tags"],
        "one_shot_appearance": ["appearance"],
        "positive": ["positive"],
        "negative": ["neg", "negative"],
    }

    payload: dict[str, Any] = {
        "filename_prefix": service.config.get("animatool_filename_prefix", "sucyubot_turbo"),
        "seed": seed,
        "steps": int(float(service.config.get("animatool_turbo_steps", "10") or 10)),
        "cfg": float(service.config.get("animatool_turbo_cfg", "1.0") or 1.0),
    }

    aspect = _aspect_ratio_from_dimensions(service)
    if aspect:
        payload["aspect_ratio"] = aspect

    if "neg" in properties or "negative" in properties:
        neg_field = "neg" if "neg" in properties else "negative"
        payload[neg_field] = negative
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
                if field_name == "tags" and not value:
                    # tags 必填，后面兜底
                    continue
                payload[field_name] = _schema_type_convert(field_name, value, prop)
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
        # 无槽位时，若 schema 支持 positive 就放正面提示词
        if "positive" in properties:
            payload["positive"] = positive

    # 必填字段兜底
    if "quality_meta_year_safe" in required:
        if "quality_meta_year_safe" not in payload or not payload["quality_meta_year_safe"]:
            payload["quality_meta_year_safe"] = (
                getattr(slots, "quality", "") or "masterpiece, best quality, highres, newest, year 2025, safe"
            )
    if "count" in required:
        if "count" not in payload or not payload["count"]:
            payload["count"] = getattr(slots, "count", "") or "1girl"
    if "tags" in required:
        if "tags" not in payload or not payload["tags"]:
            # tags 兜底：依次用场景、自然语言正面提示词
            tags_value = (
                getattr(slots, "scene", "")
                or positive
                or ""
            )
            payload["tags"] = tags_value

    # 如果 schema 支持 tags 且我们已经提供了结构化 tags，就不要再发送 positive（positive 会覆盖结构化字段）
    if "tags" in properties and "tags" in payload and payload["tags"]:
        payload.pop("positive", None)

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
) -> tuple[bool, list[bytes], str]:
    """AnimaTool 生图：把槽位交给 LLM 直出 animatool JSON，失败回退旧逻辑。"""
    from .image_planning import plan_animatool_slots

    slots = getattr(service, "_last_prompt_slots", None)
    intent = scene_desc or ""

    # 尝试新流程：LLM 直出 animatool JSON
    llm_payload = None
    if isinstance(slots, PromptSlots):
        llm_payload = await plan_animatool_slots(
            service, session_id, slots, intent=intent,
        )

    if llm_payload:
        # 补充固定超参数
        llm_payload["seed"] = seed
        llm_payload["filename_prefix"] = service.config.get("animatool_filename_prefix", "sucyubot_turbo")
        llm_payload["steps"] = int(float(service.config.get("animatool_turbo_steps", "10") or 10))
        llm_payload["cfg"] = float(service.config.get("animatool_turbo_cfg", "1.0") or 1.0)
        aspect = _aspect_ratio_from_dimensions(service)
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
    """POST /anima/generate_turbo 并下载图片。"""
    try:
        if hasattr(service, "_ulog") and isinstance(slots, PromptSlots):
            service._ulog(
                session_id,
                "ANIMATOOL_TURBO_PAYLOAD",
                f"seed={seed} payload={json.dumps(payload, ensure_ascii=False)}",
            )
        async with service.comfy_session.post(f"{service.comfyui_url}/anima/generate_turbo", json=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                return False, [], f"AnimaTool turbo failed: {resp.status} {data}"
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
                    result.append(await view_resp.read())
        if not result:
            return False, [], f"AnimaTool turbo returned no images: {data}"
        return True, result, ""
    except Exception as exc:
        return False, [], f"AnimaTool turbo exception: {exc}"


async def submit_animatool_turbo(service: Any, positive: str, negative: str, seed: int) -> tuple[bool, list[bytes], str]:
    slots = getattr(service, "_last_prompt_slots", None)
    schema = await _fetch_animatool_turbo_schema(service)
    if not schema:
        # schema 获取失败时回退到原来的硬编码字段，但尽量去掉 schema 中不存在的字段
        logger.warning("animatool turbo schema not available, falling back to hardcoded fields")
        payload = {
            "filename_prefix": service.config.get("animatool_filename_prefix", "sucyubot_turbo"),
            "seed": seed,
            "steps": int(float(service.config.get("animatool_turbo_steps", "10") or 10)),
            "cfg": float(service.config.get("animatool_turbo_cfg", "1.0") or 1.0),
            "neg": negative,
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
            payload.update({
                "quality_meta_year_safe": slots.quality,
                "count": slots.count,
                "character": slots.character or slots.identity,
                "series": slots.series,
                "artist": slots.style_artist,
                "appearance": appearance,
                "tags": slots.scene or "",
            })
        else:
            payload["positive"] = positive
        cleaned = {k: v for k, v in payload.items() if v not in (None, "")}
    else:
        cleaned = _build_animatool_turbo_payload(service, slots, positive, negative, seed, schema)
    try:
        if hasattr(service, "_ulog") and isinstance(slots, PromptSlots):
            service._ulog(
                getattr(slots, "session_id", ""),
                "ANIMATOOL_TURBO_PAYLOAD",
                f"seed={seed} payload={json.dumps(cleaned, ensure_ascii=False)}",
            )
        async with service.comfy_session.post(f"{service.comfyui_url}/anima/generate_turbo", json=cleaned) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                return False, [], f"AnimaTool turbo failed: {resp.status} {data}"
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
                    result.append(await view_resp.read())
        if not result:
            return False, [], f"AnimaTool turbo returned no images: {data}"
        return True, result, ""
    except Exception as exc:
        return False, [], f"AnimaTool turbo exception: {exc}"


def _aspect_ratio_from_dimensions(service: Any) -> str:
    """从全局 width/height 推算最接近的 AnimaTool aspect_ratio。

    AnimaTool turbo 模式按 aspect_ratio 自动推算宽高（约 1MP），不接受 width/height。
    这里把项目的 width/height 映射到 schema 支持的比例字符串。
    """
    try:
        w = int(service.config.get("width", "1024") or 1024)
        h = int(service.config.get("height", "1024") or 1024)
    except Exception:
        return "1:1"
    ratio = w / h if h else 1.0
    # ref/app.py 支持的比例及其数值
    candidates = [
        ("21:9", 21 / 9), ("2:1", 2.0), ("16:9", 16 / 9), ("16:10", 16 / 10),
        ("5:3", 5 / 3), ("3:2", 3 / 2), ("4:3", 4 / 3), ("1:1", 1.0),
        ("3:4", 3 / 4), ("2:3", 2 / 3), ("3:5", 3 / 5), ("10:16", 10 / 16),
        ("9:16", 9 / 16), ("1:2", 1 / 2), ("9:21", 9 / 21),
    ]
    best = min(candidates, key=lambda item: abs(item[1] - ratio))
    return best[0]

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
) -> tuple[bool, list[bytes], str]:
    async with service._gen_lock:
        service._generating = True
        try:
            return await do_generate_locked(
                service, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
                is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
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
) -> tuple[bool, list[bytes], str]:
    ensure_comfy_session(service)
    positive, negative = build_prompt(
        service, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
        is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
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
        return await _do_generate_animatool(service, scene_desc, session_id, seed)
    workflow = build_workflow(service, positive, negative, seed)
    try:
        async with service.comfy_session.post(f"{service.comfyui_url}/prompt", json={"prompt": workflow}) as resp:
            data = await resp.json()
        if "prompt_id" not in data:
            err = data.get("error", {})
            msg = err.get("message", str(data)) if isinstance(err, dict) else str(data)
            return False, [], f"ComfyUI submit failed: {msg}"
        prompt_id = data["prompt_id"]
        for _ in range(int(600 / 1.5)):
            await asyncio.sleep(1.5)
            async with service.comfy_session.get(f"{service.comfyui_url}/history/{prompt_id}") as resp:
                history = await resp.json()
            if prompt_id not in history:
                continue
            outputs = history[prompt_id].get("outputs", {})
            images = outputs.get("46", {}).get("images", [])
            if not images:
                continue
            result = []
            for img in images:
                params = {"filename": img["filename"]}
                if img.get("subfolder"):
                    params["subfolder"] = img.get("subfolder")
                async with service.comfy_session.get(f"{service.comfyui_url}/view", params=params) as resp:
                    if resp.status == 200:
                        result.append(await resp.read())
            return True, result, ""
        return False, [], "timeout"
    except Exception as exc:
        return False, [], f"exception: {exc}"
