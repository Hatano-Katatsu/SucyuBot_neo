"""角色邂逅编排器：跨会话互访与同会话角色互动推送。

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

同会话扩展由用户在动线页选择参与角色和每日上限；它作为普通每日推送的一种动态方向，
先为被抽中的非活动角色建立今日生活线，再编排互动。图片发送成功后才把事件分别写入双方
角色空间并扣额度，且沿用单角色生图管线，让非活动角色保持在画外。
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
from .world_runtime import PLACE_TYPES

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

        配置文件格式（列表）：
        [{"a": {"chat_id": 123456, "character": "角色名A"},
          "b": {"chat_id": 654321, "character": "角色名B"}}]
        WebUI 文本格式（字符串）：每行一对 `chat_id:角色名 = chat_id:角色名`，
        兼容全角标点，# 开头为注释行。
        """
        raw = self.config.get("cross_world_pairs") or []
        if isinstance(raw, str):
            raw = self._parse_cross_world_pair_lines(raw)
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

    @staticmethod
    def _parse_cross_world_pair_lines(text: str) -> list[dict[str, Any]]:
        """解析 WebUI 文本格式的配对：每行 `chat_id:角色名 = chat_id:角色名`。

        全角冒号/等号先归一为半角；空行与 # 注释行跳过；格式不符的行跳过并告警。
        """
        items: list[dict[str, Any]] = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip().replace("：", ":").replace("＝", "=")
            if not line or line.startswith("#"):
                continue
            left, sep, right = line.partition("=")
            if not sep:
                logger.warning("cross_world_pairs 行缺少 = 分隔符，已跳过: %r", raw_line)
                continue
            sides: dict[str, dict[str, str]] = {}
            for side, part in (("a", left), ("b", right)):
                chat_id, colon, character = part.strip().partition(":")
                if not colon or not chat_id.strip() or not character.strip():
                    break
                sides[side] = {"chat_id": chat_id.strip(), "character": character.strip()}
            if len(sides) != 2:
                logger.warning("cross_world_pairs 行格式应为 chat_id:角色名 = chat_id:角色名，已跳过: %r", raw_line)
                continue
            items.append({"a": sides["a"], "b": sides["b"]})
        return items

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
    # 同会话角色互动推送配置与候选
    # ------------------------------------------------------------------
    @staticmethod
    def _local_character_ui_key(character_key: str) -> str:
        return character_key or "__default__"

    @staticmethod
    def _local_character_internal_key(character_key: Any) -> str:
        text = str(character_key or "").strip()
        return "" if text == "__default__" else text

    def _local_character_display_name(self, session_id: str, character_key: str) -> str:
        card, _exists = self._character_card_snapshot_for_key(session_id, character_key)
        return _clean_text(
            card.get("character") or card.get("bot_name") or character_key or "默认角色",
            80,
        ) or "默认角色"

    def _local_character_options(self, session_id: str) -> list[dict[str, Any]]:
        """列出默认角色、角色池和当前活动角色，内部键与 UI 默认角色令牌分离。"""
        state = self._get_session_state(session_id)
        active_key = self._context_character_key(session_id)
        keys = [""]
        for raw_key, raw_card in session_schema.get_saved_characters(state).items():
            key = str(raw_key or "").strip()
            if not key or (isinstance(raw_card, dict) and raw_card.get("is_default")):
                continue
            if key not in keys:
                keys.append(key)
        if active_key and active_key not in keys:
            keys.append(active_key)
        options = []
        for key in keys:
            _card, exists = self._character_card_snapshot_for_key(session_id, key)
            if not exists:
                continue
            options.append({
                "key": self._local_character_ui_key(key),
                "character_key": key,
                "name": self._local_character_display_name(session_id, key),
                "active": key == active_key,
            })
        return options

    def _local_character_schedule_minutes(
        self,
        session_id: str,
        character_key: str,
        local_dt: datetime,
    ) -> dict[str, int]:
        active_key = self._context_character_key(session_id)
        if character_key == active_key:
            schedule = self._character_schedule_minutes(session_id, local_dt)
            return {"wake": int(schedule["wake"]), "sleep": int(schedule["sleep"])}
        card, exists = self._character_card_snapshot_for_key(session_id, character_key)
        if not exists:
            return {"wake": 0, "sleep": 0}
        wake_weekend = self._is_weekend_schedule_day(local_dt)
        sleep_weekend = self._is_weekend_schedule_day(local_dt + timedelta(days=1))
        wake_key = "weekend_wake_time" if wake_weekend else "workday_wake_time"
        sleep_key = "weekend_sleep_time" if sleep_weekend else "workday_sleep_time"
        wake = self._parse_schedule_time_minutes(
            card.get(wake_key) or self.config.get(wake_key, "08:00"),
            8 * 60,
        )
        sleep = self._parse_schedule_time_minutes(
            card.get(sleep_key) or self.config.get(sleep_key, "23:50"),
            23 * 60 + 50,
        )
        return {"wake": wake, "sleep": sleep}

    def _local_character_is_awake(self, session_id: str, character_key: str, local_dt: datetime) -> bool:
        schedule = self._local_character_schedule_minutes(session_id, character_key, local_dt)
        wake, sleep = int(schedule["wake"]), int(schedule["sleep"])
        minute = local_dt.hour * 60 + local_dt.minute
        if sleep >= wake:
            return wake <= minute < sleep
        return minute >= wake or minute < sleep

    def _local_interaction_push_status(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """返回 WebUI 状态与调度候选；跨日时旧计数只按 0 展示，不提前写盘。"""
        state = self._get_session_state(session_id)
        local_dt = now or self._session_now(session_id)
        today = local_dt.date().isoformat()
        settings = session_schema.get_character_interaction_push(state)
        daily_limit = int(settings.get("daily_limit") or 0)
        count = int(settings.get("count") or 0) if settings.get("date") == today else 0
        remaining = max(0, daily_limit - count)
        selected = set(settings.get("character_keys") or [])
        active_key = self._context_character_key(session_id)
        roles = self._local_character_options(session_id)
        for role in roles:
            role["selected"] = role["character_key"] in selected
            role["awake"] = self._local_character_is_awake(session_id, role["character_key"], local_dt)
        candidates = [
            role for role in roles
            if role["character_key"] != active_key and role["selected"] and role["awake"]
        ]
        active_awake = any(
            role["character_key"] == active_key and role["awake"] for role in roles
        )
        world_enabled = bool(
            hasattr(self, "_world_runtime_enabled") and self._world_runtime_enabled()
        )
        life_enabled = bool(
            hasattr(self, "_life_plan_enabled") and self._life_plan_enabled(session_id)
        )
        active_selected = active_key in selected
        available = bool(
            daily_limit > 0 and remaining > 0 and active_selected and active_awake and candidates
            and world_enabled and life_enabled
        )
        if daily_limit <= 0:
            reason = "每日上限为 0，功能关闭"
        elif remaining <= 0:
            reason = "今日互动额度已用完"
        elif not active_selected:
            reason = "当前活动角色未加入互动角色"
        elif not active_awake:
            reason = "当前活动角色处于休息时段"
        elif not candidates:
            reason = "没有已选择且当前清醒的非活动角色"
        elif not world_enabled or not life_enabled:
            reason = "自动动线或生活线未启用"
        else:
            reason = "可作为本次普通每日推送的话题方向"
        return {
            "enabled": daily_limit > 0,
            "available": available,
            "reason": reason,
            "date": today,
            "daily_limit": daily_limit,
            "count": count,
            "remaining": remaining,
            "active_character_key": active_key,
            "active_key": self._local_character_ui_key(active_key),
            "active_selected": active_selected,
            "active_awake": active_awake,
            "selected_keys": [self._local_character_ui_key(key) for key in settings.get("character_keys") or []],
            "roles": roles,
            "candidates": candidates,
        }

    def _configure_local_interaction_push(
        self,
        session_id: str,
        character_keys: list[Any],
        daily_limit: int,
    ) -> dict[str, Any]:
        """保存参与角色和每日上限；修改配置不重置已经使用的当日额度。"""
        if isinstance(daily_limit, bool) or (
            isinstance(daily_limit, float)
            and (not math.isfinite(daily_limit) or not daily_limit.is_integer())
        ):
            raise ValueError("每日互动上限必须是整数")
        try:
            limit = int(daily_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("每日互动上限必须是整数") from exc
        if not 0 <= limit <= 20:
            raise ValueError("每日互动上限必须在 0 到 20 之间")
        if not isinstance(character_keys, list):
            raise ValueError("参与角色必须是列表")
        valid = {role["character_key"] for role in self._local_character_options(session_id)}
        normalized: list[str] = []
        for raw in character_keys:
            key = self._local_character_internal_key(raw)
            if key not in valid:
                raise ValueError(f"角色不存在: {raw}")
            if key not in normalized:
                normalized.append(key)
        if limit > 0 and len(normalized) < 2:
            raise ValueError("启用角色互动时至少选择两个角色")
        state = self._get_session_state(session_id)
        previous = session_schema.get_character_interaction_push(state)
        session_schema.set_character_interaction_push(state, {
            "character_keys": normalized,
            "daily_limit": limit,
            "date": previous.get("date") or "",
            "count": previous.get("count") or 0,
        })
        self._save_session_state(session_id, state)
        return self._local_interaction_push_status(session_id)

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
        display_name = card.get("character") or card.get("bot_name") or character or "默认角色"
        lines = [f"名字: {display_name}"]
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
    # 同会话角色互动推送：先准备编排，图片成功后再提交双方历史
    # ------------------------------------------------------------------
    def _local_life_plan_context(self, session_id: str, character_key: str) -> str:
        row = self._load_life_plan_row(session_id, character_key)
        payload = row.get("payload") if isinstance(row, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        today = payload.get("today") if isinstance(payload.get("today"), dict) else {}
        lines = []
        texture = _clean_text(today.get("texture"), 240)
        if texture:
            lines.append(f"今日底色: {texture}")
        for event in today.get("events") or []:
            if not isinstance(event, dict):
                continue
            text = _clean_text(event.get("text"), 180)
            if text:
                lines.append(
                    f"- {event.get('time_hint') or 'unspecified'} / "
                    f"{event.get('place_key') or 'unknown'} / {event.get('status') or 'planned'}: {text}"
                )
        return "\n".join(lines[:8]) or "今日生活线已建立，但没有额外片段。"

    def _local_character_route_snapshot(
        self,
        session_id: str,
        character_key: str,
        local_dt: datetime,
        weather: Any,
        *,
        active_world: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """从指定角色今日生活线取当前时段地点，缺少片段时按其职业动线推断。"""
        active_key = self._context_character_key(session_id)
        if character_key == active_key and isinstance(active_world, dict):
            place = active_world.get("character_place")
            if isinstance(place, dict) and place.get("key") in PLACE_TYPES:
                return dict(place)
        row = self._load_life_plan_row(session_id, character_key)
        payload = row.get("payload") if isinstance(row, dict) else {}
        today = payload.get("today") if isinstance(payload, dict) and isinstance(payload.get("today"), dict) else {}
        time_hint = self._life_time_hint_for_dt(local_dt)
        for event in today.get("events") or []:
            if not isinstance(event, dict) or event.get("time_hint") != time_hint:
                continue
            place_key = str(event.get("place_key") or "").strip()
            if place_key not in PLACE_TYPES:
                continue
            meta = PLACE_TYPES[place_key]
            city = self._session_city(session_id)
            return {
                "key": place_key,
                "label": meta.get("label", place_key),
                "name": self._place_example(city, place_key, index=random.randint(0, 4)),
                "public": bool(meta.get("public")),
                "indoor": bool(meta.get("indoor")),
                "views": list(meta.get("views") or []),
                "activities": list(meta.get("activities") or []),
            }
        snapshot = self._life_plan_character_snapshot(session_id, character_key)
        profile = ((snapshot.get("materials") or {}).get("life_profile") or {})
        place = self._place_for_time(
            self._session_city(session_id),
            local_dt,
            weather,
            mode="normal",
            profile=profile,
        )
        return dict(place or {})

    def _local_interaction_venue(
        self,
        session_id: str,
        active_route: dict[str, Any],
        other_route: dict[str, Any],
    ) -> tuple[str, str]:
        """优先让双方在共同公共动线相遇，否则选择一处自然的公共会面地点。"""
        active_key = str(active_route.get("key") or "")
        other_key = str(other_route.get("key") or "")
        if active_key and active_key == other_key and PLACE_TYPES.get(active_key, {}).get("public"):
            venue_key = active_key
            venue_name = str(active_route.get("name") or other_route.get("name") or "").strip()
        else:
            route_keys = [
                key for key in (other_key, active_key)
                if key in ENCOUNTER_VENUE_PLACE_KEYS and PLACE_TYPES.get(key, {}).get("public")
            ]
            venue_key = random.choice(route_keys or list(ENCOUNTER_VENUE_PLACE_KEYS))
            venue_name = ""
        if not venue_name:
            venue_name = self._place_example(
                self._session_city(session_id), venue_key, index=random.randint(0, 4),
            )
        return venue_key, venue_name

    async def _prepare_local_character_interaction_push(
        self,
        session_id: str,
        local_dt: datetime,
        *,
        weather: Any = None,
        active_world: dict[str, Any] | None = None,
        target_character: str | None = None,
    ) -> dict[str, Any] | None:
        """建立非活动角色今日动线并编排互动；这里不扣额度、不写邂逅历史。"""
        status = self._local_interaction_push_status(session_id, now=local_dt)
        if not status.get("available"):
            return None
        candidates = status.get("candidates") or []
        if target_character is not None:
            target_key = self._local_character_internal_key(target_character)
            target = next((item for item in candidates if item.get("character_key") == target_key), None)
            if target is None:
                return None
        else:
            target = random.choice(candidates)
            target_key = str(target.get("character_key") or "")
        ensure = await self.ensure_life_plan_for_today(
            session_id,
            force=False,
            reason="local-character-interaction",
            character_key=target_key,
        )
        row = self._load_life_plan_row(session_id, target_key)
        today = ((row or {}).get("payload") or {}).get("today") or {}
        expected_date = self._life_today_date(session_id, local_dt)
        if ensure.get("status") in {"failed", "stale", "skipped"} or today.get("date") != expected_date:
            logger.info(
                "local character interaction skipped: target life plan unavailable session=%s character=%s status=%s",
                session_id, target_key, ensure.get("status"),
            )
            return None
        active_key = str(status.get("active_character_key") or "")
        active_route = self._local_character_route_snapshot(
            session_id, active_key, local_dt, weather, active_world=active_world,
        )
        other_route = self._local_character_route_snapshot(
            session_id, target_key, local_dt, weather,
        )
        venue_key, venue_name = self._local_interaction_venue(session_id, active_route, other_route)
        pair_key = self._encounter_pair_key(session_id, active_key, session_id, target_key)
        history = self.app_store.list_encounters_for_pair(pair_key, limit=5)
        orchestration = await self._orchestrate_local_character_interaction(
            session_id,
            active_key,
            target_key,
            local_dt=local_dt,
            weather=weather,
            active_route=active_route,
            other_route=other_route,
            venue_key=venue_key,
            venue_name=venue_name,
            history=history,
        )
        if not orchestration:
            return None
        return {
            **orchestration,
            "pair_key": pair_key,
            "date": expected_date,
            "active_character": active_key,
            "other_character": target_key,
            "active_name": self._local_character_display_name(session_id, active_key),
            "other_name": self._local_character_display_name(session_id, target_key),
            "city": self._session_city(session_id),
            "venue_key": venue_key,
            "venue_name": venue_name,
            "active_route": active_route,
            "other_route": other_route,
            "local_dt": local_dt,
        }

    async def _orchestrate_local_character_interaction(
        self,
        session_id: str,
        active_character: str,
        other_character: str,
        *,
        local_dt: datetime,
        weather: Any,
        active_route: dict[str, Any],
        other_route: dict[str, Any],
        venue_key: str,
        venue_name: str,
        history: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        active_name = self._local_character_display_name(session_id, active_character)
        other_name = self._local_character_display_name(session_id, other_character)
        system = (
            "你是同一用户角色池内的角色互动编排器。当前活动角色会在一次普通每日图片推送里，"
            "自然提到自己刚刚与一个非活动角色发生的互动。只输出严格 JSON，不要解释。\n"
            "规则:\n"
            "- 两者都是独立角色；非活动角色不是人类用户，绝不能把用户写成该角色。\n"
            "- 互动必须承接两者各自今日生活线和当前动线，在给定公共场所自然发生。\n"
            "- summary 用第三人称写 80-200 字完整经过；pov_active / pov_other 分别用第一人称写各自所见所感。\n"
            "- relationship 写关系阶段和下次承接钩子；memory_active / memory_other 只写值得长期记住的稳定事实，可为空。\n"
            "- push_caption 是活动角色发给用户的单段单行第一人称图片说明，明确提到另一角色和刚发生的事，"
            "不向用户索要回复，不冒充实时双角色群聊。\n"
            "- scene_hint 只描述活动角色在互动现场或刚分别后的单人画面；另一角色必须在画外，"
            "不要出现双人同框、第二个身体或第二套角色外貌。\n"
            "- 若有既往邂逅记录，本次按重逢承接关系，不得重写成初遇。\n"
            "输出: {\"summary\":\"\",\"pov_active\":\"\",\"pov_other\":\"\","
            "\"relationship\":\"\",\"memory_active\":\"\",\"memory_other\":\"\","
            "\"push_caption\":\"\",\"scene_hint\":\"\"}"
        )
        lines = [
            f"时间: {local_dt.strftime('%Y-%m-%d %H:%M')}",
            f"城市/场所: {self._session_city(session_id)} / {venue_name}（{venue_key}）",
            f"天气: {self._weather_text(weather) if hasattr(self, '_weather_text') else weather or '未知'}",
            "",
            f"当前活动角色 {active_name}:\n{self._encounter_character_brief(session_id, active_character)}",
            f"当前动线: {active_route.get('label') or active_route.get('key') or '未知'} · {active_route.get('name') or ''}",
            self._local_life_plan_context(session_id, active_character),
            "",
            f"被抽中的非活动角色 {other_name}:\n{self._encounter_character_brief(session_id, other_character)}",
            f"当前动线: {other_route.get('label') or other_route.get('key') or '未知'} · {other_route.get('name') or ''}",
            self._local_life_plan_context(session_id, other_character),
        ]
        if history:
            lines.extend(["", "两者既往邂逅记录（新到旧）:"])
            for item in history:
                lines.append(
                    f"- {_clean_text(item.get('summary'), 200)}"
                    f"（关系: {_clean_text(item.get('relationship'), 120) or '未记录'}）"
                )
        purpose = "fast" if self.has_llm_config("fast", session_id) else "image"
        parsed = None
        for attempt in (1, 2):
            try:
                raw = await self._call_llm(
                    system,
                    "\n".join(lines),
                    temp=0.7,
                    tag="local_character_interaction",
                    purpose=purpose,
                    session_id=session_id,
                    max_tokens=1200,
                )
                parsed = self._parse_llm_json(raw)
                break
            except Exception as exc:
                logger.warning(
                    "local character interaction orchestration failed session=%s attempt=%s: %s",
                    session_id, attempt, exc,
                )
        if not isinstance(parsed, dict):
            return None
        result = {
            key: _clean_text(parsed.get(key), 600 if key == "summary" else _ENCOUNTER_TEXT_LIMIT)
            for key in (
                "summary", "pov_active", "pov_other", "relationship",
                "memory_active", "memory_other", "push_caption", "scene_hint",
            )
        }
        required = ("summary", "pov_active", "pov_other", "push_caption", "scene_hint")
        if any(not result[key] for key in required):
            logger.warning("local character interaction incomplete session=%s: %r", session_id, parsed)
            return None
        return result

    def _local_interaction_push_system_prompt(self, event: dict[str, Any]) -> str:
        """把已编排事件固定给生图规划器；图片仍保持单活动角色管线。"""
        return (
            "【同会话角色互动推送，优先级高】\n"
            f"当前活动角色刚在{event.get('venue_name') or '公共场所'}与"
            f"{event.get('other_name') or '另一个角色'}发生了以下真实互动："
            f"{event.get('summary') or ''}\n"
            f"单人画面提示：{event.get('scene_hint') or ''}\n"
            "规划图片时只让当前活动角色入画，另一角色必须在画外；不要生成双人同框、第二个身体或第二套外貌。"
            "caption 围绕这次互动，但最终发送会采用编排器已写好的第一人称说明。"
        )

    def _commit_local_character_interaction_push(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> bool:
        """图片发出后提交：扣额度，并分别写双方历史、记忆、生活线和地点。"""
        state = self._get_session_state(session_id)
        active_key = self._context_character_key(session_id)
        other_key = str(event.get("other_character") or "")
        if active_key != str(event.get("active_character") or "") or active_key == other_key:
            return False
        settings = session_schema.get_character_interaction_push(state)
        selected = set(settings.get("character_keys") or [])
        today = self._session_now(session_id).date().isoformat()
        count = int(settings.get("count") or 0) if settings.get("date") == today else 0
        daily_limit = int(settings.get("daily_limit") or 0)
        valid_keys = {role["character_key"] for role in self._local_character_options(session_id)}
        if (
            daily_limit <= 0 or count >= daily_limit
            or active_key not in selected or other_key not in selected
            or active_key not in valid_keys or other_key not in valid_keys
        ):
            return False
        active_name = str(event.get("active_name") or self._local_character_display_name(session_id, active_key))
        other_name = str(event.get("other_name") or self._local_character_display_name(session_id, other_key))
        ts = time.time()
        encounter_id = self.app_store.record_encounter(
            pair_key=str(event.get("pair_key") or self._encounter_pair_key(session_id, active_key, session_id, other_key)),
            session_id_a=session_id,
            character_a=active_key,
            session_id_b=session_id,
            character_b=other_key,
            ts=ts,
            type="local_push",
            city=str(event.get("city") or self._session_city(session_id)),
            venue=str(event.get("venue_name") or ""),
            summary=str(event.get("summary") or ""),
            pov_a=str(event.get("pov_active") or ""),
            pov_b=str(event.get("pov_other") or ""),
            relationship=str(event.get("relationship") or ""),
        )
        self._record_encounter_memory(
            session_id, active_key, str(event.get("memory_active") or ""), encounter_id, other_name,
        )
        self._record_encounter_memory(
            session_id, other_key, str(event.get("memory_other") or ""), encounter_id, active_name,
        )
        local_dt = event.get("local_dt")
        if not isinstance(local_dt, datetime):
            local_dt = self._session_now(session_id)
        when_text = local_dt.strftime("%Y-%m-%d %H:%M")
        city = str(event.get("city") or self._session_city(session_id))
        venue_name = str(event.get("venue_name") or "")
        relationship = str(event.get("relationship") or "")
        self._append_encounter_system_message(
            session_id,
            city=city,
            venue=venue_name,
            other=other_name,
            pov=str(event.get("pov_active") or ""),
            relationship=relationship,
            when=when_text,
            role="同一用户角色池中的非活动角色",
            character_key=active_key,
            event_scope="local_push",
        )
        self._append_encounter_system_message(
            session_id,
            city=city,
            venue=venue_name,
            other=active_name,
            pov=str(event.get("pov_other") or ""),
            relationship=relationship,
            when=when_text,
            role="同一用户角色池中的当前活动角色",
            character_key=other_key,
            event_scope="local_push",
        )
        venue_key = str(event.get("venue_key") or "")
        summary = str(event.get("summary") or "")
        self._record_encounter_life_event(
            session_id, active_key, other_name, venue_name, venue_key, local_dt, summary, role="local",
        )
        self._record_encounter_life_event(
            session_id, other_key, active_name, venue_name, venue_key, local_dt, summary, role="local",
        )
        if venue_key in PLACE_TYPES:
            self._set_character_place_for_key(
                session_id, active_key, venue_key, venue_name, 0.95,
                source="local-encounter", name=venue_name,
            )
            self._set_character_place_for_key(
                session_id, other_key, venue_key, venue_name, 0.95,
                source="local-encounter", name=venue_name,
            )
        session_schema.set_character_interaction_push(state, {
            "character_keys": list(settings.get("character_keys") or []),
            "daily_limit": daily_limit,
            "date": today,
            "count": count + 1,
        })
        self._save_session_state(session_id, state)
        self._ulog(
            session_id,
            "ENCOUNTER",
            f"local_push id={encounter_id} active={active_name} with={other_name} "
            f"venue={venue_name} daily={count + 1}/{daily_limit}",
        )
        return True

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
            if role == "local":
                text = f"按今日动线来到{venue_name}，与{other}相遇并互动"
            elif role == "host":
                text = f"在{venue_name}与{other}相遇"
            else:
                text = f"去{other}的城市旅行，在{venue_name}与{other}相遇"
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
