from __future__ import annotations

import json
import re
from typing import Any


INTAKE_FIELDS = (
    "name",
    "role",
    "age",
    "anchor",
    "persona",
    "base_appearance",
    "dynamic_appearance",
    "relationship",
    "city",
    "style",
    "scene_preference",
    "selfie_preference",
    "unclassified",
)

QUALITY_TERMS = {
    "masterpiece",
    "best quality",
    "absurdres",
    "highres",
    "score_9",
    "score_8",
    "score_7",
    "anime coloring",
    "clean lineart",
    "soft cel shading",
    "detailed illustration",
}

NAME_RE = re.compile(r"[\w\u4e00-\u9fffぁ-んァ-ヶー·・]{1,24}")
BASE_APPEARANCE_RE = re.compile(
    r"(发|头发|马尾|辫|刘海|呆毛|眼|瞳|眸|肤|皮肤|身材|体型|胸|角|尾巴|尾|翅|翼|耳|"
    r"纹身|疤|泪痣|雀斑|高挑|矮|娇小|纤细|苗条|丰满|blonde|hair|eyes?|pupil|skin|"
    r"horn|tail|wing|scar|tattoo|freckle|slender|petite|curvy)"
)
OUTFIT_RE = re.compile(
    r"(穿|衣|裙|裤|袜|鞋|靴|外套|毛衣|衬衫|制服|校服|西装|连衣裙|吊带|披风|盔甲|"
    r"项链|眼镜|耳环|戒指|发卡|蝴蝶结|配饰|包|帽|领结|dress|shirt|skirt|coat|"
    r"sweater|hoodie|uniform|suit|boots?|shoes?|glasses|necklace|earring|ring|ribbon|bow)"
)
ROLE_MAP = (
    (re.compile(r"大学生"), ("大学生", "adult", "school")),
    (re.compile(r"高中生|初中生|中学生"), ("学生", "minor", "school")),
    (re.compile(r"学生|学校"), ("学生", "", "school")),
    (re.compile(r"上班族|职员|白领|公司"), ("上班族", "adult", "company")),
    (re.compile(r"医生|护士|医护"), ("医护人员", "adult", "medical")),
    (re.compile(r"店员|咖啡师|服务员|零售"), ("店员", "adult", "retail")),
    (re.compile(r"司机|配送|外卖|快递"), ("配送/驾驶从业者", "adult", "delivery")),
    (re.compile(r"自由职业|自由工作|画师|作家"), ("自由职业者", "adult", "flexible")),
)
RELATION_RE = re.compile(r"(关系|暧昧|同居|异地|朋友|恋人|情侣|同事|同学|邻居|青梅竹马)")
CITY_RE = re.compile(r"(?:所在城市|城市|住在|生活在)[:：\s]*([\u4e00-\u9fffA-Za-z .·-]{2,24})")
STYLE_RE = re.compile(r"(画风|风格|artist|style|@)")
SCENE_RE = re.compile(r"(公园|家里|家中|房间|公司|学校|商场|大街|街头|咖啡|餐厅|车站|海边|自拍|对镜)")
PERSONA_RE = re.compile(r"(性格|人格|温柔|冷淡|强势|慢热|活泼|开朗|傲娇|病娇|认真|喜欢|习惯|说话|语气)")


def blank_intake(raw_text: str = "") -> dict[str, str]:
    data = {key: "" for key in INTAKE_FIELDS}
    data["raw_text"] = (raw_text or "").strip()
    return data


def clean_text(value: Any) -> str:
    if isinstance(value, list):
        value = "，".join(str(v).strip() for v in value if str(v).strip())
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ，,。；;")
    return text


def clean_quality_terms(text: str) -> str:
    parts = re.split(r"[,，、;；]\s*", text or "")
    kept = []
    for part in parts:
        item = clean_text(part)
        if not item:
            continue
        if item.lower().replace("_", " ") in QUALITY_TERMS:
            continue
        kept.append(item)
    return "，".join(kept)


def normalize_intake(data: dict[str, Any] | None, raw_text: str = "") -> dict[str, str]:
    out = blank_intake(raw_text)
    if not isinstance(data, dict):
        return out
    for key in INTAKE_FIELDS:
        out[key] = clean_text(data.get(key, ""))
    out["base_appearance"] = clean_quality_terms(out["base_appearance"])
    out["dynamic_appearance"] = clean_quality_terms(out["dynamic_appearance"])
    if not out["raw_text"]:
        out["raw_text"] = clean_text(data.get("raw_text", raw_text))
    return out


def merge_intake(primary: dict[str, Any] | None, fallback: dict[str, Any] | None, raw_text: str = "") -> dict[str, str]:
    left = normalize_intake(primary, raw_text)
    right = normalize_intake(fallback, raw_text)
    merged = blank_intake(raw_text or left.get("raw_text") or right.get("raw_text"))
    for key in INTAKE_FIELDS:
        merged[key] = left.get(key) or right.get(key) or ""
    return merged


def parse_llm_json(text: str, raw_text: str = "") -> dict[str, str]:
    body = (text or "").strip()
    body = re.sub(r"^```(?:json)?\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*```$", "", body)
    try:
        return normalize_intake(json.loads(body), raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", body, flags=re.DOTALL)
        if not match:
            raise
        return normalize_intake(json.loads(match.group(0)), raw_text)


def strip_command_prefix(text: str) -> str:
    return re.sub(r"^\s*/?(?:创建OC|创建oc|oc)\s*", "", text or "", flags=re.IGNORECASE).strip()


def split_phrases(text: str) -> list[str]:
    body = strip_command_prefix(text)
    parts = re.split(r"[\n,，。；;]+", body)
    return [clean_text(part) for part in parts if clean_text(part)]


def infer_name(text: str, phrases: list[str]) -> str:
    body = strip_command_prefix(text)

    def usable_name(candidate: str) -> bool:
        return (
            candidate not in {"她", "他", "角色", "名字"}
            and NAME_RE.fullmatch(candidate) is not None
            and not (
                BASE_APPEARANCE_RE.search(candidate)
                or OUTFIT_RE.search(candidate)
                or PERSONA_RE.search(candidate)
                or STYLE_RE.search(candidate)
                or SCENE_RE.search(candidate)
            )
        )

    for pattern in (
        r"(?:名字|姓名|角色名|名叫|叫|称作|设定为)[是叫为:：\s]*([\w\u4e00-\u9fffぁ-んァ-ヶー·・]{1,24})",
        r"^([\w\u4e00-\u9fffぁ-んァ-ヶー·・]{1,24})\s*(?:是|,|，)",
    ):
        match = re.search(pattern, body)
        if match:
            candidate = match.group(1).strip()
            if usable_name(candidate):
                return candidate
    if phrases:
        first = phrases[0]
        if usable_name(first):
            return first
    return ""


def append_field(data: dict[str, str], key: str, value: str):
    value = clean_text(value)
    if not value:
        return
    current = data.get(key, "")
    if not current:
        data[key] = value
        return
    values = [clean_text(v) for v in re.split(r"[，,]\s*", current) if clean_text(v)]
    if value not in values:
        data[key] = current + "，" + value


def classify_phrase(phrase: str, out: dict[str, str]):
    city = CITY_RE.search(phrase)
    if city:
        out["city"] = clean_text(city.group(1))
        return
    if STYLE_RE.search(phrase):
        append_field(out, "style", phrase)
        return
    for role_re, (role, age, anchor) in ROLE_MAP:
        if role_re.search(phrase):
            out["role"] = out["role"] or role
            out["age"] = out["age"] or age
            out["anchor"] = out["anchor"] or anchor
            return
    if RELATION_RE.search(phrase):
        append_field(out, "relationship", phrase)
        return
    if OUTFIT_RE.search(phrase):
        append_field(out, "dynamic_appearance", phrase)
        return
    if BASE_APPEARANCE_RE.search(phrase):
        append_field(out, "base_appearance", phrase)
        return
    if PERSONA_RE.search(phrase):
        append_field(out, "persona", phrase)
        return
    if SCENE_RE.search(phrase):
        append_field(out, "scene_preference", phrase)
        if "自拍" in phrase:
            append_field(out, "selfie_preference", phrase)
        return
    append_field(out, "unclassified", phrase)


def heuristic_intake(text: str) -> dict[str, str]:
    out = blank_intake(text)
    phrases = split_phrases(text)
    out["name"] = infer_name(text, phrases)
    for phrase in phrases:
        if phrase == out["name"]:
            continue
        classify_phrase(phrase, out)
    return normalize_intake(out, text)


def merge_oc_fields(fields: dict[str, str], intake: dict[str, Any]) -> dict[str, str]:
    merged = dict(fields or {})
    intake = normalize_intake(intake)
    mapping = {
        "name": "name",
        "role": "role",
        "age": "age",
        "anchor": "anchor",
        "persona": "persona",
        "base_appearance": "appearance",
        "dynamic_appearance": "outfit",
        "relationship": "relationship",
        "city": "city",
    }
    for src, dst in mapping.items():
        if intake.get(src) and not merged.get(dst):
            merged[dst] = intake[src]
    return merged


def useful_summary(intake: dict[str, Any]) -> str:
    intake = normalize_intake(intake)
    labels = (
        ("base_appearance", "基础外观"),
        ("dynamic_appearance", "穿搭/配饰"),
        ("style", "画风"),
        ("scene_preference", "场景偏好"),
        ("selfie_preference", "自拍偏好"),
    )
    parts = [f"{label}: {intake[key]}" for key, label in labels if intake.get(key)]
    return "；".join(parts)
