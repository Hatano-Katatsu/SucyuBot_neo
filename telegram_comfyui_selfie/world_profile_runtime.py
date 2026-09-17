"""导入世界背景按角色取用；不改变聊天交互，也不引入剧情脚本。"""
from __future__ import annotations

import copy
import re
from typing import Any

from . import session_schema


class WorldProfileRuntimeMixin:
    def _imported_place_named(self, session_id: str, name: str) -> dict[str, Any] | None:
        """具体地名保留独立身份，只将用途映射给已有动线。"""
        candidates = []
        for entry in self._imported_world(session_id).get("entries", []):
            if entry.get("type") != "place" or not entry.get("enabled", True) or not entry.get("known") or not entry.get("place_key"):
                continue
            for term in [entry.get("name", ""), *entry.get("aliases", [])]:
                if isinstance(term, str) and len(term) > 1 and term.casefold() in name.casefold():
                    candidates.append((len(term), entry))
        return max(candidates, key=lambda pair: pair[0])[1] if candidates else None

    def _imported_world(self, session_id: str, card: dict[str, Any] | None = None) -> dict[str, Any]:
        if not session_id:
            return {}
        if card is None:
            state = self._get_session_state(session_id)
            world_id = session_schema.get_character_value(state, "custom_world_id", "")
            fallback = session_schema.get_character_value(state, "custom_world_snapshot", {})
        else:
            world_id, fallback = card.get("world_id", ""), card.get("world_snapshot", {})
        if world_id:
            row = self.app_store.get_world_profile(self._user_id_for_session(session_id), world_id)
            if row:
                return row["data"]
        return copy.deepcopy(fallback) if isinstance(fallback, dict) else {}

    def _imported_world_context(self, session_id: str, text: str = "", *, stable: bool = False) -> str:
        world = self._imported_world(session_id)
        if not world:
            return ""
        if stable:
            return "初始世界背景（非共同经历；自然使用，不复述设定；已有对话中的明确纠正优先，个人经历不改写所有角色的共同背景）:\n" + str(world.get("summary") or "")[:1600]
        state = self._get_session_state(session_id)
        place = session_schema.get_character_place(state)
        relevant = []
        for entry in world.get("entries", []):
            if not isinstance(entry, dict) or not entry.get("enabled", True) or not entry.get("known"):
                continue
            terms = [entry.get("name", "")] + entry.get("aliases", [])
            if (entry.get("type") == "place" and entry.get("place_key") == place) or any(t and len(t) > 1 and t.casefold() in text.casefold() for t in terms):
                relevant.append(f"{entry.get('name', '')}: {entry.get('content', '')}")
        photo = "摄影与手机符合此世界设定。" if world.get("photography") == "modern" else "保留原世界技术；图片作为场景分享，不叙述为角色使用手机自拍，不增加现代设备。"
        return "相关环境事实（不代表新事件）:\n" + "\n".join(relevant[:4])[:1300] + "\n" + photo

    def _imported_role_style(self, session_id: str, *, first_turn: bool = False, input_text: str = "") -> str:
        state = self._get_session_state(session_id)
        examples = session_schema.get_character_value(state, "custom_dialogue_examples", "")
        opening = session_schema.get_character_value(state, "custom_opening_message", "") if first_turn else ""
        if first_turn:
            alternatives = session_schema.get_character_value(state, "custom_alternate_greetings", [])
            options = [str(value) for value in [opening, *(alternatives if isinstance(alternatives, list) else [])] if value]
            terms = re.findall(r"[\w\u4e00-\u9fff]{2,}", input_text)
            opening = max(options, key=lambda value: sum(term in value for term in terms), default="")
        content = str(examples or "")[:1600]
        if opening:
            content += "\n可参考的初次见面表达（按当前输入适配，不强制开场事件发生）:\n" + str(opening)[:1000]
        # 仅替换简单角色宏；所有其他宏作为不支持内容移除，不执行任何脚本。
        name = self._get_session_cfg(session_id, "bot_name", "角色")
        content = content.replace("{{char}}", name).replace("{{user}}", "用户")
        content = re.sub(r"\{\{.*?\}\}", "", content, flags=re.S)
        return "说话风格参考（示例不是历史事实，不创建菜单或用户经历）:\n" + content if content.strip() else ""

    def _world_card_snapshot(self, session_id: str, card: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(card)
        world = self._imported_world(session_id, card)
        if world:
            result["world_snapshot"] = world
        return result
