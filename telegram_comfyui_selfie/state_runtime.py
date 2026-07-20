from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import character_card
from . import session_schema
from .config_store import dump_simple_yaml, flatten_config, load_simple_yaml
from .defaults import DEFAULT_CONFIG


logger = logging.getLogger(__name__)

LEGACY_STATE_IMPORT_MARKER = "legacy_state_json_import_completed"
_CONFIG_LOCK_INIT_GUARD = threading.Lock()


class ServiceStateMixin:
    """配置、持久状态、会话访问与本地活动日志基础运行时。"""

    # ---------------------------------------------------------------------
    # Config / state
    # ---------------------------------------------------------------------
    def _config_file_lock(self) -> threading.RLock:
        lock = getattr(self, "_config_file_thread_lock", None)
        if lock is not None:
            return lock
        with _CONFIG_LOCK_INIT_GUARD:
            lock = getattr(self, "_config_file_thread_lock", None)
            if lock is None:
                lock = threading.RLock()
                self._config_file_thread_lock = lock
        return lock

    def config_update_lock(self) -> asyncio.Lock:
        """Web 配置事务锁；等待锁不会阻塞事件循环。"""
        lock = getattr(self, "_config_update_async_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._config_update_async_lock = lock
        return lock

    @staticmethod
    def _validate_serializable_config_numbers(value: Any, path: str = "config") -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} 包含非有限数值")
        if isinstance(value, dict):
            for key, child in value.items():
                ServiceStateMixin._validate_serializable_config_numbers(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                ServiceStateMixin._validate_serializable_config_numbers(child, f"{path}[{index}]")

    def _serialize_config(self, config: dict[str, Any]) -> str:
        self._validate_serializable_config_numbers(config)
        if self.config_path.suffix.lower() in (".yml", ".yaml"):
            return dump_simple_yaml(config)
        return json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False)

    def _write_config_atomic(self, config: dict[str, Any]) -> None:
        """在目标文件同目录完成 flush/fsync 后原子替换配置文件。"""
        content = self._serialize_config(config)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.",
            suffix=".tmp",
            dir=str(self.config_path.parent),
        )
        temp_path = Path(temp_name)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            fd = -1
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.config_path)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _set_runtime_config(self, config: dict[str, Any]) -> None:
        self.config = config
        for attr in ("_cached_outfit_kw", "_cached_accessory_kw"):
            if hasattr(self, attr):
                delattr(self, attr)

    def _load_config(self) -> dict[str, Any]:
        with self._config_file_lock():
            cfg = copy.deepcopy(DEFAULT_CONFIG)
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
                self._write_config_atomic(cfg)
                logger.warning("配置文件不存在，已写入默认配置: %s", self.config_path)
            self._persisted_config_snapshot = copy.deepcopy(cfg)
            return cfg

    def save_config(self):
        """原子保存当前配置；失败时恢复最近一次成功落盘的运行态快照。"""
        with self._config_file_lock():
            previous = copy.deepcopy(getattr(self, "_persisted_config_snapshot", self.config))
            try:
                candidate = copy.deepcopy(self.config)
                persisted = copy.deepcopy(candidate)
                self._write_config_atomic(candidate)
            except Exception:
                self._set_runtime_config(previous)
                raise
            self._set_runtime_config(candidate)
            self._persisted_config_snapshot = persisted

    def replace_config_and_save(self, config: dict[str, Any]) -> None:
        """候选配置成功原子落盘后，再一次性替换运行态。"""
        with self._config_file_lock():
            candidate = copy.deepcopy(config)
            persisted = copy.deepcopy(candidate)
            self._write_config_atomic(candidate)
            self._set_runtime_config(candidate)
            self._persisted_config_snapshot = persisted

    def reload_config_from_disk(self) -> dict[str, Any]:
        """从当前配置文件重新载入运行态配置，不写回磁盘。"""
        with self._config_file_lock():
            self._set_runtime_config(self._load_config())
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
            legacy_import_done = self.app_store.get_metadata(
                LEGACY_STATE_IMPORT_MARKER,
                "",
            ) == "1"
            if self.app_store.has_session_states():
                self.sessions = self.app_store.load_all_session_states()
                logger.info("Loaded %d sessions from SQLite", len(self.sessions))
                if not legacy_import_done:
                    self.app_store.set_metadata(LEGACY_STATE_IMPORT_MARKER, "1")
            elif not legacy_import_done:
                if self.state_path.exists():
                    self._migrate_from_state_json()
                else:
                    # 首次启动时没有旧文件；之后即使生成同名文件也不能被当作待迁移源复活。
                    self.app_store.set_metadata(LEGACY_STATE_IMPORT_MARKER, "1")
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

    def _migrate_from_state_json(self) -> bool:
        """从旧版 state.json 迁移数据到 SQLite。"""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            if not raw.strip():
                self.app_store.set_metadata(LEGACY_STATE_IMPORT_MARKER, "1")
                return True
            data = json.loads(raw)
            sessions = data.get("sessions", {})
            catalogs = data.get("city_place_catalogs", {})
            sessions = sessions if isinstance(sessions, dict) else {}
            catalogs = catalogs if isinstance(catalogs, dict) else {}
            if (isinstance(sessions, dict) and sessions) or (isinstance(catalogs, dict) and catalogs):
                self._backup_file(self.state_path, "state-json-migration")
            self.app_store.import_legacy_state_bundle(
                sessions,
                catalogs,
                marker_key=LEGACY_STATE_IMPORT_MARKER,
            )
            self.sessions = {
                str(sid): state
                for sid, state in sessions.items()
                if isinstance(state, dict)
            }
            self.city_place_catalogs = {
                str(key): catalog
                for key, catalog in catalogs.items()
                if isinstance(catalog, dict)
            }
            logger.info("Migrated %d sessions from %s to SQLite", len(self.sessions), self.state_path)
            return True
        except Exception as exc:
            logger.warning("state.json 迁移失败: %s", exc)
            return False

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
            if getattr(self, "_session_deletion_in_progress", lambda _sid: False)(session_id):
                continue
            state = self.sessions.get(session_id)
            if state is not None:
                self.app_store.save_session_state(session_id, state)
        self._dirty_sessions.clear()
        self._last_state_write = time.time()

    def _mark_dirty(self, session_id: str):
        if session_id and not getattr(
            self,
            "_session_deletion_in_progress",
            lambda _sid: False,
        )(session_id):
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
        if getattr(self, "_session_deletion_in_progress", lambda _sid: False)(session_id):
            logger.warning("忽略删除事务期间的会话状态写回: %s", session_id)
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
            "workday_wake_time": str(cfg.get("workday_wake_time", "08:00") or "").strip(),
            "workday_sleep_time": str(cfg.get("workday_sleep_time", "23:50") or "").strip(),
            "weekend_wake_time": str(cfg.get("weekend_wake_time", "08:00") or "").strip(),
            "weekend_sleep_time": str(cfg.get("weekend_sleep_time", "23:50") or "").strip(),
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

    def character_operation_lock(self, session_id: str) -> asyncio.Lock:
        """会话级角色操作锁：WebUI 头像生成/手动推送/角色激活等临时切角色操作，
        与 Telegram 消息处理、定时推送互斥，避免窗口期内按错误角色处理并落库。"""
        locks = getattr(self, "_character_op_locks", None)
        if not isinstance(locks, dict):
            locks = {}
            self._character_op_locks = locks
        lock = locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[session_id] = lock
        return lock

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
