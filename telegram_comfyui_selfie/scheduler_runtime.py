from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from . import session_schema
from .defaults import DEFAULT_CONFIG, WEEKDAY_NAMES
from .image_planning import (
    VALID_VIEWS,
    format_dialog_context,
    format_sent_photo_context,
    normalize_scene_visual_subject,
    scene_implies_mirror_selfie,
)

logger = logging.getLogger(__name__)


class SchedulerRuntimeMixin:
    def _format_scene_continuity_context(
        self,
        state: dict[str, Any],
        session_id: str = "",
        now: datetime | None = None,
    ) -> str:
        try:
            ttl = max(0.25, float(self.config.get("push_continuity_hours", "2") or "2")) * 3600
        except Exception:
            ttl = 2 * 3600
        now_ts = now.timestamp() if isinstance(now, datetime) else time.time()
        latest = float(session_schema.get_last_interaction(state) or 0)
        latest = max(latest, session_schema.get_last_message_time(state))
        for msg in session_schema.get_recent_message_history(state):
            latest = max(latest, float(msg.get("time", 0) or 0))
        for photo in session_schema.get_sent_photos_history(state):
            latest = max(latest, float(photo.get("timestamp", 0) or 0))
        if not latest or now_ts - latest > ttl:
            return ""

        dialog = format_dialog_context(self, state, session_id, limit=8)
        photos = format_sent_photo_context(self, state, session_id, limit=3)
        parts = []
        if dialog:
            parts.append("最近对话:\n" + dialog)
        if photos:
            parts.append("最近发过的图片:\n" + photos)
        if not parts:
            return ""
        return (
            "短期连续性上下文（优先级高于自动动线；用于承接刚才停住的场景）:\n"
            + "\n\n".join(parts)
            + "\n连续性要求: 主动推送应优先承接最近已建立的地点、未完成约定、情绪和可见状态。"
            "如果现实动线与这里冲突，短时间内以连续性为主；确实需要换地点时必须写出自然过渡，"
            "例如离开咖啡店、去车站、回家路上，而不要突然跳到无关场景。"
        )

    async def _llm_write_scene(self, mode, weather, weekday, time_period, recent_chat=None, session_id="", now=None, weather_data=None):
        from .image_planning import plan_roleplay_image
        if not self.has_llm_config("image"):
            return None, None, None, None
        plan = await plan_roleplay_image(
            self, session_id, mode=mode or "normal",
            weather_data=weather_data, now=now,
        )
        return (
            plan.get("scene") or "",
            plan.get("caption") or "",
            plan.get("new_appearance_tags") or "",
            plan.get("view") or "",
        )
    # ---------------------------------------------------------------------
    # Weather / scheduler
    # ---------------------------------------------------------------------
    async def _fetch_weather(self, location: str = "", session_id: str = ""):
        if not location:
            now = time.time()
            loc = self._get_session_cfg(session_id, "location", self.config.get("location", "上海"))
            key = session_id or "__default__"
            cached = self._weather_caches.get(key)
            if cached and now - cached["ts"] < 1800:
                return cached["data"]
            location = loc
            cache_key = key
        else:
            cache_key = None
        try:
            encoded = urllib.parse.quote(location.strip())
            async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(f"https://wttr.in/{encoded}?format=j1&lang=zh-cn", headers={"User-Agent": "curl/7.81.0"}) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
            cur = data.get("current_condition", [{}])[0]
            desc = ""
            for key in ("lang_zh-cn", "lang_zh", "lang_zh_cn"):
                items = cur.get(key, [])
                if items:
                    desc = items[0].get("value", "")
                    break
            if not desc:
                desc = cur.get("weatherDesc", [{}])[0].get("value", "")
            nearest = data.get("nearest_area", [])
            city, lon, lat = location, None, None
            if nearest:
                names = nearest[0].get("areaName", [])
                if names:
                    city = names[0].get("value", location)
                lon = nearest[0].get("longitude")
                lat = nearest[0].get("latitude")
            astronomy = {}
            daily = data.get("weather", [])
            if daily and isinstance(daily[0], dict):
                astronomy_items = daily[0].get("astronomy", [])
                if astronomy_items and isinstance(astronomy_items[0], dict):
                    astronomy = astronomy_items[0]
            weather = {
                "desc": desc,
                "code": cur.get("weatherCode", "0"),
                "temp": cur.get("temp_C", "?"),
                "city": city,
                "lon": lon,
                "lat": lat,
                "sunrise": astronomy.get("sunrise", ""),
                "sunset": astronomy.get("sunset", ""),
            }
            if cache_key:
                self._weather_caches[cache_key] = {"data": weather, "ts": time.time()}
            return weather
        except Exception as exc:
            logger.warning("weather fetch failed: %s", exc)
            return None

    def _schedule_weather_refresh(self, session_id: str) -> bool:
        """聊天时若天气缓存已过期（>30 分钟），后台异步刷新。

        纯文字聊天本身从不拉天气（时间/光照/世界状态只读缓存），不刷新会让天气停在最近一次
        生图/推送/手动查询时的值（常常是早安推送那次）。这里在缓存过期时 fire-and-forget 拉一次，
        不阻塞当前回复，下一轮即用上新天气。`_fetch_weather` 内部已捕获异常并写回缓存。
        """
        cached = self._weather_caches.get(session_id or "__default__")
        ts = float(cached.get("ts", 0)) if isinstance(cached, dict) else 0.0
        if time.time() - ts < 1800:
            return False
        try:
            asyncio.create_task(self._fetch_weather(session_id=session_id))
        except RuntimeError:
            return False  # 无运行中的事件循环（极少见）
        return True

    @staticmethod
    def _is_bad_weather(w) -> bool:
        return bool(w and w.get("code", "0") in {"200", "299", "300", "399", "500", "599", "600", "699", "700", "799"})

    def _dream_idle_seconds(self) -> float:
        try:
            return max(0.0, float(self.config.get("dream_idle_hours", "2") or 2) * 3600)
        except Exception:
            return 7200.0

    def _dream_morning_hour(self) -> int:
        try:
            return max(0, min(23, int(self.config.get("dream_morning_hour", "8") or 8)))
        except Exception:
            return 8

    def _dream_diary_date(self, local_dt: datetime, *, force_previous_day: bool = False) -> str:
        if force_previous_day or local_dt.hour < self._dream_morning_hour():
            return (local_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        return local_dt.strftime("%Y-%m-%d")

    def _should_run_dream_before_push(self, session_id: str, state: dict[str, Any]) -> bool:
        last = float(session_schema.get_last_interaction(state) or 0)
        if not last or time.time() - last < self._dream_idle_seconds():
            return False
        key = self._context_character_key(session_id) if hasattr(self, "_context_character_key") else self._memory_character(session_id)
        try:
            meta = self.app_store.get_context_meta(session_id, key)
        except Exception:
            return False
        return int(meta.get("last_checkpoint_message_id") or 0) > int(meta.get("last_dream_message_id") or 0)

    async def _run_dream(self, session_id: str, local_dt: datetime, *, reason: str, force: bool = False):
        if not session_id:
            return
        key = self._context_character_key(session_id) if hasattr(self, "_context_character_key") else self._memory_character(session_id)
        scope = f"{session_id}\n{key}"
        task = getattr(self, "_dream_tasks", {}).get(scope)
        if task and not task.done():
            if force:
                await task
            return

        async def runner():
            try:
                await self._dream_once(session_id, key, local_dt, reason=reason)
            except Exception:
                logger.warning("dream task failed", exc_info=True)

        task = asyncio.create_task(runner())
        self._dream_tasks[scope] = task
        if force:
            await task

    async def _dream_once(self, session_id: str, character_key: str, local_dt: datetime, *, reason: str):
        meta = self.app_store.get_context_meta(session_id, character_key)
        from_id = int(meta.get("last_dream_message_id") or 0)
        to_id = self.app_store.latest_message_id(session_id, character_key)
        messages = self.app_store.list_messages(session_id, character_key, after_id=from_id, before_or_equal_id=to_id)
        source_limit = max(1000, int(self.config.get("dream_source_hard_limit_chars", "50000") or 50000))
        source_text = self._format_store_messages(messages, limit_chars=source_limit) if hasattr(self, "_format_store_messages") else ""
        diary_date = self._dream_diary_date(local_dt, force_previous_day=(reason == "morning"))
        existing = self.app_store.get_diary(session_id, character_key, diary_date) or {}
        diary = await self._write_dream_diary(session_id, diary_date, source_text, existing.get("content", ""), reason=reason)
        self.app_store.upsert_diary(session_id, character_key, diary_date, diary, from_message_id=from_id + 1, to_message_id=to_id)
        await self._organize_memories_after_dream(session_id, character_key)
        diaries = self.app_store.recent_diaries(session_id, character_key, limit=2)
        await self._generate_character_history_summary(session_id, character_key, diaries)
        self.app_store.mark_dream(session_id, character_key, to_id)
        state = self._get_session_state(session_id)
        session_schema.set_last_dream_at(state, time.time())
        session_schema.set_last_dream_message_id(state, to_id)
        self._save_session_state(session_id, state)
        self._ulog(session_id, "DREAM", f"reason={reason} date={diary_date} messages={len(messages)}")

    async def _write_dream_diary(self, session_id: str, diary_date: str, source_text: str, existing_diary: str = "", *, reason: str = "") -> str:
        if not source_text and existing_diary:
            return existing_diary
        if not self.has_llm_config("chat", session_id):
            base = (existing_diary + "\n" if existing_diary else "") + (source_text or "No new dialogue.")
            return base[-4000:]
        system = (
            "You write a private roleplay diary for the character. Consolidate the existing diary and "
            "new dialogue into a coherent diary entry for the given date. Preserve emotional continuity, "
            "relationship progress, promises, unresolved events, and important facts. Output Chinese diary text only."
        )
        user = f"Diary date: {diary_date}\nReason: {reason}\n\nExisting diary:\n{existing_diary or 'none'}\n\nNew dialogue since last dream:\n{source_text or 'none'}"
        return await self._call_llm(system, user, temp=0.2, tag="dream-diary", purpose="chat", disable_thinking=True, session_id=session_id)

    async def _organize_memories_after_dream(self, session_id: str, character_key: str):
        diaries = self.app_store.recent_diaries(session_id, character_key, limit=2)
        if not diaries or not self.has_llm_config("chat", session_id):
            return
        memories = self.memory.list_memories(session_id, character=character_key, limit=120)
        editable = [m for m in memories if m.get("kind") != "manual"]
        if not editable:
            return
        limit = self._long_memory_limit()
        threshold = max(1, limit // 2)
        if len(editable) > limit:
            await self._summarize_all_memories(session_id, character_key, editable, target_n=threshold, diaries=diaries)
        else:
            await self._incremental_organize_memories(session_id, character_key, editable, diaries=diaries)

    async def _incremental_organize_memories(
        self, session_id: str, character_key: str,
        editable: list[dict[str, Any]], *, diaries: list[dict[str, Any]] | None = None,
    ):
        checkpoint = self.app_store.get_checkpoint(session_id, character_key).get("summary", "")
        current = self._format_store_messages(self._active_chat_history(self._get_session_state(session_id), self._checkpoint_keep_message_limit()), limit_chars=12000)
        limit = self._long_memory_limit()
        threshold = max(1, limit // 2)
        system = (
            "You maintain long-term memories for a roleplay bot. Based on recent diaries, current context, "
            "and checkpoint, decide how to update non-manual memories. Never modify manual memories. "
            f"Keep total non-manual memories under {threshold} items. Merge similar memories, remove outdated ones. "
            "Return strict JSON: {\"ops\":[{\"op\":\"add|update|delete\",\"id\":123,\"kind\":\"profile|preference|relationship|setting|boundary|visual|event|correction\",\"summary\":\"...\",\"importance\":1-5,\"tags\":[\"...\"]}]}"
        )
        diary_text = "\n\n".join(f"[{d.get('diary_date')}]\n{d.get('content','')}" for d in (diaries or []))
        mem_text = "\n".join(f"{m['id']}. [{m.get('kind')}] {m.get('summary')}" for m in editable)
        user = f"Recent diaries:\n{diary_text}\n\nCheckpoint:\n{checkpoint or 'none'}\n\nCurrent window:\n{current or 'none'}\n\nEditable memories:\n{mem_text or 'none'}"
        try:
            raw = await self._call_llm(system, user, temp=0.1, tag="dream-memory", purpose="chat", disable_thinking=True, session_id=session_id)
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", raw).strip())
        except Exception:
            logger.warning("dream memory organize failed", exc_info=True)
            return
        ops = parsed.get("ops") if isinstance(parsed, dict) else None
        if not isinstance(ops, list):
            return
        for op in ops[:30]:
            if not isinstance(op, dict):
                continue
            action = str(op.get("op") or "").lower()
            if action == "add" and op.get("summary"):
                self.memory.add_memory(session_id, op.get("kind", "event"), op.get("summary", ""), character=character_key, importance=op.get("importance", 3), tags=op.get("tags") or [], source="dream")
            elif action == "update" and op.get("id") and op.get("summary"):
                self.memory.update_memory(session_id, int(op.get("id")), character=character_key, summary=op.get("summary"), kind=op.get("kind"), importance=op.get("importance"), tags=op.get("tags") or [], source="dream")
            elif action == "delete" and op.get("id"):
                self.memory.deactivate_non_manual_memory(session_id, int(op.get("id")), character=character_key)

    async def _summarize_all_memories(
        self, session_id: str, character_key: str,
        editable: list[dict[str, Any]], *, target_n: int = 4,
        diaries: list[dict[str, Any]] | None = None,
    ):
        checkpoint = self.app_store.get_checkpoint(session_id, character_key).get("summary", "")
        system = (
            f"You are a memory consolidator for a roleplay bot. The character has {len(editable)} non-manual memories, "
            f"which exceeds the limit. Consolidate ALL of them into at most {target_n} compact, non-redundant memories. "
            "Merge similar items, drop outdated or trivial ones, keep the most important and durable information. "
            "Each memory should be self-contained and cover a broader theme rather than a single fact. "
            "Never include manual memories. "
            f"Return strict JSON: {{\"memories\":[{{\"kind\":\"profile|preference|relationship|setting|boundary|visual|event|correction\","
            "\"summary\":\"一句中文记忆摘要\",\"importance\":1-5,\"tags\":[\"标签\"]}]}} "
            f"memories 数组长度不超过 {target_n}。"
        )
        diary_text = "\n\n".join(f"[{d.get('diary_date')}]\n{d.get('content','')}" for d in (diaries or []))
        mem_text = "\n".join(f"{m['id']}. [{m.get('kind')}] {m.get('summary')}" for m in editable)
        user = f"Recent diaries:\n{diary_text or 'none'}\n\nCheckpoint:\n{checkpoint or 'none'}\n\nAll editable memories:\n{mem_text}"
        try:
            raw = await self._call_llm(system, user, temp=0.1, tag="dream-memory-summarize", purpose="chat", disable_thinking=True, session_id=session_id)
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", raw).strip())
        except Exception:
            logger.warning("dream memory summarize failed", exc_info=True)
            return
        new_memories = parsed.get("memories") if isinstance(parsed, dict) else None
        if not isinstance(new_memories, list) or not new_memories:
            return
        for m in editable:
            self.memory.deactivate_non_manual_memory(session_id, int(m["id"]), character=character_key)
        for item in new_memories[:target_n]:
            if not isinstance(item, dict) or not item.get("summary"):
                continue
            self.memory.add_memory(
                session_id, item.get("kind", "event"), item["summary"],
                character=character_key, importance=item.get("importance", 3),
                tags=item.get("tags") or [], source="dream-summarize",
            )
        self._ulog(session_id, "MEMORY", f"全量重写 {len(editable)}→{min(len(new_memories), target_n)} 条（上限{target_n}）")

    async def _generate_character_history_summary(self, session_id: str, character_key: str, diaries: list[dict[str, Any]]):
        if not diaries or not self.has_llm_config("chat", session_id):
            return
        limit = self._checkpoint_hard_limit_chars()
        key = self._context_character_key(session_id) if hasattr(self, "_context_character_key") else character_key
        meta = self.app_store.get_context_meta(session_id, key)
        previous = (meta.get("character_history_summary") or "").strip()
        diary_text = "\n\n".join(f"[{d.get('diary_date')}]\n{d.get('content','')}" for d in diaries)
        system = (
            "你是角色历史提要生成器。根据上一次的历史提要和最近两天的日记，"
            "生成一份简洁的角色发展脉络摘要。涵盖关系进展、情感变化、重要承诺、未解事件和角色成长。"
            "这是给聊天模型的长期背景参考，不是日记复述。"
            f"字数控制在 {limit} 字以内。只输出中文摘要文本。"
        )
        user = f"上次历史提要:\n{previous or '无'}\n\n最近日记:\n{diary_text}"
        try:
            summary = await self._call_llm(
                system, user, temp=0.2, tag="history-summary",
                purpose="chat", disable_thinking=True, session_id=session_id,
            )
            summary = (summary or "").strip()
            if not summary:
                return
            hard = self._checkpoint_hard_limit_chars()
            if len(summary) > hard:
                summary = summary[-hard:]
            self.app_store.upsert_character_history_summary(session_id, key, summary)
            state = self._get_session_state(session_id)
            session_schema.set_character_history_summary(state, summary)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "HISTORY", f"角色历史提要更新 chars={len(summary)}")
        except Exception:
            logger.warning("character history summary generation failed", exc_info=True)

    async def scheduler_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                for session_id in list(self.sessions.keys()):
                    state = self._get_session_state(session_id)
                    if session_schema.get_frozen(state):
                        continue
                    if self._is_recently_active(state):
                        continue
                    now = self._session_now(session_id)
                    today = now.strftime("%Y-%m-%d")
                    time_str = now.strftime("%H:%M")
                    try:
                        daily_limit = int(str(self._get_session_cfg(session_id, "daily_selfie_limit", "3")).strip())
                    except ValueError:
                        daily_limit = 3
                    if session_schema.get_daily_trigger_date(state) != today:
                        times = []
                        if daily_limit > 0:
                            start, end = 8 * 60 + 30, 23 * 60 + 50
                            slot = (end - start) / daily_limit
                            for i in range(daily_limit):
                                minute = random.randint(int(start + i * slot), int(start + (i + 1) * slot))
                                times.append(f"{minute // 60:02d}:{minute % 60:02d}")
                        session_schema.set_daily_trigger_times(state, sorted(times))
                        session_schema.set_daily_trigger_date(state, today)
                        session_schema.set_daily_triggered_times(state, [])
                        self._mark_dirty(session_id)

                    # 推送关闭(每日次数=0)时，早安推送也不发——否则“关闭推送”每天早上又冒出来（用户报的“只持续一天”）。
                    if daily_limit > 0 and now.hour == 8 and now.minute < 5 and session_schema.get_last_morning_greet_date(state) != today:
                        session_schema.set_last_morning_greet_date(state, today)
                        self._mark_dirty(session_id)
                        if not self._check_goodnight_inhibition(state) and session_id not in self._active_pushes:
                            asyncio.create_task(self._sched_fire(session_id, now, mode_override="morning"))

                    triggered = session_schema.get_daily_triggered_times(state)
                    for t in session_schema.get_daily_trigger_times(state):
                        if t <= time_str and t not in triggered:
                            triggered.append(t)
                            session_schema.set_daily_triggered_times(state, triggered)
                            self._mark_dirty(session_id)
                            t_min = int(t.split(":")[0]) * 60 + int(t.split(":")[1])
                            now_min = now.hour * 60 + now.minute
                            if now_min - t_min <= 5 and not self._check_goodnight_inhibition(state) and session_id not in self._active_pushes:
                                asyncio.create_task(self._sched_fire(session_id, now, mode_override="normal"))

                    await self._check_ntr_stage(session_id, state)
                self._flush_sessions(force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("scheduler error: %s", exc, exc_info=True)
            await asyncio.sleep(60)

    async def _check_ntr_stage(self, session_id: str, state: dict[str, Any]):
        last = session_schema.get_last_interaction(state)
        if not last:
            return
        days = (time.time() - last) / 86400
        threshold = self._compute_ntr_threshold(self._get_purity(session_id))
        current = self._compute_ntr_stage(days, threshold)
        reached = session_schema.get_ntr_stage_reached(state)
        if current <= reached:
            return
        for stage in range(reached + 1, current + 1):
            if stage <= 3:
                asyncio.create_task(self._fire_ntr_stage_message(session_id, stage, int(days)))
            elif stage == 4:
                session_schema.set_ntr_affection_reset(state, True)
                session_schema.set_ntr_reconcile_count(state, 0)
            elif stage == 5:
                logger.info("session %s reached NTR stage 5", session_id)
        session_schema.set_ntr_stage_reached(state, current)
        self._mark_dirty(session_id)

    async def _sched_fire(self, session_id: str, local_dt: datetime, mode_override=None, skip_active_check=False):
        if not session_id or (not skip_active_check and session_id in self._active_pushes):
            return
        self._active_pushes.add(session_id)
        chat_id = self.chat_id_from_session(session_id)
        try:
            state = self._get_session_state(session_id)
            if self._check_goodnight_inhibition(state):
                return
            if not skip_active_check and self._is_recently_active(state):
                return
            last = session_schema.get_last_interaction(state)
            purity = self._get_purity(session_id)
            mode = mode_override or "normal"
            if last and time.time() - last > self._compute_ntr_threshold(purity) * 86400:
                mode = "ntr"
            if mode == "normal" and purity <= 0 and random.random() < 0.4:
                mode = "ntr"
            if mode == "morning":
                await self._run_dream(session_id, local_dt, reason="morning", force=True)
            elif self._should_run_dream_before_push(session_id, state):
                await self._run_dream(session_id, local_dt, reason=f"push-{mode}", force=False)
            self._ulog(session_id, "PUSH", f"触发 mode={mode}")
            w = await self._fetch_weather(session_id=session_id)
            weather = f"{w['desc']} {w['temp']} C" if w else "未知"
            time_ctx = self._get_time_context(session_id, now=local_dt, weather=w)
            time_period = time_ctx.get("period") or self._get_time_period(local_dt.hour)
            if hasattr(self, "_ensure_life_profile"):
                try:
                    await self._ensure_life_profile(session_id)
                except Exception:
                    logger.debug("life profile ensure failed for scheduler push", exc_info=True)
            if hasattr(self, "build_world_state"):
                try:
                    world = self.build_world_state(session_id, weather=w or weather, now=local_dt, mode=mode)
                    if world:
                        profile = self._format_life_profile(world.get("life_profile")) if hasattr(self, "_format_life_profile") else ""
                        current = world.get("character_place") or {}
                        nxt = world.get("next_place") or {}
                        self._ulog(
                            session_id,
                            "WORLD",
                            "推送动线 "
                            f"mode={mode} profile={profile or 'unknown'} "
                            f"current={current.get('label', '?')}({current.get('name', '?')}) "
                            f"next={world.get('next_time_period', '?')}:{nxt.get('label', '?')}({nxt.get('name', '?')})",
                        )
                except Exception:
                    logger.debug("world route log failed for scheduler push", exc_info=True)
            scene, caption, new_app, view = await self._llm_write_scene(
                mode,
                weather,
                WEEKDAY_NAMES[local_dt.weekday()],
                time_period,
                None,
                session_id,
                now=local_dt,
                weather_data=w,
            )
            if not scene:
                return
            english = await self._translate_to_tags(scene, session_id=session_id, view=view)
            ok, imgs, err = await self._do_generate(
                english,
                is_ntr=(mode == "ntr"),
                session_id=session_id,
                one_shot_appearance=new_app or "",
            )
            if ok and imgs:
                await self.send_photo(chat_id, imgs[0], caption or "")
                source = self._format_image_source_description(
                    intent=f"{mode} 模式自动推送，时段: {time_period}，天气: {weather}",
                    prompt=caption or "",
                )
                self._record_sent_photo(session_id, scene, caption or "", appearance=new_app or None, view=view, source_description=source)
            else:
                self._ulog(session_id, "PUSH", f"生图失败 mode={mode}: {err}")
                logger.error("scheduled generate failed: %s", err)
        finally:
            self._active_pushes.discard(session_id)

    def _check_goodnight_inhibition(self, state: dict[str, Any]) -> bool:
        text = (session_schema.get_last_message_text(state) or "").lower()
        ts = session_schema.get_last_message_time(state)
        return time.time() - ts < 3600 and any(word in text for word in ("晚安", "睡觉", "睡了", "去睡", "good night", "sleep"))

    @staticmethod
    def _is_recently_active(state: dict[str, Any]) -> bool:
        last = session_schema.get_last_interaction(state)
        return last > 0 and time.time() - last < 30 * 60

    async def _fire_ntr_stage_message(self, session_id: str, stage: int, days: int):
        persona = self._get_effective_persona(session_id)
        desc = {
            1: ("不安", "角色感到孤独和不安，开始担心用户是不是不在乎自己。"),
            2: ("难受", "角色越来越难受，开始怀疑自己的魅力。"),
            3: ("幽怨", "角色充满幽怨和不满，开始考虑是否放下。"),
        }.get(stage, ("不安", "角色感到不安。"))
        try:
            msg = await self._call_llm(
                f"你正在扮演以下角色:\n{persona}\n\n用户已经 {days} 天没有互动。现在状态: {desc[0]}。{desc[1]}",
                "用第一人称写 30-60 字角色台词，不要解释。",
                temp=float(self._get_llm_value("image", "temperature_scene", "0.95")),
                tag="ntr-stage",
                purpose="image",
            )
        except Exception:
            msg = {1: "已经好几天没和我说过话了……是不是我哪里做得不好？", 2: "又是没等到你的一天。你是不是已经忘了这里还有一个我。", 3: "如果你真的不在乎我，那我也该学着不等了。"}[stage]
        self._ulog(session_id, "NTR", f"stage={stage} 冷落{days}天: {msg}")
        await self.send_message(self.chat_id_from_session(session_id), msg)
