from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from . import appearance as appearance_rules
from . import generation as image_generation
from . import prompt_intake
from . import session_schema
from . import web_search
from .app_store import AppStateStore
from .appearance_runtime import (
    AppearanceRuntimeMixin,
    CHAT_VISUAL_NOISE_TAGS,
    PERSISTENT_ACCESSORY_FAMILY_TERMS,
    WARDROBE_STATE_EVENT_PREFIX,
    _CJK_RE,
    _HAS_CJK,
)
from .character_checkpoint import CharacterCheckpointMixin
from .defaults import DEFAULT_CONFIG
from .deletion_runtime import DeletionRuntimeMixin
from .image_planning import VALID_VIEWS, plan_roleplay_image
from .image_state_runtime import ImageStateRuntimeMixin
from .memory import LongTermMemoryStore
from .chat_context import ChatContextMixin
from .commands import CommandHandlersMixin
from .git_update import GitUpdateMixin
from .life_plan import LifePlanMixin
from .llm_runtime import LLMRuntimeMixin
from .memory_policy import MemoryPolicyMixin
from .process_restart import ProcessRestartMixin
from .scheduler_runtime import SchedulerRuntimeMixin
from .state_runtime import ServiceStateMixin
from .task_runtime import TaskRuntimeMixin
from .telegram_io import TelegramIOMixin
from .telegram_update_runtime import TelegramUpdateRuntimeMixin
from .time_context import build_time_context, format_light_guard, format_time_context, rough_time_period
from .world_runtime import WorldRuntimeMixin

logger = logging.getLogger(__name__)

IMG_CALL_LEAK_RE = re.compile(
    r"\*{0,2}\s*[（(]\s*(?:调用|使用|call)?\s*`?generate_roleplay_image`?\s*[:：，,]?\s*(.*?)\s*[)）]\s*\*{0,2}",
    re.DOTALL | re.IGNORECASE,
)
IMG_NARRATION_LEAK_RE = re.compile(
    r"\*{0,2}\s*[（(]\s*[^（()）]*?(?:照片|画面|展示|呈现|出现在用户眼前)[^（()）]*?[:：]\s*(?:[^（()）]*?)[)）]\s*\*{0,2}",
    re.DOTALL,
)
VISUAL_IDENTITY_OVERRIDES = {
    ("天童爱丽丝", "碧蓝档案"): ("aris (blue archive)", "Blue Archive"),
    ("天童爱丽丝", "蔚蓝档案"): ("aris (blue archive)", "Blue Archive"),
    ("天童アリス", "ブルーアーカイブ"): ("aris (blue archive)", "Blue Archive"),
    ("Arisu Tendou", "Blue Archive"): ("aris (blue archive)", "Blue Archive"),
    ("Tendou Alice", "Blue Archive"): ("aris (blue archive)", "Blue Archive"),
    ("和泉紗霧", "エロマンガ先生"): ("izumi sagiri", "Eromanga Sensei"),
    ("和泉纱雾", "埃罗芒阿老师"): ("izumi sagiri", "Eromanga Sensei"),
    ("Kirito", "Sword Art Online"): ("kirito", "Sword Art Online"),
    ("Serika Kuromi", "Blue Archive"): ("kuromi serika", "Blue Archive"),
    ("Kuromi Serika", "Blue Archive"): ("kuromi serika", "Blue Archive"),
    ("Yukikaze", "Azur Lane"): ("yukikaze (azur lane)", "Azur Lane"),
    ("Jeanne d'Arc", "Fate/Grand Order"): ("jeanne d'arc (fate)", "Fate/Grand Order"),
    ("雷军", "小米公司"): ("Lei Jun", "Xiaomi"),
    ("姬野星奏", "想要传达给你的爱恋"): ("Himeno Sena", "Koi x Shin Ai Kanojo"),
    ("姫野星奏", "恋×シンアイ彼女"): ("Himeno Sena", "Koi x Shin Ai Kanojo"),
}

SERIES_CANONICAL_NAMES = {
    "blue archive": "Blue Archive",
    "azur lane": "Azur Lane",
    "fate/grand order": "Fate/Grand Order",
    "sword art online": "Sword Art Online",
    "eromanga sensei": "Eromanga Sensei",
    "eromanga-sensei": "Eromanga Sensei",
    "koi x shin ai kanojo": "Koi x Shin Ai Kanojo",
    "xiaomi": "Xiaomi",
}




class TelegramComfyUIService(
    ProcessRestartMixin,
    LLMRuntimeMixin,
    ServiceStateMixin,
    AppearanceRuntimeMixin,
    ImageStateRuntimeMixin,
    TaskRuntimeMixin,
    DeletionRuntimeMixin,
    TelegramUpdateRuntimeMixin,
    TelegramIOMixin,
    CharacterCheckpointMixin,
    CommandHandlersMixin,
    ChatContextMixin,
    MemoryPolicyMixin,
    LifePlanMixin,
    SchedulerRuntimeMixin,
    WorldRuntimeMixin,
    GitUpdateMixin,
):
    def __init__(self, config_path: str | Path = "data/config.json", state_path: str | Path = "data/state.json"):
        self.config_path = Path(config_path)
        if self.config_path.suffix.lower() == ".json":
            yml_path = self.config_path.with_suffix(".yml")
            if yml_path.exists() and not self.config_path.exists():
                self.config_path = yml_path
        self.state_path = Path(state_path)
        self.config = self._load_config()
        self.memory = LongTermMemoryStore(self._memory_db_path())
        self.app_store = AppStateStore(self.memory.path)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.city_place_catalogs: dict[str, dict[str, Any]] = {}
        self.startup_backup_paths: list[str] = []
        self._load_state()

        self.http: aiohttp.ClientSession | None = None
        self.comfy_session: aiohttp.ClientSession | None = None
        self._gen_lock = asyncio.Lock()
        self._generating = False
        self._active_pushes: set[str] = set()
        self._push_locks: dict[str, asyncio.Lock] = {}
        self._dirty_sessions: set[str] = set()
        self._last_state_write = 0.0
        self._state_write_interval = 30.0
        self._weather_caches: dict[str, dict[str, Any]] = {}
        self._skill_reference_cache: str | None = None
        self._bot_username = ""
        self._offset = 0
        self._init_task_runtime()
        self._init_deletion_runtime()
        self._init_telegram_update_runtime()
        self._bot_tasks: list[asyncio.Task] = []
        self._checkpoint_tasks: dict[str, asyncio.Task] = {}
        self._checkpoint_locks: dict[str, asyncio.Lock] = {}
        self._dream_tasks: dict[str, asyncio.Task] = {}
        self._life_plan_tasks: dict[str, asyncio.Task] = {}
        self._post_chat_push_tasks: dict[str, asyncio.Task] = {}
        self._interruptible_tasks: dict[str, set[asyncio.Task]] = {}
        self._pending_photo_inputs: dict[str, dict[str, Any]] = {}
        self._pending_media_group_inputs: dict[str, dict[str, Any]] = {}
        self._protected_image_tasks: set[asyncio.Task] = set()
        self._llm_debug_buffer: list[dict[str, Any]] = []
        self._llm_debug_flush_threshold = 10
        self._web_runner: Any = None
        self._stop_event: asyncio.Event | None = None
        self.process_started_at = time.time()
        self._restart_requested = False

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------
    async def run(self):
        self._stop_event = asyncio.Event()
        if self.config.get("web_enabled", True):
            await self.start_web_console()

        if self.config.get("telegram_bot_token", ""):
            await self.start_bot()
        elif not self.config.get("web_enabled", True):
            raise RuntimeError("telegram_bot_token 未配置，请先复制 config.example.json 并填写 token")
        else:
            logger.warning("telegram_bot_token 未配置；仅启动本地 Web 控制台。")

        try:
            await self._stop_event.wait()
        finally:
            await self.stop_bot()
            await self.stop_web_console()
            await self.close()

    def _telegram_proxy_url(self) -> str:
        enabled = self.config.get("telegram_proxy_enabled", False)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("true", "1", "yes", "on")
        if not enabled:
            return ""
        return str(self.config.get("telegram_proxy_url") or "").strip()

    def _telegram_proxy_connector(self):
        proxy = self._telegram_proxy_url()
        if not proxy or not proxy.lower().startswith(("socks5://", "socks4://")):
            return None
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError as exc:
            raise RuntimeError("telegram_proxy_url uses SOCKS proxy, please install aiohttp_socks") from exc
        return ProxyConnector.from_url(proxy)

    def _telegram_http_proxy(self) -> str:
        proxy = self._telegram_proxy_url()
        if proxy.lower().startswith(("http://", "https://")):
            return proxy
        return ""

    async def start_bot(self):
        if self.is_bot_running:
            return
        token = self.config.get("telegram_bot_token", "")
        if not token:
            raise RuntimeError("telegram_bot_token 未配置")
        timeout = aiohttp.ClientTimeout(total=620)
        connector = self._telegram_proxy_connector()
        self.http = aiohttp.ClientSession(timeout=timeout, trust_env=(connector is None), connector=connector)
        me = await self.tg_api("getMe")
        self._bot_username = (me.get("result") or {}).get("username", "")
        await self._start_telegram_update_runtime()
        self._bot_tasks = [
            self._spawn_background(
                self.poll_loop(),
                name="telegram-poll-loop",
                scope="bot-loop",
            ),
            self._spawn_background(
                self.scheduler_loop(),
                name="selfie-scheduler-loop",
                scope="bot-loop",
            ),
        ]
        logger.info("Telegram bot connected as @%s", self._bot_username or "?")

    async def stop_bot(self):
        for task in list(self._bot_tasks):
            if not task.done():
                task.cancel()
        for task in list(self._bot_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("bot task stopped with error: %s", exc)
        self._bot_tasks.clear()
        drain_timeout = self._telegram_update_float_config(
            "telegram_update_drain_timeout_seconds",
            30.0,
        )
        await self._stop_telegram_update_runtime(timeout=drain_timeout)
        await self._shutdown_background_tasks(drain_timeout, final=False)
        await self._drain_protected_image_tasks(drain_timeout)
        if self.http is not None and not self.http.closed:
            await self.http.close()
        self.http = None

    async def start_web_console(self):
        if self._web_runner is not None:
            return
        from aiohttp import web
        from .webui import create_web_app

        host = str(self.config.get("web_host", "127.0.0.1"))
        port = int(self.config.get("web_port", 8787))
        app = create_web_app(self)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._web_runner = runner
        logger.info("Web console listening on http://%s:%s", host, port)

    async def stop_web_console(self):
        runner = self._web_runner
        self._web_runner = None
        if runner is not None:
            await runner.cleanup()

    @property
    def is_bot_running(self) -> bool:
        return bool(self.http and not self.http.closed and self._bot_tasks and all(not task.done() for task in self._bot_tasks))

    async def close(self):
        drain_timeout = self._telegram_update_float_config(
            "telegram_update_drain_timeout_seconds",
            30.0,
        )
        await self._shutdown_background_tasks(drain_timeout, final=True)
        self._flush_sessions(force=True)
        self._flush_llm_debug(force=True)
        if self.comfy_session and not self.comfy_session.closed:
            await self.comfy_session.close()

    # ---------------------------------------------------------------------
    # Chat handling
    # ---------------------------------------------------------------------
    # ---------------------------------------------------------------------
    # Session-derived behavior
    # ---------------------------------------------------------------------
    @property
    def comfyui_url(self) -> str:
        return self.config.get("comfyui_url", "http://127.0.0.1:8188").rstrip("/")

    @property
    def _local_tz(self):
        return timezone(timedelta(hours=float(self.config.get("timezone_offset", "8.0"))))

    def _session_tz(self, session_id: str = ""):
        raw = self._get_session_cfg(session_id, "timezone_offset", self.config.get("timezone_offset", "8.0"))
        try:
            return timezone(timedelta(hours=float(raw)))
        except (TypeError, ValueError):
            return self._local_tz

    def _session_now(self, session_id: str = ""):
        return datetime.now(self._session_tz(session_id))

    @staticmethod
    def _offset_from_lon(lon):
        try:
            return float(round(float(lon) / 15))
        except (TypeError, ValueError):
            return None

    def _is_character_set(self, session_id: str) -> bool:
        state = self._get_session_state(session_id)
        return bool(
            session_schema.get_character_value(state, "custom_character", "")
            or session_schema.get_character_value(state, "persona_user_set", False)
        )

    def _get_purity(self, session_id: str) -> int:
        state = self._get_session_state(session_id) if session_id else {}
        raw = session_schema.get_character_value(state, "purity") if state else None
        if raw is not None:
            try:
                return max(0, min(10, int(raw)))
            except (TypeError, ValueError):
                return 5
        default = self.config.get("default_purity", "")
        if default not in ("", None):
            try:
                return max(0, min(10, int(default)))
            except (TypeError, ValueError):
                pass
        return 1

    @staticmethod
    def _compute_ntr_threshold(purity: int) -> int:
        if purity <= 0:
            return 1
        if purity >= 10:
            return 99999
        return int(7 + (purity - 1) * (120 - 7) / 8)

    @staticmethod
    def _compute_ntr_stage(days_since: float, threshold_days: int) -> int:
        if threshold_days <= 0 or days_since <= 0:
            return 0
        ratio = days_since / threshold_days
        if ratio >= 1.0:
            return 5
        if ratio >= 0.9:
            return 4
        if ratio >= 0.75:
            return 3
        if ratio >= 0.5:
            return 2
        if ratio >= 0.25:
            return 1
        return 0

    def _get_effective_safety(self, session_id: str) -> dict[str, Any]:
        purity = self._get_purity(session_id)
        now = self._session_now(session_id)
        period = self._get_time_context(session_id, now=now).get("period") or self._get_time_period(now.hour)
        effective = purity
        context = ""
        if period == "深夜":
            effective -= 3
            context = "深夜时段，氛围更私密暧昧"
        elif period == "傍晚":
            effective -= 1
            context = "傍晚时段，一天工作结束"
        elif period == "早晨":
            effective += 1
            context = "早晨时段，适合保持清新"
        if now.weekday() >= 5:
            effective -= 1
            context = (context + "；周末放松模式") if context else "周末放松模式"
        effective = max(0, min(10, effective))
        tag = "nsfw" if effective <= 2 else "safe" if effective >= 8 else None
        return {"level": effective, "tag": tag, "context": context}

    def _get_effective_persona(self, session_id: str, include_appearance: bool = True) -> str:
        """读时组装聊天人格：纯人格描述串 + 身份安全前缀 + 短期附加外型。

        custom_scheduled_persona 只存纯人格描述（性格/语气/习惯），不含身份、角色类型、
        关系、职业——这些是字段单源，由本函数（身份）和各 prompt 的身份行/关系行实时拼。

        include_appearance=False：不追加短期穿搭。供放进【静态前缀（messages[0]）】的调用方
        使用——穿搭是中频变化字段，焊进静态前缀会让每次换装作废整条历史的服务端 prefix cache；
        聊天/场景 builder 已在【动态层】单独注入当前外型，故静态前缀这里去掉，避免双注入+毒化缓存。
        """
        state = self._get_session_state(session_id)
        char_set = self._is_character_set(session_id)
        if char_set:
            base = session_schema.get_character_value(state, "custom_scheduled_persona", "")
            character = session_schema.get_character_value(state, "custom_character", "")
            if character:
                base = self._persona_with_character_identity(
                    character,
                    session_schema.get_character_value(state, "custom_series", ""),
                    base,
                )
        else:
            base = self._get_session_cfg(session_id, "scheduled_persona", DEFAULT_CONFIG["scheduled_persona"])
        if not base:
            # 角色态下人设为空时，用角色名构造最小身份，避免回退到全局默认（魅魔蕾伊）串到其他 OC 角色。
            # 非角色态（无角色/全局默认）才回退到全局 scheduled_persona。
            character = session_schema.get_character_value(state, "custom_character", "")
            if char_set and character:
                base = f"你是{character}。"
            elif char_set:
                bot = session_schema.get_character_value(state, "custom_bot_name", "") or self.config.get("bot_name", "角色")
                base = f"你是{bot}。"
            else:
                base = self.config.get("scheduled_persona") or DEFAULT_CONFIG["scheduled_persona"]
        if not include_appearance:
            return base
        additional = self._effective_dynamic_appearance(session_id)
        return f"{base}\n\n[当前附加人设/短期穿搭与配饰: {additional}]" if additional else base


    @staticmethod
    def _purity_directive(purity: int) -> str:
        if purity <= 0:
            return "你是纯粹的性欲化身：无条件且主动地发情，频繁发送挑逗自拍，也可能主动制造 NTR 危机感。"
        if purity <= 2:
            return "你对性始终保持开放，任意场合、任意时间都不设防，语言风格挑逗暧昧。"
        if purity <= 4:
            return "你多数情况下较为开放、乐于主动挑逗，但会视场合气氛适当收敛。"
        if purity == 5:
            return "在合适的私密空间你对亲密保持开放，在公开或不合适的场合保持得体。"
        if purity <= 7:
            return "只有在合适的私密场合和合适时间，氛围到位时你才会逐渐放开。"
        if purity <= 9:
            return "你性格保守，不轻易越界；只有在对方持续引导下才一点点打开心扉。"
        return "你是纯洁的天使，完全不涉及任何性相关内容，言行永远纯真 safe。"

    def _get_user_gender(self, session_id: str = "") -> str:
        """用户自己的性别（male/female/空）：只在用户身体明确入画时决定局部身体词。"""
        state = self._get_session_state(session_id) if session_id else {}
        raw = state.get("custom_user_gender")
        if raw in (None, ""):
            raw = self.config.get("user_gender")
        g = re.sub(r"\s+", "", str(raw).strip().lower())
        if g in ("female", "f", "woman", "女", "女性", "女生", "girl"):
            return "female"
        if g in ("male", "m", "man", "男", "男性", "男生", "boy"):
            return "male"
        return ""

    @staticmethod
    def _get_time_period(hour: int) -> str:
        return rough_time_period(hour)

    def _get_time_context(self, session_id: str = "", now: datetime | None = None, weather: Any = None) -> dict[str, Any]:
        now = now or self._session_now(session_id)
        if weather is None:
            cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
            if isinstance(cached, dict):
                weather = cached.get("data")
        return build_time_context(now, weather)

    def _format_time_context(self, session_id: str = "", now: datetime | None = None, weather: Any = None) -> str:
        return format_time_context(self._get_time_context(session_id, now=now, weather=weather))

    def _format_light_guard(self, session_id: str = "", now: datetime | None = None, weather: Any = None) -> str:
        return format_light_guard(self._get_time_context(session_id, now=now, weather=weather))

    def _tick_ntr_reconcile(self, state: dict[str, Any]) -> bool:
        if not session_schema.get_ntr_affection_reset(state):
            return False
        cnt = session_schema.get_ntr_reconcile_count(state) + 1
        if cnt >= 5:
            session_schema.set_ntr_affection_reset(state, False)
            session_schema.set_ntr_reconcile_count(state, 0)
            return True
        session_schema.set_ntr_reconcile_count(state, cnt)
        return False

    def _touch(self, session_id: str):
        if session_id:
            state = self._get_session_state(session_id)
            session_schema.set_last_interaction(state, time.time())
            session_schema.set_ntr_stage_reached(state, 0)
            self._save_session_state(session_id, state)

    @staticmethod
    def _format_image_source_description(intent: str = "", mood: str = "", must_include: str = "", prompt: str = "") -> str:
        parts = []
        for label, value in (
            ("意图", intent),
            ("情绪/关系推进", mood),
            ("必须包含", must_include),
            ("原始草案/上下文", prompt),
        ):
            value = (value or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        return "；".join(parts)

    def _record_sent_photo(
        self,
        session_id: str,
        scene: str,
        caption: str = "",
        appearance: str | None = None,
        view: str = "",
        source_description: str = "",
        nltag: str = "",
        source_kind: str = "",
        defer_history_message: bool = False,
    ):
        state = self._get_session_state(session_id)
        history = session_schema.get_sent_photos_history(state)
        source_description = (source_description or "").strip()
        nltag_text = (nltag or self._last_generated_photo_nltag(session_id) or scene or "").strip()
        source_intent = self._compact_photo_source_intent(source_description)
        appearance_snapshot = self._last_prompt_visual_appearance(session_id) or (appearance or "").strip()
        if not appearance_snapshot:
            try:
                appearance_snapshot = self._effective_visual_prompt_tags(session_id)
            except Exception:
                appearance_snapshot = session_schema.get_outfit(state)
        visual_state = self._compact_photo_visual_state(scene, nltag_text, appearance_snapshot)
        photo = {
            "timestamp": time.time(),
            "scene": scene,
            "caption": caption,
            "appearance": appearance_snapshot,
            "view": (view or "").strip().lower(),
            "source_kind": (source_kind or "unknown").strip().lower() or "unknown",
            "source_description": source_description,
            "source_intent": source_intent,
            "nltag": nltag_text,
            "visual_state": visual_state,
        }
        history.append(photo)
        session_schema.set_sent_photos_history(state, history[-10:])
        photo_message = self._format_photo_history_system_message(photo)
        if defer_history_message:
            self._queue_pending_photo_history_message(session_id, photo_message)
        else:
            self._append_photo_history_message(session_id, photo_message, state=state)
        session_schema.set_last_sent_selfie_time(state, time.time())
        session_schema.set_last_sent_selfie_caption(state, caption)
        session_schema.set_last_sent_selfie_source_description(state, source_description or scene)
        session_schema.set_last_sent_selfie_replied(state, False)
        session_schema.set_rounds_since_image(state, 0)
        self._save_session_state(session_id, state)
        self._ulog(
            session_id, "IMAGE",
            f"view={(view or '').strip().lower() or '?'} caption={caption or '-'} scene={scene}",
        )

    def _last_generated_photo_nltag(self, session_id: str = "") -> str:
        try:
            if session_id:
                cache = getattr(self, "_last_generated_nltag_by_session", {})
                if isinstance(cache, dict):
                    text = str(cache.get(session_id) or "").strip()
                    if text:
                        return text
            return str(getattr(self, "_last_generated_nltag", "") or "").strip()
        except Exception:
            return ""

    def _last_prompt_visual_appearance(self, session_id: str = "") -> str:
        try:
            slots = getattr(self, "_last_prompt_slots", None)
            if not slots:
                return ""
            slot_sid = str(getattr(slots, "session_id", "") or "")
            if session_id and slot_sid and slot_sid != session_id:
                return ""
            parts = [
                str(getattr(slots, "effective_appearance", "") or "").strip(),
                str(getattr(slots, "one_shot_appearance", "") or "").strip(),
            ]
            return ", ".join(part for part in parts if part)
        except Exception:
            return ""

    @staticmethod
    def _compact_photo_source_intent(source_description: str, max_chars: int = 180) -> str:
        text = re.sub(r"\s+", " ", str(source_description or "")).strip()
        if not text:
            return ""
        chunks = [part.strip() for part in re.split(r"[；;]\s*", text) if part.strip()]
        keep: list[str] = []
        saw_labeled = False
        for chunk in chunks:
            match = re.match(r"^(意图|情绪/关系推进|必须包含|原始草案/上下文)\s*[:：]\s*(.+)$", chunk)
            if not match:
                continue
            saw_labeled = True
            label, value = match.group(1), match.group(2).strip()
            if label == "原始草案/上下文" or not value:
                continue
            keep.append(f"{label}: {value}")
        result = "；".join(keep)
        if not result and not saw_labeled and "原始草案/上下文" not in text:
            result = f"意图: {text}"
        if len(result) > max_chars:
            result = result[:max_chars].rstrip() + "..."
        return result

    def _compact_photo_visual_state(self, scene: str, nltag: str = "", appearance: str = "", max_chars: int = 180) -> str:
        visual = re.sub(r"\s+", " ", " ".join([str(nltag or ""), str(scene or "")])).strip()
        visual_lower = visual.lower()
        if re.search(
            r"\b(nude|naked|completely nude|fully undressed|undressed|no clothes|topless|bottomless|no panties|no underwear|exposed breasts|exposed nipples)\b",
            visual_lower,
        ):
            return "visible clothing: nude / not properly dressed"
        try:
            parsed = self._parse_appearance(appearance or "")
        except Exception:
            parsed = {}
        outfit: list[str] = []
        if isinstance(parsed, dict):
            for key in ("outfit", "accessory"):
                outfit.extend(str(tag or "").strip() for tag in parsed.get(key, []) or [])
        outfit = [tag for tag in outfit if tag]
        if not outfit:
            return ""
        result = "visible outfit: " + ", ".join(outfit[:6])
        if len(result) > max_chars:
            result = result[:max_chars].rstrip() + "..."
        return result

    @staticmethod
    def _format_photo_history_system_message(photo: dict[str, Any]) -> dict[str, str]:
        scene = str(photo.get("scene") or "").strip()
        caption = str(photo.get("caption") or "").strip()
        nltag = str(photo.get("nltag") or "").strip() or scene
        source_intent = (
            str(photo.get("source_intent") or "").strip()
            or TelegramComfyUIService._compact_photo_source_intent(str(photo.get("source_description") or ""))
        )
        view = str(photo.get("view") or "").strip()
        source_kind = str(photo.get("source_kind") or "unknown").strip()
        lines = [
            "照片历史（系统记录，保留到 checkpoint/历史溢出统一裁剪；低权重连续性参考，用户明确提到照片/刚才画面时再引用，不要主动复述）：",
            f"source_kind: {source_kind or 'unknown'}",
            f"view: {view or '未知视角'}",
            f"nltag: {nltag or '未记录'}",
        ]
        visual_state = str(photo.get("visual_state") or "").strip()
        if visual_state:
            lines.append(f"visual_state: {visual_state}")
        if source_intent:
            lines.append(f"source_intent: {source_intent}")
        if caption and caption != scene:
            lines.append(f"caption: {caption}")
        return {"role": "system", "content": "\n".join(lines)}

    def _pending_photo_history_bucket(self) -> dict[str, list[dict[str, str]]]:
        bucket = getattr(self, "_pending_photo_history_messages", None)
        if not isinstance(bucket, dict):
            bucket = {}
            self._pending_photo_history_messages = bucket
        return bucket

    def _queue_pending_photo_history_message(self, session_id: str, message: dict[str, str]):
        self._pending_photo_history_bucket().setdefault(session_id, []).append(message)

    def _take_pending_photo_history_messages(self, session_id: str) -> list[dict[str, str]]:
        return self._pending_photo_history_bucket().pop(session_id, [])

    def _append_photo_history_message(self, session_id: str, message: dict[str, str], *, state: dict[str, Any] | None = None):
        if not session_id or not message:
            return
        state = state if state is not None else self._get_session_state(session_id)
        history = session_schema.get_chat_history(state)
        history.append(dict(message))
        session_schema.set_chat_history(state, history)
        try:
            self.app_store.append_messages(session_id, self._context_character_key(session_id), [dict(message)])
        except Exception:
            logger.warning("photo history sqlite append failed", exc_info=True)
        if hasattr(self, "_apply_history_trim"):
            self._apply_history_trim(state, self._history_storage_cap())

    @staticmethod
    def _identity_key(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).lower()

    @staticmethod
    def _canon_visual_series(value: str) -> str:
        cleaned = image_generation._clean_visual_identity_tag(value)
        return SERIES_CANONICAL_NAMES.get(cleaned.lower(), cleaned)

    def _known_visual_identity(self, character: Any, series: Any) -> tuple[str, str]:
        key = (self._identity_key(character), self._identity_key(series))
        for (known_character, known_series), visual in VISUAL_IDENTITY_OVERRIDES.items():
            if key == (self._identity_key(known_character), self._identity_key(known_series)):
                return visual
        return "", ""

    def _resolve_migrated_visual_identity(self, character: Any, series: Any, *sources: Any) -> tuple[str, str]:
        series_key = self._identity_key(series)
        if series_key in image_generation.ORIGINAL_SERIES_MARKERS:
            return "", ""
        mapped = self._known_visual_identity(character, series)
        if mapped[0] and mapped[1]:
            return mapped
        fallback_source = ", ".join(str(source or "") for source in sources if source)
        fallback_character, fallback_series = image_generation._appearance_identity_fallback(fallback_source)
        if fallback_character and fallback_series:
            return fallback_character, self._canon_visual_series(fallback_series)
        clean_character = image_generation._clean_visual_identity_tag(character)
        clean_series = self._canon_visual_series(str(series or ""))
        if clean_character and clean_series:
            return clean_character, clean_series
        return "", ""

    def migrate_visual_identity_tags(self, *, create_backup: bool = True) -> dict[str, Any]:
        updates: list[str] = []

        def update_pair(container: dict[str, Any], char_key: str, series_key: str, visual_char_key: str, visual_series_key: str, *sources: Any) -> bool:
            character = container.get(char_key, "")
            series = container.get(series_key, "")
            if not character and not series:
                container.setdefault(visual_char_key, "")
                container.setdefault(visual_series_key, "")
                return False
            target_character, target_series = self._resolve_migrated_visual_identity(character, series, *sources)
            current_character = container.get(visual_char_key) or ""
            current_series = container.get(visual_series_key) or ""
            missing_keys = visual_char_key not in container or visual_series_key not in container
            if not missing_keys and (current_character, current_series) == (target_character, target_series):
                return False
            container[visual_char_key] = target_character
            container[visual_series_key] = target_series
            return True

        sessions_updated = 0
        saved_updated = 0
        for sid in list(self.sessions.keys()):
            state = self._get_session_state(sid)
            if update_pair(
                state,
                "custom_character",
                "custom_series",
                "custom_visual_character",
                "custom_visual_series",
                session_schema.get_character_value(state, "custom_positive_prefix", ""),
                session_schema.get_outfit(state),
            ):
                session_schema.set_character_value(state, "custom_visual_character", state.get("custom_visual_character", ""))
                session_schema.set_character_value(state, "custom_visual_series", state.get("custom_visual_series", ""))
                sessions_updated += 1
                updates.append(f"{sid}: {state.get('custom_character') or '-'} -> {state.get('custom_visual_character') or '(blank)'} / {state.get('custom_visual_series') or '(blank)'}")
            saved = session_schema.get_saved_characters(state)
            for key, data in saved.items():
                if not isinstance(data, dict):
                    continue
                if update_pair(
                    data,
                    "character",
                    "series",
                    "visual_character",
                    "visual_series",
                    data.get("appearance", ""),
                ):
                    saved_updated += 1
                    updates.append(f"{sid} saved:{key}: {data.get('visual_character') or '(blank)'} / {data.get('visual_series') or '(blank)'}")

        backup_path = ""
        if (sessions_updated or saved_updated) and create_backup and self.app_store.path.exists():
            backup = self.app_store.path.with_name(f"{self.app_store.path.stem}.visual-tags-backup-{int(time.time())}{self.app_store.path.suffix}")
            backup.write_bytes(self.app_store.path.read_bytes())
            backup_path = str(backup)
        if sessions_updated or saved_updated:
            for sid in self.sessions:
                self._mark_dirty(sid)
            self._flush_sessions(force=True)
        return {
            "sessions_updated": sessions_updated,
            "saved_characters_updated": saved_updated,
            "backup_path": backup_path,
            "updates": updates,
        }

    @staticmethod
    def _preview_style_pool(raw: Any) -> list[str]:
        if isinstance(raw, str):
            parts = re.split(r"[\n;；]+", raw)
        elif isinstance(raw, list):
            parts = raw
        else:
            parts = []
        pool: list[str] = []
        seen: set[str] = set()
        for item in parts:
            style = str(item or "").strip()
            key = style.lower()
            if style and key not in seen:
                pool.append(style)
                seen.add(key)
        return pool or ["@00 gx4"]

    def _preview_current_style(self, state: dict[str, Any] | None = None, saved: dict[str, Any] | None = None) -> str:
        if saved and str(saved.get("style") or "").strip():
            return str(saved.get("style") or "").strip()
        if state and str(session_schema.get_character_value(state, "custom_current_style", "") or "").strip():
            return str(session_schema.get_character_value(state, "custom_current_style", "") or "").strip()
        current = str(self.config.get("current_style") or "").strip()
        if current:
            return current
        return self._preview_style_pool(self.config.get("style_pool") or self.config.get("style_prefix"))[0]

    @staticmethod
    def _combine_prompt_styles(*styles: str) -> str:
        tags: list[str] = []
        for style in styles:
            tags.extend(image_generation._split_tags(style))
        return image_generation._join_unique_tags(tags)

    @staticmethod
    def _clean_prompt_prefix_value(value: Any) -> dict[str, str] | None:
        before = str(value or "").strip()
        if not before:
            return None
        parts = image_generation._split_prompt_prefix(before)
        if not parts.quality and not parts.style and not parts.count:
            return None
        after = parts.base
        if after == before and not parts.style:
            return None
        return {
            "before": before,
            "after": after,
            "removed_quality": parts.quality,
            "removed_count": parts.count,
            "moved_style": parts.style,
        }

    def _add_style_pool_entry(self, style: str):
        style = (style or "").strip()
        if not style:
            return
        pool = self._normalize_style_pool()
        if style not in pool:
            pool.append(style)
            self.config["style_pool"] = "\n".join(pool)
        self.config["current_style"] = style

    def cleanup_prompt_prefix_slots(self, *, apply: bool = False, create_backup: bool = True) -> dict[str, Any]:
        """清理老数据中混进 positive_prefix 的质量词和风格词，并将人数词迁移到 custom_count 槽。

        默认只预览，不改配置/状态。执行时会先备份，再把质量词和人数词从存储中删掉，并把风格词合并到
        current_style/custom_current_style/saved_character.style。人数词迁移到 custom_count 字段。
        """
        changes: list[dict[str, Any]] = []

        config_clean = self._clean_prompt_prefix_value(self.config.get("positive_prefix", ""))
        if config_clean:
            style_before = self._preview_current_style()
            style_after = self._combine_prompt_styles(style_before, config_clean["moved_style"])
            changes.append({
                "scope": "config",
                "label": "config.positive_prefix",
                "session_id": "",
                "character": "",
                "field": "positive_prefix",
                "style_field": "current_style",
                "style_before": style_before,
                "style_after": style_after,
                **config_clean,
            })

        for sid in list(self.sessions.keys()):
            state = self._get_session_state(sid)
            state_clean = self._clean_prompt_prefix_value(session_schema.get_character_value(state, "custom_positive_prefix", ""))
            if state_clean:
                style_before = self._preview_current_style(state)
                style_after = self._combine_prompt_styles(style_before, state_clean["moved_style"])
                changes.append({
                    "scope": "session",
                    "label": f"{sid}.custom_positive_prefix",
                    "session_id": sid,
                    "character": session_schema.get_character_value(state, "custom_character", "") or "",
                    "field": "custom_positive_prefix",
                    "style_field": "custom_current_style",
                    "style_before": style_before,
                    "style_after": style_after,
                    **state_clean,
                })

            saved = session_schema.get_saved_characters(state)
            if not isinstance(saved, dict):
                continue
            for key, data in saved.items():
                if not isinstance(data, dict):
                    continue
                saved_clean = self._clean_prompt_prefix_value(data.get("appearance", ""))
                if not saved_clean:
                    continue
                style_before = self._preview_current_style(state, data)
                style_after = self._combine_prompt_styles(style_before, saved_clean["moved_style"])
                changes.append({
                    "scope": "saved_character",
                    "label": f"{sid}.saved_characters.{key}.appearance",
                    "session_id": sid,
                    "saved_key": str(key),
                    "character": data.get("character") or str(key),
                    "field": "appearance",
                    "style_field": "style",
                    "style_before": style_before,
                    "style_after": style_after,
                    **saved_clean,
                })

        result: dict[str, Any] = {
            "applied": bool(apply),
            "config_updated": 0,
            "sessions_updated": 0,
            "saved_characters_updated": 0,
            "count_migrated": 0,
            "backup_paths": [],
            "changes": changes,
        }
        if not apply or not changes:
            return result

        config_changed = any(change["scope"] == "config" for change in changes)
        state_changed = any(change["scope"] in {"session", "saved_character"} for change in changes)
        stamp = int(time.time())
        if create_backup and config_changed and self.config_path.exists():
            backup = self.config_path.with_name(f"{self.config_path.stem}.prompt-prefix-backup-{stamp}{self.config_path.suffix}")
            backup.write_bytes(self.config_path.read_bytes())
            result["backup_paths"].append(str(backup))
        if create_backup and state_changed and self.app_store.path.exists():
            backup = self.app_store.path.with_name(f"{self.app_store.path.stem}.prompt-prefix-backup-{stamp}{self.app_store.path.suffix}")
            backup.write_bytes(self.app_store.path.read_bytes())
            result["backup_paths"].append(str(backup))

        touched_sessions: set[str] = set()
        touched_saved = 0
        count_migrated = 0
        for change in changes:
            scope = change["scope"]
            if scope == "config":
                self.config["positive_prefix"] = change["after"]
                if change["moved_style"]:
                    self._add_style_pool_entry(change["style_after"])
                result["config_updated"] = 1
                continue
            sid = change["session_id"]
            state = self._get_session_state(sid)
            removed_count = (change.get("removed_count") or "").strip()
            if scope == "session":
                session_schema.set_character_value(state, "custom_positive_prefix", change["after"])
                if change["moved_style"]:
                    session_schema.set_character_value(state, "custom_current_style", change["style_after"])
                if removed_count and not session_schema.get_character_value(state, "custom_count", ""):
                    session_schema.set_character_value(state, "custom_count", removed_count)
                    count_migrated += 1
                touched_sessions.add(sid)
            elif scope == "saved_character":
                saved = session_schema.get_saved_characters(state)
                data = saved.get(change.get("saved_key", ""))
                if not isinstance(data, dict):
                    for item in saved.values():
                        if isinstance(item, dict) and item.get("character") == change["character"]:
                            data = item
                            break
                if not isinstance(data, dict):
                    continue
                data["appearance"] = change["after"]
                if change["moved_style"]:
                    data["style"] = change["style_after"]
                if removed_count and not data.get("count"):
                    data["count"] = removed_count
                    count_migrated += 1
                touched_sessions.add(sid)
                touched_saved += 1

        if config_changed:
            self.save_config()
        if touched_sessions:
            for sid in touched_sessions:
                self._mark_dirty(sid)
            self._flush_sessions(force=True)
        result["sessions_updated"] = len(touched_sessions)
        result["saved_characters_updated"] = touched_saved
        result["count_migrated"] = count_migrated
        return result

    # ---------------------------------------------------------------------

    @staticmethod
    def _infer_gender_from_prefix(prefix: str) -> str:
        return appearance_rules.infer_gender_from_prefix(prefix)

    @staticmethod
    def _view_opener(view: str, gender: str = "girl") -> str:
        return image_generation.view_opener(view, gender)

    def _build_prompt(
        self,
        scene_desc: str,
        is_ntr: bool = False,
        session_id: str = "",
        one_shot_appearance: str = "",
        is_intimate: bool = False,
        partner_in_frame: bool = False,
        device_in_frame: bool = False,
        clothing_off: str = "",
        ignore_wardrobe_item_states: bool = False,
    ) -> tuple[str, str]:
        return image_generation.build_prompt(
            self, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
            is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
            clothing_off=clothing_off,
            ignore_wardrobe_item_states=ignore_wardrobe_item_states,
        )

    def _format_last_prompt_slots(self, session_id: str = "") -> str:
        slots = None
        if session_id:
            cache = getattr(self, "_last_prompt_slots_by_session", {})
            if isinstance(cache, dict):
                slots = cache.get(session_id)
        slots = slots or getattr(self, "_last_prompt_slots", None)
        if hasattr(slots, "pretty"):
            return slots.pretty()
        return ""

    def _prompt_scene_preferences(self, session_id: str) -> dict[str, str]:
        state = self._get_session_state(session_id)
        raw_intake = session_schema.get_character_value(state, "custom_prompt_intake")
        intake = raw_intake if isinstance(raw_intake, dict) else {}
        scene_preference = session_schema.get_character_value(state, "custom_scene_preference", "") or intake.get("scene_preference") or ""
        selfie_preference = session_schema.get_character_value(state, "custom_selfie_preference", "") or intake.get("selfie_preference") or ""
        return {
            "scene_preference": str(scene_preference).strip(),
            "selfie_preference": str(selfie_preference).strip(),
        }

    def _build_workflow(self, positive: str, negative: str, seed: int) -> dict[str, Any]:
        return image_generation.build_workflow(self, positive, negative, seed)

    def _build_anima_workflow(self, positive: str, negative: str, seed: int) -> dict[str, Any]:
        return image_generation.build_anima_workflow(self, positive, negative, seed)

    # ---------------------------------------------------------------------
    # Vision / ComfyUI
    # ---------------------------------------------------------------------
    def _recent_dialogue_text_for_vision(self, session_id: str, limit: int = 4) -> str:
        """给图片理解模型的短上下文，只取最近两轮实际 user/assistant 对话。"""
        if not session_id:
            return ""
        try:
            state = self._get_session_state(session_id)
            history = session_schema.get_chat_history(state)
        except Exception:
            return ""
        lines: list[str] = []
        for msg in reversed(history):
            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            label = "用户" if role == "user" else "角色"
            lines.append(f"{label}: {content[:500]}")
            if len(lines) >= limit:
                break
        return "\n".join(reversed(lines))

    async def _describe_image_for_chat(
        self,
        session_id: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        *,
        source_label: str = "图片",
        nearby_text: str = "",
    ) -> str:
        """把 Telegram 图片转成纯文本描述，供 chat 输入注入；chat 模型不接收多模态内容。"""
        if not image_bytes or not self.has_llm_config("vision", session_id):
            return ""
        mime_type = (mime_type or "image/jpeg").strip() or "image/jpeg"
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        recent = self._recent_dialogue_text_for_vision(session_id)
        context_parts = []
        if recent:
            context_parts.append("最近两轮对话:\n" + recent)
        if nearby_text:
            context_parts.append("用户当前文字/引用线索:\n" + nearby_text.strip()[:1200])
        context = "\n\n".join(context_parts) or "无额外上下文。"
        system = (
            "你是聊天输入的图片理解器。只负责把图片内容描述成中文纯文本，供后续角色聊天模型阅读。"
            "可以参考最近两轮对话理解代词、场景和用户意图，但不要编造图片里没有的内容。"
            "输出应客观、紧凑，优先描述主体、动作、表情、文字信息、环境和与对话相关的细节。"
            "不要输出 JSON、Markdown 标题或解释。"
        )
        prompt = f"{context}\n\n请描述这张{source_label}，120 字以内。"
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        data = await self._call_llm_messages(messages, tag="describe-image", temp=0.2, purpose="vision", session_id=session_id)
        msg = data.get("choices", [{}])[0].get("message", {})
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
        return text

    async def _describe_images_for_chat(
        self,
        session_id: str,
        images: list[tuple[bytes, str]],
        *,
        source_label: str = "多张图片",
        nearby_text: str = "",
    ) -> str:
        """把 Telegram 相册作为一个整体转成纯文本描述，保留跨图关系。"""
        if not images or not self.has_llm_config("vision", session_id):
            return ""
        recent = self._recent_dialogue_text_for_vision(session_id)
        context_parts = []
        if recent:
            context_parts.append("最近两轮对话:\n" + recent)
        if nearby_text:
            context_parts.append("用户当前文字/引用线索:\n" + nearby_text.strip()[:1200])
        context = "\n\n".join(context_parts) or "无额外上下文。"
        system = (
            "你是聊天输入的图片理解器。用户可能一次发送多张图片；请把这些图片作为同一组相册整体理解，"
            "输出一段中文纯文本供后续角色聊天模型阅读。可以参考最近两轮对话理解代词、场景和用户意图，"
            "但不要编造图片里没有的内容。优先描述每张图的主体差异、共同主题、顺序关系、文字信息、环境和与对话相关的细节。"
            "不要输出 JSON、Markdown 标题或解释。"
        )
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": f"{context}\n\n请统一描述这组{source_label}，180 字以内；如多图之间有对比、连续动作或同一物体的不同角度，请明确说明。",
            }
        ]
        for idx, (image_bytes, mime_type) in enumerate(images[:5], start=1):
            if not image_bytes:
                continue
            mime_type = (mime_type or "image/jpeg").strip() or "image/jpeg"
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
            content.append({"type": "text", "text": f"第 {idx} 张图片:"})
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        if len(content) <= 1:
            return ""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        data = await self._call_llm_messages(messages, tag="describe-images", temp=0.2, purpose="vision", session_id=session_id)
        msg = data.get("choices", [{}])[0].get("message", {})
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
        return text

    async def _translate_to_tags(
        self,
        natural: str,
        session_id: str = "",
        view: str = "",
        is_intimate: bool = False,
        free_composition: bool = False,
    ) -> str:
        if not self.has_llm_config("image", session_id):
            return natural
        view = (view or "").strip().lower()
        if view not in VALID_VIEWS:
            view = ""
        char_prefix = self._get_session_cfg(session_id, "positive_prefix", "")
        state = self._get_session_state(session_id) if session_id else {}
        persisted_count = (session_schema.get_character_value(state, "custom_count", "") or "").strip()
        gender = appearance_rules.infer_gender_from_count(persisted_count) if persisted_count else self._infer_gender_from_prefix(char_prefix)
        opener = self._view_opener(view, gender) if view and not free_composition else ""
        light_guard = self._format_light_guard(session_id)
        weather_text = ""
        cached = self._weather_caches.get(session_id or "__default__")
        if isinstance(cached, dict):
            data = cached.get("data")
            if data:
                weather_text = self._weather_text(data) if hasattr(self, "_weather_text") else str(data)
        weather_guard = ""
        if weather_text:
            weather_guard = (
                f" Current weather: {weather_text}. "
                "Preserve visible weather in the English visual description. "
                "For rain, snow, fog, thunderstorm, wind, heat or cold, show it through the window, ground, umbrella, wet surfaces, clothing, air, sky, or lighting. "
            )
        if free_composition:
            system = (
                "Visual subject rule: the image subject remains the roleplay scene, usually the character, "
                "but the user's explicit composition request has highest priority. "
                "For default or original characters, do not turn role names into English names or visual tags; describe appearance and action instead. "
                "Only keep a character name when it is paired with its published series. "
                "Stable appearance is injected later; do not invent or restate stable hair, eye, body, species, clothing, or accessory traits unless the source explicitly asks for a one-shot change. "
                "你是专业的 Anima3 提示词工程师。把中文场景重构为英文自然语言画面描述，后接少量 danbooru 补强标签。"
                "直接输出英文提示词，不要 JSON、不要解释，不要压缩成纯标签列表。"
                "保留用户指定的视角、机位、远近、焦段、构图和局部特写；不要自动改写成自拍、POV 或看镜头。"
                "允许部位特写、背影、环境承接、道具或手机/相机入画，只要原文明确要求。"
                "自然语言句子尽量不要使用逗号。输出格式: English visual sentence. key tag, key tag, key tag"
            )
        elif view:
            if view == "mirror":
                view_rule = "固定视角是 mirror 对镜自拍；系统会添加镜子和一部手机，你不要重复输出 mirror/phone/smartphone。"
            elif view == "selfie":
                view_rule = "固定视角是 selfie 前摄自拍：角色伸手举着手机自拍、看向镜头；但画面中不得出现手机本体、手机屏幕、手机 UI、相机、镜子、拿手机的手、消息界面、倒计时界面。"
            elif view == "portrait":
                view_rule = "固定视角是 portrait：别人（用户或他人）帮角色拍的照片，角色看向镜头、为镜头摆姿势，拍摄者在画面外，画面里只有角色一个人；画面中不得出现手机、相机、镜子、拿手机的手、手机屏幕、UI、消息界面。"
            elif view == "pov":
                view_rule = "固定视角是 POV；画面中不得出现自拍手机、镜子、拿手机的手、手机屏幕、消息界面、倒计时界面。"
            else:
                view_rule = "固定视角是 third 第三人称；不要把画面写成自拍或对镜自拍。"
            system = (
                "你是专业的 Anima3 提示词工程师。Anima3 支持英文自然语言与 danbooru 标签混编。"
                "把中文画面重构为一句英文自然语言画面描述，后接少量 danbooru 补强标签。"
                "不要输出 JSON、不要前缀、不要解释；不要压缩成纯标签列表。"
                "不要重复输出自拍/POV/镜子/手机/1girl/1boy 等结构词，系统会统一添加。"
                "可以保留 she/the character 作为动作主语，确保坐、站、躺、跪、脚边、腿上、身后、肩膀、手脚等动作和身体关系归属清楚。"
                f"{view_rule}"
                "Visual subject rule: the image subject is the character, not the user. "
                "For default or original characters, do not turn role names into English names or visual tags; describe appearance and action instead. "
                "Only keep a character name when it is paired with its published series. "
                "Stable appearance is injected later; do not invent or restate stable hair, eye, body, species, clothing, or accessory traits unless the source explicitly asks for a one-shot change. "
                "自然语言句子尽量不要使用逗号；重点保留动作、表情、姿态、环境光线、空间关系和氛围。"
                "避免复杂手势和多手互动；除非原文强制要求，尽量不强调手部。"
                "输出格式: English visual sentence. key tag, key tag, key tag"
            )
        else:
            system = (
                "Visual subject rule: the image subject is the character, not the user. "
                "For default or original characters, do not turn role names into English names or visual tags; describe appearance and action instead. "
                "Only keep a character name when it is paired with its published series. "
                "Stable appearance is injected later; do not invent or restate stable hair, eye, body, species, clothing, or accessory traits unless the source explicitly asks for a one-shot change. "
                "你是专业的 Anima3 提示词工程师。Anima3 支持英文自然语言与 danbooru 标签混编。"
                "将中文场景描述重构为一句英文自然语言画面描述，后接少量 danbooru 补强标签。"
                "直接输出英文提示词，不要 JSON、不要解释，不要压缩成纯标签列表。"
                "根据物理距离判断自拍、对镜、POV 或第三人称视角。"
                "前摄自拍不出现手机和镜子；只有对镜自拍才允许镜子和手机同时出现。"
                "避免复杂手势和多手互动；除非原文强制要求，尽量不强调手部。"
                "自然语言句子尽量不要使用逗号。输出格式: English visual sentence. key tag, key tag, key tag"
            )
        if is_intimate:
            # 亲密场景翻译护栏：第二人称身体翻成“用户作为伴侣的局部身体”，按用户性别决定男/女，绝不能写成完整的第二个主角（双女根因）。
            ug = self._get_user_gender(session_id)
            body = "female" if ug == "female" else "male"
            system += (
                " Intimate scene override: the only fully drawn character is the role (one woman). "
                f"The user appears only as an intimate partner's partial {body} body (hands, arms, chest, torso, back, thighs), "
                "never as a complete second character with their own face, hair, or expression. "
                f"Chinese 你的手/你的胸/你的背/你的腿 must become visible partial {body} body parts of the partner, not a second person. "
                "Do not turn the user into a second full character. "
                "Keep the character's full or three-quarter body in frame; do not crop her into a face or bust close-up "
                "when the scene describes intercourse or straddling. "
                "Translate explicit sexual content faithfully: do not euphemize genitals, penetration, or bodily fluids."
            )
        text = await self._call_llm(
            system,
            f"动态天气与自然光约束: {weather_guard} {light_guard}\n请翻译: {natural}",
            temp=float(self._get_llm_value("image", "temperature_translate", "0.3")),
            tag="translate",
            purpose="image",
            session_id=session_id,
        )
        natural_text = str(natural or "").strip()
        body = text.strip().strip(",")
        if opener:
            detail = body or natural_text
            return opener if not detail else f"{opener}, {detail}"
        return body or natural_text


    async def _llm_classify_character(self, user_text: str) -> dict[str, Any]:
        system = (
            "你是角色设定助手。判断用户描述属于既有作品角色或外观体貌特征，只输出 JSON。\n"
            "既有作品角色输出 {\"type\":\"character\",\"name\":\"用户可读角色名\",\"series\":\"用户可读作品名\","
            "\"prompt_name\":\"Anima/danbooru可识别的英文或罗马音角色tag\",\"prompt_series\":\"Anima/danbooru可识别的英文或罗马音作品tag\","
            "\"persona\":\"中文人设\",\"appearance\":\"英文prompt标签\",\"purity\":0到10整数,"
            "\"age\":\"minor或adult\",\"occupation\":\"角色的中文职业/身份，如 高中生/上班族/护士\","
            "\"anchor\":\"按职业从 company/school/factory/farm/construction/medical/retail/delivery/driver/home/flexible 里选一个白天去向\","
            "\"relationship\":\"若用户在描述里写了和角色的关系就原样填，没写则留空\"}。\n"
            "age/occupation/anchor 依据你对该角色的了解判断；relationship 只在用户明确写了关系时才填，不要替用户编造。\n"
            "外观描述输出 {\"type\":\"appearance\",\"tags\":\"英文prompt标签\"}。\n"
            "name/series 可以保留中文或日文方便用户识别；prompt_name/prompt_series 必须使用英文、罗马音或官方英文名，不要输出中文、日文假名或汉字。"
            "例如：天童爱丽丝 => prompt_name 写 aris (blue archive)，prompt_series 写 Blue Archive；和泉纱雾 => prompt_name 写 Sagiri Izumi，prompt_series 写 Eromanga Sensei。\n"
            "appearance 只写【稳定的身体身份特征】：性别、发色、发长、瞳色、肤色、体型，以及兽耳/兽角/伤疤/纹身等永久性标志；必须以 1girl 或 1boy 开头。\n"
            "appearance 绝对不要包含：服装、盔甲、制服、披风、武器、旗帜、持有物、配饰、姿势、表情、场景、灯光——这些会随每张图的剧情变化，由场景单独决定，写进身份特征会导致换装时和场景服装冲突。"
        )
        text = await self._call_llm(system, user_text, temp=float(self._get_llm_value("image", "temperature_classify", "0.1")), tag="classify", purpose="image")
        return json.loads(text)

    async def _normalize_prompt_intake(self, user_text: str, context: str = "oc") -> dict[str, str]:
        local = prompt_intake.heuristic_intake(user_text)
        if not self.has_llm_config("image"):
            return local
        system = (
            "你是提示词槽位归档器，只输出 JSON。用户会自然描述角色、外观、穿搭、画风、场景偏好或关系。"
            "不要扩写，不要润色，不要替用户新增设定，只把原文按用途归档。"
            "字段固定为: name, source_type, series, original_name, visual_character, visual_series, role, age, occupation, anchor, workday_wake_time, workday_sleep_time, weekend_wake_time, weekend_sleep_time, persona, user_address, base_appearance, dynamic_appearance, relationship, city, style, scene_preference, selfie_preference, unclassified。"
            "name 是本地角色卡主键，保留用户给的称呼。source_type 只允许 original/existing/空。"
            "如果是原创角色，source_type 写 original，series/original_name/visual_character/visual_series 留空，除非用户明确给了可用标签。"
            "如果是现有作品角色，source_type 写 existing。original_name 写英文官方名或罗马音，姓氏在前，不要写中文、日文假名或汉字。"
            "series 写英文官方作品名或英文罗马音，不要写中文、日文假名或汉字。"
            "visual_character 和 visual_series 写 Danbooru 风格标签：小写英文/罗马音、下划线分词、必要时用括号消歧义，例如 tendou_aris、aris_(blue_archive)、blue_archive；不要输出中文、日文假名或汉字。"
            "occupation 放角色的中文职业/身份原文（如 高中生/上班族/护士）；anchor 从职业推断白天去向枚举。"
            "workday_wake_time/workday_sleep_time/weekend_wake_time/weekend_sleep_time 只在用户明确写作息时填写，格式 HH:MM。"
            "user_address 放角色对用户的称呼（如 主人/前辈/哥哥/姐姐），不是角色自称，也不是角色名。"
            "base_appearance 只放稳定身体身份特征：性别、发色、发型、瞳色、肤色、体型、物种特征、伤疤、纹身等永久标志。"
            "dynamic_appearance 只放当前/默认穿搭、配饰、临时发型瞳色、持有物；不要放场景、姿势、灯光。"
            "style 只放画风、artist tag、渲染风格；不要放质量词。"
            "scene_preference 只放地点、时间、自拍习惯、常去场所等偏好，不要放稳定外貌。"
            "质量词如 masterpiece/best quality/absurdres/score_9 不得进入任何外观字段。"
            "age 只允许 minor/adult/空；anchor 可用 company/school/factory/farm/construction/medical/retail/delivery/driver/home/flexible/空。"
            "如果不确定，放 unclassified。所有字段值都用简短原文或标签字符串，不能使用数组。"
        )
        try:
            text = await self._call_llm(
                system,
                user_text,
                temp=float(self._get_llm_value("image", "temperature_classify", "0.1")),
                tag=f"prompt-intake-{context}",
                purpose="image",
            )
            llm = prompt_intake.parse_llm_json(text, user_text)
            return prompt_intake.merge_intake(llm, local, user_text)
        except Exception as exc:
            logger.warning("prompt intake normalization failed: %s", exc)
            return local

    async def _llm_infer_timezone(self, city: str):
        if not city or not self.has_llm_config("image"):
            return None
        system = "根据城市名，只输出该城市 UTC 标准时区偏移小时数，忽略夏令时。例如 北京 8，东京 9，纽约 -5。只输出数字。"
        try:
            text = await self._call_llm(system, city, temp=0.0, tag="tz", purpose="image")
            m = re.search(r"-?\d+(?:\.\d+)?", text)
            if not m:
                return None
            val = float(m.group(0))
            return val if -12 <= val <= 14 else None
        except Exception:
            return None

    async def _resolve_city_timezone(self, city: str, lon=None):
        off = await self._llm_infer_timezone(city)
        return off if off is not None else self._offset_from_lon(lon)

    def _ensure_comfy_session(self):
        image_generation.ensure_comfy_session(self)

    async def _do_generate(
        self,
        scene_desc: str,
        is_ntr: bool = False,
        session_id: str = "",
        one_shot_appearance: str = "",
        is_intimate: bool = False,
        partner_in_frame: bool = False,
        device_in_frame: bool = False,
        clothing_off: str = "",
        orientation: str = "",
        view: str = "",
        ignore_wardrobe_item_states: bool = False,
    ) -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate(
            self, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
            is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
            clothing_off=clothing_off, orientation=orientation, view=view,
            ignore_wardrobe_item_states=ignore_wardrobe_item_states,
        )

    async def _do_generate_locked(
        self,
        scene_desc: str,
        is_ntr: bool = False,
        session_id: str = "",
        one_shot_appearance: str = "",
        is_intimate: bool = False,
        partner_in_frame: bool = False,
        device_in_frame: bool = False,
        clothing_off: str = "",
        orientation: str = "",
        view: str = "",
        ignore_wardrobe_item_states: bool = False,
    ) -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate_locked(
            self, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
            is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
            clothing_off=clothing_off, orientation=orientation, view=view,
            ignore_wardrobe_item_states=ignore_wardrobe_item_states,
        )

    async def _await_protected_image_task(
        self,
        session_id: str,
        coro,
        *,
        label: str = "生图任务",
        on_outer_cancel=None,
        after_cancel_done=None,
    ):
        """等待一个生图/发图协程；外层消息处理取消时让图片链路继续完成。"""
        character_key = self._context_character_key(session_id) if session_id else ""
        task = self._spawn_background(
            coro,
            name=f"protected-image:{session_id or 'unknown'}",
            session_id=session_id,
            character_key=character_key,
            scope="protected-image",
            drain=True,
        )
        protected = getattr(self, "_protected_image_tasks", None)
        if isinstance(protected, set):
            protected.add(task)

        def _discard(done_task: asyncio.Task) -> None:
            protected_tasks = getattr(self, "_protected_image_tasks", None)
            if isinstance(protected_tasks, set):
                protected_tasks.discard(done_task)

        task.add_done_callback(_discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if callable(on_outer_cancel):
                try:
                    on_outer_cancel()
                except Exception:
                    logger.debug("protected image cancel callback failed", exc_info=True)

            def _finish_after_cancel(done_task: asyncio.Task) -> None:
                try:
                    done_task.result()
                except asyncio.CancelledError:
                    self._ulog(session_id, "WARN", f"被打断后的{label}被取消")
                except Exception as exc:
                    self._ulog(session_id, "ERROR", f"被打断后{label}失败: {exc}")
                    logger.error("protected image task failed after cancellation: %s", exc, exc_info=True)
                finally:
                    if callable(after_cancel_done):
                        try:
                            after_cancel_done()
                        except Exception:
                            logger.debug("protected image done callback failed", exc_info=True)

            task.add_done_callback(_finish_after_cancel)
            raise

    # ---------------------------------------------------------------------
    # Tools
    # ---------------------------------------------------------------------
    async def tool_generate_image(
        self,
        chat_id,
        session_id: str,
        prompt: str = "",
        view: str = "",
        intent: str = "",
        mood: str = "",
        must_include: str = "",
        defer_photo_history: bool = False,
        planning_mode: str = "chat",
    ) -> str:
        if not any((prompt, intent, must_include)):
            return "缺少图片意图"
        await self.send_action(chat_id, "upload_photo")
        source_description = self._format_image_source_description(
            intent=intent,
            mood=mood,
            must_include=must_include,
            prompt=prompt,
        )
        plan = await plan_roleplay_image(
            self,
            session_id,
            intent=intent,
            mood=mood,
            must_include=must_include,
            prompt=prompt,
            view=view,
            mode=planning_mode or "chat",
        )
        scene = (plan.get("scene") or "").strip()
        if not scene:
            return "缺少图片意图"
        final_view = (plan.get("view") or "").strip()
        new_app = (plan.get("new_appearance_tags") or "").strip()
        clothing_off = (plan.get("clothing_off") or "").strip()
        is_intimate = bool(plan.get("is_intimate"))
        partner_in_frame = bool(plan.get("partner_in_frame"))
        device_in_frame = bool(plan.get("device_in_frame"))
        orientation = (plan.get("aspect_ratio") or "").strip()
        state_mutation = self._image_state_mutation_from_plan(
            plan,
            intent,
            prompt,
            scene,
        )
        # 伴侣同框时也套用翻译护栏（对方只画局部、不画成完整第二人）。
        free_composition = (planning_mode or "").strip().lower() == "illustration"
        translate_kwargs = {
            "session_id": session_id,
            "view": final_view,
            "is_intimate": is_intimate or partner_in_frame,
        }
        if free_composition:
            translate_kwargs["free_composition"] = True
        english = await self._translate_to_tags(scene, **translate_kwargs)
        generation_kwargs = {
            "session_id": session_id,
            "one_shot_appearance": new_app or "",
            "is_intimate": is_intimate,
            "partner_in_frame": partner_in_frame,
            "device_in_frame": device_in_frame,
            "clothing_off": clothing_off,
            "orientation": orientation,
            "view": final_view,
        }
        if state_mutation.get("clear_undress_state"):
            generation_kwargs["ignore_wardrobe_item_states"] = True
        ok, imgs, err = await self._do_generate(english, **generation_kwargs)
        if not ok or not imgs:
            self._ulog(session_id, "ERROR", f"工具生图失败: {err}")
            return f"生图失败: {err}"
        # 聊天途中的配图不带配文：聊天模型已经在文字回复里说话了，再加配文会重复。
        await self.send_photo(chat_id, imgs[0], "")
        self._record_sent_photo(
            session_id,
            scene,
            "",
            appearance=new_app or self._preview_image_mutation_appearance(session_id, state_mutation),
            view=final_view,
            source_description=source_description,
            source_kind="chat_image",
            defer_history_message=defer_photo_history,
        )
        self._commit_image_state_mutation(session_id, state_mutation)
        return f"图片已生成并发送。画面: {scene}"






    # ── 联网搜索（Tavily）──────────────────────────────────────────────────

    def _web_search_enabled(self) -> bool:
        return self._bool_config("web_search_enabled", False) and bool(
            str(self.config.get("tavily_api_key", "") or "").strip()
        )

    def _web_search_daily_limit(self) -> int:
        try:
            return max(0, int(str(self.config.get("web_search_daily_limit", "5")).strip() or "5"))
        except ValueError:
            return 5

    async def tool_search_web(self, session_id: str, query: str = "") -> str:
        """聊天工具：角色遇到不熟悉/时效性话题时联网查资料。

        所有失败路径都返回可扮演的软失败文案（不抛异常穿透聊天回合）；
        缓存命中不扣每日限额，资料只进对话动态尾部。
        """
        query = (query or "").strip()
        self._ulog(session_id, "SEARCH", f'模型调用 search_web query="{query[:100]}"')
        if not query:
            return "搜索关键词为空，没有执行搜索。"
        if not self._web_search_enabled():
            return "联网搜索功能未开启，查不到外部资料。用角色口吻坦然承认不了解这个话题或把话题引回对话，不要编造事实。"
        cached = web_search.cache_get(query)
        if cached is not None:
            self._ulog(session_id, "SEARCH", f"命中缓存 {len(cached)} 条")
            return web_search.format_results_for_roleplay(query, cached)
        state = self._get_session_state(session_id)
        today = self._session_now(session_id).strftime("%Y-%m-%d")
        if session_schema.get_web_search_date(state) != today:
            session_schema.set_web_search_date(state, today)
            session_schema.set_web_search_count(state, 0)
        limit = self._web_search_daily_limit()
        used = session_schema.get_web_search_count(state)
        if used >= limit:
            self._save_session_state(session_id, state)
            self._ulog(session_id, "SEARCH", f"跳过: 每日搜索限额已用完 {used}/{limit}")
            return "今天的联网搜索次数已用完，查不了资料。用角色口吻自然带过这个话题，不要编造事实。"
        try:
            results = await web_search.tavily_search(
                str(self.config.get("tavily_api_key", "") or "").strip(), query
            )
        except Exception as exc:
            self._ulog(session_id, "SEARCH", f"搜索失败: {exc}")
            return "联网搜索暂时失败了，没查到资料。用角色口吻自然带过，不要编造事实。"
        session_schema.set_web_search_count(state, used + 1)
        self._save_session_state(session_id, state)
        if not results:
            self._ulog(session_id, "SEARCH", f"无结果 {used + 1}/{limit}")
            return f"没有搜到关于「{query}」的资料。用角色口吻自然带过，不要编造事实。"
        web_search.cache_put(query, results)
        self._ulog(session_id, "SEARCH", f"返回 {len(results)} 条 {used + 1}/{limit}")
        return web_search.format_results_for_roleplay(query, results)

    async def _push_image_from_text(self, session_id: str, scene: str):
        chat_id = self.chat_id_from_session(session_id)
        try:
            is_intimate = self._detect_intimate_context(scene)
            english = await self._translate_to_tags(scene, session_id=session_id, is_intimate=is_intimate)
            ok, imgs, err = await self._do_generate(english, session_id=session_id, is_intimate=is_intimate)
            if ok and imgs:
                await self.send_photo(chat_id, imgs[0])
                self._record_sent_photo(
                    session_id,
                    scene,
                    "",
                    source_description=self._format_image_source_description(intent="聊天模型文字中泄漏出的配图描述", prompt=scene),
                    source_kind="auto_chat_image",
                )
            else:
                logger.error("leak fallback generate failed: %s", err)
        except Exception as exc:
            logger.error("leak fallback failed: %s", exc, exc_info=True)

    @staticmethod
    def _handle_leaked_image_text(text: str) -> str | None:
        if not text or "generate_roleplay_image" not in text:
            return None
        match = IMG_CALL_LEAK_RE.search(text)
        if not match:
            return None
        scene = (match.group(1) or "").strip()
        scene = re.sub(r"^\s*(画面中你|画面|场景|内容|prompt)\s*[:：]\s*", "", scene, flags=re.IGNORECASE).strip()
        return scene or None

    @staticmethod
    def _strip_leaked_image_text(text: str) -> str:
        return IMG_CALL_LEAK_RE.sub("", text or "").strip()

    @staticmethod
    def _strip_photo_memory_echo(text: str) -> str:
        if not text or not any(k in text for k in ("照片", "画面", "展示", "呈现", "出现在用户眼前")):
            return text
        return IMG_NARRATION_LEAK_RE.sub("", text).strip()

    def _mgmt_characters(self) -> str:
        lines, total = ["角色档案池", ""], 0
        for sid, state in self.sessions.items():
            saved = session_schema.get_saved_characters(state)
            if not saved:
                continue
            lines.append(f"会话: {sid}")
            for key, data in saved.items():
                total += 1
                lines.append(f"  - {key}: {data.get('character', key)} {('(' + data.get('series', '') + ')') if data.get('series') else ''}")
        if total == 0:
            lines.append("暂无已保存角色档案。")
        lines.append(f"\n共 {total} 个角色。")
        return "\n".join(lines)

    def _mgmt_locations(self) -> str:
        lines = [f"地区设定总览\n全局默认城市: {self.config.get('location')} | 时区: UTC+{self.config.get('timezone_offset')}", ""]
        found = False
        for sid, state in self.sessions.items():
            custom_location = session_schema.get_character_value(state, "custom_location", "")
            custom_timezone = session_schema.get_character_value(state, "custom_timezone_offset", "")
            if custom_location or custom_timezone:
                found = True
                lines.append(f"{sid}: {custom_location or '(全局)'} | UTC{custom_timezone or self.config.get('timezone_offset')}")
        if not found:
            lines.append("所有会话均使用全局默认地区设置。")
        return "\n".join(lines)

    def _mgmt_sessions(self) -> str:
        lines = ["会话概况", ""]
        if not self.sessions:
            return "会话概况\n\n暂无会话记录。"
        for sid, state in self.sessions.items():
            last = session_schema.get_last_interaction(state)
            ago = "无记录"
            if last:
                sec = time.time() - last
                ago = f"{int(sec // 60)}分钟前" if sec < 3600 else f"{int(sec // 3600)}小时前" if sec < 86400 else f"{int(sec // 86400)}天前"
            push = f"{len(session_schema.get_daily_triggered_times(state))}/{len(session_schema.get_daily_trigger_times(state))}" if session_schema.get_daily_trigger_times(state) else "关闭"
            lines.append(f"{sid}\n  角色: {state.get('custom_character') or '(未设定)'} | 纯良度: {self._get_purity(sid)}/10\n  上次互动: {ago} | 今日推送: {push}")
        return "\n".join(lines)
