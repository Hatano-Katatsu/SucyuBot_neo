from __future__ import annotations

import re
from typing import Any

from . import session_schema
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


# ── 衣柜换装系统 ─────────────────────────────────────────────────────────────
# 结构化衣柜：每个槽位存英文标签；服装层“同槽替换”，配饰累积。连衣裙与上衣/下装互斥。
# 真源是 state["wardrobe"]（dict），每次改动后渲染回扁平的 dynamic_appearance 供其余模块读取。
WARDROBE_CLOTHING_SLOTS = ("dress", "top", "bottom", "outerwear", "bra", "panties", "legwear", "footwear")
WARDROBE_SET_SLOTS = ("hair", "eyes") + WARDROBE_CLOTHING_SLOTS + ("other",)
WARDROBE_RENDER_ORDER = (
    "hair", "eyes", "dress", "top", "bottom", "outerwear", "bra", "panties", "legwear", "footwear", "accessory", "other",
)
# 细粒度服装关键词→槽位（顺序即优先级）。仅用于迁移旧 dynamic_appearance 与 LLM 失败兜底；
# 主分类由大模型完成，所以这里不求穷尽。
FINE_SLOT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bra", ("sports bra", "bralette", "brassiere", "bra")),
    ("panties", ("g-string", "thong", "panties", "panty", "knickers", "briefs")),
    ("legwear", ("pantyhose", "thigh-highs", "thighhighs", "thigh highs", "stockings", "tights", "knee socks", "socks", "leg warmers", "garter belt", "garter")),
    ("footwear", ("high heels", "heels", "boots", "sneakers", "sandals", "slippers", "loafers", "flats", "pumps", "mary janes", "shoes")),
    ("outerwear", ("windbreaker", "trench coat", "trenchcoat", "overcoat", "parka", "cardigan", "hoodie", "blazer", "jacket", "coat", "cloak", "cape", "robe")),
    ("dress", ("sundress", "nightgown", "nightdress", "negligee", "cheongsam", "qipao", "kimono", "yukata", "hanfu", "jumpsuit", "romper", "bodysuit", "leotard", "swimsuit", "one-piece", "dress", "gown")),
    ("top", ("bikini top", "tube top", "crop top", "tank top", "camisole", "turtleneck", "sweatshirt", "sweater", "t-shirt", "blouse", "shirt", "jersey", "halter", "vest", "top")),
    ("bottom", ("bikini bottom", "miniskirt", "skirt", "jeans", "trousers", "slacks", "sweatpants", "joggers", "leggings", "hotpants", "shorts", "pants", "culottes")),
)


def _wardrobe_slot_for_tag(tag: str, accessory_kw: list[str] | None = None) -> str:
    tl = tag.lower()
    if "hair" in tl or "发" in tl or any(h in tl for h in HAIRSTYLE_WORDS):
        return "hair"
    if "eye" in tl or "pupil" in tl or "瞳" in tl or "眼" in tl:
        return "eyes"
    for slot, kws in FINE_SLOT_KEYWORDS:
        if any(k in tl for k in kws):
            return slot
    if accessory_kw and any(k in tl for k in accessory_kw):
        return "accessory"
    return "other"


def seed_wardrobe_from_text(text: str, outfit_kw: list[str] | None = None, accessory_kw: list[str] | None = None) -> dict[str, str]:
    """把一段扁平外型标签串按细槽位归类（迁移旧数据 / LLM 兜底用，关键词尽力分类）。"""
    wd: dict[str, str] = {}
    for tag in [normalize_appearance_tag(t) for t in str(text or "").split(",") if t.strip()]:
        if not tag:
            continue
        slot = _wardrobe_slot_for_tag(tag, accessory_kw)
        wd[slot] = f"{wd[slot]}, {tag}" if wd.get(slot) else tag
    return wd


def apply_wardrobe_change(wardrobe: dict[str, str], change: dict[str, Any]) -> dict[str, str]:
    """把一次换装指令应用到衣柜：同槽替换、连衣裙互斥、配饰累积/摘除、按需清空槽位。"""
    if change.get("reset_all"):
        return {}
    wd = {k: v for k, v in (wardrobe or {}).items() if (v or "").strip()}

    set_vals = {slot: normalize_appearance_text(change.get(slot) or "") for slot in WARDROBE_SET_SLOTS}
    for slot, val in set_vals.items():
        if val:
            wd[slot] = val

    # 连衣裙互斥：本次设了连衣裙 → 清上衣/下装；本次设了上衣或下装 → 清连衣裙。
    if set_vals["dress"]:
        wd.pop("top", None)
        wd.pop("bottom", None)
    elif set_vals["top"] or set_vals["bottom"]:
        wd.pop("dress", None)

    # 配饰：累积 + 按名摘除。
    acc = [t.strip() for t in (wd.get("accessory") or "").split(",") if t.strip()]
    seen = {t.lower() for t in acc}
    for tag in [normalize_appearance_tag(t) for t in (change.get("accessory_add") or "").split(",") if t.strip()]:
        if tag and tag.lower() not in seen:
            acc.append(tag)
            seen.add(tag.lower())
    remove_acc = {normalize_appearance_tag(t).lower() for t in (change.get("accessory_remove") or "").split(",") if t.strip()}
    if remove_acc:
        acc = [t for t in acc if t.lower() not in remove_acc]
    if acc:
        wd["accessory"] = ", ".join(acc)
    else:
        wd.pop("accessory", None)

    # 显式清空某些槽位（脱掉外套/光脚等）。放最后，使移除意图优先。
    for slot in (change.get("remove") or []):
        if isinstance(slot, str):
            wd.pop(slot.strip(), None)
    return wd


def apply_wardrobe_seed(wardrobe: dict[str, str], seed: dict[str, str]) -> dict[str, str]:
    """把一份种子（slot→tags，来自关键词兜底分类）作为“设置/累积”应用到衣柜。"""
    change: dict[str, Any] = {}
    for slot, val in seed.items():
        if slot == "accessory":
            change["accessory_add"] = val
        else:
            change[slot] = val
    return apply_wardrobe_change(wardrobe, change)


def render_wardrobe(wardrobe: dict[str, str]) -> str:
    """把衣柜渲染成扁平 dynamic_appearance 串（固定顺序，供生图/规划器等读取）。"""
    if not isinstance(wardrobe, dict):
        return ""
    parts = [(wardrobe.get(k) or "").strip() for k in WARDROBE_RENDER_ORDER]
    return normalize_appearance_text(", ".join(p for p in parts if p))


def wardrobe_summary(wardrobe: dict[str, str]) -> str:
    """给用户/LLM 看的当前衣柜分槽摘要。"""
    if not isinstance(wardrobe, dict):
        return ""
    lines = []
    for k in WARDROBE_RENDER_ORDER:
        v = (wardrobe.get(k) or "").strip()
        if v:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


# ── 衣橱收藏（closet）─────────────────────────────────────────────────────────
# state["wardrobe_closet"]: {短名: {"slot","tags","added_at","times_worn","last_worn"}}。
# 单件为主：一件衣服占一个槽位（连衣裙本身算一件）。角色穿过的衣服自动收藏，便于点名复穿。
CLOSET_CAP = 30


def closet_add(closet: dict[str, Any], name: str, slot: str, tags: str, *, now: float = 0.0, cap: int = CLOSET_CAP) -> dict[str, Any]:
    tags = normalize_appearance_text(tags or "")
    name = (name or "").strip() or tags
    if not name or not tags or slot not in WARDROBE_CLOTHING_SLOTS:
        return closet or {}
    closet = dict(closet or {})
    # 同 tags 视为同一件：换名时清掉旧名，避免重复收藏。
    for other, entry in list(closet.items()):
        if other != name and normalize_appearance_text(entry.get("tags", "")) == tags:
            closet.pop(other, None)
    entry = dict(closet.get(name, {}))
    entry["slot"] = slot
    entry["tags"] = tags
    entry["added_at"] = entry.get("added_at") or now
    entry["times_worn"] = int(entry.get("times_worn", 0)) + 1
    entry["last_worn"] = now
    closet[name] = entry
    if len(closet) > cap:
        # 超额淘汰最久没穿的（按 last_worn/added_at）。
        doomed = sorted(closet, key=lambda k: closet[k].get("last_worn") or closet[k].get("added_at") or 0)
        for k in doomed[: len(closet) - cap]:
            closet.pop(k, None)
    return closet


def closet_summary(closet: dict[str, Any]) -> str:
    """给用户看的衣橱清单（按槽位分组）。"""
    if not isinstance(closet, dict) or not closet:
        return ""
    by_slot: dict[str, list[str]] = {}
    for name, entry in closet.items():
        by_slot.setdefault(entry.get("slot", "other"), []).append(name)
    lines = []
    for slot in WARDROBE_RENDER_ORDER:
        if by_slot.get(slot):
            lines.append(f"{slot}: " + "、".join(by_slot[slot]))
    return "\n".join(lines)


def closet_brief_for_llm(closet: dict[str, Any], limit: int = 40) -> str:
    """给分槽器/聊天模型看的衣橱清单：名→标签，便于点名复穿。"""
    if not isinstance(closet, dict) or not closet:
        return ""
    items = sorted(closet.items(), key=lambda kv: kv[1].get("last_worn") or kv[1].get("added_at") or 0, reverse=True)
    return "\n".join(f"- {name}（{e.get('slot','')}）: {e.get('tags','')}" for name, e in items[:limit])


def inject_appearance(service: Any, char: str, session_id: str = "") -> str:
    if not session_id:
        return char
    state = service._get_session_state(session_id)
    outfit_kw = service._outfit_kw
    accessory_kw = service._accessory_kw
    slots = parse_appearance(session_schema.get_outfit(state), outfit_kw, accessory_kw)
    char_set = service._is_character_set(session_id)

    # 默认角色（非角色态）若没有自定义衣柜，用全局 dynamic_appearance 作为初始穿搭回退：
    # 否则 config 里的默认装扮（吊带裙/开衫等）只进场景规划、不进 appearance 标签，导致画不出来。
    # 发色/瞳色已在下方 resolve() 各自回退全局默认；这里补齐 outfit/accessory/other。
    if not char_set and not (slots["outfit"] or slots["accessory"] or slots["other"]):
        fb = parse_appearance(service.config.get("dynamic_appearance", "") or "", outfit_kw, accessory_kw)
        for s in ("outfit", "accessory", "other"):
            if fb[s]:
                slots[s] = fb[s]

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
