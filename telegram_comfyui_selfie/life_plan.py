from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any

from . import session_schema
from .world_runtime import PLACE_TYPES

logger = logging.getLogger(__name__)

LIFE_PLAN_PURPOSE_WORDS = ("目标", "计划", "任务", "为了", "争取", "完成", "打算", "必须")
LIFE_PLAN_STATUSES = {"active", "achieved", "abandoned"}
LIFE_EVENT_STATUSES = {"planned", "done", "derailed", "skipped"}
LIFE_TIME_HINTS = {"morning", "noon", "afternoon", "evening", "night"}


def _compact_text(value: Any, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


class LifePlanMixin:
    """角色生活线：结构化目标只给后台，聊天只注入降解后的生活底色。"""

    def _life_plan_enabled(self, session_id: str = "") -> bool:
        value = self._get_session_cfg(session_id, "life_plan_enabled", self.config.get("life_plan_enabled", True))
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def _life_plan_limit(self, session_id: str, key: str, default: int) -> int:
        try:
            return max(1, int(self._get_session_cfg(session_id, key, self.config.get(key, default)) or default))
        except (TypeError, ValueError):
            return default

    def _life_plan_limits(self, session_id: str) -> dict[str, int]:
        return {
            "long": self._life_plan_limit(session_id, "life_plan_max_long", 3),
            "mid": self._life_plan_limit(session_id, "life_plan_max_mid", 4),
            "events": self._life_plan_limit(session_id, "life_plan_max_events", 5),
            "texture_goals": self._life_plan_limit(session_id, "life_plan_texture_goal_count", 2),
        }

    def _life_plan_character_key(self, session_id: str) -> str:
        if hasattr(self, "_context_character_key"):
            try:
                return self._context_character_key(session_id)
            except Exception:
                pass
        if hasattr(self, "_memory_character"):
            try:
                return self._memory_character(session_id)
            except Exception:
                pass
        return ""

    def _life_today_date(self, session_id: str, now: datetime | None = None) -> str:
        current = now or self._session_now(session_id)
        return current.date().isoformat()

    def _life_long_review_due(self, session_id: str, previous: dict[str, Any], today_date: str) -> bool:
        if not previous.get("long_goals"):
            return True
        review_days = self._life_plan_limit(session_id, "life_plan_long_review_days", 10)
        last_date = str(previous.get("last_long_review_date") or "").strip()
        try:
            last = datetime.fromisoformat(last_date).date()
            today = datetime.fromisoformat(today_date).date()
        except Exception:
            return True
        return (today - last).days >= review_days

    def _life_plan_needs_bootstrap(self, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict) or not payload:
            return True
        longs = [item for item in payload.get("long_goals") or [] if isinstance(item, dict)]
        mids = [item for item in payload.get("mid_goals") or [] if isinstance(item, dict)]
        active_longs = [item for item in longs if item.get("status") == "active"]
        active_mids = [item for item in mids if item.get("status") == "active"]
        return not active_longs or not active_mids

    @staticmethod
    def _life_time_hint_for_dt(dt: datetime) -> str:
        hour = dt.hour
        if 5 <= hour < 11:
            return "morning"
        if 11 <= hour < 14:
            return "noon"
        if 14 <= hour < 18:
            return "afternoon"
        if 18 <= hour < 22:
            return "evening"
        return "night"

    @staticmethod
    def _life_empty_payload(today_date: str = "") -> dict[str, Any]:
        return {
            "long_goals": [],
            "mid_goals": [],
            "today": {"date": today_date, "events": [], "texture": ""},
            "last_long_review_date": today_date,
            "npcs": [],
        }

    @staticmethod
    def _life_next_id(items: list[dict[str, Any]], prefix: str) -> str:
        used = set()
        for item in items:
            text = str(item.get("id") or "")
            if text.startswith(prefix):
                try:
                    used.add(int(text[len(prefix):]))
                except ValueError:
                    continue
        idx = 1
        while idx in used:
            idx += 1
        return f"{prefix}{idx}"

    def _normalize_life_goal(
        self,
        item: Any,
        *,
        prefix: str,
        today_date: str,
        existing: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        text = _compact_text(item.get("text"), 220)
        if not text:
            return None
        gid = str(item.get("id") or "").strip() or self._life_next_id(existing, prefix)
        status = str(item.get("status") or "active").strip()
        if status not in LIFE_PLAN_STATUSES:
            status = "active"
        goal = {
            "id": gid,
            "text": text,
            "status": status,
            "created_date": str(item.get("created_date") or today_date),
            "updated_date": str(item.get("updated_date") or today_date),
        }
        if prefix == "l":
            goal["motivation"] = _compact_text(item.get("motivation"), 220)
        else:
            goal["parent_id"] = str(item.get("parent_id") or "").strip()
            goal["progress_note"] = _compact_text(item.get("progress_note"), 240)
        return goal

    def _normalize_life_event(self, item: Any, *, today_date: str, existing: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        text = _compact_text(item.get("text"), 220)
        if not text:
            return None
        eid = str(item.get("id") or "").strip() or self._life_next_id(existing, "e")
        time_hint = str(item.get("time_hint") or "").strip().lower()
        if time_hint not in LIFE_TIME_HINTS:
            time_hint = "afternoon"
        place_key = str(item.get("place_key") or "").strip().lower()
        if place_key not in PLACE_TYPES:
            place_key = "home"
        status = str(item.get("status") or "planned").strip()
        if status not in LIFE_EVENT_STATUSES:
            status = "planned"
        event = {
            "id": eid,
            "time_hint": time_hint,
            "text": text,
            "place_key": place_key,
            "related_mid_id": str(item.get("related_mid_id") or "").strip() or None,
            "status": status,
        }
        side_note = _compact_text(item.get("side_note"), 180)
        if side_note:
            event["side_note"] = side_note
        return event

    def _normalize_life_plan_payload(self, payload: Any, *, today_date: str = "", session_id: str = "") -> dict[str, Any]:
        raw = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        today_date = today_date or str((raw.get("today") or {}).get("date") or "")
        limits = self._life_plan_limits(session_id)
        plan = self._life_empty_payload(today_date)
        longs: list[dict[str, Any]] = []
        for item in raw.get("long_goals") or []:
            goal = self._normalize_life_goal(item, prefix="l", today_date=today_date, existing=longs)
            if goal:
                longs.append(goal)
            if len(longs) >= limits["long"]:
                break
        mids: list[dict[str, Any]] = []
        valid_long_ids = {item["id"] for item in longs}
        fallback_parent = next((item["id"] for item in longs if item.get("status") == "active"), "")
        for item in raw.get("mid_goals") or []:
            goal = self._normalize_life_goal(item, prefix="m", today_date=today_date, existing=mids)
            if goal:
                if goal.get("parent_id") not in valid_long_ids:
                    goal["parent_id"] = fallback_parent
                mids.append(goal)
            if len(mids) >= limits["mid"]:
                break
        today = raw.get("today") if isinstance(raw.get("today"), dict) else {}
        events: list[dict[str, Any]] = []
        valid_mid_ids = {item["id"] for item in mids}
        for item in today.get("events") or []:
            event = self._normalize_life_event(item, today_date=today_date, existing=events)
            if event:
                if event.get("related_mid_id") not in valid_mid_ids:
                    event["related_mid_id"] = None
                events.append(event)
            if len(events) >= limits["events"]:
                break
        plan["long_goals"] = longs
        plan["mid_goals"] = mids
        plan["today"] = {
            "date": str(today.get("date") or today_date),
            "events": events,
            "texture": str(today.get("texture") or "").strip(),
        }
        if isinstance(today.get("event_sides"), dict):
            plan["today"]["event_sides"] = {str(k): str(v).strip() for k, v in today["event_sides"].items() if str(v).strip()}
        plan["last_long_review_date"] = str(raw.get("last_long_review_date") or today_date)
        plan["npcs"] = raw.get("npcs") if isinstance(raw.get("npcs"), list) else []
        return plan

    def _load_life_plan_row(self, session_id: str, character_key: str | None = None) -> dict[str, Any] | None:
        character_key = self._life_plan_character_key(session_id) if character_key is None else character_key
        row = self.app_store.get_life_plan(session_id, character_key or "")
        if row and isinstance(row.get("payload"), dict):
            row["payload"] = self._normalize_life_plan_payload(row["payload"], session_id=session_id)
            try:
                state = self._get_session_state(session_id)
                if (character_key or "") == self._life_plan_character_key(session_id):
                    session_schema.set_life_plan_payload(state, row["payload"])
            except Exception:
                logger.debug("life plan state cache update failed", exc_info=True)
        return row

    def _save_life_plan_payload(self, session_id: str, character_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_life_plan_payload(payload, session_id=session_id)
        row = self.app_store.upsert_life_plan(session_id, character_key or "", normalized)
        try:
            if (character_key or "") == self._life_plan_character_key(session_id):
                state = self._get_session_state(session_id)
                session_schema.set_life_plan_payload(state, normalized)
                self._save_session_state(session_id, state)
        except Exception:
            logger.debug("life plan state cache save failed", exc_info=True)
        return row

    def delete_life_plan(self, session_id: str, character_key: str | None = None) -> bool:
        character_key = self._life_plan_character_key(session_id) if character_key is None else character_key
        ok = self.app_store.delete_life_plan(session_id, character_key or "")
        try:
            if (character_key or "") == self._life_plan_character_key(session_id):
                state = self._get_session_state(session_id)
                session_schema.set_life_plan_payload(state, {})
                self._save_session_state(session_id, state)
        except Exception:
            pass
        return ok

    def delete_life_plans_for_session(self, session_id: str) -> int:
        count = self.app_store.delete_life_plans_for_session(session_id)
        try:
            state = self._get_session_state(session_id)
            session_schema.set_life_plan_payload(state, {})
            self._save_session_state(session_id, state)
        except Exception:
            pass
        return count

    def _apply_life_plan_ops(self, plan: dict[str, Any], ops: list[Any], *, today_date: str, session_id: str = "") -> dict[str, Any]:
        result = {"applied": 0, "ignored": 0, "details": []}
        longs = plan.setdefault("long_goals", [])
        mids = plan.setdefault("mid_goals", [])
        limits = self._life_plan_limits(session_id)
        for op in ops or []:
            if not isinstance(op, dict):
                result["ignored"] += 1
                continue
            name = str(op.get("op") or "").strip().lower()
            oid = str(op.get("id") or "").strip()
            applied = False
            if name in {"progress", "update_mid"} and oid:
                for mid in mids:
                    if mid.get("id") == oid:
                        note = _compact_text(op.get("note") or op.get("progress_note"), 240)
                        if note:
                            mid["progress_note"] = note
                        if op.get("text"):
                            mid["text"] = _compact_text(op.get("text"), 220)
                        mid["updated_date"] = today_date
                        applied = True
                        break
            elif name in {"achieve", "abandon"} and oid:
                for bucket in (mids, longs):
                    for item in bucket:
                        if item.get("id") == oid:
                            item["status"] = "achieved" if name == "achieve" else "abandoned"
                            item["updated_date"] = today_date
                            if op.get("reason") and "progress_note" in item:
                                item["progress_note"] = _compact_text(op.get("reason"), 240)
                            applied = True
                            break
                    if applied:
                        break
            elif name == "add_mid":
                active_long_ids = [item.get("id") for item in longs if item.get("status") == "active"]
                parent_id = str(op.get("parent_id") or "").strip()
                if parent_id not in active_long_ids:
                    parent_id = active_long_ids[0] if active_long_ids else ""
                if parent_id and len(mids) < limits["mid"]:
                    mid = self._normalize_life_goal(
                        {
                            "id": op.get("id") or self._life_next_id(mids, "m"),
                            "parent_id": parent_id,
                            "text": op.get("text"),
                            "progress_note": op.get("note") or op.get("progress_note") or "",
                            "status": "active",
                            "created_date": today_date,
                            "updated_date": today_date,
                        },
                        prefix="m",
                        today_date=today_date,
                        existing=mids,
                    )
                    if mid:
                        mids.append(mid)
                        applied = True
            elif name == "add_long" and len(longs) < limits["long"]:
                long_goal = self._normalize_life_goal(
                    {
                        "id": op.get("id") or self._life_next_id(longs, "l"),
                        "text": op.get("text"),
                        "motivation": op.get("motivation") or "",
                        "status": "active",
                        "created_date": today_date,
                        "updated_date": today_date,
                    },
                    prefix="l",
                    today_date=today_date,
                    existing=longs,
                )
                if long_goal:
                    longs.append(long_goal)
                    applied = True
            elif name == "update_long" and oid:
                for goal in longs:
                    if goal.get("id") == oid:
                        if op.get("text"):
                            goal["text"] = _compact_text(op.get("text"), 220)
                        if op.get("motivation"):
                            goal["motivation"] = _compact_text(op.get("motivation"), 220)
                        goal["updated_date"] = today_date
                        applied = True
                        break
            if applied:
                result["applied"] += 1
            else:
                result["ignored"] += 1
            result["details"].append({"op": name, "id": oid, "applied": applied})
        return result

    def _life_plan_events_from_update(self, parsed: dict[str, Any], *, today_date: str, mids: list[dict[str, Any]], session_id: str = "") -> list[dict[str, Any]]:
        raw_events = parsed.get("today_events")
        if raw_events is None and isinstance(parsed.get("today"), dict):
            raw_events = parsed["today"].get("events")
        if raw_events is None:
            return []
        events: list[dict[str, Any]] = []
        valid_mid_ids = {item.get("id") for item in mids}
        for item in raw_events if isinstance(raw_events, list) else []:
            event = self._normalize_life_event(item, today_date=today_date, existing=events)
            if event:
                if event.get("related_mid_id") not in valid_mid_ids:
                    event["related_mid_id"] = None
                events.append(event)
            if len(events) >= self._life_plan_limits(session_id)["events"]:
                break
        return events

    def _life_plan_from_update(self, previous: dict[str, Any], parsed: dict[str, Any], *, today_date: str, session_id: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
        plan = self._normalize_life_plan_payload(previous, today_date=today_date, session_id=session_id)
        long_review_touched = isinstance(parsed.get("long_goals"), list)
        if isinstance(parsed.get("long_goals"), list):
            plan["long_goals"] = []
            for item in parsed.get("long_goals") or []:
                goal = self._normalize_life_goal(item, prefix="l", today_date=today_date, existing=plan["long_goals"])
                if goal:
                    plan["long_goals"].append(goal)
                if len(plan["long_goals"]) >= self._life_plan_limits(session_id)["long"]:
                    break
        if isinstance(parsed.get("mid_goals"), list):
            plan["mid_goals"] = []
            for item in parsed.get("mid_goals") or []:
                goal = self._normalize_life_goal(item, prefix="m", today_date=today_date, existing=plan["mid_goals"])
                if goal:
                    plan["mid_goals"].append(goal)
                if len(plan["mid_goals"]) >= self._life_plan_limits(session_id)["mid"]:
                    break
        long_ids = {str(item.get("id") or "") for item in plan.get("long_goals") or [] if isinstance(item, dict)}
        for op in parsed.get("ops") or []:
            if not isinstance(op, dict):
                continue
            name = str(op.get("op") or "").strip().lower()
            oid = str(op.get("id") or "").strip()
            if name in {"add_long", "update_long"} or (name in {"achieve", "abandon"} and oid in long_ids):
                long_review_touched = True
        op_result = self._apply_life_plan_ops(plan, parsed.get("ops") or [], today_date=today_date, session_id=session_id)
        events = self._life_plan_events_from_update(parsed, today_date=today_date, mids=plan.get("mid_goals") or [], session_id=session_id)
        if not events:
            events = self._heuristic_life_events(plan, today_date=today_date)
        plan["today"] = {
            "date": today_date,
            "events": events,
            "texture": str((parsed.get("today") or {}).get("texture") or "").strip() if isinstance(parsed.get("today"), dict) else "",
        }
        if long_review_touched:
            plan["last_long_review_date"] = today_date
        plan.setdefault("last_long_review_date", today_date)
        return self._normalize_life_plan_payload(plan, today_date=today_date, session_id=session_id), op_result

    def _heuristic_life_plan(self, session_id: str, *, today_date: str) -> dict[str, Any]:
        profile = self._life_profile(session_id) if hasattr(self, "_life_profile") else {}
        anchor = (profile or {}).get("day_anchor") or "unknown"
        state = self._get_session_state(session_id)
        role = session_schema.get_character_value(state, "custom_role_name", "")
        if not role and hasattr(self, "_get_session_cfg"):
            role = self._get_session_cfg(session_id, "role_name", "")
        occupation = session_schema.get_character_value(state, "custom_character_occupation", "")
        persona = session_schema.get_character_value(state, "custom_scheduled_persona", "")
        drive_seed = "、".join(part for part in (role, occupation, persona[:60]) if part) or "自己的身份和生活压力"
        long_text = {
            "company": f"在{occupation or '白天的工作'}里证明自己的能力，同时不被职责耗空",
            "school": "把学业、同龄关系和真正想成为的人慢慢对齐",
            "factory": "在重复班次里守住自己的手艺、自尊和未来出路",
            "farm": "把眼前土地、家计和自己想要的自由都慢慢稳住",
            "construction": "靠辛苦攒出能选择下一步生活的底气",
            "medical": "在照顾别人和专业责任之外，守住自己的精神边界",
            "retail": "在被顾客和班次推着走的日子里攒出独立选择的余地",
            "delivery": "从奔波路线里攒出稳定收入和不被生活牵着跑的节奏",
            "driver": "在路上、休息和责任之间找到能长久撑下去的方向",
            "home": f"围绕{drive_seed}，把生活空间变成能承载自己愿望的地方",
            "flexible": f"围绕{drive_seed}，把松散时间变成真正属于自己的作品、技能或选择",
        }.get(anchor, f"围绕{drive_seed}，找到一个值得长期追下去的自我方向")
        mid_text = {
            "company": "这周先把一个棘手任务处理到能被看见的程度",
            "school": "这周先把最拖着自己的课程、作业或社交压力往前推一步",
            "factory": "这周先把一个反复出错或压心的班次问题处理顺",
            "medical": "这周先把值班后的疲惫和一个专业压力点分开消化",
            "retail": "这周先从一个班次、人际或库存小麻烦里找回主动感",
            "delivery": "这周先优化一段最消耗情绪的路线或收入节奏",
            "driver": "这周先把一段路上的疲惫和休息安排调匀",
        }.get(anchor, "这周先选一个能贴近长期追求的小突破口")
        plan = {
            "long_goals": [{
                "id": "l1",
                "text": long_text,
                "motivation": "想让自己的生活不像只是被时间推着走",
                "status": "active",
                "created_date": today_date,
                "updated_date": today_date,
            }],
            "mid_goals": [{
                "id": "m1",
                "parent_id": "l1",
                "text": mid_text,
                "progress_note": "还只是压在心里的几件小事，没有急着说出口",
                "status": "active",
                "created_date": today_date,
                "updated_date": today_date,
            }],
            "today": {"date": today_date, "events": [], "texture": ""},
            "last_long_review_date": today_date,
            "npcs": [],
        }
        plan["today"]["events"] = self._heuristic_life_events(plan, today_date=today_date)
        return self._normalize_life_plan_payload(plan, today_date=today_date, session_id=session_id)

    def _heuristic_life_events(self, plan: dict[str, Any], *, today_date: str) -> list[dict[str, Any]]:
        mids = [item for item in plan.get("mid_goals") or [] if item.get("status") == "active"]
        related = mids[0].get("id") if mids else None
        events = [
            {
                "id": "e1",
                "time_hint": "afternoon",
                "text": "找个安静地方处理手头那点事情",
                "place_key": "cafe",
                "related_mid_id": related,
                "status": "planned",
            },
            {
                "id": "e2",
                "time_hint": "evening",
                "text": "回去路上顺手补一点日用品",
                "place_key": "supermarket",
                "related_mid_id": None,
                "status": "planned",
            },
        ]
        normalized: list[dict[str, Any]] = []
        for item in events:
            event = self._normalize_life_event(item, today_date=today_date, existing=normalized)
            if event:
                normalized.append(event)
        return normalized

    def _life_plan_materials(self, session_id: str, character_key: str, *, diary_date: str = "", diary: str = "") -> dict[str, Any]:
        state = self._get_session_state(session_id)
        history_summary = ""
        try:
            meta = self.app_store.get_context_meta(session_id, character_key)
            history_summary = str(meta.get("character_history_summary") or "").strip()
        except Exception:
            pass
        if not history_summary:
            history_summary = session_schema.get_character_history_summary(state)
        diaries = []
        try:
            diaries = self.app_store.recent_diaries(session_id, character_key, limit=5)
        except Exception:
            diaries = []
        memories = []
        try:
            memories = self.memory.list_memories(session_id, character=character_key, limit=10, include_inactive=False)
        except Exception:
            memories = []
        return {
            "persona": self._get_effective_persona(session_id, include_appearance=False) if hasattr(self, "_get_effective_persona") else "",
            "life_profile": self._life_profile(session_id) if hasattr(self, "_life_profile") else {},
            "history_summary": history_summary,
            "diaries": diaries,
            "fresh_diary": {"date": diary_date, "content": diary},
            "memories": memories,
        }

    def _format_life_plan_materials(self, materials: dict[str, Any]) -> str:
        diary_lines = []
        fresh = materials.get("fresh_diary") or {}
        if fresh.get("content"):
            diary_lines.append(f"[{fresh.get('date') or 'fresh'}] {_compact_text(fresh.get('content'), 1200)}")
        for diary in materials.get("diaries") or []:
            content = diary.get("content") or ""
            if content and diary.get("diary_date") != fresh.get("date"):
                diary_lines.append(f"[{diary.get('diary_date')}] {_compact_text(content, 800)}")
        memory_lines = []
        for memory in materials.get("memories") or []:
            if memory.get("summary"):
                memory_lines.append(f"- [{memory.get('kind', 'event')}/重要度{memory.get('importance', 3)}] {_compact_text(memory.get('summary'), 240)}")
        return (
            f"Persona:\n{materials.get('persona') or 'none'}\n\n"
            f"Life profile:\n{json.dumps(materials.get('life_profile') or {}, ensure_ascii=False)}\n\n"
            f"Character history summary:\n{materials.get('history_summary') or 'none'}\n\n"
            f"Recent diaries:\n{chr(10).join(diary_lines) or 'none'}\n\n"
            f"High-importance memories:\n{chr(10).join(memory_lines) or 'none'}"
        )

    async def _call_life_plan_json(self, session_id: str, system: str, user: str, *, tag: str, temp: float = 0.2) -> Any:
        purposes: list[str] = []
        if self.has_llm_config("chat", session_id):
            purposes.append("chat")
        if self.has_llm_config("image", session_id):
            purposes.append("image")
        if not purposes:
            raise RuntimeError("chat/fast model API Key is not configured")
        last_exc: Exception | None = None
        for purpose in purposes:
            raw = ""
            try:
                raw = await self._call_llm(
                    system,
                    user,
                    temp=temp,
                    tag=tag if purpose == "chat" else f"{tag}-fast-fallback",
                    purpose=purpose,
                    disable_thinking=True if purpose == "chat" else None,
                    session_id=session_id,
                    max_tokens=4096,
                )
                parser = self._parse_llm_json if hasattr(self, "_parse_llm_json") else json.loads
                return parser(raw)
            except Exception as exc:
                last_exc = exc
                self._ulog(session_id, "WARN", f"LIFE_PLAN_JSON_RETRY tag={tag} purpose={purpose} error={exc}")
        raise RuntimeError(str(last_exc or "life plan json failed"))

    async def _generate_life_plan_update(
        self,
        session_id: str,
        character_key: str,
        previous: dict[str, Any],
        *,
        today_date: str,
        diary_date: str = "",
        diary: str = "",
        reason: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.has_llm_config("chat", session_id) and not self.has_llm_config("image", session_id):
            plan = self._heuristic_life_plan(session_id, today_date=today_date)
            return plan, {"status": "heuristic", "reason": "no_llm"}
        materials = self._life_plan_materials(session_id, character_key, diary_date=diary_date, diary=diary)
        place_keys = ", ".join(sorted(PLACE_TYPES))
        review_days = self._life_plan_limit(session_id, "life_plan_long_review_days", 10)
        long_review_due = self._life_long_review_due(session_id, previous or {}, today_date)
        system = (
            "You maintain a private structured life plan for a roleplay character. The chat model will never see this JSON. "
            "Update it from diary evidence without making the character sound task-driven. Output strict JSON only.\n"
            "Schema: {\"ops\":[...],\"today_events\":[...]}. You may include full long_goals/mid_goals for cold start or when existing goals are empty/misaligned.\n"
            "Ops: progress/update_mid/achieve/abandon/add_mid/update_long/add_long. Apply changes by stable id. "
            "Unknown ids are ignored by code, so use existing ids when possible.\n"
            "Rules: long_goals max 3, mid_goals max 4, today_events max 5. Each mid goal must have a parent_id from active long_goals. "
            "Long goals must come from the character's own core drive: ideals, obsession, fear, ambition, identity pressure, career/artistic pursuit, "
            "defect compensation, family/social conflict, or worldview position. Be creative but compatible with persona, history, memories, and diaries. "
            "Infer that core drive yourself from the source materials; do not rely on pre-extracted candidate labels. "
            "Privately reason from inside the character's point of view about what they would want, avoid, prove, repair, protect, or become, but output JSON only and do not expose that reasoning. "
            "Do not default to hollow relationship maintenance such as '维系感情', '和用户更亲密', or '把生活过安稳一点' unless the character setting explicitly makes that the central drive. "
            "Mid goals must be concrete stages toward long goals, not generic chores or daily relationship upkeep. "
            f"Review long_goals roughly every {review_days} days. Long-goal review is {'due' if long_review_due else 'not due'} today; "
            "when it is not due, avoid long_goals/add_long/update_long unless diary evidence forces a major stable change. "
            "At least 1 today event should relate to a mid goal when any active mid goal exists. "
            "If an event did not happen, that is normal; mark yesterday as derailed/skipped only when diary evidence says so. "
            "Do not carry derailed events forward as debt. Do not invent facts that contradict diaries, memories, or history.\n"
            f"Allowed place_key values: {place_keys}."
        )
        user = (
            f"Today: {today_date}\nReason: {reason}\nPrevious plan JSON:\n"
            f"{json.dumps(previous or {}, ensure_ascii=False, indent=2)}\n\n"
            f"Evidence and character materials:\n{self._format_life_plan_materials(materials)}"
        )
        parsed = await self._call_life_plan_json(session_id, system, user, tag="life-plan", temp=0.2)
        if not isinstance(parsed, dict):
            raise ValueError("life-plan output must be JSON object")
        return self._life_plan_from_update(previous, parsed, today_date=today_date, session_id=session_id)

    def _select_life_texture_mid_goals(self, plan: dict[str, Any], *, today_date: str, session_id: str) -> list[dict[str, Any]]:
        active = [item for item in plan.get("mid_goals") or [] if item.get("status") == "active"]
        if not active:
            return []
        rng = random.Random(f"{session_id}:{today_date}:{','.join(item.get('id','') for item in active)}")
        items = list(active)
        rng.shuffle(items)
        limit = self._life_plan_limits(session_id)["texture_goals"]
        return items[:limit]

    @staticmethod
    def _life_text_has_purpose_words(text: str) -> bool:
        return any(word in str(text or "") for word in LIFE_PLAN_PURPOSE_WORDS)

    def _fallback_life_texture(self, plan: dict[str, Any]) -> str:
        events = (plan.get("today") or {}).get("events") or []
        if events:
            event = events[0]
            place = PLACE_TYPES.get(event.get("place_key") or "", {}).get("label", "外面")
            return f"最近心里有些细碎牵挂，{place}那边的事还压着一点。白天多半会被日常琐事牵着走，情绪不算太松。"
        return "最近心里有些细碎牵挂，醒来时还带着一点没散开的倦意。今天的生活背景很轻，只适合偶尔自然流露。"

    def _fallback_life_event_side(self, event: dict[str, Any]) -> str:
        place = PLACE_TYPES.get(event.get("place_key") or "", {}).get("label", "外面")
        return f"她刚从{place}那边缓过来，身上还留着一点日常琐事的余温。"

    async def _render_life_plan_texture(self, session_id: str, character_key: str, plan: dict[str, Any], *, today_date: str) -> dict[str, Any]:
        plan = self._normalize_life_plan_payload(plan, today_date=today_date, session_id=session_id)
        selected = self._select_life_texture_mid_goals(plan, today_date=today_date, session_id=session_id)
        events = (plan.get("today") or {}).get("events") or []
        texture = ""
        event_sides: dict[str, str] = {}
        if self.has_llm_config("chat", session_id) or self.has_llm_config("image", session_id):
            system = (
                "Render private structured life plan into low-purpose daily texture for a roleplay chat bot. "
                "Return strict JSON: {\"texture\":\"2-4 Chinese lines\", \"event_sides\":{\"event_id\":\"one vague Chinese state sentence\"}}.\n"
                "Do not use these words in texture: 目标、计划、任务、为了、争取、完成、打算、必须. "
                "No lists, no timetable, no '今天要做X'. Write mood, body state, vague background, and ordinary life residue. "
                "Event side sentences are for image push planner: they must describe current state/emotion, not progress reports."
            )
            base_user = (
                f"Date: {today_date}\nSelected mid-goal shadows:\n"
                f"{json.dumps(selected, ensure_ascii=False, indent=2)}\n\n"
                f"Today events:\n{json.dumps(events, ensure_ascii=False, indent=2)}"
            )
            for attempt in range(2):
                user = base_user
                if attempt:
                    user += "\n\nPrevious output used forbidden purposeful wording. Rewrite softer and vaguer."
                try:
                    parsed = await self._call_life_plan_json(session_id, system, user, tag="life-texture", temp=0.7)
                    if isinstance(parsed, dict):
                        candidate = str(parsed.get("texture") or "").strip()
                        if candidate and not self._life_text_has_purpose_words(candidate):
                            texture = candidate
                            raw_sides = parsed.get("event_sides") if isinstance(parsed.get("event_sides"), dict) else {}
                            event_sides = {
                                str(k): str(v).strip()
                                for k, v in raw_sides.items()
                                if str(v).strip() and not self._life_text_has_purpose_words(str(v))
                            }
                            break
                        self._ulog(session_id, "WARN", f"LIFE_PLAN_TEXTURE_PURPOSE_WORDS attempt={attempt + 1}")
                except Exception as exc:
                    self._ulog(session_id, "WARN", f"LIFE_PLAN_TEXTURE_FAILED attempt={attempt + 1} error={exc}")
                    break
        if not texture:
            fallback = self._fallback_life_texture(plan)
            texture = "" if self._life_text_has_purpose_words(fallback) else fallback
        today = plan.setdefault("today", {"date": today_date, "events": []})
        today["date"] = today_date
        today["texture"] = texture
        for event in today.get("events") or []:
            side = event_sides.get(str(event.get("id") or "")) or self._fallback_life_event_side(event)
            if side and not self._life_text_has_purpose_words(side):
                event["side_note"] = side
        return self._normalize_life_plan_payload(plan, today_date=today_date, session_id=session_id)

    async def _update_life_plan_after_dream(
        self,
        session_id: str,
        character_key: str,
        local_dt: datetime,
        *,
        diary_date: str = "",
        diary: str = "",
        reason: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        if not self._life_plan_enabled(session_id):
            return {"status": "skipped", "reason": "disabled"}
        today_date = self._life_today_date(session_id, local_dt)
        row = self._load_life_plan_row(session_id, character_key)
        previous = row.get("payload") if row else {}
        needs_bootstrap = self._life_plan_needs_bootstrap(previous)
        if not force and previous and (previous.get("today") or {}).get("date") == today_date and not needs_bootstrap:
            return {"status": "skipped", "reason": "already_current", "date": today_date}
        try:
            plan, op_result = await self._generate_life_plan_update(
                session_id,
                character_key,
                previous,
                today_date=today_date,
                diary_date=diary_date,
                diary=diary,
                reason=reason,
            )
            plan = await self._render_life_plan_texture(session_id, character_key, plan, today_date=today_date)
            saved = self._save_life_plan_payload(session_id, character_key, plan)
            result = {
                "status": "updated",
                "date": today_date,
                "character": character_key,
                "ops": op_result,
                "events": len((plan.get("today") or {}).get("events") or []),
                "updated_at": saved.get("updated_at", 0),
            }
            self._ulog(session_id, "LIFE", f"生活线更新 {json.dumps(result, ensure_ascii=False, default=str)}")
            return result
        except Exception as exc:
            self._ulog(session_id, "ERROR", f"LIFE_PLAN_FAILED date={today_date} reason={reason} error={exc}")
            logger.warning("life plan update failed", exc_info=True)
            return {"status": "failed", "date": today_date, "error": str(exc)}

    async def ensure_life_plan_for_today(self, session_id: str, *, force: bool = False, reason: str = "manual") -> dict[str, Any]:
        local_dt = self._session_now(session_id)
        character_key = self._life_plan_character_key(session_id)
        row = self._load_life_plan_row(session_id, character_key)
        today_date = self._life_today_date(session_id, local_dt)
        if (
            not force
            and row
            and (row.get("payload", {}).get("today") or {}).get("date") == today_date
            and not self._life_plan_needs_bootstrap(row.get("payload") or {})
        ):
            return {"status": "current", "life_plan": row}
        result = await self._update_life_plan_after_dream(
            session_id,
            character_key,
            local_dt,
            diary_date=(local_dt.date() - timedelta(days=1)).isoformat(),
            diary="",
            reason=reason,
            force=True,
        )
        row = self._load_life_plan_row(session_id, character_key)
        return {"status": result.get("status"), "result": result, "life_plan": row}

    def queue_life_plan_refresh_if_needed(self, session_id: str, *, reason: str = "lazy-chat") -> bool:
        if not self._life_plan_enabled(session_id):
            return False
        today_date = self._life_today_date(session_id)
        row = self._load_life_plan_row(session_id)
        if row and (row.get("payload", {}).get("today") or {}).get("date") == today_date:
            return False
        tasks = getattr(self, "_life_plan_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._life_plan_tasks = tasks
        task = tasks.get(session_id)
        if task and not task.done():
            return False

        async def runner():
            try:
                await self.ensure_life_plan_for_today(session_id, force=False, reason=reason)
            except Exception as exc:
                self._ulog(session_id, "ERROR", f"LIFE_PLAN_LAZY_FAILED error={exc}")

        tasks[session_id] = asyncio.create_task(runner(), name=f"life-plan:{session_id}")
        return True

    def _life_plan_chat_context(self, session_id: str, *, now: datetime | None = None) -> str:
        if not self._life_plan_enabled(session_id):
            return ""
        row = self._load_life_plan_row(session_id)
        if not row:
            return ""
        plan = row.get("payload") or {}
        today = plan.get("today") or {}
        if today.get("date") != self._life_today_date(session_id, now):
            return ""
        texture = str(today.get("texture") or "").strip()
        if not texture or self._life_text_has_purpose_words(texture):
            return ""
        return (
            "生活底色（角色近日的心绪与生活背景。这不是日程或剧本：不要主动汇报、不要刻意推进、不要每轮都提及；"
            "只在用户问起、或情境自然触及时自然流露。用户当前的话题永远优先于这里的任何内容）:\n"
            f"{texture}"
        )

    def _life_plan_event_for_now(self, session_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        row = self._load_life_plan_row(session_id)
        if not row:
            return None
        plan = row.get("payload") or {}
        today = plan.get("today") or {}
        current = now or self._session_now(session_id)
        if today.get("date") != self._life_today_date(session_id, current):
            return None
        hint = self._life_time_hint_for_dt(current)
        events = [item for item in today.get("events") or [] if isinstance(item, dict) and item.get("status") == "planned"]
        for event in events:
            if event.get("time_hint") == hint:
                return event
        return events[0] if events else None

    def _life_plan_push_context(self, session_id: str, *, now: datetime | None = None) -> str:
        if not self._life_plan_enabled(session_id):
            return ""
        event = self._life_plan_event_for_now(session_id, now=now)
        if not event:
            return ""
        side = str(event.get("side_note") or "").strip() or self._fallback_life_event_side(event)
        if not side or self._life_text_has_purpose_words(side):
            return ""
        return (
            "生活线侧面（只作此刻状态和情绪背景，不是安排播报）: "
            f"{side}\n处理规则: 推送写此刻的状态、地点、疲惫或余韵，不要写成进度汇报。"
        )

    def _format_life_plan_diary_context(self, session_id: str, character_key: str, diary_date: str) -> str:
        row = self._load_life_plan_row(session_id, character_key)
        if not row:
            return ""
        plan = row.get("payload") or {}
        today = plan.get("today") or {}
        if today.get("date") != diary_date:
            return ""
        event_lines = []
        for event in today.get("events") or []:
            place = PLACE_TYPES.get(event.get("place_key") or "", {}).get("label", event.get("place_key") or "")
            related = "related" if event.get("related_mid_id") else "life"
            event_lines.append(
                f"- {event.get('time_hint')}: {event.get('text')} @ {place} status={event.get('status')} kind={related}"
            )
        if not event_lines and not today.get("texture"):
            return ""
        return (
            "Life plan for this diary date (private structured background; use only if supported by dialogue/diary evidence, do not invent completion):\n"
            f"Texture: {today.get('texture') or 'none'}\n"
            f"Events:\n{chr(10).join(event_lines) or 'none'}"
        )

    def life_plan_snapshot(self, session_id: str, character_key: str) -> dict[str, Any] | None:
        row = self._load_life_plan_row(session_id, character_key)
        if not row:
            return None
        return copy.deepcopy(row)
