"""角色卡 schema 的单一事实来源。

「一个角色」此前在多处各手写一份字段表：导出（`_character_export_payload`）、
快照（`_snapshot_character`）、写回（`_apply_character_payload`）三处几乎一字不差，
新增一个角色字段要同步改三处，漏一处即 drift（历史上「快照格式统一为 18 字段」
正是这种维护负担留下的疤）。

此模块把字段表收成唯一定义，三处从它派生：
- `card_from_state`  ：state → 可移植角色卡（导出 / 快照共用）
- `apply_card_to_state`：角色卡 → state（写回 / 导入）

默认角色卡（`_default_character_payload`，从全局 config 读、且各字段有特殊默认值）
不在此处派生，但其字段集由 `CARD_KEYS` 钉住一致性，见 tests。
"""

from __future__ import annotations

from typing import Any

from . import session_schema


# (可移植卡片键, 会话 state 键)：17 个 1:1 字符串字段，顺序即对外 JSON 字段顺序。
# outfit 不在此表：它已收进 clothing box（state["clothing"]["dynamic_appearance"]），
# 在 card_from_state/apply_card_to_state 里经 session_schema 访问器单独处理。
CARD_STRING_FIELDS: tuple[tuple[str, str], ...] = (
    ("character", "custom_character"),
    ("series", "custom_series"),
    ("role_name", "custom_role_name"),
    ("bot_name", "custom_bot_name"),
    ("bot_self_name", "custom_bot_self_name"),
    ("visual_character", "custom_visual_character"),
    ("visual_series", "custom_visual_series"),
    ("persona", "custom_scheduled_persona"),
    ("appearance", "custom_positive_prefix"),
    ("count", "custom_count"),
    ("age_stage", "custom_character_age_stage"),
    ("occupation", "custom_character_occupation"),
    ("day_anchor", "custom_character_day_anchor"),
    ("relationship", "custom_spatial_relationship"),
    ("scene_preference", "custom_scene_preference"),
    ("selfie_preference", "custom_selfie_preference"),
    ("style", "custom_current_style"),
)

# 自动换装开关的 state 键；三态（None=跟随全局 / True / False），单独处理。
ALLOW_KEY = "custom_allow_llm_change_appearance"

# 卡片字段全集（含 outfit 及两个特殊字段）：默认角色卡用它做一致性校验。
CARD_KEYS: tuple[str, ...] = tuple(
    [card_key for card_key, _ in CARD_STRING_FIELDS] + ["outfit", "allow_change_appearance", "purity"]
)

# 默认角色卡（蕾伊）字段 → 全局 config 键：卡编辑器改默认角色即写回这些 config 键。
# appearance↔positive_prefix 是 1:1（发/瞳已折进 positive_prefix）。未列出的卡片字段
# 对默认角色恒为空（character/series/visual_*/count/occupation/scene_preference/selfie_preference）。
DEFAULT_CARD_TO_CONFIG: dict[str, str] = {
    "role_name": "role_name",
    "bot_name": "bot_name",
    "bot_self_name": "bot_self_name",
    "persona": "scheduled_persona",
    "appearance": "positive_prefix",
    "style": "current_style",
    "relationship": "spatial_relationship",
    "age_stage": "character_age_stage",
    "day_anchor": "character_day_anchor",
    "outfit": "dynamic_appearance",
}

_ALLOW_TRUE_WORDS = ("true", "1", "yes", "on", "开", "允许", "启用")


def parse_allow_change_appearance(raw: Any) -> bool | None:
    """自动换装三态解析：空/None=跟随全局（None）；其余按真假词解析。"""
    s = "" if raw is None else str(raw).strip().lower()
    if not s:
        return None
    return s in _ALLOW_TRUE_WORDS


def card_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """从会话 state 抽出可移植角色卡（不含 id）。导出与快照共用。"""
    card: dict[str, Any] = {
        card_key: state.get(state_key, "") for card_key, state_key in CARD_STRING_FIELDS
    }
    card["outfit"] = session_schema.get_outfit(state)  # 当前穿搭来自 clothing box
    card["allow_change_appearance"] = state.get(ALLOW_KEY)
    card["purity"] = state.get("purity")
    return card


def apply_card_to_state(state: dict[str, Any], data: dict[str, Any]) -> None:
    """把角色卡 payload 写回 state（导入 / 修改角色）。只写 data 里出现的字段。"""
    for card_key, state_key in CARD_STRING_FIELDS:
        if card_key in data:
            state[state_key] = "" if data[card_key] is None else str(data[card_key])
    if "outfit" in data:  # 当前穿搭写进 clothing box
        session_schema.set_outfit(state, "" if data["outfit"] is None else str(data["outfit"]))
    if "allow_change_appearance" in data:
        state[ALLOW_KEY] = parse_allow_change_appearance(data.get("allow_change_appearance"))
    if "purity" in data:
        raw = data.get("purity")
        s = str(raw).strip() if raw is not None else ""
        if s:
            try:
                state["purity"] = max(0, min(10, int(s)))
                state["purity_user_set"] = True
            except (TypeError, ValueError):
                state["purity"] = None
                state["purity_user_set"] = False
        else:
            state["purity"] = None
            state["purity_user_set"] = False
