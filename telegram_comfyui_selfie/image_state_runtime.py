from __future__ import annotations

import copy
import re
import time
from typing import Any

from . import session_schema
from .world_runtime import PLACE_TYPES


class ImageStateRuntimeMixin:
    """管理图片规划产生、并在发送成功后提交的会话状态变更。"""

    def _commit_image_state_mutation(self, session_id: str, mutation: Any) -> bool:
        """在图片已发送且照片历史已写入后，一次性提交规划器提出的状态变更。"""
        if not session_id or not isinstance(mutation, dict) or not mutation:
            return False
        state = self._get_session_state(session_id)
        before = copy.deepcopy(state)
        working = copy.deepcopy(state)
        logs: list[tuple[str, str]] = []
        changed = False
        committed_at = time.time()

        if mutation.get("clear_undress_state"):
            had_undress_state = bool(
                session_schema.get_nudity(working)
                or session_schema.get_wardrobe_item_states(working)
            )
            session_schema.clear_nudity(working)
            session_schema.clear_wardrobe_item_states(working)
            if had_undress_state:
                changed = True
                logs.append(("WARDROBE", "图片成功后清理上一场景的裸体与衣物部件状态"))

        nudity = str(mutation.get("nudity") or "").strip()
        if nudity:
            previous = (
                session_schema.get_nudity(working),
                session_schema.get_nudity_at(working),
            )
            session_schema.set_nudity(working, nudity, at=committed_at)
            if previous != (nudity, committed_at):
                changed = True

        user_location = mutation.get("user_location")
        world_enabled = True
        if hasattr(self, "_world_runtime_enabled"):
            try:
                world_enabled = bool(self._world_runtime_enabled())
            except Exception:
                world_enabled = False
        if world_enabled and isinstance(user_location, dict):
            loc = re.sub(r"\s+", "", str(user_location.get("value") or "").strip().lower())
            co_located = bool(user_location.get("co_located"))
            try:
                location_at = float(user_location.get("planned_at") or 0) or committed_at
            except (TypeError, ValueError):
                location_at = committed_at
            if co_located or loc in ("with_user", "with_character", "together", "同处", "一起"):
                session_schema.set_user_place(
                    working,
                    key="",
                    label="与角色同处",
                    text="生图前判断：与角色在同一空间",
                    updated_at=location_at,
                    confidence=None,
                    co_located=True,
                    source="llm",
                )
                changed = True
                logs.append(("LOC", f"图片成功后提交用户位置：与角色同处（{loc or '-'}）"))
            elif loc in PLACE_TYPES:
                session_schema.set_user_place(
                    working,
                    key=loc,
                    label=PLACE_TYPES[loc]["label"],
                    text="生图前推断",
                    updated_at=location_at,
                    confidence=None,
                    co_located=False,
                    source="llm",
                )
                changed = True
                logs.append(("LOC", f"图片成功后提交用户位置：{PLACE_TYPES[loc]['label']}"))

        character_location = mutation.get("character_location")
        if isinstance(character_location, dict):
            key = str(character_location.get("value") or "").strip().lower()
            if key in PLACE_TYPES:
                try:
                    confidence = float(character_location.get("confidence") or 0.6)
                except (TypeError, ValueError):
                    confidence = 0.6
                source = str(character_location.get("source") or "image").strip() or "image"
                session_schema.set_character_place(
                    working,
                    key=key,
                    label=PLACE_TYPES[key]["label"],
                    text=key[:40],
                    name="",
                    updated_at=committed_at,
                    confidence=confidence,
                    rounds=0,
                )
                history = session_schema.get_character_place_history(working)
                if not history or history[-1].get("key") != key:
                    session_schema.append_character_place_history(working, {
                        "key": key,
                        "label": PLACE_TYPES[key]["label"],
                        "source": source,
                        "confidence": confidence,
                        "ts": committed_at,
                    })
                changed = True
                logs.append(("MOVE", f"图片成功后提交角色位置：{PLACE_TYPES[key]['label']}"))

        accessory_removal = mutation.get("persistent_accessory_removal")
        if isinstance(accessory_removal, dict):
            clothing_off = str(accessory_removal.get("clothing_off") or "").strip()
            raw_sources = accessory_removal.get("sources")
            sources = (
                tuple(str(value or "") for value in raw_sources)
                if isinstance(raw_sources, list)
                else ()
            )
            rendered, remove_tags = self._apply_removed_accessories_from_image(
                working,
                clothing_off,
                *sources,
            )
            if remove_tags:
                changed = True
                logs.append((
                    "WARDROBE",
                    f'图片成功后持久化 accessory_remove={remove_tags} '
                    f'来源=clothing_off="{clothing_off[:80]}" | 结果="{rendered[:140]}"',
                ))

        if not changed:
            return False
        state.clear()
        state.update(working)
        try:
            self._save_session_state(session_id, state)
        except Exception:
            state.clear()
            state.update(before)
            self.sessions[session_id] = state
            raise
        for kind, message in logs:
            self._ulog(session_id, kind, message)
        return True

    @staticmethod
    def _image_state_mutation_from_plan(
        plan: dict[str, Any],
        *accessory_sources: str,
    ) -> dict[str, Any]:
        """读取规划器 mutation，并兼容旧规划器只返回 clothing_off 的格式。"""
        proposed = plan.get("state_mutation")
        mutation = copy.deepcopy(proposed) if isinstance(proposed, dict) else {}
        clothing_off = str(plan.get("clothing_off") or "").strip()
        if clothing_off and "persistent_accessory_removal" not in mutation:
            mutation["persistent_accessory_removal"] = {
                "clothing_off": clothing_off,
                "sources": [
                    str(value or "")
                    for value in accessory_sources
                    if str(value or "").strip()
                ],
            }
        if "nude" in clothing_off.lower() and not mutation.get("nudity"):
            mutation["nudity"] = "completely nude"
        return mutation

    def _preview_image_mutation_appearance(self, session_id: str, mutation: Any) -> str:
        """在不改共享状态的前提下，计算照片历史应记录的配饰移除后穿搭。"""
        state = self._get_session_state(session_id)
        working = copy.deepcopy(state)
        accessory_removal = (
            mutation.get("persistent_accessory_removal")
            if isinstance(mutation, dict)
            else None
        )
        if isinstance(accessory_removal, dict):
            clothing_off = str(accessory_removal.get("clothing_off") or "").strip()
            raw_sources = accessory_removal.get("sources")
            sources = (
                tuple(str(value or "") for value in raw_sources)
                if isinstance(raw_sources, list)
                else ()
            )
            rendered, _ = self._apply_removed_accessories_from_image(
                working,
                clothing_off,
                *sources,
            )
            if rendered:
                return rendered
        return session_schema.get_outfit(working)
