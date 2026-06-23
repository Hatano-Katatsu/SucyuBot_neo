"""会话 state 字段的单一事实来源（数据结构重构·阶段 1）。

会话 state 是一个 ~70 字段的扁平 dict，字段分属三个互不相关的子系统：
- **会话全局**（`SESSION_GLOBAL`）：属于这场会话/这个人，绝不随角色冻结/清空
  （计时、调度、NTR 进度、frozen、角色池容器自身）。
- **角色配置**（`CHARACTER_CONFIG`）：身份/人设/外貌设定，走 saved_characters 卡。
  约定：`custom_*` 前缀一律是配置；另有少数非前缀配置项（purity 等）显式列出。
- **角色短期态**（`CHARACTER_TRANSIENT`）：对话/位置/照片/穿搭等工作记忆，随角色冻结/解冻/清空。

此前「默认值表」（service `_session_state_defaults`）与「归属分类」（commands 里的
`SESSION_GLOBAL_STATE_KEYS` / `CHARACTER_CONFIG_EXTRA_KEYS` / `RESET_PRESERVED_TRANSIENT_KEYS`）
分散在两个文件、各列一份，新增字段须两处同步、漏一处即 drift。

阶段 1 把它们收成**唯一一张 `STATE_SCHEMA`**：每个字段在此声明一次（归属 + 默认值 +
是否 reset 保留），默认值表与三个集合、两个分类器全部从它派生。

刻意**不改扁平命名空间**：`state["custom_bot_name"]` 这类读写点全不变，零迁移、零调用点改动。
分类器仍保留「`custom_` 前缀 ⇒ 配置」「其余 ⇒ 短期态」的前缀/兜底规则，使**未登记的新字段**
也能正确归类（失败方向是"正确隔离"而非串味）。
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from typing import Any

# ── 三个归属 scope ──
SESSION_GLOBAL = "session_global"
CHARACTER_CONFIG = "character_config"
CHARACTER_TRANSIENT = "character_transient"

_NO_DEFAULT = object()  # 标记「该字段不进默认值表」（动态产生，如 ntr_affection_reset/life_profile）


@dataclass(frozen=True)
class Field:
    """一个 state 字段的声明。

    scope：归属（三选一）。
    default：默认值；缺省表示不进 `state_defaults()`（运行时动态产生的字段）。
    factory：动态默认值（可调用，如 time.time），优先于 default。
    reset_preserved：是否属于「/reset（清对话）要保留、只有切角色才清」的短期态子集。
    """

    scope: str
    default: Any = _NO_DEFAULT
    factory: Any = None
    reset_preserved: bool = False

    def has_default(self) -> bool:
        return self.factory is not None or self.default is not _NO_DEFAULT

    def make_default(self) -> Any:
        if self.factory is not None:
            return self.factory()
        return copy.deepcopy(self.default)


G = SESSION_GLOBAL
C = CHARACTER_CONFIG
T = CHARACTER_TRANSIENT

# ── 唯一字段表 ──（新增 state 字段只在这里加一行；漏加也会按前缀/兜底正确归类）
STATE_SCHEMA: dict[str, Field] = {
    # —— 会话全局：计时 / 早安 / 推送调度 ——
    "last_interaction": Field(G, factory=time.time),
    "last_morning_greet_date": Field(G, default=""),
    "daily_trigger_times": Field(G, default=[]),
    "daily_trigger_date": Field(G, default=""),
    "daily_triggered_times": Field(G, default=[]),
    # —— 会话全局：角色池容器 / 初始化流程 ——
    "saved_characters": Field(G, default={}),
    "character_contexts": Field(G, default={}),
    "init_flow": Field(G, default={}),
    # —— 会话全局：NTR 进度 / 冻结 ——
    "ntr_stage_reached": Field(G, default=0),
    "ntr_reconcile_count": Field(G, default=0),
    "ntr_affection_reset": Field(G),  # 动态写入，不进默认表
    "frozen": Field(G, default=False),
    "frozen_at": Field(G, default=0),

    # —— 角色配置：custom_* 身份/人设/外貌设定 ——
    "custom_scheduled_persona": Field(C, default=""),
    "custom_role_name": Field(C, default=""),
    "custom_bot_name": Field(C, default=""),
    "custom_bot_self_name": Field(C, default=""),
    "custom_spatial_relationship": Field(C, default=""),
    "custom_location": Field(C, default=""),
    "custom_timezone_offset": Field(C, default=""),
    "custom_count": Field(C, default=""),
    "custom_positive_prefix": Field(C, default=""),
    "custom_default_hair": Field(C, default=""),
    "custom_default_eyes": Field(C, default=""),
    "custom_current_style": Field(C, default=""),
    "custom_scene_preference": Field(C, default=""),
    "custom_selfie_preference": Field(C, default=""),
    "custom_raw_profile_text": Field(C, default=""),
    "custom_prompt_intake": Field(C, default={}),
    "custom_allow_llm_change_appearance": Field(C, default=None),
    "custom_character": Field(C, default=""),
    "custom_series": Field(C, default=""),
    "custom_visual_character": Field(C, default=""),
    "custom_visual_series": Field(C, default=""),
    "custom_character_age_stage": Field(C, default=""),
    "custom_character_occupation": Field(C, default=""),
    "custom_character_day_anchor": Field(C, default=""),
    # —— 角色配置：非 custom_ 前缀的配置项（纯良度 / 标志位）——
    "persona_user_set": Field(C, default=False),
    "purity": Field(C, default=None),
    "purity_user_set": Field(C, default=False),

    # —— 角色短期态：对话上下文 ——
    "recent_message_history": Field(T, default=[]),
    "chat_history": Field(T, default=[]),
    "checkpoint_summary": Field(T, default=""),
    "checkpoint_message_id": Field(T, default=0),
    "last_checkpoint_at": Field(T, default=0),
    "last_dream_at": Field(T, default=0),
    "last_dream_message_id": Field(T, default=0),
    # —— 角色短期态：照片历史 / 回图标志 ——
    "sent_photos_history": Field(T, default=[]),
    "replying_to_selfie": Field(T, default=False),
    "last_sent_selfie_time": Field(T, default=0),
    "last_sent_selfie_caption": Field(T, default=""),
    "last_sent_selfie_source_description": Field(T, default=""),
    "last_sent_selfie_replied": Field(T, default=False),
    # —— 角色短期态：当前穿着（clothing box）/ 生活档案（reset 保留，仅切角色才清）——
    # clothing 整盒作为一个短期态单元冻结/解冻/reset 保留；盒内沿用原字段名 + 新增持久裸体态。
    # 访问一律走本模块的访问器（get_outfit/set_wardrobe/…），不要直接下钻 state["clothing"][...]。
    "clothing": Field(T, default={
        "dynamic_appearance": "",   # 当前穿搭（渲染自 wardrobe）
        "wardrobe": {},             # 分槽衣柜（真源）
        "wardrobe_closet": {},      # 收藏的整套穿搭
        "nudity": "",               # 持久裸体态（如 "completely nude"），空=穿着
        "nudity_at": 0.0,           # 裸体态确立时间，供 TTL 老化
    }, reset_preserved=True),
    "life_profile": Field(T, reset_preserved=True),  # 动态产生，不进默认表
    # —— 角色短期态：位置（place box）——
    # 用户位置 / 角色位置 / 同处判定 / 陈旧度全部收进 state["place"] 子字典。
    # 访问一律走本模块的访问器，不要直接下钻 state["place"][...]。
    "place": Field(T, default={
        "user_place": "",
        "user_place_label": "",
        "user_place_text": "",
        "user_place_updated_at": 0,
        "user_place_confidence": 0,
        "user_co_located": False,
        "user_place_source": "",
        "character_place": "",
        "character_place_label": "",
        "character_place_text": "",
        "character_place_name": "",
        "character_place_updated_at": 0,
        "character_place_confidence": 0,
        "character_place_history": [],
        "rounds_since_location": 0,
    }, reset_preserved=False),
    # —— 角色短期态：配图节奏 / 短期场景边界 ——
    "rounds_since_image": Field(T, default=0),
    "short_context_start": Field(T, default=0),
    "short_context_reset_time": Field(T, default=0),
    "short_context_reset_reason": Field(T, default=""),
}


# ── 派生：默认值表（单一来源，供 _get_session_state setdefault 与清空复位）──
def state_defaults() -> dict[str, Any]:
    """会话 state 全部有默认值字段的默认值（每调用一次产生独立的可变对象副本）。"""
    return {key: f.make_default() for key, f in STATE_SCHEMA.items() if f.has_default()}


# ── 派生：三个归属集合 ──
SESSION_GLOBAL_STATE_KEYS = frozenset(k for k, f in STATE_SCHEMA.items() if f.scope == SESSION_GLOBAL)
# custom_ 前缀之外的角色配置项（标志位 / 纯良度），供前缀分类器补充。
CHARACTER_CONFIG_EXTRA_KEYS = frozenset(
    k for k, f in STATE_SCHEMA.items() if f.scope == CHARACTER_CONFIG and not k.startswith("custom_")
)
# 短期态里「/reset 清对话要保留、只有切角色才清」的子集（当前外型/穿搭/生活档案）。
RESET_PRESERVED_TRANSIENT_KEYS = frozenset(k for k, f in STATE_SCHEMA.items() if f.reset_preserved)


# ── 派生：两个分类器（保留前缀/兜底规则，未登记的新字段也能正确归类）──
def is_character_config_key(key: str) -> bool:
    """是否为「角色配置」字段：custom_ 前缀，或显式列出的非前缀配置项。"""
    return key.startswith("custom_") or key in CHARACTER_CONFIG_EXTRA_KEYS


def is_transient_state_key(key: str) -> bool:
    """是否为「角色短期态」字段：既非会话全局、也非角色配置，即随角色冻结/解冻/清空。

    新增字段默认跟角色走，漏配的失败方向是"正确隔离"而非串味。
    """
    return key not in SESSION_GLOBAL_STATE_KEYS and not is_character_config_key(key)


# ──────────────────────────────────────────────────────────────────────────
# 嵌套分盒（数据结构重构·阶段 1 的真正落地：把扁平 state 按域装进子盒子）
#
# 这是「先搭骨架」的一步：只提供 **分盒定义 + 扁平↔嵌套双向迁移**，不改任何 state 访问点
# （仍是 state["custom_bot_name"]）。之后再逐 box 把访问点切到 state["character"]["..."]。
#
# box 与 scope 是两个正交维度：scope 管「切角色时冻结/清空」，box 管「存储如何分组」。
# 为避免逐字段手标，box 由 scope 派生 + 短期态按域细分（clothing/place，其余归 context）。
# ──────────────────────────────────────────────────────────────────────────

BOX_SESSION = "session"        # 会话全局：计时/调度/NTR/frozen/角色池容器
BOX_CHARACTER = "character"    # 角色配置：角色卡（custom_* + purity）+ 派生生活档案
BOX_CLOTHING = "clothing"      # 当前穿着：穿搭/衣柜/收藏（+ 后续的持久裸体态）
BOX_PLACE = "place"            # 位置：用户位置 / 角色位置 / 同处判定
BOX_CONTEXT = "context"        # 对话上下文：聊天历史/checkpoint/dream/照片/短期场景边界

BOXES: tuple[str, ...] = (BOX_SESSION, BOX_CHARACTER, BOX_CLOTHING, BOX_PLACE, BOX_CONTEXT)

# 短期态里按域细分（其余短期态默认归 context）。
_CLOTHING_KEYS = frozenset({"clothing"})
_PLACE_KEYS = frozenset({"place"})
# 短期态但归属角色域的派生缓存（生活档案：年龄段/职业/白天去向推断）。
_CHARACTER_DOMAIN_TRANSIENT = frozenset({"life_profile"})


def box_for(key: str) -> str:
    """字段归哪个 box。未登记字段按前缀/兜底分类映射，保持与 scope 分类器一致。"""
    f = STATE_SCHEMA.get(key)
    scope = f.scope if f is not None else None
    if scope == SESSION_GLOBAL or (scope is None and key in SESSION_GLOBAL_STATE_KEYS):
        return BOX_SESSION
    if scope == CHARACTER_CONFIG or (scope is None and is_character_config_key(key)):
        return BOX_CHARACTER
    # 角色短期态：按域细分
    if key in _CLOTHING_KEYS:
        return BOX_CLOTHING
    if key in _PLACE_KEYS:
        return BOX_PLACE
    if key in _CHARACTER_DOMAIN_TRANSIENT:
        return BOX_CHARACTER
    return BOX_CONTEXT


# 已登记字段 → box（派生表，便于查阅/测试）。
BOX_OF: dict[str, str] = {key: box_for(key) for key in STATE_SCHEMA}


# ──────────────────────────────────────────────────────────────────────────
# clothing box：第一个真正切换的盒。把穿搭/衣柜/收藏从扁平顶层收进 state["clothing"]，
# 并新增持久裸体态（nudity）。所有访问走下面的访问器，调用方不直接下钻盒内键名。
# ──────────────────────────────────────────────────────────────────────────

# 盒内默认值（与 STATE_SCHEMA["clothing"].default 同源含义）。
_CLOTHING_DEFAULT: dict[str, Any] = {
    "dynamic_appearance": "",
    "wardrobe": {},
    "wardrobe_closet": {},
    "nudity": "",
    "nudity_at": 0.0,
}
# 旧扁平字段名（顶层）→ 迁移进盒。
_LEGACY_CLOTHING_FLAT_KEYS = ("dynamic_appearance", "wardrobe", "wardrobe_closet")


def normalize_outfit_string(text: str) -> str:
    """穿搭串归一：折叠内部空格 + 去重（大小写不敏感），保持顺序。

    剥衣 `remove_tag` 是裸 `text.replace(tag, "")`：worn 标签若带双空格/重复，会与渲染串
    对不上而删不掉（"脱不掉衣服"的直接原因）。写入时归一，使 worn 与渲染串一致、replace 必中。
    刻意只动空格/去重，不剔除发色/瞳色（那与临时换发功能绑定，全裸时由 base 兜底）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in str(text or "").split(","):
        tag = re.sub(r"\s+", " ", raw.strip()).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return ", ".join(out)


def ensure_clothing_box(state: dict[str, Any]) -> dict[str, Any]:
    """保证 state["clothing"] 存在且子键补齐；把旧扁平 clothing 字段迁移进盒（幂等）。

    顺带对 dynamic_appearance 做归一（空格+去重），懒清理历史脏数据（双空格/重复标签）。
    """
    box = state.get("clothing")
    if not isinstance(box, dict):
        box = {}
        state["clothing"] = box
    for key in _LEGACY_CLOTHING_FLAT_KEYS:
        if key in state:  # 旧持久态：顶层有 → 搬进盒并删顶层
            box[key] = state.pop(key)
    for key, default in _CLOTHING_DEFAULT.items():
        if key not in box:
            box[key] = copy.deepcopy(default)
    normalized = normalize_outfit_string(box.get("dynamic_appearance", ""))
    if normalized != box.get("dynamic_appearance", ""):
        box["dynamic_appearance"] = normalized
    return box


def get_outfit(state: dict[str, Any]) -> str:
    return ensure_clothing_box(state).get("dynamic_appearance", "") or ""


def set_outfit(state: dict[str, Any], value: str) -> None:
    ensure_clothing_box(state)["dynamic_appearance"] = normalize_outfit_string(value)


def get_wardrobe(state: dict[str, Any]) -> dict[str, Any]:
    return ensure_clothing_box(state)["wardrobe"]


def set_wardrobe(state: dict[str, Any], value: Any) -> None:
    ensure_clothing_box(state)["wardrobe"] = value if isinstance(value, dict) else {}


def get_closet(state: dict[str, Any]) -> dict[str, Any]:
    return ensure_clothing_box(state)["wardrobe_closet"]


def set_closet(state: dict[str, Any], value: Any) -> None:
    ensure_clothing_box(state)["wardrobe_closet"] = value if isinstance(value, dict) else {}


def get_nudity(state: dict[str, Any]) -> str:
    return ensure_clothing_box(state).get("nudity", "") or ""


def get_nudity_at(state: dict[str, Any]) -> float:
    try:
        return float(ensure_clothing_box(state).get("nudity_at", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def set_nudity(state: dict[str, Any], level: str, *, at: float) -> None:
    box = ensure_clothing_box(state)
    level = (level or "").strip()
    box["nudity"] = level
    box["nudity_at"] = float(at or 0) if level else 0.0


def clear_nudity(state: dict[str, Any]) -> None:
    box = ensure_clothing_box(state)
    box["nudity"] = ""
    box["nudity_at"] = 0.0


# ──────────────────────────────────────────────────────────────────────────
# place box：第二个切换的盒。把用户位置 / 角色位置 / 同处判定从扁平顶层收进
# state["place"]。所有访问走下面的访问器，调用方不直接下钻盒内键名。
# ──────────────────────────────────────────────────────────────────────────

# 盒内默认值（与 STATE_SCHEMA["place"].default 同源含义）。
_PLACE_DEFAULT: dict[str, Any] = {
    "user_place": "",
    "user_place_label": "",
    "user_place_text": "",
    "user_place_updated_at": 0,
    "user_place_confidence": 0,
    "user_co_located": False,
    "user_place_source": "",
    "character_place": "",
    "character_place_label": "",
    "character_place_text": "",
    "character_place_name": "",
    "character_place_updated_at": 0,
    "character_place_confidence": 0,
    "character_place_history": [],
    "rounds_since_location": 0,
}
_LEGACY_PLACE_FLAT_KEYS = (
    "user_place", "user_place_label", "user_place_text", "user_place_updated_at",
    "user_place_confidence", "user_co_located", "user_place_source",
    "character_place", "character_place_label", "character_place_text", "character_place_name",
    "character_place_updated_at", "character_place_confidence", "character_place_history",
    "rounds_since_location",
)


def ensure_place_box(state: dict[str, Any]) -> dict[str, Any]:
    """保证 state["place"] 存在且子键补齐；把旧扁平 place 字段迁移进盒（幂等）。"""
    box = state.get("place")
    if not isinstance(box, dict):
        box = {}
        state["place"] = box
    for key in _LEGACY_PLACE_FLAT_KEYS:
        if key in state:
            box[key] = state.pop(key)
    for key, default in _PLACE_DEFAULT.items():
        if key not in box:
            box[key] = copy.deepcopy(default)
    return box


# ── 用户位置访问器 ──

def get_user_place(state: dict[str, Any]) -> str:
    return (ensure_place_box(state).get("user_place") or "").strip()

def get_user_place_label(state: dict[str, Any]) -> str:
    return ensure_place_box(state).get("user_place_label") or ""

def get_user_place_text(state: dict[str, Any]) -> str:
    return ensure_place_box(state).get("user_place_text") or ""

def get_user_place_updated_at(state: dict[str, Any]) -> float:
    try:
        return float(ensure_place_box(state).get("user_place_updated_at", 0) or 0)
    except (TypeError, ValueError):
        return 0.0

def get_user_place_confidence(state: dict[str, Any]) -> float:
    try:
        return float(ensure_place_box(state).get("user_place_confidence", 0) or 0)
    except (TypeError, ValueError):
        return 0.0

def get_user_co_located(state: dict[str, Any]) -> bool:
    return bool(ensure_place_box(state).get("user_co_located", False))

def get_user_place_source(state: dict[str, Any]) -> str:
    return ensure_place_box(state).get("user_place_source") or ""

def set_user_place(state: dict[str, Any], *, key="", label="", text="",
                   updated_at=None, confidence=None, co_located=None, source=None):
    box = ensure_place_box(state)
    if key is not None:
        box["user_place"] = key or ""
    if label is not None:
        box["user_place_label"] = label or ""
    if text is not None:
        box["user_place_text"] = text or ""
    if updated_at is not None:
        box["user_place_updated_at"] = float(updated_at or 0)
    if confidence is not None:
        box["user_place_confidence"] = float(confidence or 0)
    if co_located is not None:
        box["user_co_located"] = bool(co_located)
    if source is not None:
        box["user_place_source"] = source or ""

def set_user_co_located(state: dict[str, Any], value: bool):
    ensure_place_box(state)["user_co_located"] = bool(value)


# ── 角色位置访问器 ──

def get_character_place(state: dict[str, Any]) -> str:
    return (ensure_place_box(state).get("character_place") or "").strip()

def get_character_place_label(state: dict[str, Any]) -> str:
    return ensure_place_box(state).get("character_place_label") or ""

def get_character_place_text(state: dict[str, Any]) -> str:
    return ensure_place_box(state).get("character_place_text") or ""

def get_character_place_name(state: dict[str, Any]) -> str:
    return (ensure_place_box(state).get("character_place_name") or "").strip()

def get_character_place_updated_at(state: dict[str, Any]) -> float:
    try:
        return float(ensure_place_box(state).get("character_place_updated_at", 0) or 0)
    except (TypeError, ValueError):
        return 0.0

def get_character_place_confidence(state: dict[str, Any]) -> float:
    try:
        return float(ensure_place_box(state).get("character_place_confidence", 0) or 0)
    except (TypeError, ValueError):
        return 0.0

def get_character_place_history(state: dict[str, Any]) -> list:
    val = ensure_place_box(state).get("character_place_history")
    return val if isinstance(val, list) else []

def get_rounds_since_location(state: dict[str, Any]) -> int:
    try:
        return int(ensure_place_box(state).get("rounds_since_location", 0) or 0)
    except (TypeError, ValueError):
        return 0

def set_character_place(state: dict[str, Any], *, key="", label="", text="",
                        name="", updated_at=None, confidence=None, rounds=None):
    box = ensure_place_box(state)
    if key is not None:
        box["character_place"] = key or ""
    if label is not None:
        box["character_place_label"] = label or ""
    if text is not None:
        box["character_place_text"] = text or ""
    if name is not None:
        box["character_place_name"] = name or ""
    if updated_at is not None:
        box["character_place_updated_at"] = float(updated_at or 0)
    if confidence is not None:
        box["character_place_confidence"] = float(confidence or 0)
    if rounds is not None:
        box["rounds_since_location"] = int(rounds or 0)

def set_character_place_updated_at(state: dict[str, Any], value: float):
    ensure_place_box(state)["character_place_updated_at"] = float(value or 0)

def set_character_place_history(state: dict[str, Any], value: list):
    ensure_place_box(state)["character_place_history"] = list(value or [])

def append_character_place_history(state: dict[str, Any], entry: dict, *, max_len: int = 20):
    box = ensure_place_box(state)
    hist = box.get("character_place_history")
    if not isinstance(hist, list):
        hist = []
    hist.append(entry)
    box["character_place_history"] = hist[-max_len:]

def set_rounds_since_location(state: dict[str, Any], value: int):
    ensure_place_box(state)["rounds_since_location"] = int(value or 0)

def increment_rounds_since_location(state: dict[str, Any]):
    box = ensure_place_box(state)
    box["rounds_since_location"] = int(box.get("rounds_since_location", 0) or 0) + 1
