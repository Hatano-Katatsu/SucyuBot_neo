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
from .defaults import DEFAULT_CONFIG
from .image_planning import VALID_VIEWS, plan_roleplay_image
from .memory import LongTermMemoryStore
from .chat_context import ChatContextMixin
from .commands import CommandHandlersMixin
from .memory_policy import MemoryPolicyMixin
from .process_restart import ProcessRestartMixin
from .scheduler_runtime import SchedulerRuntimeMixin
from .telegram_io import TelegramIOMixin
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
CHAT_VISUAL_NOISE_TAGS = {
    "masterpiece", "best quality", "absurdres", "highres", "detailed illustration", "anime coloring",
    "clean lineart", "soft cel shading", "score_9", "score_8", "score_7", "safe", "sensitive",
    "1girl", "1boy", "girl", "boy", "woman", "man", "solo",
}




class TelegramComfyUIService(
    ProcessRestartMixin,
    TelegramIOMixin,
    CommandHandlersMixin,
    ChatContextMixin,
    MemoryPolicyMixin,
    SchedulerRuntimeMixin,
    WorldRuntimeMixin,
):
    def __init__(self, config_path: str | Path = "data/config.json", state_path: str | Path = "data/state.json"):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.config = self._load_config()
        self.memory = LongTermMemoryStore(self._memory_db_path())
        self.sessions: dict[str, dict[str, Any]] = {}
        self.city_place_catalogs: dict[str, dict[str, Any]] = {}
        self._load_state()

        self.http: aiohttp.ClientSession | None = None
        self.comfy_session: aiohttp.ClientSession | None = None
        self._gen_lock = asyncio.Lock()
        self._generating = False
        self._active_pushes: set[str] = set()
        self._dirty_sessions: set[str] = set()
        self._last_state_write = 0.0
        self._state_write_interval = 30.0
        self._weather_caches: dict[str, dict[str, Any]] = {}
        self._skill_reference_cache: str | None = None
        self._bot_username = ""
        self._offset = 0
        self._bot_tasks: list[asyncio.Task] = []
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

    async def start_bot(self):
        if self.is_bot_running:
            return
        token = self.config.get("telegram_bot_token", "")
        if not token:
            raise RuntimeError("telegram_bot_token 未配置")
        timeout = aiohttp.ClientTimeout(total=620)
        self.http = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        me = await self.tg_api("getMe")
        self._bot_username = (me.get("result") or {}).get("username", "")
        self._bot_tasks = [
            asyncio.create_task(self.poll_loop(), name="telegram-poll-loop"),
            asyncio.create_task(self.scheduler_loop(), name="selfie-scheduler-loop"),
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
        self._flush_sessions(force=True)
        if self.comfy_session and not self.comfy_session.closed:
            await self.comfy_session.close()

    # ---------------------------------------------------------------------
    # Config / state
    # ---------------------------------------------------------------------
    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception as exc:
                raise RuntimeError(f"读取配置失败: {self.config_path}: {exc}") from exc
        else:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.warning("配置文件不存在，已生成默认配置: %s", self.config_path)
        return cfg

    def save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _memory_db_path(self) -> Path:
        raw = str(self.config.get("long_memory_db_path") or "").strip()
        if not raw:
            return self.state_path.with_name("memory.sqlite3")
        path = Path(raw)
        if not path.is_absolute():
            path = self.config_path.parent / path
        return path

    def _load_state(self):
        self.sessions = {}
        if not self.state_path.exists():
            return
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            if not raw.strip():
                return
            data = json.loads(raw)
            sessions = data.get("sessions", {})
            if isinstance(sessions, dict):
                self.sessions = sessions
                logger.info("Loaded %d sessions from %s", len(self.sessions), self.state_path)
            catalogs = data.get("city_place_catalogs", {})
            if isinstance(catalogs, dict):
                self.city_place_catalogs = catalogs
        except Exception as exc:
            logger.warning("加载状态失败，使用空状态: %s", exc)

    def _write_state(self):
        state = {
            "sessions": self.sessions,
            "character_registry": self._build_character_registry(),
            "location_registry": self._build_location_registry(),
            "city_place_catalogs": self.city_place_catalogs,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _mark_dirty(self, session_id: str):
        if session_id:
            self._dirty_sessions.add(session_id)

    def _flush_sessions(self, force=False):
        now = time.time()
        if not force and now - self._last_state_write < self._state_write_interval:
            return
        if not self._dirty_sessions and self.state_path.exists():
            return
        self._write_state()
        self._last_state_write = now
        self._dirty_sessions.clear()

    def _save_session_state(self, session_id: str, state: dict[str, Any]):
        if not session_id:
            return
        self.sessions[session_id] = state
        self._mark_dirty(session_id)
        self._flush_sessions(force=True)

    # ---------------------------------------------------------------------
    # Per-user activity log (data/logs/telegram_<chat_id>.log)
    # ---------------------------------------------------------------------
    def _user_log_enabled(self) -> bool:
        value = self.config.get("user_log_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def _user_log_dir(self) -> Path:
        raw = str(self.config.get("user_log_dir") or "").strip()
        if not raw:
            return self.state_path.parent / "logs"
        d = Path(raw)
        return d if d.is_absolute() else self.config_path.parent / d

    def _user_log_path(self, session_id: str) -> Path:
        chat = self.chat_id_from_session(session_id)
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(chat)) or "unknown"
        return self._user_log_dir() / f"telegram_{safe}.log"

    def _ulog(self, session_id: str, tag: str, message: str = ""):
        """按用户追加一行活动日志。事件级，纯同步，事件循环内原子完成。"""
        if not session_id or not self._user_log_enabled():
            return
        try:
            stamp = self._session_now(session_id).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (message or "").replace("\r", "").replace("\n", " ⏎ ").strip()
        line = f"{stamp} {tag}" + (f" {body}" if body else "")
        try:
            path = self._user_log_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.debug("user log write failed", exc_info=True)

    @staticmethod
    def session_id_for_chat(chat_id: int | str) -> str:
        return f"telegram:{chat_id}"

    @staticmethod
    def chat_id_from_session(session_id: str) -> int | str:
        raw = session_id.removeprefix("telegram:")
        try:
            return int(raw)
        except ValueError:
            return raw

    def _get_session_state(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            self.sessions[session_id] = {}
        state = self.sessions[session_id]
        defaults = {
            "last_interaction": time.time(),
            "last_morning_greet_date": "",
            "daily_trigger_times": [],
            "daily_trigger_date": "",
            "daily_triggered_times": [],
            "recent_message_history": [],
            "chat_history": [],
            "sent_photos_history": [],
            "dynamic_appearance": "",
            "replying_to_selfie": False,
            "last_sent_selfie_time": 0,
            "last_sent_selfie_caption": "",
            "last_sent_selfie_source_description": "",
            "last_sent_selfie_replied": False,
            "custom_scheduled_persona": "",
            "custom_role_name": "",
            "custom_bot_name": "",
            "custom_bot_self_name": "",
            "custom_spatial_relationship": "",
            "custom_location": "",
            "custom_timezone_offset": "",
            "user_place": "",
            "user_place_label": "",
            "user_place_text": "",
            "user_place_updated_at": 0,
            "user_place_confidence": 0,
            "custom_positive_prefix": "",
            "custom_default_hair": "",
            "custom_default_eyes": "",
            "custom_current_style": "",
            "custom_allow_llm_change_appearance": None,
            "custom_character": "",
            "custom_series": "",
            "persona_user_set": False,
            "saved_characters": {},
            "purity": None,
            "purity_user_set": False,
            "ntr_stage_reached": 0,
            "ntr_reconcile_count": 0,
            "rounds_since_image": 0,
            "short_context_start": 0,
            "short_context_reset_time": 0,
            "short_context_reset_reason": "",
        }
        for key, val in defaults.items():
            state.setdefault(key, val)
        return state

    def _get_session_cfg(self, session_id: str, key: str, default=None):
        if session_id:
            state = self._get_session_state(session_id)
            override = state.get(f"custom_{key}")
            if override not in (None, ""):
                return override
        return self.config.get(key, default)

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
        return bool(state.get("custom_character") or state.get("persona_user_set"))

    def _get_purity(self, session_id: str) -> int:
        state = self._get_session_state(session_id) if session_id else {}
        if state.get("purity") is not None:
            return max(0, min(10, int(state["purity"])))
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
        period = self._get_time_period(now.hour)
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

    def _get_effective_persona(self, session_id: str) -> str:
        state = self._get_session_state(session_id)
        if self._is_character_set(session_id):
            base = state.get("custom_scheduled_persona", "")
        else:
            base = self._get_session_cfg(session_id, "scheduled_persona", DEFAULT_CONFIG["scheduled_persona"])
        if not base:
            # 兜底：角色态但人设被清空（半重置残留）时回退全局默认，绝不返回空人设。
            base = self.config.get("scheduled_persona") or DEFAULT_CONFIG["scheduled_persona"]
        additional = state.get("dynamic_appearance") or self.config.get("dynamic_appearance", "")
        return f"{base}\n\n[当前附加人设/短期穿搭与配饰: {additional}]" if additional else base

    def _allow_llm_change_appearance(self, session_id: str) -> bool:
        state = self._get_session_state(session_id)
        override = state.get("custom_allow_llm_change_appearance")
        if isinstance(override, bool):
            return override
        if isinstance(override, str) and override.strip():
            return override.strip().lower() in ("true", "1", "yes", "on", "开", "允许", "启用")
        return bool(self.config.get("allow_llm_change_appearance", True))

    def _normalize_style_pool(self) -> list[str]:
        raw = self.config.get("style_pool") or self.config.get("style_prefix") or "@00 gx4"
        if isinstance(raw, str):
            parts = re.split(r"[\n;；]+", raw)
        elif isinstance(raw, list):
            parts = raw
        else:
            parts = []
        pool, seen = [], set()
        for item in parts:
            style = str(item).strip()
            if style and style.lower() not in seen:
                pool.append(style)
                seen.add(style.lower())
        if not pool:
            pool = ["@00 gx4"]
        current = str(self.config.get("current_style", "")).strip()
        if current not in pool:
            self.config["current_style"] = pool[0]
        self.config["style_pool"] = "\n".join(pool)
        return pool

    def _get_current_style(self, session_id: str = "") -> str:
        pool = self._normalize_style_pool()
        if session_id:
            state = self._get_session_state(session_id)
            custom = str(state.get("custom_current_style", "")).strip()
            if custom in pool:
                return custom
            if custom:
                state["custom_current_style"] = ""
                self._mark_dirty(session_id)
        current = str(self.config.get("current_style", "")).strip()
        return current if current in pool else pool[0]

    def _set_current_style(self, session_id: str, style: str):
        if style not in self._normalize_style_pool():
            raise ValueError(f"未知画风: {style}")
        if session_id:
            state = self._get_session_state(session_id)
            state["custom_current_style"] = style
            self._save_session_state(session_id, state)
        else:
            self.config["current_style"] = style
            self.save_config()

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

    @staticmethod
    def _get_time_period(hour: int) -> str:
        if 5 <= hour < 11:
            return "早晨"
        if 11 <= hour < 17:
            return "下午"
        if 17 <= hour < 21:
            return "傍晚"
        return "深夜"

    def _tick_ntr_reconcile(self, state: dict[str, Any]) -> bool:
        if not state.get("ntr_affection_reset"):
            return False
        cnt = state.get("ntr_reconcile_count", 0) + 1
        if cnt >= 5:
            state["ntr_affection_reset"] = False
            state["ntr_reconcile_count"] = 0
            return True
        state["ntr_reconcile_count"] = cnt
        return False

    def _touch(self, session_id: str):
        if session_id:
            state = self._get_session_state(session_id)
            state["last_interaction"] = time.time()
            state["ntr_stage_reached"] = 0
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
    ):
        state = self._get_session_state(session_id)
        history = state.get("sent_photos_history", [])
        source_description = (source_description or "").strip()
        history.append({
            "timestamp": time.time(),
            "scene": scene,
            "caption": caption,
            "appearance": appearance if appearance is not None else state.get("dynamic_appearance", ""),
            "view": (view or "").strip().lower(),
            "source_description": source_description,
        })
        state["sent_photos_history"] = history[-10:]
        state["last_sent_selfie_time"] = time.time()
        state["last_sent_selfie_caption"] = caption
        state["last_sent_selfie_source_description"] = source_description or scene
        state["last_sent_selfie_replied"] = False
        state["rounds_since_image"] = 0
        self._save_session_state(session_id, state)
        self._ulog(
            session_id, "IMAGE",
            f"view={(view or '').strip().lower() or '?'} caption={caption or '-'} scene={scene}",
        )

    def _build_character_registry(self) -> dict[str, Any]:
        registry = {}
        for sid, state in self.sessions.items():
            if state.get("saved_characters"):
                registry[sid] = state["saved_characters"]
        return registry

    def _build_location_registry(self) -> dict[str, Any]:
        registry = {}
        for sid, state in self.sessions.items():
            if state.get("custom_location") or state.get("custom_timezone_offset"):
                registry[sid] = {
                    "city": state.get("custom_location") or f"(全局: {self.config.get('location', '—')})",
                    "timezone": state.get("custom_timezone_offset") or f"(全局: {self.config.get('timezone_offset', '—')})",
                }
        return registry

    # ---------------------------------------------------------------------
    # Appearance / prompt
    # ---------------------------------------------------------------------
    def _load_keywords(self, key: str, defaults: list[str]) -> list[str]:
        return appearance_rules.load_keywords(self.config, key, defaults)

    @property
    def _outfit_kw(self):
        if not hasattr(self, "_cached_outfit_kw"):
            self._cached_outfit_kw = appearance_rules.outfit_keywords(self.config)
        return self._cached_outfit_kw

    @property
    def _accessory_kw(self):
        if not hasattr(self, "_cached_accessory_kw"):
            self._cached_accessory_kw = appearance_rules.accessory_keywords(self.config)
        return self._cached_accessory_kw

    def _parse_appearance(self, appearance: str) -> dict[str, list[str]]:
        return appearance_rules.parse_appearance(appearance, self._outfit_kw, self._accessory_kw)

    @staticmethod
    def _slots_to_string(slots: dict[str, list[str]]) -> str:
        return appearance_rules.slots_to_string(slots)

    @staticmethod
    def _remove_tag(text: str, tag: str) -> str:
        return appearance_rules.remove_tag(text, tag)

    def _merge_appearance(self, current_tags: str, new_tags: str, mode: str = "merge") -> str:
        return appearance_rules.merge_appearance(current_tags, new_tags, self._outfit_kw, self._accessory_kw, mode=mode)

    def _inject_appearance(self, char: str, session_id: str = "") -> str:
        return appearance_rules.inject_appearance(self, char, session_id)

    def _effective_visual_prompt_tags(self, session_id: str) -> str:
        state = self._get_session_state(session_id)
        if self._is_character_set(session_id):
            base = state.get("custom_positive_prefix", "") or self.config.get("positive_prefix", "")
        else:
            base = self._get_session_cfg(session_id, "positive_prefix", "")
        return self._inject_appearance(base, session_id).strip()

    @staticmethod
    def _clean_chat_visual_tags(tags: list[str], limit: int = 8) -> list[str]:
        kept: list[str] = []
        seen = set()
        for tag in tags:
            text = re.sub(r"\s+", " ", (tag or "").replace("_", " ").strip(" ,"))
            key = text.lower()
            if not key or key in CHAT_VISUAL_NOISE_TAGS or key.startswith("score_") or key.startswith("@"):
                continue
            if key in seen:
                continue
            kept.append(text)
            seen.add(key)
            if len(kept) >= limit:
                break
        return kept

    @staticmethod
    def _is_worn_or_carried_item(tag: str) -> bool:
        return bool(re.search(
            r"(glasses|necklace|earring|bracelet|ring|clip|hairpin|ribbon|scarf|collar|choker|"
            r"hat|cap|crown|tiara|watch|belt|bag|bow|glove|mask|veil|sword|blade|gun|staff|"
            r"wand|banner|flag|shield|cape|boots?|armor|ornament|accessor)",
            tag.lower(),
        ))

    def _chat_visible_appearance_context(self, session_id: str) -> str:
        effective = self._effective_visual_prompt_tags(session_id)
        if not effective:
            return ""
        state = self._get_session_state(session_id)
        dynamic_slots = self._parse_appearance(state.get("dynamic_appearance", "") or self.config.get("dynamic_appearance", ""))
        slots = self._parse_appearance(effective)
        hair = self._clean_chat_visual_tags(slots.get("hair", []), limit=6)
        eyes = self._clean_chat_visual_tags(slots.get("eyes", []), limit=4)
        outfit_source = dynamic_slots.get("outfit") or slots.get("outfit", [])
        outfit = self._clean_chat_visual_tags(outfit_source, limit=8)
        accessories = self._clean_chat_visual_tags(slots.get("accessory", []), limit=8)
        other = self._clean_chat_visual_tags(slots.get("other", []), limit=12)

        carried = [tag for tag in other if self._is_worn_or_carried_item(tag)]
        other = [tag for tag in other if tag not in carried]
        accessories = self._clean_chat_visual_tags(accessories + carried, limit=10)

        lines = []
        for label, values in (
            ("发型/发色", hair),
            ("眼睛", eyes),
            ("穿搭", outfit),
            ("配饰/随身物", accessories),
            ("其他显著特征", other),
        ):
            if values:
                lines.append(f"- {label}: {', '.join(values)}")
        return "\n".join(lines)

    @staticmethod
    def _infer_gender_from_prefix(prefix: str) -> str:
        return appearance_rules.infer_gender_from_prefix(prefix)

    @staticmethod
    def _view_opener(view: str, gender: str = "girl") -> str:
        return image_generation.view_opener(view, gender)

    def _build_prompt(self, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[str, str]:
        return image_generation.build_prompt(self, scene_desc, is_ntr, session_id)

    def _build_workflow(self, positive: str, negative: str, seed: int) -> dict[str, Any]:
        return image_generation.build_workflow(self, positive, negative, seed)

    def _build_anima_workflow(self, positive: str, negative: str, seed: int) -> dict[str, Any]:
        return image_generation.build_anima_workflow(self, positive, negative, seed)

    # ---------------------------------------------------------------------
    # LLM / ComfyUI
    # ---------------------------------------------------------------------
    def _get_llm_value(self, purpose: str, name: str, default=None):
        prefix = "chat_llm" if purpose == "chat" else "image_llm"
        value = self.config.get(f"{prefix}_{name}")
        if value not in ("", None):
            return value
        legacy_map = {
            "api_base": "llm_api_base",
            "api_key": "llm_api_key",
            "model": "llm_model",
            "max_tokens": "llm_max_tokens",
            "disable_thinking": "llm_disable_thinking",
            "temperature": "llm_temperature_scene",
            "temperature_scene": "llm_temperature_scene",
            "temperature_translate": "llm_temperature_translate",
            "temperature_classify": "llm_temperature_classify",
        }
        legacy_key = legacy_map.get(name)
        if legacy_key:
            legacy_value = self.config.get(legacy_key)
            if legacy_value not in ("", None):
                return legacy_value
        return default

    def has_llm_config(self, purpose: str) -> bool:
        return bool(self._get_llm_value(purpose, "api_key", ""))

    async def _call_llm_messages(self, messages: list[dict[str, Any]], tools=None, tool_choice=None, tag: str = "", temp: float | None = None, purpose: str = "image", disable_thinking: bool | None = None) -> dict[str, Any]:
        api_base = (self._get_llm_value(purpose, "api_base", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1").rstrip("/")
        api_key = self._get_llm_value(purpose, "api_key", "")
        if not api_key:
            label = "聊天与角色扮演模型" if purpose == "chat" else "生图辅助模型"
            raise RuntimeError(f"{label} API Key 未配置")
        body = {
            "model": self._get_llm_value(purpose, "model", "deepseek-chat") or "deepseek-chat",
            "messages": messages,
            "max_tokens": int(self._get_llm_value(purpose, "max_tokens", "4096") or "4096"),
            "temperature": float(self._get_llm_value(purpose, "temperature", "0.95")) if temp is None else temp,
        }
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if disable_thinking is None:
            disable = self._get_llm_value(purpose, "disable_thinking", False)
            if isinstance(disable, str):
                disable = disable.lower() in ("true", "1", "yes", "on")
        else:
            disable = bool(disable_thinking)
        if disable:
            body["thinking"] = {"type": "disabled"}
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.post(
                f"{api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"LLM 请求失败: {resp.status} {await resp.text()}")
                return await resp.json()

    async def _call_llm(self, system: str, user: str, temp: float = 0.3, tag: str = "", purpose: str = "image", disable_thinking: bool | None = None) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        data = await self._call_llm_messages(messages, tag=tag, temp=temp, purpose=purpose, disable_thinking=disable_thinking)
        msg = data.get("choices", [{}])[0].get("message", {})
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
        if not text:
            raise RuntimeError("LLM 返回空内容")
        return text

    async def _translate_to_tags(self, natural: str, session_id: str = "", view: str = "") -> str:
        if not self.has_llm_config("image"):
            return natural
        view = (view or "").strip().lower()
        if view not in VALID_VIEWS:
            view = ""
        char_prefix = self._get_session_cfg(session_id, "positive_prefix", "")
        opener = self._view_opener(view, self._infer_gender_from_prefix(char_prefix)) if view else ""
        if view:
            if view == "mirror":
                view_rule = "固定视角是 mirror 对镜自拍；系统会添加镜子和一部手机，你不要重复输出 mirror/phone/smartphone。"
            elif view == "selfie":
                view_rule = "固定视角是 selfie 前摄自拍；画面中不得出现手机、相机、镜子、拿手机的手。"
            elif view == "pov":
                view_rule = "固定视角是 POV；画面中不得出现自拍手机、镜子或拿手机的手。"
            else:
                view_rule = "固定视角是 third 第三人称；不要把画面写成自拍或对镜自拍。"
            system = (
                "你是专业的 Anima3 提示词工程师。Anima3 支持英文自然语言与 danbooru 标签混编。"
                "把中文画面重构为一句英文自然语言画面描述，后接少量 danbooru 补强标签。"
                "不要输出 JSON、不要前缀、不要解释；不要压缩成纯标签列表。"
                "不要输出自拍/POV/镜子/手机/主语/1girl/1boy 等视角词，系统会统一添加。"
                f"{view_rule}"
                "Visual subject rule: the image subject is the character, not the user. "
                "For default or original characters, do not turn role names into English names or visual tags; describe appearance and action instead. "
                "Only keep a character name when it is paired with its published series. "
                "自然语言句子尽量不要使用逗号；重点保留动作、表情、姿态、服装、环境光线、空间关系和氛围。"
                "避免复杂手势和多手互动；除非原文强制要求，尽量不强调手部。"
                "输出格式: English visual sentence. key tag, key tag, key tag"
            )
        else:
            system = (
                "Visual subject rule: the image subject is the character, not the user. "
                "For default or original characters, do not turn role names into English names or visual tags; describe appearance and action instead. "
                "Only keep a character name when it is paired with its published series. "
                "你是专业的 Anima3 提示词工程师。Anima3 支持英文自然语言与 danbooru 标签混编。"
                "将中文场景描述重构为一句英文自然语言画面描述，后接少量 danbooru 补强标签。"
                "直接输出英文提示词，不要 JSON、不要解释，不要压缩成纯标签列表。"
                "根据物理距离判断自拍、对镜、POV 或第三人称视角。"
                "前摄自拍不出现手机和镜子；只有对镜自拍才允许镜子和手机同时出现。"
                "避免复杂手势和多手互动；除非原文强制要求，尽量不强调手部。"
                "自然语言句子尽量不要使用逗号。输出格式: English visual sentence. key tag, key tag, key tag"
            )
        text = await self._call_llm(system, f"请翻译: {natural}", temp=float(self._get_llm_value("image", "temperature_translate", "0.3")), tag="translate", purpose="image")
        body = text.strip().strip(",")
        if opener:
            return opener if not body or body == natural else f"{opener}, {body}"
        return body

    async def _translate_appearance_tags(self, text: str) -> str:
        if not self.has_llm_config("image"):
            return text
        system = "你是 danbooru 标签翻译器。把中文外观、穿搭、发型、瞳色、配饰描述翻译成英文标签，逗号分隔。只输出标签。"
        try:
            return (await self._call_llm(system, text, temp=0.3, tag="appearance-translate", purpose="image")).strip() or text
        except Exception as exc:
            logger.warning("外观标签翻译失败: %s", exc)
            return text

    async def _llm_classify_character(self, user_text: str) -> dict[str, Any]:
        system = (
            "你是角色设定助手。判断用户描述属于既有作品角色或外观体貌特征，只输出 JSON。\n"
            "既有作品角色输出 {\"type\":\"character\",\"name\":\"角色名\",\"series\":\"作品名\",\"persona\":\"中文人设\",\"appearance\":\"英文prompt标签\",\"purity\":0到10整数}。\n"
            "外观描述输出 {\"type\":\"appearance\",\"tags\":\"英文prompt标签\"}。\n"
            "appearance 必须包含性别标签开头，例如 1girl 或 1boy。"
        )
        text = await self._call_llm(system, user_text, temp=float(self._get_llm_value("image", "temperature_classify", "0.1")), tag="classify", purpose="image")
        return json.loads(text)

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

    async def _do_generate(self, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate(self, scene_desc, is_ntr, session_id)

    async def _do_generate_locked(self, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate_locked(self, scene_desc, is_ntr, session_id)

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
        )
        scene = (plan.get("scene") or "").strip()
        if not scene:
            return "缺少图片意图"
        caption = (plan.get("caption") or "").strip()
        final_view = (plan.get("view") or "").strip()
        new_app = (plan.get("new_appearance_tags") or "").strip()
        state = self._get_session_state(session_id)
        if new_app and self._allow_llm_change_appearance(session_id):
            state["dynamic_appearance"] = new_app
            self._save_session_state(session_id, state)
        english = await self._translate_to_tags(scene, session_id=session_id, view=final_view)
        ok, imgs, err = await self._do_generate(english, session_id=session_id)
        if not ok or not imgs:
            self._ulog(session_id, "ERROR", f"工具生图失败: {err}")
            return f"生图失败: {err}"
        await self.send_photo(chat_id, imgs[0], caption)
        self._record_sent_photo(
            session_id,
            scene,
            caption,
            appearance=state.get("dynamic_appearance", ""),
            view=final_view,
            source_description=source_description,
        )
        detail = f"图片已生成并发送。画面: {scene}"
        if caption:
            detail += f"；配文: {caption}"
        return detail

    async def tool_generate_selfie(self, chat_id, session_id: str) -> str:
        await self.cmd_selfie(chat_id, session_id, "")
        return "自拍已发送"

    async def tool_change_appearance(self, session_id: str, description: str = "", mode: str = "merge") -> str:
        state = self._get_session_state(session_id)
        if not self._allow_llm_change_appearance(session_id):
            return "当前会话已关闭模型自主修改外型，dynamic_appearance 未改变。"
        desc = (description or "").strip()
        if desc.lower() in ("reset", "none", "clear", "无", "", "重置", "恢复", "默认"):
            state["dynamic_appearance"] = ""
            self._save_session_state(session_id, state)
            return "外貌已重置为默认"
        if re.search(r"[a-zA-Z]{3,}", desc) and not re.search(r"[\u4e00-\u9fff]", desc):
            tags = desc
        else:
            tags = await self._translate_appearance_tags(desc)
        state["dynamic_appearance"] = self._merge_appearance(state.get("dynamic_appearance", ""), tags, mode=mode)
        self._save_session_state(session_id, state)
        return f"外貌已改变: {state['dynamic_appearance']}"

    async def _push_image_from_text(self, session_id: str, scene: str):
        chat_id = self.chat_id_from_session(session_id)
        try:
            english = await self._translate_to_tags(scene, session_id=session_id)
            ok, imgs, err = await self._do_generate(english, session_id=session_id)
            if ok and imgs:
                await self.send_photo(chat_id, imgs[0])
                self._record_sent_photo(
                    session_id,
                    scene,
                    "",
                    source_description=self._format_image_source_description(intent="聊天模型文字中泄漏出的配图描述", prompt=scene),
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
            saved = state.get("saved_characters", {})
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
            if state.get("custom_location") or state.get("custom_timezone_offset"):
                found = True
                lines.append(f"{sid}: {state.get('custom_location') or '(全局)'} | UTC{state.get('custom_timezone_offset') or self.config.get('timezone_offset')}")
        if not found:
            lines.append("所有会话均使用全局默认地区设置。")
        return "\n".join(lines)

    def _mgmt_sessions(self) -> str:
        lines = ["会话概况", ""]
        if not self.sessions:
            return "会话概况\n\n暂无会话记录。"
        for sid, state in self.sessions.items():
            last = state.get("last_interaction", 0)
            ago = "无记录"
            if last:
                sec = time.time() - last
                ago = f"{int(sec // 60)}分钟前" if sec < 3600 else f"{int(sec // 3600)}小时前" if sec < 86400 else f"{int(sec // 86400)}天前"
            push = f"{len(state.get('daily_triggered_times', []))}/{len(state.get('daily_trigger_times', []))}" if state.get("daily_trigger_times") else "关闭"
            lines.append(f"{sid}\n  角色: {state.get('custom_character') or '(未设定)'} | 纯良度: {self._get_purity(sid)}/10\n  上次互动: {ago} | 今日推送: {push}")
        return "\n".join(lines)
