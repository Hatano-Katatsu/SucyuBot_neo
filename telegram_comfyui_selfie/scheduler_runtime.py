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
    _PUSH_SCENE_END_RE = re.compile(
        r"(不聊|不说|别扯|先这样|先不|到此|结束|散了|撤了|走了|离开|出发|回家|回去|下班|"
        r"去车站|去地铁|上车|到站|晚安|睡觉|睡了|明天见|下次见|改天|回头聊|晚上见|老地方见|"
        r"待会见|等会见|一会见|晚上.*等|待会.*等|等会.*等|不.*扯|告别|准备去|准备走|收拾.*去)"
    )
    _PUSH_SCENE_HOLD_RE = re.compile(
        r"(等你|等着你|等我|别走|别离开|留下来|继续|还没结束|正在|刚开始|马上回来|马上到|"
        r"一会回来|待会回来)"
    )

    # 对话后续场推送门控：用户最后一条消息若命中这些告别/中止关键词，则不安排 followup 推送。
    # 仅判定用户对 bot 的主动告别，不混入角色扮演情节里的转场词（如"出发""回家""下班"）。
    _POST_CHAT_PUSH_END_KEYWORDS = (
        # 明确告别
        "拜拜", "拜拜了", "再见", "再聊", "byebye", "bye bye", "bye", "goodbye", "88",
        # 晚安 / 睡眠
        "晚安", "睡了", "去睡", "睡觉", "睡啦", "睡咯", "good night", "goodnight", "gnight", "gn",
        "sleep well", "sweet dreams",
        # 离开 / 下线
        "下线", "走了", "先走了", "撤了", "溜了", "先撤", "gtg", "gotta go", "logging off",
        # 改天聊
        "回头聊", "下次聊", "改天聊", "以后聊", "明天聊", "see you", "see ya", "cya", "later", "ttyl",
        # 休息
        "休息了", "去休息", "先休息",
    )

    def _last_message_indicates_conversation_end(self, state: dict[str, Any]) -> bool:
        """用户最后一条消息是否表示主动结束当前聊天会话（拜拜/晚安/走了等）。"""
        text = (session_schema.get_last_message_text(state) or "").lower()
        if not text:
            return False
        return any(word in text for word in self._POST_CHAT_PUSH_END_KEYWORDS)

    def _push_scene_transition_decision(
        self,
        state: dict[str, Any],
        session_id: str = "",
        now: datetime | None = None,
        mode: str = "normal",
    ) -> dict[str, Any]:
        """判断主动推送是否应把短期连续性当成旧场景，而不是继续强锁。"""
        now_ts = now.timestamp() if isinstance(now, datetime) else time.time()
        reset_time = session_schema.get_short_context_reset_time(state)
        latest = float(session_schema.get_last_interaction(state) or 0)
        latest = max(latest, session_schema.get_last_message_time(state))
        texts: list[str] = []

        for msg in session_schema.get_recent_message_history(state):
            msg_ts = float(msg.get("time", 0) or 0)
            if reset_time and msg_ts < reset_time:
                continue
            latest = max(latest, msg_ts)
            text = (msg.get("text") or "").strip()
            if text:
                texts.append(text)
        for msg in self._active_chat_history(state, 8):
            if msg.get("role") not in ("user", "assistant"):
                continue
            text = (msg.get("content") or "").strip()
            if text:
                texts.append(text)
        for photo in session_schema.get_sent_photos_history(state)[-3:]:
            photo_ts = float(photo.get("timestamp", 0) or 0)
            if reset_time and photo_ts < reset_time:
                continue
            latest = max(latest, photo_ts)
            for key in ("scene", "caption", "source_description"):
                text = (photo.get(key) or "").strip()
                if text:
                    texts.append(text)

        joined = "\n".join(texts[-12:])
        try:
            stale_minutes = max(0.0, float(self.config.get("scene_stale_minutes", "30") or 0))
        except Exception:
            stale_minutes = 30.0
        try:
            continuity_hours = max(0.25, float(self.config.get("push_continuity_hours", "2") or "2"))
        except Exception:
            continuity_hours = 2.0
        gap_minutes = (now_ts - latest) / 60.0 if latest else 0.0
        has_end_signal = bool(self._PUSH_SCENE_END_RE.search(joined))
        has_hold_signal = bool(self._PUSH_SCENE_HOLD_RE.search(joined))
        stale = bool(latest and stale_minutes > 0 and gap_minutes > stale_minutes)
        too_old = bool(latest and gap_minutes > continuity_hours * 60.0)
        morning = (mode or "").strip().lower() == "morning"
        # 半小时级的 scene_stale 只说明“旧场景需要推进一拍”，不足以让随机推送每次都硬转场。
        # 主动推送的硬切换交给明确结束信号、早安新一天，或 push_continuity_hours 的更长 TTL。
        should_transition = morning or has_end_signal or too_old
        should_advance_beat = stale and not should_transition
        return {
            "should_transition": should_transition,
            "should_advance_beat": should_advance_beat,
            "drop_continuity": too_old and not has_hold_signal,
            "gap_minutes": gap_minutes,
            "stale_minutes": stale_minutes,
            "has_end_signal": has_end_signal,
            "has_hold_signal": has_hold_signal,
            "too_old": too_old,
            "morning": morning,
        }

    def _format_push_scene_advance_context(
        self,
        state: dict[str, Any],
        session_id: str = "",
        now: datetime | None = None,
        mode: str = "normal",
    ) -> str:
        decision = self._push_scene_transition_decision(state, session_id, now=now, mode=mode)
        if not decision.get("should_advance_beat"):
            return ""
        return (
            "推送场景节拍推进: 距离上次互动已超过场景断档阈值，但尚未超过主动推送连续性时效。\n"
            "处理规则: 保留最近已建立的大地点、同处/异地关系、情绪和未完成约定；但时间已经自然流逝，"
            "不要把上一幕的短动作、手势、姿势、正在喝/吃的一份食物饮料、刚拿起的物件或同一句话原样冻结到此刻。"
            "如果上一幕有茶、咖啡、饭、点心、手机消息、书页等消耗品或瞬时动作，本次应写成已经喝完/放下/换了姿势/转入相邻动作，"
            "或另起同一空间里的新日常小片段；只有最近文本明确说明该动作仍在持续时才继续。"
        )

    def _format_push_scene_transition_context(
        self,
        state: dict[str, Any],
        session_id: str = "",
        now: datetime | None = None,
        mode: str = "normal",
    ) -> str:
        decision = self._push_scene_transition_decision(state, session_id, now=now, mode=mode)
        if not decision.get("should_transition"):
            return ""
        reasons = []
        if decision.get("morning"):
            reasons.append("早安推送开启新一天")
        if decision.get("has_end_signal"):
            reasons.append("最近上下文含结束/离开/改约信号")
        if decision.get("too_old"):
            reasons.append("已超过主动推送连续性时效")
        elif decision.get("gap_minutes", 0) > decision.get("stale_minutes", 0) > 0:
            reasons.append("距离上次互动已超过场景断档阈值")
        if not reasons:
            reasons.append("短期场景可能已经自然结束")
        return (
            "推送场景转换判定: " + "；".join(reasons) + "。\n"
            "处理规则: 最近对话/照片只能作为情绪、约定和避免重复的参考，不要把上一场景的地点、姿势或话题强行续写成此刻仍在发生。"
            "如果有未完成约定，只保留约定本身，并根据当前时间、天气和角色动线写出自然过渡后的单一瞬间。"
            "除非最近文本明确表示角色仍在原地等待或动作尚未结束，否则应允许角色离开旧地点、到路上、回家、去下一个目的地或进入新的日常片段。"
        )

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
        transition = self._format_push_scene_transition_context(state, session_id, now=now)
        transition = ("\n" + transition) if transition else ""
        return (
            "短期连续性上下文（优先级高于自动动线；用于承接刚才停住的场景）:\n"
            + "\n\n".join(parts)
            + "\n连续性要求: 主动推送应优先承接最近已建立的地点、未完成约定、情绪和可见状态。"
            "如果现实动线与这里冲突，短时间内以连续性为主；确实需要换地点时必须写出自然过渡，"
            "例如离开咖啡店、去车站、回家路上，而不要突然跳到无关场景。"
            + transition
        )

    async def _llm_write_scene(self, mode, weather, weekday, time_period, recent_chat=None, session_id="", now=None, weather_data=None):
        from .image_planning import plan_roleplay_image
        if not self.has_llm_config("image"):
            return None, None, None, None, None
        plan = await plan_roleplay_image(
            self, session_id, mode=mode or "normal",
            weather_data=weather_data, now=now,
        )
        return (
            plan.get("scene") or "",
            plan.get("caption") or "",
            plan.get("new_appearance_tags") or "",
            plan.get("view") or "",
            plan.get("aspect_ratio") or "",
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

    @staticmethod
    def _dream_diary_weekday(diary_date: str) -> str:
        try:
            day = datetime.strptime(str(diary_date), "%Y-%m-%d")
            return WEEKDAY_NAMES[day.weekday()]
        except Exception:
            return ""

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
            except Exception as exc:
                self._ulog(session_id, "ERROR", f"DREAM_FAILED reason={reason}: {exc}")
                logger.warning("dream task failed", exc_info=True)

        task = asyncio.create_task(runner())
        self._dream_tasks[scope] = task
        if force:
            await task

    @staticmethod
    def _log_excerpt(text: Any, limit: int = 500) -> str:
        text = str(text or "").replace("\r", "").replace("\n", " ⏎ ").strip()
        return text[:limit] + ("..." if len(text) > limit else "")

    @staticmethod
    def _parse_llm_json(raw: Any) -> Any:
        text = str(raw or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```\s*$", "", text).strip()
        if not text:
            raise ValueError("LLM 返回空 JSON 内容")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if 0 <= start < end:
                return json.loads(text[start:end + 1])
            raise

    def _format_memory_summarize_input(self, editable: list[dict[str, Any]], *, max_chars: int = 24000) -> tuple[str, set[int], int]:
        lines: list[str] = []
        included: set[int] = set()
        omitted = 0
        used = 0
        for memory in editable:
            try:
                mid = int(memory.get("id"))
            except Exception:
                omitted += 1
                continue
            tags = ",".join(str(tag) for tag in (memory.get("tags") or [])[:4])
            tag_text = f" tags={tags}" if tags else ""
            summary = self._log_excerpt(memory.get("summary", ""), 220)
            line = f"{mid}. [{memory.get('kind', 'event')}/重要度{memory.get('importance', 3)}] {summary}{tag_text}"
            line_len = len(line) + 1
            if lines and used + line_len > max_chars:
                omitted += 1
                continue
            lines.append(line)
            included.add(mid)
            used += line_len
        if omitted:
            lines.append(f"... omitted {omitted} lower-priority memories in this pass due to prompt budget; omitted memories remain unchanged.")
        return "\n".join(lines), included, omitted

    async def _call_memory_json_llm(
        self,
        session_id: str,
        system: str,
        user: str,
        *,
        tag: str,
        temp: float = 0.1,
        allow_fast_fallback: bool = True,
        max_tokens: int | None = None,
    ) -> tuple[str, Any, str, list[dict[str, str]]]:
        attempts: list[dict[str, str]] = []
        purposes: list[str] = []
        if self.has_llm_config("chat", session_id):
            purposes.append("chat")
        if allow_fast_fallback and self.has_llm_config("image", session_id):
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
                    max_tokens=max_tokens,
                )
                parsed = self._parse_llm_json(raw)
                attempts.append({"purpose": purpose, "status": "ok"})
                return raw, parsed, purpose, attempts
            except Exception as exc:
                last_exc = exc
                attempts.append({
                    "purpose": purpose,
                    "status": "failed",
                    "error": str(exc),
                    "raw_excerpt": self._log_excerpt(raw, 240),
                })
                if purpose == "chat" and allow_fast_fallback and "image" in purposes:
                    self._ulog(session_id, "MEMORY", f"chat 记忆 JSON 失败，回落 fast 模型 attempts={json.dumps(attempts, ensure_ascii=False)}")
                    continue
                break
        raise RuntimeError(json.dumps({"attempts": attempts, "error": str(last_exc or '')}, ensure_ascii=False))

    async def _dream_once(self, session_id: str, character_key: str, local_dt: datetime, *, reason: str):
        meta = self.app_store.get_context_meta(session_id, character_key)
        from_id = int(meta.get("last_dream_message_id") or 0)
        to_id = self.app_store.latest_message_id(session_id, character_key)
        messages = self.app_store.list_messages(session_id, character_key, after_id=from_id, before_or_equal_id=to_id)
        if hasattr(self, "_ensure_style_pool_entry"):
            try:
                self._ensure_style_pool_entry(self._get_current_style(session_id))
            except Exception:
                logger.warning("dream style pool sync failed", exc_info=True)
        source_limit = max(1000, int(self.config.get("dream_source_hard_limit_chars", "50000") or 50000))
        source_text = self._format_store_messages(messages, limit_chars=source_limit, roles={"user", "assistant"}) if hasattr(self, "_format_store_messages") else ""
        diary_date = self._dream_diary_date(local_dt, force_previous_day=(reason == "morning"))
        if hasattr(self, "write_character_checkpoint"):
            try:
                checkpoint_path = self.write_character_checkpoint(
                    session_id,
                    character_key,
                    diary_date,
                    reason=f"dream:{reason}",
                    to_message_id=to_id,
                )
                self._ulog(session_id, "CHECKPOINT", f"角色检查点已写入 date={diary_date} path={checkpoint_path}")
            except Exception as exc:
                logger.warning("character checkpoint before dream failed", exc_info=True)
                self._ulog(session_id, "ERROR", f"CHARACTER_CHECKPOINT_FAILED date={diary_date} error={exc}")
        existing = self.app_store.get_diary(session_id, character_key, diary_date) or {}
        life_plan_diary_context = ""
        if hasattr(self, "_format_life_plan_diary_context"):
            try:
                life_plan_diary_context = self._format_life_plan_diary_context(session_id, character_key, diary_date)
            except Exception:
                logger.debug("life plan diary context failed", exc_info=True)
        diary_kwargs: dict[str, Any] = {"reason": reason}
        if life_plan_diary_context:
            diary_kwargs["life_plan_context"] = life_plan_diary_context
        diary = await self._write_dream_diary(
            session_id,
            diary_date,
            source_text,
            existing.get("content", ""),
            **diary_kwargs,
        )
        self.app_store.upsert_diary(session_id, character_key, diary_date, diary, from_message_id=from_id + 1, to_message_id=to_id)
        self._ulog(
            session_id,
            "DREAM",
            f"日记更新 reason={reason} date={diary_date} messages={len(messages)} "
            f"source_chars={len(source_text)} diary_chars={len(diary or '')} output={self._log_excerpt(diary)}",
        )
        memory_result = await self._organize_memories_after_dream(session_id, character_key)
        if isinstance(memory_result, dict):
            self._ulog(session_id, "MEMORY", f"dream整理结果 {json.dumps(memory_result, ensure_ascii=False, default=str)}")
        if hasattr(self, "_update_life_plan_after_dream"):
            life_result = await self._update_life_plan_after_dream(
                session_id,
                character_key,
                local_dt,
                diary_date=diary_date,
                diary=diary,
                reason=reason,
            )
            if isinstance(life_result, dict):
                self._ulog(session_id, "LIFE", f"dream生活线结果 {json.dumps(life_result, ensure_ascii=False, default=str)}")
        diaries = self.app_store.recent_diaries(session_id, character_key, limit=2)
        await self._generate_character_history_summary(session_id, character_key, diaries)
        self.app_store.mark_dream(session_id, character_key, to_id)
        state = self._get_session_state(session_id)
        session_schema.set_last_dream_at(state, time.time())
        session_schema.set_last_dream_message_id(state, to_id)
        self._save_session_state(session_id, state)
        self._ulog(session_id, "DREAM", f"reason={reason} date={diary_date} messages={len(messages)}")

    @staticmethod
    def _diary_body_without_heading(text: str) -> str:
        lines = str(text or "").strip().splitlines()
        if lines and lines[0].lstrip().startswith("#"):
            lines = lines[1:]
        return "\n".join(lines).strip()

    @classmethod
    def _diary_preservation_fragments(cls, text: str) -> list[str]:
        body = cls._diary_body_without_heading(text)
        if not body:
            return []
        raw_parts = re.split(r"\n\s*\n|(?<=[。！？!?])\s*", body)
        fragments: list[str] = []
        for part in raw_parts:
            part = part.strip()
            if not part:
                continue
            if len(part) <= 700:
                fragments.append(part)
                continue
            for idx in range(0, len(part), 600):
                chunk = part[idx:idx + 600].strip()
                if chunk:
                    fragments.append(chunk)
        return fragments

    @staticmethod
    def _diary_norm(text: str) -> str:
        return re.sub(r"\s+", "", str(text or ""))

    @classmethod
    def _ensure_diary_preserves_existing(cls, existing_diary: str, new_diary: str) -> str:
        existing_diary = str(existing_diary or "").strip()
        new_diary = str(new_diary or "").strip()
        if not existing_diary or not new_diary:
            return new_diary or existing_diary
        new_norm = cls._diary_norm(new_diary)
        missing = []
        for fragment in cls._diary_preservation_fragments(existing_diary):
            norm = cls._diary_norm(fragment)
            if len(norm) >= 8 and norm not in new_norm:
                missing.append(fragment)
        if not missing:
            return new_diary
        supplement = "\n".join(f"- {fragment}" for fragment in missing)
        return (
            new_diary.rstrip()
            + "\n\n补记（保留旧日记中未被新版本明确写入的信息）:\n"
            + supplement
        )

    async def _write_dream_diary(
        self,
        session_id: str,
        diary_date: str,
        source_text: str,
        existing_diary: str = "",
        *,
        reason: str = "",
        life_plan_context: str = "",
        source_role_legend: str = "",
    ) -> str:
        if not source_text and existing_diary:
            return existing_diary
        if not self.has_llm_config("chat", session_id):
            return ((existing_diary.rstrip() + "\n\n") if existing_diary else "") + (source_text or "No new dialogue.")
        weekday = self._dream_diary_weekday(diary_date)
        if not source_role_legend:
            source_role_legend = (
                self._dialog_role_legend()
                if hasattr(self, "_dialog_role_legend")
                else "User = human user; Assistant = the current bot roleplay character."
            )
        overwrite_note = ""
        if str(existing_diary or "").strip():
            overwrite_note = (
                "Existing diary is the previous saved entry for the same date. "
                "Your output will replace that old entry, not append to it or continue after it. "
                "Rewrite one complete diary for the date by merging the old entry with the new dialogue. "
                "You must preserve every concrete fact, promise, emotional turning point, unresolved issue, "
                "and relationship change already recorded in Existing diary, even if the new dialogue does not mention it. "
                "Do not shorten the entry by deleting old information; rewrite or compress it only when the same information remains recoverable. "
            )
        system = (
            "You write a private diary from the character's first-person perspective. Consolidate the existing diary and "
            "new dialogue into a coherent diary entry for the given date. Preserve emotional continuity, "
            "relationship progress, promises, unresolved events, and important facts. "
            f"{overwrite_note}"
            "Treat Existing diary as the archived record and New dialogue as new evidence. "
            "Do not invent events, motives, promises, locations, or off-screen actions that are not supported by either source. "
            "Compress repeated physical actions or low-value banter, but keep concrete facts, explicit promises, unresolved tensions, "
            "relationship changes, and the character's emotional turning points recoverable. "
            "Perspective contract: the diary's first-person 'I' is always the current bot roleplay character, never the human user. "
            "In source dialogue, User means the human user, and Assistant means the bot character's own speech/actions. "
            "Do not swap who felt, promised, touched, moved, or spoke; if ownership is unclear, write it neutrally or omit it. "
            "The first line must be a Markdown heading in this exact format: "
            f"# {diary_date} {weekday or '星期几'} 标题. Use a concise Chinese title after the weekday. "
            "Write the diary in the character's first-person private voice. "
            "Do not include roleplay advice, prompt notes, future acting directions, narrator comments, "
            "or sections such as 「新一天演绎提示」/「角色扮演建议」. Output Chinese diary text only."
        )
        write_mode = "overwrite existing diary" if str(existing_diary or "").strip() else "new diary"
        user = (
            f"Diary date: {diary_date}\nWeekday: {weekday or 'unknown'}\nWrite mode: {write_mode}\nReason: {reason}"
            f"\n\nExisting diary:\n{existing_diary or 'none'}"
            f"\n\nDialogue role legend:\n{source_role_legend or 'User = human user; Assistant = the current bot roleplay character.'}"
            f"\n\nNew dialogue since last dream:\n{source_text or 'none'}"
            f"\n\nPrivate life background for diary:\n{life_plan_context or 'none'}"
        )
        diary = await self._call_llm(system, user, temp=0.2, tag="dream-diary", purpose="chat", disable_thinking=True, session_id=session_id)
        preserved = self._ensure_diary_preserves_existing(existing_diary, diary)
        if preserved != diary:
            self._ulog(session_id, "DREAM", f"旧日记保全追加 date={diary_date} missing_chars={len(preserved) - len(diary)}")
        return preserved

    def _record_memory_operation_failure(self, session_id: str, stage: str, request: Any, result: Any) -> None:
        payload = {
            "stage": stage,
            "request": request,
            "result": result,
        }
        try:
            if hasattr(self, "_json_safe"):
                payload = self._json_safe(payload)
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            text = str(payload)
        self._ulog(session_id, "ERROR", f"MEMORY_OP_FAILED {text}")

    async def _organize_memories_after_dream(self, session_id: str, character_key: str) -> dict[str, Any]:
        diaries = self.app_store.recent_diaries(session_id, character_key, limit=2)
        if not self.has_llm_config("chat", session_id) and not self.has_llm_config("image", session_id):
            result = {"status": "skipped", "reason": "no_chat_or_fast_llm", "character": character_key}
            self._ulog(session_id, "MEMORY", f"整理跳过 {json.dumps(result, ensure_ascii=False)}")
            return result
        try:
            scan_limit = max(120, int(self.config.get("long_memory_organize_scan_limit", "1000") or 1000))
        except Exception:
            scan_limit = 1000
        memories = self.memory.list_memories(session_id, character=character_key, limit=scan_limit)
        editable = [m for m in memories if m.get("kind") != "manual"]
        if not editable:
            result = {
                "status": "skipped",
                "reason": "no_editable_memories",
                "character": character_key,
                "total": len(memories),
                "diaries": len(diaries or []),
            }
            self._ulog(session_id, "MEMORY", f"整理跳过 {json.dumps(result, ensure_ascii=False)}")
            return result
        limit = self._long_memory_limit()
        threshold = max(1, limit // 2)
        if len(editable) > limit:
            return await self._summarize_all_memories(session_id, character_key, editable, target_n=threshold, diaries=diaries)
        return await self._incremental_organize_memories(session_id, character_key, editable, diaries=diaries)

    async def _incremental_organize_memories(
        self, session_id: str, character_key: str,
        editable: list[dict[str, Any]], *, diaries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        checkpoint = self.app_store.get_checkpoint(session_id, character_key).get("summary", "")
        current = self._format_store_messages(
            self._active_chat_history(self._get_session_state(session_id), self._checkpoint_keep_message_limit()),
            limit_chars=12000,
            roles={"user", "assistant"},
        )
        role_legend = self._dialog_role_legend() if hasattr(self, "_dialog_role_legend") else "User = human user; Assistant = the current bot roleplay character."
        limit = self._long_memory_limit()
        threshold = max(1, limit // 2)
        system = (
            "You maintain long-term memories for a roleplay bot. Based on recent diaries, current context, "
            "and checkpoint, decide how to update non-manual memories. Never modify manual memories. "
            f"Keep total non-manual memories under {threshold} items. Merge similar memories, remove outdated ones. "
            "Use diaries as archived evidence, checkpoint/current context as continuity evidence, and editable memories as the only mutable state. "
            "Diary evidence is written from the bot character's first-person perspective: diary 'I' means the character, not the human user. "
            "When reading current context, User is the human user and Assistant is the current bot roleplay character; never swap their actions, emotions, promises, or preferences. "
            "Do not create new memories from inference or roleplay taste unless the sources explicitly state a durable preference, boundary, promise, correction, or relationship change. "
            "For memories that are time nodes, deadlines, appointments, schedules, or countdowns, do not delete them "
            "only because the date or time has passed. Update or delete them only when the related event is clearly "
            "resolved, canceled, superseded, or has fully faded from recent diaries, checkpoint, and current window. "
            "Return strict JSON: {\"ops\":[{\"op\":\"add|update|delete\",\"id\":123,\"kind\":\"profile|preference|relationship|setting|boundary|visual|event|correction\",\"summary\":\"...\",\"importance\":1-5,\"tags\":[\"...\"]}]}"
        )
        diary_text = "\n\n".join(f"[{d.get('diary_date')}]\n{d.get('content','')}" for d in (diaries or []))
        mem_text = "\n".join(f"{m['id']}. [{m.get('kind')}] {m.get('summary')}" for m in editable)
        user = f"Recent diaries:\n{diary_text}\n\nCheckpoint:\n{checkpoint or 'none'}\n\nCurrent dialogue role legend:\n{role_legend}\n\nCurrent window:\n{current or 'none'}\n\nEditable memories:\n{mem_text or 'none'}"
        try:
            raw, parsed, llm_purpose, attempts = await self._call_memory_json_llm(
                session_id, system, user, tag="dream-memory", temp=0.1)
        except Exception as exc:
            logger.warning("dream memory organize failed", exc_info=True)
            result = {"status": "failed", "mode": "incremental", "error": str(exc), "raw_excerpt": self._log_excerpt(locals().get("raw", ""), 500)}
            self._record_memory_operation_failure(
                session_id,
                "dream-memory-parse",
                {"system": system, "user": user},
                result,
            )
            return result
        ops = parsed.get("ops") if isinstance(parsed, dict) else None
        if not isinstance(ops, list):
            result = {"status": "failed", "mode": "incremental", "raw": raw, "parsed": parsed}
            self._record_memory_operation_failure(
                session_id,
                "dream-memory-invalid-ops",
                {"system": system, "user": user},
                result,
            )
            return result
        if not ops:
            result = {"status": "no_op", "mode": "incremental", "editable": len(editable), "ops": 0}
            self._ulog(session_id, "MEMORY", f"增量整理无操作 {json.dumps(result, ensure_ascii=False)}")
            return result
        applied = 0
        failed = 0
        details: list[dict[str, Any]] = []
        for op in ops[:30]:
            if not isinstance(op, dict):
                failed += 1
                detail = {"op": "invalid", "ok": False, "request": op, "result": "op is not object"}
                details.append(detail)
                self._record_memory_operation_failure(session_id, "dream-memory-op", op, detail)
                continue
            action = str(op.get("op") or "").lower()
            ok = False
            result_detail: dict[str, Any] = {"op": action or "unknown", "id": op.get("id"), "ok": False}
            if action == "add" and op.get("summary"):
                mid = self.memory.add_memory(session_id, op.get("kind", "event"), op.get("summary", ""), character=character_key, importance=op.get("importance", 3), tags=op.get("tags") or [], source="dream")
                ok = mid is not None
                result_detail.update({"id": mid, "ok": ok, "summary": self._log_excerpt(op.get("summary"), 160)})
            elif action == "update" and op.get("id") and op.get("summary"):
                ok = self.memory.update_memory(session_id, int(op.get("id")), character=character_key, summary=op.get("summary"), kind=op.get("kind"), importance=op.get("importance"), tags=op.get("tags") or [], source="dream")
                result_detail.update({"ok": ok, "summary": self._log_excerpt(op.get("summary"), 160)})
            elif action == "delete" and op.get("id"):
                ok = self.memory.deactivate_non_manual_memory(session_id, int(op.get("id")), character=character_key)
                result_detail.update({"ok": ok})
            else:
                result_detail.update({"ok": False, "error": "invalid op or missing required fields"})
            if ok:
                applied += 1
                self._ulog(session_id, "MEMORY", f"增量整理 op={action} result={json.dumps(result_detail, ensure_ascii=False, default=str)}")
            else:
                failed += 1
                self._record_memory_operation_failure(session_id, "dream-memory-op", op, result_detail)
            details.append(result_detail)
        status = "ok" if failed == 0 else ("partial_failed" if applied else "failed")
        result = {
            "status": status,
            "mode": "incremental",
            "llm_purpose": llm_purpose,
            "llm_attempts": attempts,
            "editable": len(editable),
            "ops": len(ops[:30]),
            "applied": applied,
            "failed": failed,
            "details": details[:20],
        }
        self._ulog(session_id, "MEMORY", f"增量整理完成 {json.dumps(result, ensure_ascii=False, default=str)}")
        return result

    async def _summarize_all_memories(
        self, session_id: str, character_key: str,
        editable: list[dict[str, Any]], *, target_n: int = 4,
        diaries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        checkpoint = self.app_store.get_checkpoint(session_id, character_key).get("summary", "")
        mem_text, included_ids, omitted = self._format_memory_summarize_input(editable)
        system = (
            f"You are a memory consolidator for a roleplay bot. The character has {len(editable)} non-manual memories. "
            f"This request supplies {len(included_ids)} editable memories for this pass; consolidate only supplied memories "
            f"into at most {target_n} compact, non-redundant memories. "
            "If the user prompt says some memories were omitted, those omitted memories remain unchanged and must not be invented or referenced. "
            "Merge similar items, drop outdated or trivial ones, keep the most important and durable information. "
            "Use only the supplied memories plus diary/checkpoint evidence; do not add new facts, motives, or commitments by inference. "
            "Diary evidence is written from the bot character's first-person perspective: diary 'I' means the character, not the human user. "
            "For time nodes, deadlines, appointments, schedules, or countdowns, do not drop them merely because the "
            "date or time has passed; keep or merge them until the related event is resolved, canceled, superseded, "
            "or has fully faded from recent diaries and checkpoint. "
            "Each memory should be self-contained and cover a broader theme rather than a single fact. "
            "Never include manual memories. "
            f"Return strict JSON: {{\"memories\":[{{\"kind\":\"profile|preference|relationship|setting|boundary|visual|event|correction\","
            "\"summary\":\"一句中文记忆摘要\",\"importance\":1-5,\"tags\":[\"标签\"]}]}} "
            f"memories 数组长度不超过 {target_n}。"
        )
        diary_text = "\n\n".join(f"[{d.get('diary_date')}]\n{d.get('content','')}" for d in (diaries or []))
        user = f"Recent diaries:\n{diary_text or 'none'}\n\nCheckpoint:\n{checkpoint or 'none'}\n\nEditable memories for this pass:\n{mem_text or 'none'}"
        try:
            summarize_max_tokens = max(1024, int(self.config.get("dream_memory_summarize_max_tokens", "8192") or 8192))
        except (TypeError, ValueError):
            summarize_max_tokens = 8192
        try:
            raw, parsed, llm_purpose, attempts = await self._call_memory_json_llm(
                session_id, system, user, tag="dream-memory-summarize", temp=0.1, max_tokens=summarize_max_tokens)
        except Exception as exc:
            logger.warning("dream memory summarize failed", exc_info=True)
            result = {
                "status": "failed",
                "mode": "summarize",
                "error": str(exc),
                "raw_excerpt": self._log_excerpt(locals().get("raw", ""), 500),
                "editable": len(editable),
                "included": len(included_ids),
                "omitted": omitted,
            }
            self._record_memory_operation_failure(
                session_id,
                "dream-memory-summarize-parse",
                {"system": system, "user": user},
                result,
            )
            return result
        new_memories = parsed.get("memories") if isinstance(parsed, dict) else None
        if not isinstance(new_memories, list) or not new_memories:
            result = {"status": "failed", "mode": "summarize", "raw": raw, "parsed": parsed}
            self._record_memory_operation_failure(
                session_id,
                "dream-memory-summarize-empty",
                {"system": system, "user": user},
                result,
            )
            return result
        deactivated = 0
        failed = 0
        for m in editable:
            try:
                mid = int(m["id"])
            except Exception:
                failed += 1
                continue
            if included_ids and mid not in included_ids:
                continue
            ok = self.memory.deactivate_non_manual_memory(session_id, mid, character=character_key)
            if ok:
                deactivated += 1
            else:
                failed += 1
                self._record_memory_operation_failure(
                    session_id,
                    "dream-memory-summarize-deactivate",
                    {"id": m.get("id"), "summary": m.get("summary")},
                    {"ok": False},
                )
        added = 0
        for item in new_memories[:target_n]:
            if not isinstance(item, dict) or not item.get("summary"):
                failed += 1
                self._record_memory_operation_failure(
                    session_id,
                    "dream-memory-summarize-add",
                    item,
                    {"ok": False, "error": "invalid memory item"},
                )
                continue
            mid = self.memory.add_memory(
                session_id, item.get("kind", "event"), item["summary"],
                character=character_key, importance=item.get("importance", 3),
                tags=item.get("tags") or [], source="dream-summarize",
            )
            if mid is None:
                failed += 1
                self._record_memory_operation_failure(
                    session_id,
                    "dream-memory-summarize-add",
                    item,
                    {"ok": False, "error": "add_memory returned None"},
                )
            else:
                added += 1
        status = "ok" if failed == 0 else ("partial_failed" if added else "failed")
        result = {
            "status": status,
            "mode": "summarize",
            "llm_purpose": llm_purpose,
            "llm_attempts": attempts,
            "editable": len(editable),
            "target": target_n,
            "deactivated": deactivated,
            "added": added,
            "failed": failed,
            "included": len(included_ids),
            "omitted": omitted,
        }
        self._ulog(session_id, "MEMORY", f"全量重写 {len(editable)}→{added} 条（上限{target_n}） result={json.dumps(result, ensure_ascii=False)}")
        return result

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
            "日记是当前 bot 角色的一人称记录；日记里的「我」指角色本人，「用户」「对方」指人类用户。"
            "必须保持角色和用户的视角归属，不要把用户的动作、承诺、情绪写成角色的，也不要反过来。"
            "建议结构为「关系/剧情惯性」「角色心理与心情界定」「未解事件」「新一天演绎提示」四段，内容必须精炼。"
            "「新一天演绎提示」需要尊重剧情逻辑惯性，重点分析角色当前心理、防御/期待/羞耻/依恋等心情边界，"
            "给出顺着既有矛盾和情绪自然延展的扮演方向；不要写死具体台词、地点、日程或剧情分支。"
            "只基于日记原文内容，不要编造、推断或补充日记中没有明确提到的事件、规则、约定或承诺。"
            "如果只能判断情绪倾向，必须写成倾向或可能性，不要包装成已发生事实。"
            f"字数控制在 {limit} 字以内。只输出中文摘要文本。"
        )
        user = f"视角说明: 日记中的第一人称=当前 bot 角色；用户/对方=人类用户。\n\n上次历史提要:\n{previous or '无'}\n\n最近日记:\n{diary_text}"
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
            self._ulog(session_id, "HISTORY", f"角色历史提要更新 chars={len(summary)} output={self._log_excerpt(summary)}")
        except Exception as exc:
            self._ulog(session_id, "ERROR", f"HISTORY_FAILED: {exc}")
            logger.warning("character history summary generation failed", exc_info=True)

    def _mark_daily_triggered_time(self, session_id: str, trigger_time: str, *, reason: str = "sent"):
        """标记一个随机推送点已经处理完成。"""
        trigger_time = (trigger_time or "").strip()
        if not session_id or not trigger_time:
            return
        state = self._get_session_state(session_id)
        triggered = session_schema.get_daily_triggered_times(state)
        if trigger_time not in triggered:
            triggered.append(trigger_time)
            session_schema.set_daily_triggered_times(state, sorted(triggered))
            self._save_session_state(session_id, state)
        self._ulog(session_id, "PUSH", f"随机推送点 {trigger_time} 已处理 reason={reason}")

    def _mark_morning_greet_sent(self, session_id: str, local_dt: datetime, *, reason: str = "sent"):
        """标记今日早安推送已经处理完成。"""
        if not session_id:
            return
        state = self._get_session_state(session_id)
        today = local_dt.strftime("%Y-%m-%d")
        if session_schema.get_last_morning_greet_date(state) != today:
            session_schema.set_last_morning_greet_date(state, today)
            self._save_session_state(session_id, state)
        self._ulog(session_id, "PUSH", f"早安推送已处理 reason={reason}")

    def _post_chat_push_enabled(self, session_id: str) -> bool:
        value = self._get_session_cfg(session_id, "post_chat_push_enabled", self.config.get("post_chat_push_enabled", True))
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def _post_chat_push_number(self, session_id: str, key: str, default: float) -> float:
        try:
            return float(self._get_session_cfg(session_id, key, self.config.get(key, default)) or default)
        except (TypeError, ValueError):
            return float(default)

    def _reset_post_chat_push_counter_if_needed(self, session_id: str, state: dict[str, Any], local_dt: datetime | None = None) -> bool:
        today = (local_dt or self._session_now(session_id)).strftime("%Y-%m-%d")
        if session_schema.get_post_chat_push_date(state) != today:
            session_schema.set_post_chat_push_date(state, today)
            session_schema.set_post_chat_push_count(state, 0)
            return True
        return False

    def _post_chat_push_quota_ok(self, session_id: str, state: dict[str, Any], local_dt: datetime | None = None) -> bool:
        if not self._post_chat_push_enabled(session_id):
            return False
        self._reset_post_chat_push_counter_if_needed(session_id, state, local_dt)
        daily_limit = int(max(0, self._post_chat_push_number(session_id, "post_chat_push_daily_limit", 3)))
        if daily_limit <= 0:
            return False
        if session_schema.get_post_chat_push_count(state) >= daily_limit:
            return False
        cooldown = max(0.0, self._post_chat_push_number(session_id, "post_chat_push_cooldown_minutes", 60)) * 60
        last = session_schema.get_last_post_chat_push_time(state)
        return not last or time.time() - last >= cooldown

    def _schedule_post_chat_push(self, session_id: str) -> bool:
        if not session_id or not self._post_chat_push_enabled(session_id):
            return False
        state = self._get_session_state(session_id)
        if session_schema.get_frozen(state) or not self.has_llm_config("image", session_id):
            return False
        if self._last_message_indicates_conversation_end(state):
            self._ulog(session_id, "PUSH", "跳过对话后续场推送: 用户消息含告别/中止信号")
            return False
        local_dt = self._session_now(session_id)
        counter_reset = self._reset_post_chat_push_counter_if_needed(session_id, state, local_dt)
        if not self._post_chat_push_quota_ok(session_id, state, local_dt):
            if counter_reset:
                self._save_session_state(session_id, state)
            return False
        if counter_reset:
            self._save_session_state(session_id, state)
        min_minutes = max(0.1, self._post_chat_push_number(session_id, "post_chat_push_delay_min_minutes", 5))
        max_minutes = max(min_minutes, self._post_chat_push_number(session_id, "post_chat_push_delay_max_minutes", 15))
        delay = random.uniform(min_minutes, max_minutes) * 60
        expected_message_time = session_schema.get_last_message_time(state)
        tasks = getattr(self, "_post_chat_push_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._post_chat_push_tasks = tasks
        old = tasks.get(session_id)
        if old and not old.done():
            old.cancel()

        async def runner():
            try:
                await asyncio.sleep(delay)
                await self._fire_post_chat_push(session_id, expected_message_time, delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ulog(session_id, "PUSH", f"对话后续场推送任务异常: {exc}")
                logger.error("post-chat push task failed: %s", exc, exc_info=True)
            finally:
                cur = getattr(self, "_post_chat_push_tasks", {}).get(session_id)
                if cur is asyncio.current_task():
                    getattr(self, "_post_chat_push_tasks", {}).pop(session_id, None)

        tasks[session_id] = asyncio.create_task(runner(), name=f"post-chat-push:{session_id}")
        self._ulog(session_id, "PUSH", f"已安排对话后续场推送 delay={delay / 60:.1f}min")
        return True

    async def _fire_post_chat_push(self, session_id: str, expected_message_time: float, delay: float = 0.0) -> bool:
        if not session_id:
            return False
        state = self._get_session_state(session_id)
        local_dt = self._session_now(session_id)
        if session_schema.get_frozen(state):
            self._ulog(session_id, "PUSH", "跳过对话后续场推送: frozen")
            return False
        if abs(session_schema.get_last_message_time(state) - float(expected_message_time or 0)) > 0.001:
            self._ulog(session_id, "PUSH", "跳过对话后续场推送: 用户已有新消息")
            return False
        if self._check_goodnight_inhibition(state):
            self._ulog(session_id, "PUSH", "跳过对话后续场推送: goodnight inhibition")
            return False
        if session_id in self._active_pushes:
            self._ulog(session_id, "PUSH", "跳过对话后续场推送: active push")
            return False
        if not self._post_chat_push_quota_ok(session_id, state, local_dt):
            self._ulog(session_id, "PUSH", "跳过对话后续场推送: quota/cooldown")
            return False
        ok = await self._sched_fire(session_id, local_dt, mode_override="followup", skip_active_check=True)
        if ok:
            session_schema.set_post_chat_push_count(state, session_schema.get_post_chat_push_count(state) + 1)
            session_schema.set_last_post_chat_push_time(state, time.time())
            self._save_session_state(session_id, state)
            self._ulog(session_id, "PUSH", f"对话后续场推送已发送 delay={delay / 60:.1f}min")
        return ok

    def _create_scheduled_push_task(
        self,
        session_id: str,
        local_dt: datetime,
        *,
        mode_override: str,
        trigger_time: str = "",
        mark_morning: bool = False,
        skip_active_check: bool = False,
    ):
        """启动后台推送任务，并只在实际成功后写完成标记。"""

        async def runner():
            try:
                ok = await self._sched_fire(
                    session_id,
                    local_dt,
                    mode_override=mode_override,
                    skip_active_check=skip_active_check,
                )
            except Exception as exc:
                ok = False
                self._ulog(session_id, "PUSH", f"后台推送任务异常 mode={mode_override}: {exc}")
                logger.error("scheduled push task failed: %s", exc, exc_info=True)
            if ok:
                if trigger_time:
                    self._mark_daily_triggered_time(session_id, trigger_time, reason="sent")
                if mark_morning:
                    self._mark_morning_greet_sent(session_id, local_dt, reason="sent")
            elif trigger_time:
                self._ulog(session_id, "PUSH", f"随机推送点 {trigger_time} 本次未完成，窗口内等待重试")
            elif mark_morning:
                self._ulog(session_id, "PUSH", "早安推送本次未完成，窗口内等待重试")
            return ok

        return asyncio.create_task(runner())

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
                        if self._check_goodnight_inhibition(state):
                            self._mark_morning_greet_sent(session_id, now, reason="inhibited-goodnight")
                        elif session_id not in self._active_pushes:
                            self._create_scheduled_push_task(
                                session_id,
                                now,
                                mode_override="morning",
                                mark_morning=True,
                            )

                    triggered = session_schema.get_daily_triggered_times(state)
                    for t in session_schema.get_daily_trigger_times(state):
                        if t > time_str or t in triggered:
                            continue
                        t_min = int(t.split(":")[0]) * 60 + int(t.split(":")[1])
                        now_min = now.hour * 60 + now.minute
                        if now_min - t_min > 5:
                            self._mark_daily_triggered_time(session_id, t, reason="missed-window")
                            continue
                        if self._check_goodnight_inhibition(state):
                            self._mark_daily_triggered_time(session_id, t, reason="inhibited-goodnight")
                            continue
                        if session_id not in self._active_pushes:
                            self._create_scheduled_push_task(
                                session_id,
                                now,
                                mode_override="normal",
                                trigger_time=t,
                            )
                            break

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

    async def _sched_fire(self, session_id: str, local_dt: datetime, mode_override=None, skip_active_check=False) -> bool:
        if not session_id or (not skip_active_check and session_id in self._active_pushes):
            return False
        self._active_pushes.add(session_id)
        chat_id = self.chat_id_from_session(session_id)
        try:
            state = self._get_session_state(session_id)
            if self._check_goodnight_inhibition(state):
                self._ulog(session_id, "PUSH", "跳过推送: goodnight inhibition")
                return False
            if not skip_active_check and self._is_recently_active(state):
                self._ulog(session_id, "PUSH", "跳过推送: session recently active")
                return False
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
            if hasattr(self, "ensure_life_plan_for_today"):
                try:
                    await self.ensure_life_plan_for_today(session_id, force=False, reason=f"push-{mode}")
                except Exception:
                    logger.debug("life plan ensure failed for scheduler push", exc_info=True)
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
            scene, caption, new_app, view, orientation = await self._llm_write_scene(
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
                self._ulog(session_id, "PUSH", f"推送规划为空 mode={mode}")
                return False
            english = await self._translate_to_tags(scene, session_id=session_id, view=view)
            ok, imgs, err = await self._do_generate(
                english,
                is_ntr=(mode == "ntr"),
                session_id=session_id,
                one_shot_appearance=new_app or "",
                orientation=orientation or "",
            )
            if ok and imgs:
                await self.send_photo(chat_id, imgs[0], caption or "")
                source = self._format_image_source_description(
                    intent=f"{mode} 模式自动推送，时段: {time_period}，天气: {weather}",
                    prompt=caption or "",
                )
                source_kind = "followup_push" if mode == "followup" else ("manual_push" if skip_active_check else "scheduled_push")
                self._record_sent_photo(
                    session_id,
                    scene,
                    caption or "",
                    appearance=new_app or None,
                    view=view,
                    source_description=source,
                    source_kind=source_kind,
                )
                return True
            else:
                self._ulog(session_id, "PUSH", f"生图失败 mode={mode}: {err}")
                logger.error("scheduled generate failed: %s", err)
                return False
        except Exception as exc:
            self._ulog(session_id, "PUSH", f"推送异常 mode={mode_override or 'normal'}: {exc}")
            logger.error("scheduled push failed: %s", exc, exc_info=True)
            return False
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
