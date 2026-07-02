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
from . import character_card
from . import generation as image_generation
from . import prompt_intake
from . import session_schema
from .app_store import AppStateStore
from .character_checkpoint import CharacterCheckpointMixin
from .config_store import dump_simple_yaml, flatten_config, load_simple_yaml
from .defaults import DEFAULT_CONFIG
from .image_planning import VALID_VIEWS, plan_roleplay_image
from .memory import LongTermMemoryStore
from .chat_context import ChatContextMixin
from .commands import CommandHandlersMixin
from .git_update import GitUpdateMixin
from .life_plan import LifePlanMixin
from .memory_policy import MemoryPolicyMixin
from .process_restart import ProcessRestartMixin
from .scheduler_runtime import SchedulerRuntimeMixin
from .telegram_io import TelegramIOMixin
from .time_context import build_time_context, format_light_guard, format_time_context, rough_time_period
from .world_runtime import WorldRuntimeMixin

logger = logging.getLogger(__name__)

_SIMPLE_LLM_CACHE_ANCHORS: dict[str, str] = {
    "roleplay-image-plan": (
        "Stable prefix for roleplay-image-plan v1.\n"
        "Task: convert roleplay context into one image plan and return strict JSON only.\n"
        "Output contract: scene, view, aspect_ratio, caption, new_appearance_tags, clothing_off, "
        "character_location, user_location, is_intimate, partner_in_frame, device_in_frame.\n"
        "Stable rules: plan exactly one frozen moment, never a collage or sequence; keep stable "
        "appearance out of scene; use new_appearance_tags only for one-shot clothing/accessory/hair "
        "changes; use clothing_off for removed garments/accessories or explicit nudity; preserve hard "
        "spatial/body constraints; obey the user's explicit camera/composition request for free image "
        "commands; avoid phones, camera UI, chat UI, mirrors, and devices unless the requested view "
        "explicitly requires mirror/device visibility; choose only 2:3 or 3:2 aspect ratio.\n"
        "Dynamic persona, weather, world state, memories, continuity, and the current request appear "
        "after this stable prefix and override only where they provide concrete facts."
    ),
    "translate": (
        "Stable prefix for image tag translation v1.\n"
        "Task: translate the provided scene into concise English image-generation tags while preserving "
        "subject ownership, action direction, camera/view constraints, visible weather/light, and safety "
        "guards. Return prompt text only, not explanations."
    ),
}

_CJK_RE = re.compile(r"[一-鿿]")


def _HAS_CJK(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


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
PERSISTENT_ACCESSORY_FAMILY_TERMS: dict[str, tuple[str, ...]] = {
    "glasses": ("glasses", "sunglasses", "spectacles"),
    "necklace": ("necklace",),
    "earring": ("earring", "earrings"),
    "bracelet": ("bracelet",),
    "ring": ("ring",),
    "hair_clip": ("hair clip", "hairclip", "hairpin", "clip"),
    "ribbon": ("ribbon",),
    "bow": ("bow",),
    "scarf": ("scarf",),
    "collar": ("collar",),
    "choker": ("choker",),
    "hat": ("hat", "cap"),
    "crown": ("crown", "tiara"),
    "watch": ("watch",),
    "belt": ("belt",),
    "glove": ("glove",),
    "mask": ("mask",),
    "veil": ("veil",),
}

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
        self._dirty_sessions: set[str] = set()
        self._last_state_write = 0.0
        self._state_write_interval = 30.0
        self._weather_caches: dict[str, dict[str, Any]] = {}
        self._skill_reference_cache: str | None = None
        self._bot_username = ""
        self._offset = 0
        self._bot_tasks: list[asyncio.Task] = []
        self._checkpoint_tasks: dict[str, asyncio.Task] = {}
        self._dream_tasks: dict[str, asyncio.Task] = {}
        self._life_plan_tasks: dict[str, asyncio.Task] = {}
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
        self._flush_llm_debug(force=True)
        if self.comfy_session and not self.comfy_session.closed:
            await self.comfy_session.close()

    # ---------------------------------------------------------------------
    # Config / state
    # ---------------------------------------------------------------------
    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            try:
                if self.config_path.suffix.lower() in (".yml", ".yaml"):
                    loaded = flatten_config(load_simple_yaml(self.config_path))
                else:
                    loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception as exc:
                raise RuntimeError(f"配置文件读取失败: {self.config_path}: {exc}") from exc
        else:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            if self.config_path.suffix.lower() in (".yml", ".yaml"):
                self.config_path.write_text(dump_simple_yaml(cfg), encoding="utf-8")
            else:
                self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.warning("配置文件不存在，已写入默认配置: %s", self.config_path)
        return cfg

    def save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config_path.suffix.lower() in (".yml", ".yaml"):
            self.config_path.write_text(dump_simple_yaml(self.config), encoding="utf-8")
        else:
            self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def reload_config_from_disk(self) -> dict[str, Any]:
        """从当前配置文件重新载入运行态配置，不写回磁盘。"""
        self.config = self._load_config()
        for attr in ("_cached_outfit_kw", "_cached_accessory_kw"):
            if hasattr(self, attr):
                delattr(self, attr)
        return self.config

    def _memory_db_path(self) -> Path:
        raw = str(self.config.get("long_memory_db_path") or "").strip()
        if not raw:
            return self.state_path.with_name("memory.sqlite3")
        path = Path(raw)
        if not path.is_absolute():
            path = self.config_path.parent / path
        return path

    def _load_state(self):
        """从 SQLite 加载会话状态和城市目录。

        首次启动时如果 SQLite 无数据但 state.json 存在，自动迁移旧数据。
        """
        self.sessions = {}
        self.city_place_catalogs = {}
        try:
            if self.app_store.has_session_states():
                self.sessions = self.app_store.load_all_session_states()
                logger.info("Loaded %d sessions from SQLite", len(self.sessions))
            elif self.state_path.exists():
                self._migrate_from_state_json()
            # 城市目录独立加载（可能先于会话写入）
            self.city_place_catalogs = self.app_store.load_all_city_catalogs()
            self._migrate_session_boxes_on_startup()
        except Exception as exc:
            logger.warning("加载状态失败，使用空状态: %s", exc)
        self._migrate_legacy_personas()

    def _backup_file(self, source: Path, reason: str) -> str:
        """在自动迁移写回前备份旧数据文件。"""
        if not source.exists():
            return ""
        stamp = int(time.time())
        base = f"{source.stem}.{reason}-backup-{stamp}"
        backup = source.with_name(f"{base}{source.suffix}")
        index = 1
        while backup.exists():
            backup = source.with_name(f"{base}-{index}{source.suffix}")
            index += 1
        backup.write_bytes(source.read_bytes())
        path = str(backup)
        self.startup_backup_paths.append(path)
        logger.info("已备份旧状态文件: %s -> %s", source, backup)
        return path

    def _migrate_from_state_json(self):
        """从旧版 state.json 迁移数据到 SQLite。"""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            if not raw.strip():
                return
            data = json.loads(raw)
            sessions = data.get("sessions", {})
            catalogs = data.get("city_place_catalogs", {})
            if (isinstance(sessions, dict) and sessions) or (isinstance(catalogs, dict) and catalogs):
                self._backup_file(self.state_path, "state-json-migration")
            if isinstance(sessions, dict):
                self.sessions = sessions
                for sid, state in sessions.items():
                    if isinstance(state, dict):
                        self.app_store.save_session_state(sid, state)
                logger.info("Migrated %d sessions from %s to SQLite", len(sessions), self.state_path)
            if isinstance(catalogs, dict):
                self.city_place_catalogs = catalogs
                for key, catalog in catalogs.items():
                    if isinstance(catalog, dict):
                        self.app_store.save_city_catalog(key, catalog)
        except Exception as exc:
            logger.warning("state.json 迁移失败: %s", exc)

    def _ensure_session_boxes(self, state: dict[str, Any]) -> None:
        """补齐所有 state 分盒；老扁平数据在各 ensure_* 内幂等迁移。"""
        session_schema.ensure_character_box(state)
        session_schema.ensure_clothing_box(state)
        session_schema.ensure_place_box(state)
        session_schema.ensure_context_box(state)
        session_schema.ensure_session_box(state)

    def _migrate_session_boxes_on_startup(self) -> None:
        """重启即迁移旧 state 结构，并在写回前备份 SQLite。"""
        candidates = [
            (sid, state)
            for sid, state in self.sessions.items()
            if isinstance(state, dict) and session_schema.state_needs_box_migration(state)
        ]
        if not candidates:
            return
        self._backup_file(self.app_store.path, "box-migration")
        for sid, state in candidates:
            self._ensure_session_boxes(state)
            self.app_store.save_session_state(sid, state)
        logger.info("已迁移 %d 个会话 state 分盒结构", len(candidates))

    # 老数据迁移：早期把身份/关系焊进了 custom_scheduled_persona，现已改为读时组装。
    # 启动时把这两类前缀剥掉，让人设串退化成纯人格描述（幂等，剥干净后不再变动）。
    _LEGACY_OC_IDENTITY_RE = re.compile(r"^你是[^，,。\n]+，一名[^。\n]*。[ \t]*\n?")
    _LEGACY_REL_LINE_RE = re.compile(r"(?:^|\n)你和用户的关系[:：][^\n]*")

    @classmethod
    def _strip_legacy_persona_bakein(cls, text: Any) -> tuple[Any, bool]:
        if not text or not isinstance(text, str):
            return text, False
        new = cls._LEGACY_OC_IDENTITY_RE.sub("", text, count=1)
        new = cls._LEGACY_REL_LINE_RE.sub("", new).strip()
        return new, new != text.strip()

    def _migrate_legacy_personas(self):
        changed = False
        for state in self.sessions.values():
            if not isinstance(state, dict):
                continue
            new, ch = self._strip_legacy_persona_bakein(
                session_schema.get_character_value(state, "custom_scheduled_persona")
            )
            if ch:
                session_schema.set_character_value(state, "custom_scheduled_persona", new)
                changed = True
            saved = session_schema.get_saved_characters(state)
            if isinstance(saved, dict):
                for entry in saved.values():
                    if not isinstance(entry, dict):
                        continue
                    np, ch2 = self._strip_legacy_persona_bakein(entry.get("persona"))
                    if ch2:
                        entry["persona"] = np
                        changed = True
        if changed:
            try:
                for sid, state in self.sessions.items():
                    self.app_store.save_session_state(sid, state)
                logger.info("已清洗历史人设串里焊死的身份/关系前缀")
            except Exception:
                logger.warning("历史人设迁移写回失败", exc_info=True)

    def _write_state(self):
        """将所有脏会话写入 SQLite（替代旧版全量 JSON 写入）。"""
        for session_id in self._dirty_sessions:
            state = self.sessions.get(session_id)
            if state is not None:
                self.app_store.save_session_state(session_id, state)
        self._dirty_sessions.clear()
        self._last_state_write = time.time()

    def _mark_dirty(self, session_id: str):
        if session_id:
            self._dirty_sessions.add(session_id)

    def _flush_sessions(self, force=False):
        now = time.time()
        if not force and now - self._last_state_write < self._state_write_interval:
            return
        if not self._dirty_sessions:
            return
        self._write_state()

    def _save_session_state(self, session_id: str, state: dict[str, Any]):
        if not session_id:
            return
        self.sessions[session_id] = state
        self.app_store.save_session_state(session_id, state)
        self._dirty_sessions.discard(session_id)

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

    def _error_log_enabled(self) -> bool:
        value = self.config.get("error_log_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def _error_log_path(self) -> Path:
        return self._user_log_dir() / "errors.log"

    def _log_archive_paths(self, path: Path) -> list[Path]:
        archive_dir = path.parent / "chunks"
        candidates: list[Path] = []
        if archive_dir.exists():
            candidates.extend(archive_dir.glob(f"{path.stem}.*{path.suffix}"))
        # 兼容上一版同目录分片，读取和清理时仍能找到。
        candidates.extend(path.parent.glob(f"{path.stem}.*{path.suffix}"))
        try:
            return sorted(
                {item.resolve(): item for item in candidates}.values(),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []

    def _log_all_paths(self, path: Path) -> list[Path]:
        paths = []
        if path.exists():
            paths.append(path)
        paths.extend(self._log_archive_paths(path))
        return paths

    def _log_latest_path(self, path: Path) -> Path:
        if path.exists():
            return path
        archived = self._log_archive_paths(path)
        return archived[0] if archived else path

    def _resolve_log_chunk_path(self, path: Path, chunk: str = "") -> Path:
        chunk = (chunk or "").strip()
        if not chunk or chunk in {"current", "latest", path.name}:
            return self._log_latest_path(path)
        # 只允许按文件名选择已知分块，避免路径穿越。
        if "/" in chunk or "\\" in chunk:
            return self._log_latest_path(path)
        for item in self._log_all_paths(path):
            if item.name == chunk:
                return item
        return self._log_latest_path(path)

    def _user_log_archive_paths(self, session_id: str) -> list[Path]:
        return self._log_archive_paths(self._user_log_path(session_id))

    def _user_log_latest_path(self, session_id: str) -> Path:
        return self._log_latest_path(self._user_log_path(session_id))

    def _user_log_all_paths(self, session_id: str) -> list[Path]:
        return self._log_all_paths(self._user_log_path(session_id))

    def _error_log_all_paths(self) -> list[Path]:
        return self._log_all_paths(self._error_log_path())

    def _rotate_log_file_if_needed(self, path: Path) -> None:
        """日志按完整行滚动分块；只有写入下一条前才切块，不拆当前条目。"""
        try:
            limit = int(self.config.get("user_log_rotate_bytes", 6 * 1024 * 1024) or 6 * 1024 * 1024)
        except Exception:
            limit = 6 * 1024 * 1024
        if limit <= 0:
            return
        try:
            if not path.exists() or path.stat().st_size < limit:
                return
            archive_dir = path.parent / "chunks"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = archive_dir / f"{path.stem}.{stamp}{path.suffix}"
            index = 1
            while target.exists():
                target = archive_dir / f"{path.stem}.{stamp}.{index}{path.suffix}"
                index += 1
            path.replace(target)
        except Exception:
            logger.debug("user log rotate failed", exc_info=True)

    def _ulog(self, session_id: str, tag: str, message: str = ""):
        """按用户追加一行活动日志。事件级，纯同步，事件循环内原子完成。"""
        if not session_id:
            return
        try:
            stamp = self._session_now(session_id).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (message or "").replace("\r", "").replace("\n", " ⏎ ").strip()
        if str(tag or "").upper() == "ERROR":
            self._write_error_log_line(stamp, session_id, body)
        if not self._user_log_enabled():
            return
        line = f"{stamp} {tag}" + (f" {body}" if body else "")
        try:
            path = self._user_log_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_log_file_if_needed(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.debug("user log write failed", exc_info=True)

    def _write_error_log_line(self, stamp: str, session_id: str, body: str) -> None:
        """把 ERROR 镜像到全局错误日志，便于 Web 错误页直接读取完整请求/返回。"""
        if not self._error_log_enabled():
            return
        safe_session = str(session_id or "").replace("\r", "").replace("\n", " ").strip() or "unknown"
        line = f"{stamp} ERROR session={safe_session}" + (f" {body}" if body else "")
        try:
            path = self._error_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_log_file_if_needed(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.debug("error log write failed", exc_info=True)

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

    @staticmethod
    def _session_state_defaults() -> dict[str, Any]:
        """会话 state 全部字段的默认值。

        单一来源在 `session_schema.STATE_SCHEMA`（每字段声明 归属 + 默认值 + reset 保留）。
        既供 `_get_session_state` 做 setdefault，也供通用上下文清空按字段默认值复位
        （见 commands._clear_conversation_context）。新增字段在 STATE_SCHEMA 加一行即可。
        """
        return session_schema.state_defaults()

    def _get_session_state(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            self.sessions[session_id] = {}
        state = self.sessions[session_id]
        # character 要先补盒再补扁平默认值；否则盒内旧数据会被刚 setdefault 出来的空扁平键遮住。
        session_schema.ensure_character_box(state)
        for key, val in self._session_state_defaults().items():
            state.setdefault(key, val)
        # character 字段已收进 state["character"] 盒；保留旧扁平 custom_* 兼容读写。
        session_schema.ensure_character_box(state)
        # clothing 字段已收进 state["clothing"] 盒；迁移旧扁平持久态并补齐子键。
        session_schema.ensure_clothing_box(state)
        # place 字段已收进 state["place"] 盒；迁移旧扁平持久态并补齐子键。
        session_schema.ensure_place_box(state)
        # context 字段已收进 state["context"] 盒；迁移旧扁平持久态并补齐子键。
        session_schema.ensure_context_box(state)
        # session 字段已收进 state["session"] 盒；迁移旧扁平持久态并补齐子键。
        session_schema.ensure_session_box(state)
        return state

    def _default_character_payload(self) -> dict[str, Any]:
        """从全局默认值构建系统默认角色卡（蕾伊），用于在角色池中始终展示、可选中加载。

        character 留空：加载它即 custom_character="" 回到隐式默认态，由全局配置渲染，
        因此无需把发/瞳烘焙进 appearance（隐式默认态下 default_hair/default_eyes 正常回落）。
        与 webui 的角色池默认条目共用此唯一来源。
        """
        cfg = self.config
        bot_name = str(cfg.get("bot_name", "蕾伊") or "蕾伊").strip()
        return {
            "id": bot_name,
            "is_default": True,
            "character": "",
            "series": "",
            "role_name": str(cfg.get("role_name", "魅魔") or "").strip(),
            "bot_name": bot_name,
            "bot_self_name": str(cfg.get("bot_self_name", "我") or "").strip(),
            "user_address": "",
            "visual_character": "",
            "visual_series": "",
            "persona": str(cfg.get("scheduled_persona", "") or "").strip(),
            "appearance": str(cfg.get("positive_prefix", "") or "").strip(),
            "count": "",
            "age_stage": str(cfg.get("character_age_stage", "") or "").strip(),
            "occupation": "",
            "day_anchor": str(cfg.get("character_day_anchor", "") or "").strip(),
            "relationship": str(cfg.get("spatial_relationship", "") or "").strip(),
            "scene_preference": "",
            "selfie_preference": "",
            "style": str(cfg.get("current_style", "") or "").strip(),
            "outfit": str(cfg.get("dynamic_appearance", "") or "").strip(),
            "allow_change_appearance": bool(cfg.get("allow_llm_change_appearance", True)),
            "purity": None,
        }

    # 卡片字段 → config 键的映射：默认角色以 config 为存储，编辑卡片即写回 config。
    # 单一来源见 character_card.DEFAULT_CARD_TO_CONFIG（与默认卡读取共用同一字段集）。
    _DEFAULT_CARD_TO_CONFIG = character_card.DEFAULT_CARD_TO_CONFIG

    def _apply_default_character_payload(self, payload: dict[str, Any]) -> None:
        """把卡编辑器对默认角色的修改写回 config（不进 saved_characters，不动 custom_*）。"""
        if not isinstance(payload, dict):
            return
        for src, dst in self._DEFAULT_CARD_TO_CONFIG.items():
            if src in payload:
                self.config[dst] = "" if payload[src] is None else str(payload[src]).strip()
        if "allow_change_appearance" in payload:
            # 默认角色以全局配置为存储：非空才写回全局开关，空（跟随全局）对默认角色即不改。
            raw = payload.get("allow_change_appearance")
            s = "" if raw is None else str(raw).strip().lower()
            if s:
                self.config["allow_llm_change_appearance"] = s in ("true", "1", "yes", "on", "开", "允许", "启用")
        if "purity" in payload:
            p = payload.get("purity")
            self.config["default_purity"] = "" if p in (None, "") else str(p)
        self.save_config()

    def _get_session_cfg(self, session_id: str, key: str, default=None):
        if session_id:
            state = self._get_session_state(session_id)
            override = session_schema.get_custom_value(state, key)
            if override not in (None, ""):
                return override
        return self.config.get(key, default)

    @staticmethod
    def _persona_with_character_identity(character: Any, series: Any, persona: Any) -> str:
        """确保既有角色的人设里有明确身份，避免模型回落到全局默认角色。"""
        base = str(persona or "").strip()
        name = str(character or "").strip()
        if not name:
            return base
        if name in base:
            return base
        work = str(series or "").strip()
        prefix = f"你是{name}{f'（{work}）' if work else ''}。"
        return f"{prefix}\n{base}" if base else prefix

    def _session_role_identity(self, session_id: str = "") -> tuple[str, str, str]:
        """返回当前会话的角色类型、角色名、自称；角色态不回落到全局默认角色名。"""
        state = self._get_session_state(session_id) if session_id else {}
        character = session_schema.get_character_value(state, "custom_character", "") if state else ""
        series = session_schema.get_character_value(state, "custom_series", "") if state else ""
        role_override = session_schema.get_character_value(state, "custom_role_name", "") if state else ""
        bot_override = session_schema.get_character_value(state, "custom_bot_name", "") if state else ""
        self_name = session_schema.get_character_value(state, "custom_bot_self_name", "") if state else ""
        if session_id and self._is_character_set(session_id) and character:
            role_name = (
                role_override
                or series
                or "角色"
            )
            bot_name = (
                bot_override
                or character
                or self.config.get("bot_name", "蕾伊")
            )
            bot_self_name = self_name or "我"
            return role_name, bot_name, bot_self_name
        # 角色态但 custom_character 为空（persona_user_set=True 但未填角色名）：用 neutral 占位，
        # 避免回退到全局默认魅魔/蕾伊串到其他 OC 角色。
        if session_id and self._is_character_set(session_id):
            bot = bot_override or self.config.get("bot_name", "角色")
            role = role_override or series or "角色"
            return role, bot, self_name or "我"
        return (
            self._get_session_cfg(session_id, "role_name", "魅魔"),
            self._get_session_cfg(session_id, "bot_name", "蕾伊"),
            self._get_session_cfg(session_id, "bot_self_name", "我"),
        )

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

    def _effective_dynamic_appearance(self, session_id: str = "") -> str:
        """当前临时穿搭。全局默认 dynamic_appearance 只属于默认角色（魅魔）；
        一旦设了角色（OC/既有），就不再回退全局默认，避免默认服装串到东云绘名这类角色身上——
        既有角色没有自带初始穿搭时返回空，交给画面规划器按场景决定。"""
        state = self._get_session_state(session_id) if session_id else {}
        own = session_schema.get_outfit(state).strip() if state else ""
        if own:
            return own
        if session_id and self._is_character_set(session_id):
            return ""
        return self.config.get("dynamic_appearance", "")

    def _allow_llm_change_appearance(self, session_id: str) -> bool:
        state = self._get_session_state(session_id)
        override = session_schema.get_character_value(state, "custom_allow_llm_change_appearance")
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
            custom = str(session_schema.get_character_value(state, "custom_current_style", "")).strip()
            if custom or self._is_character_set(session_id):
                return custom
        current = str(self.config.get("current_style", "")).strip()
        return current if current in pool else pool[0]

    def _set_current_style(self, session_id: str, style: str):
        style = (style or "").strip()
        if session_id:
            state = self._get_session_state(session_id)
            session_schema.set_character_value(state, "custom_current_style", style)
            if hasattr(self, "_snapshot_character"):
                self._snapshot_character(state)
            self._save_session_state(session_id, state)
        else:
            pool = self._normalize_style_pool()
            if style and style not in pool:
                pool.append(style)
                self.config["style_pool"] = "\n".join(pool)
            self.config["current_style"] = style
            self.save_config()

    def _ensure_style_pool_entry(self, style: str) -> bool:
        """把角色卡里出现的新画风补进全局画风池，供其他用户参考。"""
        style = (style or "").strip()
        if not style:
            return False
        pool = self._normalize_style_pool()
        if any(style.lower() == item.lower() for item in pool):
            return False
        pool.append(style)
        self.config["style_pool"] = "\n".join(pool)
        self.save_config()
        return True

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
        """用户自己的性别（male/female）：决定亲密场景里“用户身体”画成男性还是女性局部。"""
        state = self._get_session_state(session_id) if session_id else {}
        raw = state.get("custom_user_gender") or self.config.get("user_gender") or "male"
        g = re.sub(r"\s+", "", str(raw).strip().lower())
        if g in ("female", "f", "woman", "女", "女性", "女生", "girl"):
            return "female"
        return "male"

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
        defer_history_message: bool = False,
    ):
        state = self._get_session_state(session_id)
        history = session_schema.get_sent_photos_history(state)
        source_description = (source_description or "").strip()
        nltag_text = (nltag or self._last_generated_photo_nltag(session_id) or scene or "").strip()
        source_intent = self._compact_photo_source_intent(source_description)
        appearance_snapshot = (appearance or "").strip()
        if not appearance_snapshot:
            try:
                appearance_snapshot = self._effective_visual_prompt_tags(session_id)
            except Exception:
                appearance_snapshot = session_schema.get_outfit(state)
        photo = {
            "timestamp": time.time(),
            "scene": scene,
            "caption": caption,
            "appearance": appearance_snapshot,
            "view": (view or "").strip().lower(),
            "source_description": source_description,
            "source_intent": source_intent,
            "nltag": nltag_text,
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
        lines = [
            "照片历史（系统记录，保留到对话历史裁剪；用户明确提到照片/刚才画面时再引用，不要主动复述；视觉内容只保留最终 nltag）：",
            f"nltag: {nltag or '未记录'}",
        ]
        if source_intent:
            lines.append(source_intent)
        if caption and caption != scene:
            lines.append(f"配文: {caption}")
        if view:
            lines.append(f"视角: {view}")
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
            base = session_schema.get_character_value(state, "custom_positive_prefix", "") or self.config.get("positive_prefix", "")
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

    @staticmethod
    def _persistent_accessory_family(tag: str) -> str:
        low = str(tag or "").lower()
        for family, terms in PERSISTENT_ACCESSORY_FAMILY_TERMS.items():
            if any(term in low for term in terms):
                return family
        return ""

    def _resolve_persistent_accessory_removals(
        self,
        state: dict[str, Any],
        clothing_off: str,
        *sources: str,
    ) -> list[str]:
        raw = (clothing_off or "").strip()
        if not raw:
            return []
        wardrobe = self._get_wardrobe(state)
        current_accessories = [
            appearance_rules.normalize_appearance_tag(tag)
            for tag in (wardrobe.get("accessory") or "").split(",")
            if tag.strip()
        ]
        if not current_accessories:
            return []
        requested = [
            appearance_rules.normalize_appearance_tag(tag)
            for tag in re.split(r"[,;]+", raw)
            if tag.strip()
        ]
        requested = [tag for tag in requested if self._persistent_accessory_family(tag)]
        if not requested:
            return []
        matched: list[str] = []
        seen: set[str] = set()
        for acc in current_accessories:
            acc_low = acc.lower()
            acc_family = self._persistent_accessory_family(acc_low)
            if not acc_family:
                continue
            for token in requested:
                tok_low = token.lower()
                tok_family = self._persistent_accessory_family(tok_low)
                if not tok_family:
                    continue
                if tok_low in acc_low or acc_low in tok_low or tok_family == acc_family:
                    if acc_low not in seen:
                        matched.append(acc)
                        seen.add(acc_low)
                    break
        return matched

    def _persist_removed_accessories_from_image(
        self,
        session_id: str,
        clothing_off: str,
        *sources: str,
    ) -> str:
        state = self._get_session_state(session_id)
        remove_tags = self._resolve_persistent_accessory_removals(state, clothing_off, *sources)
        if not remove_tags:
            return ""
        wardrobe_before = self._get_wardrobe(state)
        wardrobe_after = appearance_rules.apply_wardrobe_change(
            wardrobe_before,
            {"accessory_remove": ", ".join(remove_tags)},
        )
        if wardrobe_after == wardrobe_before:
            return ""
        session_schema.set_wardrobe(state, wardrobe_after)
        rendered = appearance_rules.render_wardrobe(wardrobe_after)
        session_schema.set_outfit(state, rendered)
        self._save_session_state(session_id, state)
        self._ulog(
            session_id,
            "WARDROBE",
            f'图像后持久化 accessory_remove={remove_tags} 来源=clothing_off="{clothing_off[:80]}" | 结果="{rendered[:140]}"',
        )
        return rendered

    def _chat_visible_appearance_context(self, session_id: str) -> str:
        effective = self._effective_visual_prompt_tags(session_id)
        if not effective:
            return ""
        state = self._get_session_state(session_id)
        dynamic_slots = self._parse_appearance(self._effective_dynamic_appearance(session_id))
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
    ) -> tuple[str, str]:
        return image_generation.build_prompt(
            self, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
            is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
            clothing_off=clothing_off,
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
    # LLM / ComfyUI
    # ---------------------------------------------------------------------
    @staticmethod
    def _llm_profile_model_name(profile: dict[str, Any], thinking: bool) -> tuple[str, str, str]:
        """按 ref/app.py 的 profile 结构解析思考/非思考模型。"""
        if thinking and profile.get("model_think"):
            return profile.get("model_think") or "", profile.get("base_url") or "", profile.get("api_key") or ""
        if not thinking and profile.get("model_no_think"):
            return (
                profile.get("model_no_think") or "",
                profile.get("base_url_no_think") or profile.get("base_url") or "",
                profile.get("api_key_no_think") or profile.get("api_key") or "",
            )
        return profile.get("model") or profile.get("model_no_think") or profile.get("model_think") or "", profile.get("base_url") or "", profile.get("api_key") or ""

    def _user_id_for_session(self, session_id: str = "") -> str:
        return str(session_id or "").removeprefix("telegram:")

    def _global_model_profiles(self) -> dict[str, dict[str, Any]]:
        profiles = self.config.get("global_model_profiles") or {}
        return profiles if isinstance(profiles, dict) else {}

    def _resolve_llm_profile(self, purpose: str, session_id: str = "") -> tuple[str, dict[str, Any], bool]:
        """解析当前会话实际使用的 LLM profile。

        chat 使用 chat_profile_id，image/fast 使用 fast_profile_id，vision 使用 vision_profile_id。
        vision 没有显式配置时保持为空，用于关闭图片理解链路。
        """
        user_id = self._user_id_for_session(session_id)
        settings = self.app_store.get_user_model_settings(user_id) if user_id else {}
        user_profiles = self.app_store.list_model_profiles(user_id) if user_id else {}
        global_profiles = self._global_model_profiles()
        if purpose == "chat":
            profile_id = settings.get("chat_profile_id") or self.config.get("default_chat_model_profile") or ""
        elif purpose == "vision":
            profile_id = settings.get("vision_profile_id") or self.config.get("default_vision_model_profile") or ""
        else:
            profile_id = settings.get("fast_profile_id") or self.config.get("default_fast_model_profile") or ""
        profile = user_profiles.get(profile_id) or global_profiles.get(profile_id) or {}
        if purpose == "vision" and not profile:
            return str(profile_id or ""), {}, False
        if not profile and global_profiles:
            profile_id, profile = next(iter(global_profiles.items()))
        disable = profile.get("disable_thinking", self._get_llm_value(purpose, "disable_thinking", False))
        if isinstance(disable, str):
            disable = disable.lower() in ("true", "1", "yes", "on")
        thinking = not bool(disable)
        return str(profile_id or ""), dict(profile or {}), thinking

    def _resolved_llm_config(self, purpose: str, session_id: str = "", disable_thinking: bool | None = None) -> dict[str, Any]:
        profile_id, profile, thinking = self._resolve_llm_profile(purpose, session_id)
        model, api_base, api_key = self._llm_profile_model_name(profile, thinking)
        if purpose != "vision" and not api_base:
            api_base = self._get_llm_value(purpose, "api_base", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1"
        if purpose != "vision" and not api_key:
            api_key = self._get_llm_value(purpose, "api_key", "") or ""
        if purpose != "vision" and not model:
            model = self._get_llm_value(purpose, "model", "deepseek-chat") or "deepseek-chat"
        return {
            "profile_id": profile_id,
            "profile": profile,
            "thinking": thinking,
            "api_base": str(api_base).rstrip("/"),
            "api_key": api_key,
            "model": model,
            "max_tokens": profile.get("max_tokens") or self._get_llm_value(purpose, "max_tokens", "4096") or "4096",
            "timeout": profile.get("timeout") or 120,
            "thinking_control": profile.get("thinking_control", "model_name"),
        }

    def _record_llm_usage_from_response(
        self,
        data: dict[str, Any],
        resolved: dict[str, Any],
        *,
        tag: str = "",
        purpose: str = "",
        session_id: str = "",
    ):
        """从 LLM 返回的 usage 字段提取 token 消耗并写入数据库。"""
        usage = data.get("usage") or {} if isinstance(data, dict) else {}
        if not usage:
            return
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cached_tokens = self._cached_tokens_from_usage(usage, prompt_tokens=prompt_tokens)
        self.app_store.record_llm_usage(
            profile_id=str(resolved.get("profile_id") or ""),
            model=str(resolved.get("model") or ""),
            purpose=str(purpose or ""),
            tag=str(tag or ""),
            session_id=str(session_id or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens or prompt_tokens + completion_tokens,
        )

    @staticmethod
    def _cached_tokens_from_usage(usage: dict[str, Any] | None, *, prompt_tokens: int = 0) -> int:
        """兼容不同 OpenAI-compatible provider 的缓存命中字段。"""
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cached_tokens = int(
            usage.get("prompt_cache_hit_tokens")
            or usage.get("prompt_cached_tokens")
            or usage.get("cached_tokens")
            or details.get("cached_tokens")
            or 0
        )
        miss_tokens = int(usage.get("prompt_cache_miss_tokens") or usage.get("cache_miss_tokens") or 0)
        if not cached_tokens and miss_tokens and prompt_tokens:
            cached_tokens = max(0, int(prompt_tokens or 0) - miss_tokens)
        return max(0, cached_tokens)

    @staticmethod
    def _json_safe(value: Any) -> Any:
        """把调试数据压成可 JSON 序列化结构，避免日志写入影响主请求。"""
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return str(value)

    @staticmethod
    def _llm_usage_debug_summary(data: dict[str, Any] | None) -> dict[str, Any]:
        usage = (data or {}).get("usage") if isinstance(data, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cached_tokens = TelegramComfyUIService._cached_tokens_from_usage(usage, prompt_tokens=prompt_tokens)
        miss_tokens = int(usage.get("prompt_cache_miss_tokens") or usage.get("cache_miss_tokens") or 0)
        return {
            "raw": dict(usage),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "cache_miss_tokens": miss_tokens,
            "cache_hit_rate": round(cached_tokens / prompt_tokens, 4) if prompt_tokens else 0,
        }

    @staticmethod
    def _llm_finish_reason(data: dict[str, Any] | None) -> str:
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice = choices[0] if isinstance(choices[0], dict) else {}
        return str(choice.get("finish_reason") or "")

    def _llm_debug_log_path(self) -> Path:
        return self._user_log_dir() / "llm_debug.json"

    def _flush_llm_debug(self, *, force: bool = False) -> None:
        pending = getattr(self, "_llm_debug_buffer", [])
        if not pending:
            return
        threshold = int(getattr(self, "_llm_debug_flush_threshold", 10) or 10)
        if not force and len(pending) < threshold:
            return
        batch = list(pending)
        try:
            path = self._llm_debug_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_log_file_if_needed(path)
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            else:
                data = {}
            if not isinstance(data, dict):
                data = {}
            grouped = data.get("entries_by_type")
            if not isinstance(grouped, dict):
                grouped = {}
            for entry in batch:
                key = str(entry.get("type") or "unknown:untagged")
                entries = grouped.get(key)
                if not isinstance(entries, list):
                    entries = []
                entries.append(entry)
                grouped[key] = entries[-10:]
            updated_at = batch[-1].get("time") or datetime.now().isoformat(timespec="seconds")
            data = {
                "schema_version": 1,
                "updated_at": updated_at,
                "retention": "last 10 entries per purpose:tag",
                "flush_policy": f"replace whole file after {threshold} buffered LLM records",
                "entries_by_type": grouped,
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            del pending[:len(batch)]
        except Exception as exc:
            logger.debug("flush llm debug failed: %s", exc)

    def _record_llm_debug(
        self,
        *,
        purpose: str,
        tag: str,
        session_id: str,
        resolved: dict[str, Any],
        request_url: str,
        request_body: dict[str, Any],
        response: Any,
        status: int | None = None,
        error: str = "",
    ) -> None:
        """按 purpose:tag 保存最近 10 次完整 LLM 请求/返回，供上下文缓存命中分析。"""
        key = f"{purpose or 'unknown'}:{tag or 'untagged'}"
        now = time.time()
        usage_summary = self._llm_usage_debug_summary(response if isinstance(response, dict) else None)
        entry = {
            "ts": now,
            "time": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "type": key,
            "purpose": purpose or "",
            "tag": tag or "",
            "session_id": session_id or "",
            "profile_id": str(resolved.get("profile_id") or ""),
            "model": str(resolved.get("model") or ""),
            "thinking": bool(resolved.get("thinking")),
            "status": status,
            "finish_reason": self._llm_finish_reason(response if isinstance(response, dict) else None),
            "completion_tokens": usage_summary.get("completion_tokens", 0),
            "max_tokens": (request_body or {}).get("max_tokens"),
            "request": {
                "url": request_url,
                "body": self._json_safe(request_body),
            },
            "response": self._json_safe(response),
            "usage": usage_summary,
        }
        if error:
            entry["error"] = error
        self._llm_debug_buffer.append(entry)
        self._flush_llm_debug(force=False)

    def _record_llm_error_log(
        self,
        *,
        session_id: str,
        purpose: str,
        tag: str,
        request_url: str = "",
        request_body: dict[str, Any] | None = None,
        response: Any = None,
        status: int | None = None,
        error: str = "",
    ) -> None:
        """把失败时的完整 LLM 请求/返回写入用户 ERROR 日志，避免只看到兜底文案。"""
        if not session_id:
            return
        response_data = response if isinstance(response, dict) else None
        usage_summary = self._llm_usage_debug_summary(response_data)
        payload = {
            "purpose": purpose or "",
            "tag": tag or "",
            "status": status,
            "error": error or "",
            "finish_reason": self._llm_finish_reason(response_data),
            "completion_tokens": usage_summary.get("completion_tokens", 0),
            "request": {
                "url": request_url or "",
                "body": self._json_safe(request_body or {}),
            },
            "response": self._json_safe(response),
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            text = str(payload)
        self._ulog(session_id, "ERROR", f"LLM_FULL_LOG {text}")

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

    def has_llm_config(self, purpose: str, session_id: str = "") -> bool:
        resolved = self._resolved_llm_config(purpose, session_id)
        if purpose == "vision":
            return bool(resolved.get("api_key") and resolved.get("api_base") and resolved.get("model"))
        return bool(resolved.get("api_key"))

    async def _call_llm_messages(
        self,
        messages: list[dict[str, Any]],
        tools=None,
        tool_choice=None,
        tag: str = "",
        temp: float | None = None,
        purpose: str = "image",
        disable_thinking: bool | None = None,
        session_id: str = "",
        sampling: bool = False,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolved_llm_config(purpose, session_id, disable_thinking=disable_thinking)
        api_base = resolved["api_base"]
        api_key = resolved["api_key"]
        if not api_key:
            label = "chat model" if purpose == "chat" else ("vision model" if purpose == "vision" else "fast model")
            raise RuntimeError(f"{label} API Key is not configured")
        max_tokens_value = max_tokens if max_tokens is not None else (resolved.get("max_tokens") or "4096")
        try:
            max_tokens_int = max(1, int(max_tokens_value))
        except (TypeError, ValueError):
            max_tokens_int = 4096
        body = {
            "model": resolved["model"],
            "max_tokens": max_tokens_int,
            "temperature": float(self._get_llm_value(purpose, "temperature", "0.95")) if temp is None else temp,
        }
        # 采样参数（top_p / 重复惩罚）：仅真实聊天回复链路显式开启。
        # 聊天默认带 top_p（核采样砍掉低概率胡话尾巴）+ frequency_penalty（抗车轱辘复读），
        # 摆脱「温度调高说胡话 / 调低复读」的两难；checkpoint/dream/memory 等结构化低温任务不带。
        if sampling:
            for _sample_key in ("top_p", "frequency_penalty", "presence_penalty"):
                _sample_raw = self._get_llm_value(purpose, _sample_key, "")
                if _sample_raw in ("", None):
                    continue
                try:
                    body[_sample_key] = float(_sample_raw)
                except (TypeError, ValueError):
                    logger.warning("忽略非法采样参数 %s=%r", _sample_key, _sample_raw)
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        body["messages"] = messages
        thinking = bool(resolved.get("thinking"))
        control = str(resolved.get("thinking_control") or "model_name")
        if control == "param_always":
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        elif control == "param" and not thinking:
            body["thinking"] = {"type": "disabled"}
        elif control == "enable_thinking" and not thinking:
            body["enable_thinking"] = False
        request_url = f"{api_base}/chat/completions"
        last_error = None
        for attempt in range(2):
            async with aiohttp.ClientSession(
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=float(resolved.get("timeout") or 120)),
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as s:
                async with s.post(
                    request_url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept-Encoding": "gzip, deflate"},
                    json=body,
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        self._record_llm_debug(
                            purpose=purpose,
                            tag=tag,
                            session_id=session_id,
                            resolved=resolved,
                            request_url=request_url,
                            request_body=body,
                            response={"status": resp.status, "text": text},
                            status=resp.status,
                            error=f"LLM request failed: {resp.status}",
                        )
                        self._record_llm_error_log(
                            session_id=session_id,
                            purpose=purpose,
                            tag=tag,
                            request_url=request_url,
                            request_body=body,
                            response={"status": resp.status, "text": text},
                            status=resp.status,
                            error=f"LLM request failed: {resp.status}",
                        )
                        last_error = RuntimeError(f"LLM request failed: {resp.status} {text}")
                        if resp.status == 500 and attempt == 0:
                            logger.warning("LLM request failed with 500, retrying in 1 second...")
                            await asyncio.sleep(1)
                            continue
                        raise last_error
                    data = await resp.json()
                    break
        else:
            raise last_error
        # 记录 token 消耗（不阻塞主链路，解析失败仅记录日志）。
        try:
            self._record_llm_usage_from_response(data, resolved, tag=tag, purpose=purpose, session_id=session_id)
        except Exception as exc:
            logger.debug("record llm usage failed: %s", exc)
        self._record_llm_debug(
            purpose=purpose,
            tag=tag,
            session_id=session_id,
            resolved=resolved,
            request_url=request_url,
            request_body=body,
            response=data,
            status=200,
        )
        return data

    async def _call_llm(self, system: str, user: str, temp: float = 0.3, tag: str = "", purpose: str = "image", disable_thinking: bool | None = None, session_id: str = "", max_tokens: int | None = None) -> str:
        anchor = _SIMPLE_LLM_CACHE_ANCHORS.get(tag or "")
        messages = []
        if anchor:
            messages.append({"role": "system", "content": anchor})
        messages.extend([{"role": "system", "content": system}, {"role": "user", "content": user}])
        data = await self._call_llm_messages(messages, tag=tag, temp=temp, purpose=purpose, disable_thinking=disable_thinking, session_id=session_id, max_tokens=max_tokens)
        msg = data.get("choices", [{}])[0].get("message", {})
        text = (msg.get("content") or "").strip()
        if not text:
            text = (msg.get("reasoning_content") or "").strip()
        text = re.sub(r"^\s*<thinking>.*?</thinking>\s*", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^\s*<reasoning>.*?</reasoning>\s*", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^\s*<analysis>.*?</analysis>\s*", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
        if not text:
            self._record_llm_error_log(
                session_id=session_id,
                purpose=purpose,
                tag=tag,
                request_body={"messages": messages},
                response=data,
                status=200,
                error="LLM returned empty content",
            )
            raise RuntimeError("LLM 返回空内容")
        return text

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

    async def _translate_to_tags(
        self,
        natural: str,
        session_id: str = "",
        view: str = "",
        is_intimate: bool = False,
        free_composition: bool = False,
    ) -> str:
        if not self.has_llm_config("image"):
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
                "Stable appearance is injected later; do not invent or restate stable hair, eye, body, species, or accessory traits unless the source explicitly asks for a one-shot change. "
                f"{weather_guard}"
                f"{light_guard}"
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
                "Stable appearance is injected later; do not invent or restate stable hair, eye, body, species, or accessory traits unless the source explicitly asks for a one-shot change. "
                f"{weather_guard}"
                f"{light_guard}"
                "自然语言句子尽量不要使用逗号；重点保留动作、表情、姿态、服装、环境光线、空间关系和氛围。"
                "避免复杂手势和多手互动；除非原文强制要求，尽量不强调手部。"
                "输出格式: English visual sentence. key tag, key tag, key tag"
            )
        else:
            system = (
                "Visual subject rule: the image subject is the character, not the user. "
                "For default or original characters, do not turn role names into English names or visual tags; describe appearance and action instead. "
                "Only keep a character name when it is paired with its published series. "
                "Stable appearance is injected later; do not invent or restate stable hair, eye, body, species, or accessory traits unless the source explicitly asks for a one-shot change. "
                f"{weather_guard}"
                f"{light_guard}"
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
                "Do not turn the user into a second full character."
            )
        text = await self._call_llm(system, f"请翻译: {natural}", temp=float(self._get_llm_value("image", "temperature_translate", "0.3")), tag="translate", purpose="image")
        natural_text = str(natural or "").strip()
        body = text.strip().strip(",")
        if opener:
            detail = body or natural_text
            return opener if not detail else f"{opener}, {detail}"
        return body or natural_text

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
            "字段固定为: name, source_type, series, original_name, visual_character, visual_series, role, age, occupation, anchor, persona, user_address, base_appearance, dynamic_appearance, relationship, city, style, scene_preference, selfie_preference, unclassified。"
            "name 是本地角色卡主键，保留用户给的称呼。source_type 只允许 original/existing/空。"
            "如果是原创角色，source_type 写 original，series/original_name/visual_character/visual_series 留空，除非用户明确给了可用标签。"
            "如果是现有作品角色，source_type 写 existing。original_name 写英文官方名或罗马音，姓氏在前，不要写中文、日文假名或汉字。"
            "series 写英文官方作品名或英文罗马音，不要写中文、日文假名或汉字。"
            "visual_character 和 visual_series 写 Danbooru 风格标签：小写英文/罗马音、下划线分词、必要时用括号消歧义，例如 tendou_aris、aris_(blue_archive)、blue_archive；不要输出中文、日文假名或汉字。"
            "occupation 放角色的中文职业/身份原文（如 高中生/上班族/护士）；anchor 从职业推断白天去向枚举。"
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
    ) -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate(
            self, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
            is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
            clothing_off=clothing_off, orientation=orientation,
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
    ) -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate_locked(
            self, scene_desc, is_ntr, session_id, one_shot_appearance=one_shot_appearance,
            is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
            clothing_off=clothing_off, orientation=orientation,
        )

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
        state = self._get_session_state(session_id)
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
        ok, imgs, err = await self._do_generate(
            english, session_id=session_id, one_shot_appearance=new_app or "",
            is_intimate=is_intimate, partner_in_frame=partner_in_frame, device_in_frame=device_in_frame,
            clothing_off=clothing_off, orientation=orientation,
        )
        if not ok or not imgs:
            self._ulog(session_id, "ERROR", f"工具生图失败: {err}")
            return f"生图失败: {err}"
        # 聊天途中的配图不带配文：聊天模型已经在文字回复里说话了，再加配文会重复。
        await self.send_photo(chat_id, imgs[0], "")
        self._persist_removed_accessories_from_image(
            session_id,
            clothing_off,
            intent,
            prompt,
            scene,
        )
        self._record_sent_photo(
            session_id,
            scene,
            "",
            appearance=new_app or session_schema.get_outfit(state),
            view=final_view,
            source_description=source_description,
            defer_history_message=defer_photo_history,
        )
        return f"图片已生成并发送。画面: {scene}"

    def _get_wardrobe(self, state: dict) -> dict:
        """取当前衣柜。衣柜与扁平 dynamic_appearance 不一致时（老数据无衣柜、或 webui 直接改了扁平串）
        以扁平串为准重新分槽——保证两者始终同步。"""
        wardrobe = session_schema.get_wardrobe(state)
        dyn = session_schema.get_outfit(state).strip()
        if not dyn:
            return {}
        if appearance_rules.render_wardrobe(wardrobe) != appearance_rules.normalize_appearance_text(dyn):
            wardrobe = appearance_rules.seed_wardrobe_from_text(dyn, self._outfit_kw, self._accessory_kw)
        return wardrobe

    def _wardrobe_closet_context(self, session_id: str) -> str:
        """给聊天模型看的衣橱清单（按槽位的中文名），让角色知道自己有哪些衣服。"""
        state = self._get_session_state(session_id)
        return appearance_rules.closet_summary(session_schema.get_closet(state))

    async def _classify_wardrobe_change(self, description: str, current_summary: str = "", closet_brief: str = "") -> dict:
        """大模型把一次换装描述拆解到固定衣柜槽位（含穿/脱/换意图），返回结构化 JSON。
        若用户点名衣橱里已有的衣服，用其英文标签填对应槽位；并给新穿上的衣物起个简短中文名（names）。"""
        system = (
            "你是角色换装分类器。把用户或角色描述的外观变化拆解到固定衣柜槽位，"
            "每个涉及的槽位填英文 danbooru 标签（可多件用逗号），不涉及的槽位留空。只输出 JSON，不要解释。\n"
            "服装槽位: dress, top, bottom, outerwear, bra, panties, legwear, footwear；外观槽位: hair(临时发型/发色), eyes(瞳色), other(其它视觉补充)。规则:\n"
            "- 连衣裙类（连衣裙/旗袍/和服/泳衣连体/jumpsuit/bodysuit）填 dress，系统会自动覆盖 top+bottom，不要再填 top/bottom。\n"
            "- 上半身衣物→top；下半身（裤/裙/短裤）→bottom；外套/夹克/大衣/开衫→outerwear；胸罩→bra；内裤→panties；袜/丝袜/连裤袜→legwear；鞋→footwear。\n"
            "- 眼镜/项链/耳环/手套/帽子/choker 等配饰：要戴上的填 accessory_add，要摘掉的填 accessory_remove。\n"
            "- 脱掉/不穿某一层（如脱外套、光脚、摘掉发饰）：把该槽位名放进 remove 列表。\n"
            "- 想整套换掉/全裸从头来：reset_all=true。\n"
            "- 若用户/剧情点名【衣橱里已有的衣服】（见下方清单），直接用清单里的英文标签填进对应槽位。\n"
            "- names：给本次新穿上的每个服装槽位起个简短中文名（如 dress→\"碎花连衣裙\"），用于衣橱收藏；没新衣物则留空。\n"
            "严格 JSON: {\"dress\":\"\",\"top\":\"\",\"bottom\":\"\",\"outerwear\":\"\",\"bra\":\"\",\"panties\":\"\",\"legwear\":\"\",\"footwear\":\"\",\"hair\":\"\",\"eyes\":\"\",\"other\":\"\",\"accessory_add\":\"\",\"accessory_remove\":\"\",\"remove\":[],\"reset_all\":false,\"names\":{}}"
        )
        user = (
            f"当前衣柜（穿在身上）:\n{current_summary or '（空）'}\n\n"
            f"衣橱收藏（已有的衣服，可点名复穿）:\n{closet_brief or '（空）'}\n\n"
            f"要应用的外观变化: {description}"
        )
        text = await self._call_llm(system, user, temp=0.2, tag="wardrobe-classify", purpose="image", disable_thinking=True)
        parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
        if not isinstance(parsed, dict):
            raise ValueError("wardrobe classify did not return an object")
        return parsed

    async def _wardrobe_apply_to_state(self, state: dict, description: str, *, replace: bool = False, session_id: str = "") -> str:
        """把一次换装应用到 state（改 wardrobe + dynamic_appearance），不落盘——由调用方保存。"""
        desc = (description or "").strip()
        if desc.lower() in ("reset", "none", "clear", "无", "", "重置", "恢复", "默认"):
            session_schema.set_wardrobe(state, {})
            session_schema.set_outfit(state, "")
            if session_id:
                self._ulog(session_id, "WARDROBE", f'desc="{desc[:80]}" → reset 清空全部穿搭')
            return ""
        wardrobe = {} if replace else self._get_wardrobe(state)
        closet = session_schema.get_closet(state)
        change: dict = {}
        try:
            change = await self._classify_wardrobe_change(
                desc, appearance_rules.wardrobe_summary(wardrobe), appearance_rules.closet_brief_for_llm(closet)
            )
            wardrobe = appearance_rules.apply_wardrobe_change(wardrobe, change)
            # 守卫：reset_all 但没穿任何新衣服 → 这是"脱光"，不应清空衣柜
            if change.get("reset_all") and not any(
                str(change.get(s) or "").strip()
                for s in appearance_rules.WARDROBE_CLOTHING_SLOTS
            ):
                if session_id:
                    self._ulog(session_id, "WARDROBE", f"拦截 reset_all 无新衣: desc=\"{desc[:120]}\"")
                return ""
        except Exception as exc:
            logger.warning("wardrobe classify failed, fallback to keyword slotting: %s", exc)
            if re.search(r"[a-zA-Z]{3,}", desc) and not _HAS_CJK(desc):
                tags = desc
            else:
                tags = await self._translate_appearance_tags(desc)
            seed = appearance_rules.seed_wardrobe_from_text(tags, self._outfit_kw, self._accessory_kw)
            change = {slot: val for slot, val in seed.items()}
            wardrobe = appearance_rules.apply_wardrobe_seed(wardrobe, seed)
        # 自动收藏：仅把【本次新穿上】的服装存进衣橱（用应用后的标签，含点名复穿时解析出的标签）。
        names = change.get("names") if isinstance(change.get("names"), dict) else {}
        now = time.time()
        for slot in appearance_rules.WARDROBE_CLOTHING_SLOTS:
            if not str(change.get(slot) or "").strip():
                continue  # 本次没设这个槽位 → 不动衣橱
            tags = (wardrobe.get(slot) or "").strip()
            if tags:
                closet = appearance_rules.closet_add(closet, names.get(slot, ""), slot, tags, now=now)
        session_schema.set_closet(state, closet)
        session_schema.set_wardrobe(state, wardrobe)
        rendered = appearance_rules.render_wardrobe(wardrobe)
        session_schema.set_outfit(state, rendered)
        # 她重新穿上了衣服 → 解除持久裸体态（换装是"穿回衣服"的明确叙事事件）。
        if rendered.strip():
            session_schema.clear_nudity(state)
        if session_id:
            slots = {k: v for k, v in change.items() if k != "names" and v not in ("", [], False, None)}
            self._ulog(session_id, "WARDROBE", f'desc="{desc[:80]}" replace={replace} → 分槽={slots} | 结果="{rendered[:140]}"')
        return rendered

    async def _apply_wardrobe(self, session_id: str, description: str, *, replace: bool = False) -> str:
        """换装统一入口：分槽（LLM 主判，关键词兜底）→ 应用规则 → 渲染回 dynamic_appearance 并持久化。"""
        state = self._get_session_state(session_id)
        rendered = await self._wardrobe_apply_to_state(state, description, replace=replace, session_id=session_id)
        self._save_session_state(session_id, state)
        return rendered or "（已清空）"

    _TEMPORARY_NUDITY_RE = re.compile(
        r"\b(?:脱[光精]|全裸|裸体|一[丝条]不[挂卦]|脱[得掉][精光一].*|"
        r"nude|naked|strip(?:\s+naked)?|get\s+naked|take\s+off\s+(?:everything|all|clothes)|"
        r"completely\s+(?:nude|naked|undressed)|stark\s+naked|nothing\s+on|no\s+clothes)\b",
        re.IGNORECASE,
    )
    _PUT_ON_RE = re.compile(
        r"\b(?:换[上穿]|穿[上回]|put\s+on|wear|change\s+(?:into|to)|换上)", re.IGNORECASE
    )

    async def tool_change_appearance(self, session_id: str, description: str = "", mode: str = "merge") -> str:
        allow = self._allow_llm_change_appearance(session_id)
        desc = (description or "").strip()
        self._ulog(session_id, "WARDROBE", f'模型调用 change_appearance allow={"on" if allow else "off"} mode={mode} desc="{desc[:100]}"')
        if not allow:
            return "当前会话已关闭模型自主修改外型，dynamic_appearance 未改变。"
        if self._TEMPORARY_NUDITY_RE.search(desc) and not self._PUT_ON_RE.search(desc):
            self._ulog(session_id, "WARDROBE", f'拦截临时裸体 change_appearance: "{desc[:120]}"')
            return "临时脱衣/裸体不需要调用 change_appearance——配图系统会自动处理，场景结束后角色会自动恢复原着装。只有换上不同衣服时才调用。"
        result = await self._apply_wardrobe(session_id, description, replace=(mode == "replace"))
        return f"外貌已改变: {result}"

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
