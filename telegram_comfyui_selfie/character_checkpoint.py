from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import session_schema

logger = logging.getLogger(__name__)

CHARACTER_CHECKPOINT_SCHEMA = "sucyubot.character_checkpoint.v1"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CharacterCheckpointMixin:
    """角色记忆/背景 JSON 检查点。

    检查点是文件级备份，不参与聊天 prompt 注入；导入模式分为 basic/memory/full。
    聊天记录只作为留档随 JSON 导出，默认不重放进 chat_messages，避免同一会话导入时
    制造重复 dream 输入。
    """

    def _character_checkpoint_root(self) -> Path:
        raw = str(self.config.get("character_checkpoint_dir") or "").strip()
        if raw:
            path = Path(raw)
            if not path.is_absolute():
                path = self.config_path.parent / path
            return path
        return self.config_path.parent / "character_checkpoints"

    @staticmethod
    def _safe_checkpoint_part(value: Any, fallback: str = "default") -> str:
        text = str(value or "").strip() or fallback
        base = re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("._-")[:48] or fallback
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        return f"{base}-{digest}"

    @staticmethod
    def _validate_checkpoint_date(value: Any) -> str:
        text = str(value or "").strip()
        if not _DATE_RE.match(text):
            raise ValueError("检查点日期必须是 YYYY-MM-DD")
        return text

    def _web_character_checkpoint_key(self, session_id: str, character_id: str) -> str:
        """Web 角色 id -> SQLite/记忆使用的 character_key。

        默认角色的记忆空间是空串；具名角色使用角色名本身。
        """
        state = self._get_session_state(session_id)
        default_id = str(self._default_character_payload().get("id") or "").strip()
        saved = session_schema.get_saved_characters(state)
        if character_id in ("", "__default__"):
            return ""
        if character_id == default_id and saved.get(character_id, {}).get("is_default") is True:
            return ""
        active = (session_schema.get_character_value(state, "custom_character", "") or "").strip()
        if not active and character_id == default_id:
            return ""
        return str(character_id or "").strip()

    def _character_checkpoint_dir(self, session_id: str, character_key: str) -> Path:
        char_part = self._safe_checkpoint_part(character_key or "__default__", "default")
        return self._character_checkpoint_root() / self._safe_checkpoint_part(session_id, "session") / char_part

    def _character_checkpoint_path(self, session_id: str, character_key: str, checkpoint_date: str) -> Path:
        date_text = self._validate_checkpoint_date(checkpoint_date)
        return self._character_checkpoint_dir(session_id, character_key) / f"{date_text}.json"

    def list_character_checkpoints(self, session_id: str, character_key: str) -> list[dict[str, Any]]:
        directory = self._character_checkpoint_dir(session_id, character_key)
        if not directory.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in directory.glob("*.json"):
            if not _DATE_RE.match(path.stem):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append({
                "date": path.stem,
                "filename": path.name,
                "size": stat.st_size,
                "updated_at": stat.st_mtime,
            })
        rows.sort(key=lambda item: item.get("date") or "", reverse=True)
        return rows

    def cleanup_character_checkpoints(
        self,
        session_id: str,
        character_key: str,
        *,
        keep_days: int = 7,
        reference_date: str | None = None,
    ) -> int:
        directory = self._character_checkpoint_dir(session_id, character_key)
        if not directory.exists():
            return 0
        try:
            keep_days = max(1, int(keep_days or 7))
        except (TypeError, ValueError):
            keep_days = 7
        ref = self._validate_checkpoint_date(reference_date) if reference_date else self._session_now(session_id).date().isoformat()
        cutoff = datetime.strptime(ref, "%Y-%m-%d").date() - timedelta(days=keep_days - 1)
        deleted = 0
        dated_paths: list[tuple[str, Path]] = []
        for path in directory.glob("*.json"):
            if not _DATE_RE.match(path.stem):
                continue
            dated_paths.append((path.stem, path))
        keep_dates = {date for date, _ in sorted(dated_paths, key=lambda item: item[0], reverse=True)[:keep_days]}
        for date_text, path in dated_paths:
            try:
                date_value = datetime.strptime(date_text, "%Y-%m-%d").date()
            except ValueError:
                continue
            if date_value < cutoff or date_text not in keep_dates:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    logger.warning("character checkpoint cleanup failed: %s", path, exc_info=True)
        return deleted

    def _message_local_date(self, session_id: str, message: dict[str, Any]) -> str:
        try:
            created_at = float(message.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        if created_at <= 0:
            return ""
        return datetime.fromtimestamp(created_at, self._session_tz(session_id)).date().isoformat()

    def _format_checkpoint_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []
        for msg in messages:
            item = {
                "id": int(msg.get("id") or 0),
                "role": str(msg.get("role") or ""),
                "content": str(msg.get("content") or ""),
                "created_at": float(msg.get("created_at") or 0),
                "checkpointed": int(msg.get("checkpointed") or 0),
            }
            if item["created_at"]:
                item["local_time"] = datetime.fromtimestamp(item["created_at"], self._session_tz(session_id)).isoformat()
            formatted.append(item)
        return formatted

    def _checkpoint_messages_for_date(
        self,
        session_id: str,
        character_key: str,
        checkpoint_date: str,
        *,
        before_or_equal_id: int | None = None,
    ) -> list[dict[str, Any]]:
        date_text = self._validate_checkpoint_date(checkpoint_date)
        messages = self.app_store.list_messages(
            session_id,
            character_key,
            after_id=0,
            before_or_equal_id=before_or_equal_id,
        )
        return [msg for msg in messages if self._message_local_date(session_id, msg) == date_text]

    def _checkpoint_card_for_character(self, state: dict[str, Any], character_key: str) -> dict[str, Any]:
        active_key = (session_schema.get_character_value(state, "custom_character", "") or "").strip()
        if character_key == active_key or (not character_key and not active_key):
            return self._character_export_payload(state)
        saved = session_schema.get_saved_characters(state)
        card = copy.deepcopy(saved.get(character_key) or {})
        if not card:
            card = {"character": character_key}
        return {"id": character_key or "__default__", **card}

    def _checkpoint_state_for_character(self, state: dict[str, Any], character_key: str) -> dict[str, Any]:
        active_key = (session_schema.get_character_value(state, "custom_character", "") or "").strip()
        is_active = character_key == active_key or (not character_key and not active_key)
        contexts = session_schema.get_character_contexts(state)
        saved = session_schema.get_saved_characters(state)
        frozen_context = copy.deepcopy(contexts.get(character_key or "__default__") or contexts.get(character_key) or {})
        boxes: dict[str, Any] = {}
        if is_active:
            boxes = {
                "character": copy.deepcopy(session_schema.ensure_character_box(state)),
                "clothing": copy.deepcopy(session_schema.ensure_clothing_box(state)),
                "place": copy.deepcopy(session_schema.ensure_place_box(state)),
                "context": copy.deepcopy(session_schema.ensure_context_box(state)),
            }
        return {
            "active": is_active,
            "boxes": boxes,
            "frozen_context": frozen_context,
            "saved_character": copy.deepcopy(saved.get(character_key) or {}),
            "session": {
                "daily_trigger_times": copy.deepcopy(session_schema.get_daily_trigger_times(state)),
                "daily_trigger_date": session_schema.get_daily_trigger_date(state),
                "daily_triggered_times": copy.deepcopy(session_schema.get_daily_triggered_times(state)),
                "post_chat_push_date": session_schema.get_post_chat_push_date(state),
                "post_chat_push_count": session_schema.get_post_chat_push_count(state),
                "last_post_chat_push_time": session_schema.get_last_post_chat_push_time(state),
                "frozen": session_schema.get_frozen(state),
                "frozen_at": session_schema.get_frozen_at(state),
            },
        }

    def build_character_checkpoint_payload(
        self,
        session_id: str,
        character_key: str,
        *,
        checkpoint_date: str | None = None,
        reason: str = "manual",
        to_message_id: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state = self._get_session_state(session_id)
        date_text = checkpoint_date or self._session_now(session_id).date().isoformat()
        self._validate_checkpoint_date(date_text)
        if messages is None:
            messages = self._checkpoint_messages_for_date(
                session_id,
                character_key,
                date_text,
                before_or_equal_id=to_message_id,
            )
        checkpoint = self.app_store.get_checkpoint(session_id, character_key)
        meta = self.app_store.get_context_meta(session_id, character_key)
        history_summary = (meta.get("character_history_summary") or "").strip()
        if not history_summary:
            history_summary = session_schema.get_character_history_summary(state)
        return {
            "schema": CHARACTER_CHECKPOINT_SCHEMA,
            "version": 1,
            "created_at": time.time(),
            "created_local_time": self._session_now(session_id).isoformat(),
            "checkpoint_date": date_text,
            "reason": reason,
            "session_id": session_id,
            "character_key": character_key,
            "character_card": self._checkpoint_card_for_character(state, character_key),
            "state": self._checkpoint_state_for_character(state, character_key),
            "background": {
                "sqlite_checkpoint": checkpoint,
                "character_history_summary": history_summary,
                "diaries": self.app_store.recent_diaries(session_id, character_key, limit=7),
            },
            "life_plan": self.life_plan_snapshot(session_id, character_key) if hasattr(self, "life_plan_snapshot") else None,
            "memories": self.memory.list_memories(session_id, character=character_key, limit=1000, include_inactive=False),
            "chat_messages": self._format_checkpoint_messages(session_id, messages),
        }

    def write_character_checkpoint(
        self,
        session_id: str,
        character_key: str,
        checkpoint_date: str,
        *,
        reason: str = "dream",
        to_message_id: int | None = None,
    ) -> Path:
        payload = self.build_character_checkpoint_payload(
            session_id,
            character_key,
            checkpoint_date=checkpoint_date,
            reason=reason,
            to_message_id=to_message_id,
        )
        path = self._character_checkpoint_path(session_id, character_key, checkpoint_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        self.cleanup_character_checkpoints(session_id, character_key, reference_date=checkpoint_date)
        return path

    def read_character_checkpoint(self, session_id: str, character_key: str, checkpoint_date: str) -> dict[str, Any]:
        path = self._character_checkpoint_path(session_id, character_key, checkpoint_date)
        if not path.exists():
            raise FileNotFoundError("检查点不存在")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != CHARACTER_CHECKPOINT_SCHEMA:
            raise ValueError("检查点 JSON schema 不匹配")
        return data

    def export_current_character_checkpoint(self, session_id: str, character_key: str) -> dict[str, Any]:
        today = self._session_now(session_id).date().isoformat()
        to_id = self.app_store.latest_message_id(session_id, character_key)
        return self.build_character_checkpoint_payload(
            session_id,
            character_key,
            checkpoint_date=today,
            reason="web-current",
            to_message_id=to_id,
        )

    @staticmethod
    def is_character_checkpoint_payload(payload: Any) -> bool:
        return isinstance(payload, dict) and payload.get("schema") == CHARACTER_CHECKPOINT_SCHEMA

    def _checkpoint_restore_payload(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        card = copy.deepcopy(payload.get("character_card") or {})
        if not isinstance(card, dict):
            card = {}
        key = str(payload.get("character_key") or card.get("id") or card.get("character") or card.get("bot_name") or "").strip()
        if key in ("__default__", "default"):
            key = ""
        if not key and not card:
            raise ValueError("检查点缺少角色信息")
        if key and not card.get("character"):
            card["character"] = key
        state_part = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        return key, card, state_part

    @staticmethod
    def _normalize_checkpoint_import_mode(mode: Any) -> str:
        value = str(mode or "basic").strip().lower()
        aliases = {
            "card": "basic",
            "fields": "basic",
            "base": "basic",
            "basic_fields": "basic",
            "memories": "memory",
            "long_memory": "memory",
            "full_overwrite": "full",
            "overwrite": "full",
        }
        value = aliases.get(value, value)
        if value not in {"basic", "memory", "full"}:
            raise ValueError("导入模式必须是 basic / memory / full")
        return value

    def import_character_checkpoint(self, session_id: str, payload: dict[str, Any], *, mode: str = "basic") -> dict[str, Any]:
        if not self.is_character_checkpoint_payload(payload):
            raise ValueError("不是有效的角色检查点 JSON")
        mode = self._normalize_checkpoint_import_mode(mode)
        character_key, card, state_part = self._checkpoint_restore_payload(payload)
        state = self._get_session_state(session_id)
        if mode == "full" and hasattr(self, "_save_current_character_context"):
            self._save_current_character_context(state)
        if hasattr(self, "_snapshot_character"):
            self._snapshot_character(state)

        card_for_apply = {"id": character_key or "__default__", **card}
        active_key = (session_schema.get_character_value(state, "custom_character", "") or "").strip()
        should_apply_to_current = mode == "full" or not active_key or active_key == character_key
        if should_apply_to_current:
            self._apply_character_payload(state, card_for_apply)
        if character_key and should_apply_to_current and not session_schema.get_character_value(state, "custom_character", ""):
            session_schema.set_character_value(state, "custom_character", character_key)

        if character_key:
            session_schema.get_saved_characters(state)[character_key] = {
                k: copy.deepcopy(v) for k, v in card_for_apply.items() if k != "id"
            }

        context_restored = False
        if mode == "full":
            restore_context = copy.deepcopy(state_part.get("frozen_context") or {})
            boxes = state_part.get("boxes") if isinstance(state_part.get("boxes"), dict) else {}
            if isinstance(boxes, dict):
                for box_name in ("clothing", "place", "context"):
                    if isinstance(boxes.get(box_name), dict):
                        restore_context[box_name] = copy.deepcopy(boxes[box_name])
            if restore_context:
                context_key = character_key or "__default__"
                session_schema.get_character_contexts(state)[context_key] = restore_context
                if hasattr(self, "_restore_character_context"):
                    self._restore_character_context(session_id, state)
                context_restored = True
            elif should_apply_to_current and hasattr(self, "_clear_transient_state"):
                # full 导入无冻结上下文时，清掉上一个角色残留的短期态（对话/衣柜/位置），
                # 避免从未激活过的角色带着旧角色的完整上下文继续。
                self._clear_transient_state(state, keep_appearance=False)
                if hasattr(self, "_apply_card_outfit_after_switch"):
                    self._apply_card_outfit_after_switch(state, card_for_apply, has_clothing_context=False)
            elif should_apply_to_current and hasattr(self, "_clear_transient_state"):
                # full 导入没有冻结上下文时，清掉上一个角色残留的短期态（对话/衣柜/位置），
                # 避免导入从未激活过的角色后仍带着旧角色的完整上下文；穿搭按新卡 outfit 初始化。
                self._clear_transient_state(state, keep_appearance=False)
                if hasattr(self, "_apply_card_outfit_after_switch"):
                    self._apply_card_outfit_after_switch(state, card_for_apply, has_clothing_context=False)

        background = payload.get("background") if isinstance(payload.get("background"), dict) else {}
        checkpoint = background.get("sqlite_checkpoint") if isinstance(background.get("sqlite_checkpoint"), dict) else {}
        summary = str(checkpoint.get("summary") or "").strip()
        checkpoint_replaced = False
        if mode == "full" and summary:
            try:
                source_until_id = int(checkpoint.get("source_until_id") or 0)
            except (TypeError, ValueError):
                source_until_id = 0
            self.app_store.upsert_checkpoint(session_id, character_key, summary, source_until_id)
            session_schema.set_checkpoint_summary(state, summary)
            session_schema.set_checkpoint_message_id(state, source_until_id)
            checkpoint_replaced = True

        history_summary = str(background.get("character_history_summary") or "").strip()
        if mode == "full" and history_summary:
            self.app_store.upsert_character_history_summary(session_id, character_key, history_summary)
            session_schema.set_character_history_summary(state, history_summary)

        imported_diaries = 0
        if mode in {"memory", "full"}:
            for diary in background.get("diaries") or []:
                if not isinstance(diary, dict):
                    continue
                diary_date = str(diary.get("diary_date") or "").strip()
                content = str(diary.get("content") or "").strip()
                if _DATE_RE.match(diary_date) and content:
                    self.app_store.upsert_diary(session_id, character_key, diary_date, content, from_message_id=0, to_message_id=0)
                    imported_diaries += 1

        life_plan_replaced = False
        life_plan_data = payload.get("life_plan") if isinstance(payload.get("life_plan"), dict) else None
        if mode == "full" and life_plan_data and hasattr(self, "_save_life_plan_payload"):
            life_payload = life_plan_data.get("payload") if isinstance(life_plan_data.get("payload"), dict) else life_plan_data
            if isinstance(life_payload, dict):
                self._save_life_plan_payload(session_id, character_key, life_payload)
                life_plan_replaced = True

        imported_memories = 0
        if mode in {"memory", "full"}:
            for memory in payload.get("memories") or []:
                if not isinstance(memory, dict):
                    continue
                summary_text = str(memory.get("summary") or "").strip()
                if not summary_text:
                    continue
                mid = self.memory.add_memory(
                    session_id,
                    memory.get("kind") or "event",
                    summary_text,
                    character=character_key,
                    importance=memory.get("importance", 3),
                    tags=memory.get("tags") or [],
                    source=f"checkpoint-import:{payload.get('checkpoint_date') or ''}",
                )
                if mid is not None:
                    imported_memories += 1

        self._save_session_state(session_id, state)
        return {
            "mode": mode,
            "character_key": character_key,
            "character_id": character_key or card_for_apply.get("id") or "__default__",
            "memories": imported_memories,
            "diaries": imported_diaries,
            "context_restored": context_restored,
            "checkpoint_replaced": checkpoint_replaced,
            "life_plan_replaced": life_plan_replaced,
            "chat_messages_archived": len(payload.get("chat_messages") or []),
        }
