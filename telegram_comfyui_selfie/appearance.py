from __future__ import annotations

import re
from typing import Any

from .defaults import DEFAULT_CONFIG


def load_keywords(config: dict[str, Any], key: str, defaults: list[str]) -> list[str]:
    raw = config.get(key, "")
    if not raw:
        return list(defaults)
    return [x.strip() for x in str(raw).replace(";", "\n").splitlines() if x.strip()]


def outfit_keywords(config: dict[str, Any]) -> list[str]:
    return load_keywords(config, "outfit_keywords", DEFAULT_CONFIG["outfit_keywords"].splitlines())


def accessory_keywords(config: dict[str, Any]) -> list[str]:
    return load_keywords(config, "accessory_keywords", DEFAULT_CONFIG["accessory_keywords"].splitlines())


HAIRSTYLE_WORDS = (
    "braid", "ponytail", "twintail", "twin tail", "pigtail", "bun", "bangs", "ahoge",
    "drill", "sidetail", "side tail", "hime cut", "updo", "bob cut", "马尾", "辫",
)


def normalize_appearance_tag(tag: str) -> str:
    text = str(tag or "").strip()
    if not text:
        return ""
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip(" ,")
    if text.lower() == "bun":
        return "hair bun"
    return text


def normalize_appearance_text(appearance: str) -> str:
    tags = [normalize_appearance_tag(t) for t in str(appearance or "").split(",")]
    return ", ".join(t for t in tags if t)


def parse_appearance(appearance: str, outfit_kw: list[str], accessory_kw: list[str]) -> dict[str, list[str]]:
    tags = [normalize_appearance_tag(t) for t in appearance.split(",") if t.strip()]
    slots = {"hair": [], "eyes": [], "outfit": [], "accessory": [], "other": []}
    for tag in tags:
        if not tag:
            continue
        tl = tag.lower()
        if "hair" in tl or "发" in tl or any(h in tl for h in HAIRSTYLE_WORDS):
            slots["hair"].append(tag)
        elif "eye" in tl or "pupil" in tl or "瞳" in tl or "眼" in tl:
            slots["eyes"].append(tag)
        elif any(k in tl for k in outfit_kw):
            slots["outfit"].append(tag)
        elif any(k in tl for k in accessory_kw):
            slots["accessory"].append(tag)
        else:
            slots["other"].append(tag)
    return slots


def slots_to_string(slots: dict[str, list[str]]) -> str:
    parts = []
    for key in ("hair", "eyes", "outfit", "accessory", "other"):
        if slots[key]:
            parts.append(", ".join(slots[key]))
    return ", ".join(parts)


def remove_tag(text: str, tag: str) -> str:
    if not tag:
        return text
    text = text.replace(tag, "")
    text = re.sub(r",\s*,", ",", text)
    return text.strip(", ").strip()


def merge_appearance(current_tags: str, new_tags: str, outfit_kw: list[str], accessory_kw: list[str], mode: str = "merge") -> str:
    current_tags = normalize_appearance_text(current_tags)
    new_tags = normalize_appearance_text(new_tags)
    if not current_tags or mode == "replace":
        return new_tags
    if not new_tags:
        return current_tags
    cur = parse_appearance(current_tags, outfit_kw, accessory_kw)
    new = parse_appearance(new_tags, outfit_kw, accessory_kw)
    merged = {}
    for key in ("hair", "eyes", "outfit", "other"):
        merged[key] = new[key] if new[key] else cur[key]
    merged["accessory"] = []
    seen = set()
    for src in (cur["accessory"], new["accessory"]):
        for tag in src:
            if tag.lower() not in seen:
                merged["accessory"].append(tag)
                seen.add(tag.lower())
    return slots_to_string(merged)


def inject_appearance(service: Any, char: str, session_id: str = "") -> str:
    if not session_id:
        return char
    state = service._get_session_state(session_id)
    outfit_kw = service._outfit_kw
    accessory_kw = service._accessory_kw
    slots = parse_appearance(state.get("dynamic_appearance", "") or "", outfit_kw, accessory_kw)
    char_set = service._is_character_set(session_id)

    def resolve(slot, custom_key, global_key, default):
        if slots[slot]:
            return ", ".join(slots[slot]), True
        custom = (state.get(custom_key, "") or "").strip()
        if custom:
            return custom, True
        if not char_set:
            return (service.config.get(global_key, default) or "").strip(), False
        return "", False

    for slot, ckey, gkey, default in (
        ("hair", "custom_default_hair", "default_hair", "black long flowing hair"),
        ("eyes", "custom_default_eyes", "default_eyes", "purple eyes"),
    ):
        new_tags, override = resolve(slot, ckey, gkey, default)
        new_tags = normalize_appearance_text(new_tags)
        if not new_tags:
            continue
        if override:
            for old in parse_appearance(char, outfit_kw, accessory_kw)[slot]:
                char = remove_tag(char, old)
            char += ", " + new_tags
        elif new_tags.lower() not in char.lower():
            char += ", " + new_tags
    for slot in ("outfit", "accessory", "other"):
        if slots[slot]:
            if slot == "outfit":
                # 换装时新服装替换旧服装，否则角色卡农立绘的整套衣服会和场景服装堆在一起。
                for old in parse_appearance(char, outfit_kw, accessory_kw)["outfit"]:
                    char = remove_tag(char, old)
            char += ", " + ", ".join(slots[slot])
    return char


def infer_gender_from_count(count: str) -> str:
    cl = (count or "").lower()
    if re.search(r"\b1boy\b", cl) and not re.search(r"\b1girl\b", cl):
        return "boy"
    return "girl"


def infer_gender_from_prefix(prefix: str) -> str:
    return infer_gender_from_count(prefix)
