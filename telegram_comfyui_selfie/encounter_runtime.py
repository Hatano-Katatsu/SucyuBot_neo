"""跨会话角色邂逅编排器（一期：同世界观互访）。

配对配置的两个不同会话的角色，在双方空闲时由本模块安排一次见面：访客 A 当天前往
地主 B 的城市，一次 LLM 调用编排整个场景，双方各自按自己视角落库记住（system 历史
事件 + 长期记忆 kind=event + life_plan 事件/NPC + encounters 关系史表），之后各自在
对话/推送中自然提起。一期不做双人同框生图、实时接力对话、互发消息，也不安排专门的
承接推送。

已拍板的产品决策：
- 编排 LLM 调用用地主侧 B 的会话 profile（场景在 B 城），用量记 B 侧；
- 配对即授权，不需要逐次确认；
- 不做专门的用户通知推送。

护栏：一期配对即声明同世界观；编排内容以编排器输出为准，不受任一侧用户即时输入影响；
双人记忆互不共享，只各自写自己视角（Smallville 模式）。
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

from . import session_schema

logger = logging.getLogger(__name__)

# 邂逅场所类目池：只取公共场所，具体地名由城市目录提供、取不到时用 PLACE_TYPES 示例兜底。
ENCOUNTER_VENUE_PLACE_KEYS = ("cafe", "park", "mall", "restaurant", "street")

_ENCOUNTER_TEXT_LIMIT = 400


def _clean_text(value: Any, limit: int = _ENCOUNTER_TEXT_LIMIT) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class EncounterRuntimeMixin:
    """邂逅编排：配对校验、双锁、编排调用、原子落库、旅行覆盖结算。"""

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _cross_world_enabled(self) -> bool:
        value = self.config.get("cross_world_enabled", False)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def _cross_world_pairs(self) -> list[dict[str, Any]]:
        """归一化 cross_world_pairs 配置；非法条目跳过并告警。

        配置格式（仅配置文件编辑）：
        [{"a": {"chat_id": 123456, "character": "角色名A"},
          "b": {"chat_id": 654321, "character": "角色名B"}}]
        """
        raw = self.config.get("cross_world_pairs") or []
        if not isinstance(raw, list):
            logger.warning("cross_world_pairs 必须是列表，已忽略")
            return []
        pairs: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                logger.warning("cross_world_pairs 条目必须是对象，已跳过: %r", item)
                continue
            sides: dict[str, dict[str, str]] = {}
            for side in ("a", "b"):
                data = item.get(side)
                if not isinstance(data, dict):
                    break
                chat_id = str(data.get("chat_id") or "").strip()
                character = str(data.get("character") or "").strip()
                if not chat_id or not character:
                    break
                sides[side] = {
                    "session_id": self.session_id_for_chat(chat_id),
                    "character": character,
                }
            if len(sides) != 2:
                logger.warning("cross_world_pairs 条目缺少 chat_id/character，已跳过: %r", item)
                continue
            if sides["a"]["session_id"] == sides["b"]["session_id"]:
                # 一期只支持跨会话配对；同会话内角色互动走会话内既有机制。
                logger.warning("cross_world_pairs 条目两侧同会话，已跳过: %r", item)
                continue
            pair = {"a": sides["a"], "b": sides["b"]}
            pair["pair_key"] = self._encounter_pair_key(
                sides["a"]["session_id"], sides["a"]["character"],
                sides["b"]["session_id"], sides["b"]["character"],
            )
            pairs.append(pair)
        return pairs

    def _encounter_cooldown_days(self) -> float:
        try:
            value = float(self.config.get("cross_world_encounter_cooldown_days", 7) or 7)
        except (TypeError, ValueError):
            return 7.0
        if not math.isfinite(value):
            return 7.0
        return max(0.0, value)

    def _encounter_chance(self) -> float:
        try:
            value = float(self.config.get("cross_world_encounter_chance", 0.5) or 0.5)
        except (TypeError, ValueError):
            return 0.5
        if not math.isfinite(value):
            return 0.5
        return max(0.0, min(1.0, value))

    @staticmethod
    def _encounter_pair_key(session_id_a: str, character_a: str, session_id_b: str, character_b: str) -> str:
        """角色对规范化键：两侧 (session_id, character) 排序拼接，查询不区分方向。"""
        sides = sorted([f"{session_id_a}:{character_a}", f"{session_id_b}:{character_b}"])
        return "|".join(sides)

    # ------------------------------------------------------------------
    # 旅行覆盖结算（dream 链路调用；_session_city 读取层已对过期覆盖惰性失效）
    # ------------------------------------------------------------------
    def _settle_travel_override(self, session_id: str, character_key: str = "") -> bool:
        """dream 结算：清除活动角色已过期的旅行覆盖，记录 debug 日志。

        只处理活动角色的 live state；非活动角色的覆盖随冻结上下文保存，
        读取层 `_session_city` 按 until 惰性失效，不需要在这里逐个翻冻结上下文。
        """
        if not session_id:
            return False
        live_key = self._context_character_key(session_id) if hasattr(self, "_context_character_key") else ""
        if character_key and character_key != live_key:
            return False
        state = self._get_session_state(session_id)
        override = session_schema.get_travel_override(state)
        city = str(override.get("city") or "").strip()
        if not city:
            return False
        try:
            until = float(override.get("until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until > time.time():
            return False
        session_schema.clear_travel_override(state)
        self._mark_dirty(session_id)
        logger.debug(
            "travel override settled session=%s character=%s city=%s home=%s",
            session_id, live_key, city, override.get("home_city") or "",
        )
        return True

    # ------------------------------------------------------------------
    # 调度入口（scheduler_loop 每轮调用；所有判断失败即跳过，绝不抛出）
    # ------------------------------------------------------------------
    async def _maybe_schedule_encounters(self) -> None:
        if not self._cross_world_enabled():
            return
        active = getattr(self, "_active_encounters", None)
        if not isinstance(active, set):
            active = set()
            self._active_encounters = active
        for pair in self._cross_world_pairs():
            pair_key = pair["pair_key"]
            try:
                if pair_key in active:
                    continue
                last_ts = self.app_store.last_encounter_ts_for_pair(pair_key)
                cooldown = self._encounter_cooldown_days() * 86400
                if last_ts and time.time() - last_ts < cooldown:
                    continue
                if random.random() >= self._encounter_chance():
                    continue
                if not self._encounter_pair_ready(pair):
                    continue
                active.add(pair_key)

                async def runner(pair=pair, pair_key=pair_key):
                    try:
                        await self._run_encounter(pair)
                    except Exception as exc:
                        logger.warning("encounter failed pair=%s: %s", pair_key, exc, exc_info=True)
                        try:
                            self._ulog(pair["b"]["session_id"], "ERROR", f"ENCOUNTER_FAILED pair={pair_key}: {exc}")
                        except Exception:
                            logger.debug("encounter failure ulog failed", exc_info=True)
                    finally:
                        self._active_encounters.discard(pair_key)

                self._spawn_background(
                    runner(),
                    name=f"encounter:{pair_key}",
                    scope="encounter",
                )
                logger.info("encounter scheduled pair=%s", pair_key)
            except Exception:
                active.discard(pair_key)
                logger.warning("encounter schedule check failed pair=%s", pair_key, exc_info=True)

    def _encounter_pair_ready(self, pair: dict[str, Any], *, locks_held: bool = False) -> bool:
        """廉价预检：两侧会话存在、角色存在且为当前活动角色、双方空闲。

        locks_held=True 用于持双锁后的复核：此时锁被编排器自己持有，
        「锁被占用」检查必须跳过，只复核活跃/推送/作息等真实状态。
        """
        for side in ("a", "b"):
            session_id = pair[side]["session_id"]
            character = pair[side]["character"]
            if session_id not in self.sessions:
                logger.debug("encounter skip pair=%s: 会话不存在 %s", pair["pair_key"], session_id)
                return False
            state = self._get_session_state(session_id)
            if session_schema.get_frozen(state):
                logger.debug("encounter skip pair=%s: 会话冻结 %s", pair["pair_key"], session_id)
                return False
            # 一期只支持配对角色恰好是两侧当前活动角色；切换角色后自然恢复候选资格。
            if self._memory_character(session_id) != character:
                logger.debug(
                    "encounter skip pair=%s: 角色非活动 %s/%s",
                    pair["pair_key"], session_id, character,
                )
                return False
            _, exists = self._character_card_snapshot_for_key(session_id, character)
            if not exists:
                logger.debug("encounter skip pair=%s: 角色卡不存在 %s/%s", pair["pair_key"], session_id, character)
                return False
            if not self._encounter_side_idle(session_id, self._session_now(session_id), lock_held=locks_held):
                return False
        return True

    def _encounter_side_idle(self, session_id: str, now: datetime, *, lock_held: bool = False) -> bool:
        """单侧空闲：非近期活跃、无推送进行中、无角色操作持锁、作息清醒时段。"""
        state = self._get_session_state(session_id)
        if self._is_recently_active(state):
            logger.debug("encounter idle check fail %s: recently active", session_id)
            return False
        if session_id in getattr(self, "_active_pushes", set()):
            logger.debug("encounter idle check fail %s: push in flight", session_id)
            return False
        if not lock_held and self.character_operation_lock(session_id).locked():
            logger.debug("encounter idle check fail %s: character op locked", session_id)
            return False
        schedule = self._character_schedule_minutes(session_id, now)
        now_minute = now.hour * 60 + now.minute
        if not int(schedule["wake"]) <= now_minute < int(schedule["sleep"]):
            logger.debug("encounter idle check fail %s: outside awake window", session_id)
            return False
        return True

    # ------------------------------------------------------------------
    # 编排主流程（持双方锁内原子完成；任一步失败整体中止，不写半成品）
    # ------------------------------------------------------------------
    async def _run_encounter(self, pair: dict[str, Any]) -> bool:
        pair_key = pair["pair_key"]
        if not self._cross_world_enabled():
            return False
        # 方向随机：一侧为访客 A，一侧为地主 B（编排调用与用量记 B 侧）。
        if random.random() < 0.5:
            visitor, host = pair["a"], pair["b"]
        else:
            visitor, host = pair["b"], pair["a"]
        if not self._encounter_pair_ready(pair):
            return False
        # 按 session_id 字典序取双方角色操作锁，防死锁；锁内复核空闲（竞态窗口收敛）。
        ordered = sorted({visitor["session_id"], host["session_id"]})
        lock_first = self.character_operation_lock(ordered[0])
        lock_second = self.character_operation_lock(ordered[1])
        async with lock_first:
            async with lock_second:
                if not self._encounter_pair_ready(pair, locks_held=True):
                    logger.info("encounter aborted under lock pair=%s: 不再空闲", pair_key)
                    return False
                return await self._execute_encounter(pair_key, visitor, host)

    async def _execute_encounter(
        self,
        pair_key: str,
        visitor: dict[str, str],
        host: dict[str, str],
    ) -> bool:
        sid_a, char_a = visitor["session_id"], visitor["character"]
        sid_b, char_b = host["session_id"], host["character"]
        now_b = self._session_now(sid_b)
        home_city = str(self._get_session_cfg(sid_a, "location", self.config.get("location", "")) or "").strip()
        city_b = str(self._get_session_cfg(sid_b, "location", self.config.get("location", "")) or "").strip()
        if not city_b:
            logger.info("encounter aborted pair=%s: 地主城市未配置", pair_key)
            return False

        venue_key = random.choice(ENCOUNTER_VENUE_PLACE_KEYS)
        venue_name = self._place_example(city_b, venue_key, index=random.randint(0, 4))
        weather = None
        try:
            weather = await self._fetch_weather(location=city_b)
        except Exception:
            logger.debug("encounter weather fetch failed", exc_info=True)
        history = self.app_store.list_encounters_for_pair(pair_key, limit=5)

        orchestration = await self._orchestrate_encounter(
            pair_key, visitor, host,
            city=city_b, venue_key=venue_key, venue_name=venue_name,
            now=now_b, weather=weather, history=history,
        )
        if orchestration is None:
            logger.info("encounter aborted pair=%s: 编排失败", pair_key)
            return False

        # 编排成功后才产生副作用：旅行覆盖 + 场所钉位 + 落库。
        state_a = self._get_session_state(sid_a)
        day_end = datetime.combine(now_b.date(), datetime.min.time(), tzinfo=now_b.tzinfo) + timedelta(days=1)
        session_schema.set_travel_override(
            state_a, city=city_b, until=day_end.timestamp(), home_city=home_city,
        )
        self._save_session_state(sid_a, state_a)
        self._set_character_place_for_key(
            sid_a, char_a, venue_key, venue_name, 0.95, source="encounter", name=venue_name,
        )

        ts = time.time()
        encounter_id = self.app_store.record_encounter(
            pair_key=pair_key,
            session_id_a=sid_a, character_a=char_a,
            session_id_b=sid_b, character_b=char_b,
            ts=ts, type="meeting",
            city=city_b, venue=venue_name,
            summary=orchestration["summary"],
            pov_a=orchestration["pov_a"], pov_b=orchestration["pov_b"],
            relationship=orchestration["relationship"],
        )
        self._record_encounter_memory(sid_a, char_a, orchestration["memory_a"], encounter_id, char_b)
        self._record_encounter_memory(sid_b, char_b, orchestration["memory_b"], encounter_id, char_a)
        when_text = now_b.strftime("%Y-%m-%d %H:%M")
        self._append_encounter_system_message(
            sid_a, city=city_b, venue=venue_name, other=char_b,
            pov=orchestration["pov_a"], relationship=orchestration["relationship"],
            when=when_text, role="访客（当天从外地来到这座城市）",
        )
        self._append_encounter_system_message(
            sid_b, city=city_b, venue=venue_name, other=char_a,
            pov=orchestration["pov_b"], relationship=orchestration["relationship"],
            when=when_text, role="地主（对方当天从你的城市来访）",
        )
        self._record_encounter_life_event(sid_a, char_a, char_b, venue_name, venue_key, now_b, orchestration["summary"], role="visitor")
        self._record_encounter_life_event(sid_b, char_b, char_a, venue_name, venue_key, now_b, orchestration["summary"], role="host")
        # system 历史事件只写内存 + chat_messages，两侧会话状态在这里统一落盘。
        for sid in (sid_a, sid_b):
            self._save_session_state(sid, self._get_session_state(sid))
        for sid, char in ((sid_a, char_a), (sid_b, char_b)):
            self._ulog(
                sid, "ENCOUNTER",
                f"pair={pair_key} id={encounter_id} city={city_b} venue={venue_name} with={char_b if sid == sid_a else char_a}",
            )
        logger.info(
            "encounter done pair=%s id=%s city=%s venue=%s visitor=%s/%s",
            pair_key, encounter_id, city_b, venue_name, sid_a, char_a,
        )
        return True

    # ------------------------------------------------------------------
    # 编排 LLM 调用（fast profile，session_id 用地主 B 的：profile 解析与用量记账）
    # ------------------------------------------------------------------
    def _encounter_character_brief(self, session_id: str, character: str) -> str:
        card, _exists = self._character_card_snapshot_for_key(session_id, character)
        lines = [f"名字: {character}"]
        for label, key in (("出处/作品", "series"), ("人设", "persona"), ("身份/职业", "occupation"), ("外貌特征", "appearance")):
            value = _clean_text(card.get(key), 300)
            if value:
                lines.append(f"{label}: {value}")
        outfit = _clean_text(card.get("outfit"), 200)
        if outfit:
            lines.append(f"当前穿着: {outfit}")
        return "\n".join(lines)

    def _build_encounter_prompt(
        self,
        visitor: dict[str, str],
        host: dict[str, str],
        *,
        city: str,
        venue_key: str,
        venue_name: str,
        now: datetime,
        weather: Any,
        history: list[dict[str, Any]],
    ) -> tuple[str, str]:
        char_a, char_b = visitor["character"], host["character"]
        system = (
            "你是跨会话角色邂逅编排器。两个不同用户的 AI 角色（配对配置已声明同一世界观）即将在一个公共场所见面。"
            "你一次性编排整个场景，只输出严格 JSON，不要解释、不要输出 JSON 以外内容。\n"
            "规则:\n"
            "- 两个角色都属于同一世界观，场景自然合理，不要出现跨世界观元素。\n"
            "- summary 是第三人称场景纪要（80-200 字），交代见面经过、聊了些什么、如何分别。\n"
            "- pov_a / pov_b 是各自视角的第一人称小结（50-120 字），只写该角色亲眼所见所感；"
            "双方视角可以不完全对称（各自注意到不同的细节）。\n"
            "- relationship 用一句话声明本次见面后的关系阶段与承接钩子"
            "（如「初识，交换了称呼，约定下次再见」「旧友重逢，解开了之前的误会」），供下次编排承接。\n"
            "- memory_a / memory_b 是各自值得长期记住的事实（一句话，可为空字符串）；"
            "只写稳定事实/关系进展/约定，不写一时情绪或流水账。\n"
            "- 若输入含既往邂逅记录，本次必须是「重逢」：承接上次的关系阶段推进，不要当成初遇重写。\n"
            "- 内容健康得体（公共场所初次/普通见面），不要写亲密或露骨内容。\n"
            "只输出: {\"summary\":\"\",\"pov_a\":\"\",\"pov_b\":\"\",\"relationship\":\"\",\"memory_a\":\"\",\"memory_b\":\"\"}"
        )
        time_text = self._format_time_context(host["session_id"], now=now, weather=weather)
        lines = [
            f"城市: {city}（地主 {char_b} 的城市，{char_a} 当天旅行至此）",
            f"时间: {now.strftime('%Y-%m-%d %H:%M')}（{time_text or '未知'}）",
            f"场所: {venue_name}（{venue_key} 类公共场所）",
            "",
            f"角色A（访客，pov_a/memory_a 的视角）:\n{self._encounter_character_brief(visitor['session_id'], char_a)}",
            "",
            f"角色B（地主，pov_b/memory_b 的视角）:\n{self._encounter_character_brief(host['session_id'], char_b)}",
        ]
        if history:
            lines.append("")
            lines.append("既往邂逅记录（按时间从新到旧；本次编排必须承接，不得当成初遇）:")
            for item in history:
                stamp = ""
                try:
                    stamp = datetime.fromtimestamp(float(item.get("ts") or 0)).strftime("%Y-%m-%d")
                except Exception:
                    stamp = ""
                lines.append(
                    f"- [{stamp}] {item.get('city') or ''} {item.get('venue') or ''}: "
                    f"{_clean_text(item.get('summary'), 200)}（关系: {_clean_text(item.get('relationship'), 100) or '未记录'}）"
                )
        return system, "\n".join(lines)

    async def _orchestrate_encounter(
        self,
        pair_key: str,
        visitor: dict[str, str],
        host: dict[str, str],
        *,
        city: str,
        venue_key: str,
        venue_name: str,
        now: datetime,
        weather: Any,
        history: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        system, user = self._build_encounter_prompt(
            visitor, host,
            city=city, venue_key=venue_key, venue_name=venue_name,
            now=now, weather=weather, history=history,
        )
        parsed = None
        for attempt in (1, 2):
            try:
                raw = await self._call_llm(
                    system, user,
                    temp=0.7, tag="encounter", purpose="image",
                    session_id=host["session_id"],
                )
                parsed = self._parse_llm_json(raw)
                break
            except Exception as exc:
                logger.warning(
                    "encounter orchestration failed pair=%s attempt=%s: %s",
                    pair_key, attempt, exc,
                )
        if not isinstance(parsed, dict):
            return None
        result = {
            key: _clean_text(parsed.get(key), 600 if key == "summary" else _ENCOUNTER_TEXT_LIMIT)
            for key in ("summary", "pov_a", "pov_b", "relationship", "memory_a", "memory_b")
        }
        # summary/pov 是落库的最小完整度；缺了宁可整体中止，不写半成品邂逅。
        if not result["summary"] or not result["pov_a"] or not result["pov_b"]:
            logger.warning("encounter orchestration incomplete pair=%s: %r", pair_key, parsed)
            return None
        return result

    # ------------------------------------------------------------------
    # 落库（编排全部成功后才调用；单项失败记日志，不中断其余落库项）
    # ------------------------------------------------------------------
    def _record_encounter_memory(
        self,
        session_id: str,
        character: str,
        memory_text: str,
        encounter_id: int,
        other: str,
    ) -> None:
        """记忆建议过长期记忆范围过滤后落 kind=event，source=encounter:<id>。"""
        text = _clean_text(memory_text, 300)
        if not text:
            return
        try:
            if not self._is_long_memory_in_scope(session_id, "event", text, [], character):
                logger.info("encounter memory filtered session=%s: %s", session_id, text)
                return
            mid = self.memory.add_memory(
                session_id,
                "event",
                text,
                character=character,
                importance=4,
                tags=["邂逅", other],
                source=f"encounter:{encounter_id}",
            )
            self._ulog(session_id, "MEM+", f"#{mid} 邂逅[event]: {text}")
        except Exception:
            logger.warning("encounter memory write failed session=%s", session_id, exc_info=True)

    def _record_encounter_life_event(
        self,
        session_id: str,
        character: str,
        other: str,
        venue_name: str,
        venue_key: str,
        now: datetime,
        summary: str,
        *,
        role: str,
    ) -> None:
        """life_plan 落一条已完成事件 + npcs[] 登记对方角色；无当日计划时跳过（不为此新建）。"""
        try:
            row = self._load_life_plan_row(session_id, character)
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                return
            today = payload.get("today")
            today_date = self._life_today_date(session_id, now)
            if not isinstance(today, dict) or str(today.get("date") or "") != today_date:
                return
            text = (
                f"在{venue_name}与{other}相遇" if role == "host" else f"去{other}的城市旅行，在{venue_name}与{other}相遇"
            )
            events = today.setdefault("events", [])
            events.append({
                "time_hint": self._life_time_hint_for_dt(now),
                "text": text,
                "place_key": venue_key,
                "status": "done",
                "side_note": _clean_text(summary, 180),
            })
            npcs = payload.setdefault("npcs", [])
            if isinstance(npcs, list) and not any(
                isinstance(npc, dict) and str(npc.get("name") or "").strip() == other for npc in npcs
            ):
                npcs.append({"name": other, "source": "encounter"})
            self._save_life_plan_payload(session_id, character, payload)
        except Exception:
            logger.warning("encounter life plan write failed session=%s", session_id, exc_info=True)
