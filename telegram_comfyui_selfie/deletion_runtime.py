from __future__ import annotations

import asyncio
import copy
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from . import session_schema
from .character_artifacts import avatar_file_path, avatar_session_dir


logger = logging.getLogger(__name__)


class DeletionNotFoundError(LookupError):
    """目标会话或角色不存在。"""


class DeletionForbiddenError(PermissionError):
    """目标是不可删除的系统内置对象。"""


class DeletionBusyError(RuntimeError):
    """目标作用域仍有无法安全停稳的任务。"""


class DeletionRuntimeMixin:
    """统一角色/会话删除事务、文件隔离与运行时缓存清理。"""

    def _init_deletion_runtime(self) -> None:
        self._deleting_sessions: set[str] = set()
        self._deleting_characters: set[tuple[str, str]] = set()
        self._deletion_locks: dict[str, asyncio.Lock] = {}

    def _deletion_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._deletion_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._deletion_locks[session_id] = lock
        return lock

    def _session_deletion_in_progress(self, session_id: str) -> bool:
        return bool(session_id and session_id in self._deleting_sessions)

    def _character_deletion_in_progress(self, session_id: str, character_key: str) -> bool:
        return (session_id, str(character_key or "")) in self._deleting_characters

    @staticmethod
    def _stage_delete_artifacts(paths: list[Path]) -> list[tuple[Path, Path]]:
        """把文件/目录改名到同卷隐藏路径；失败时恢复此前已移动项。"""
        staged: list[tuple[Path, Path]] = []
        seen: set[Path] = set()
        operation_id = uuid.uuid4().hex
        try:
            for raw_path in paths:
                path = Path(raw_path)
                try:
                    resolved = path.resolve()
                except OSError:
                    resolved = path.absolute()
                if resolved in seen or not path.exists():
                    continue
                seen.add(resolved)
                staged_path = path.with_name(f".{path.name}.delete-pending-{operation_id}")
                path.replace(staged_path)
                staged.append((path, staged_path))
        except Exception:
            DeletionRuntimeMixin._restore_staged_artifacts(staged)
            raise
        return staged

    @staticmethod
    def _restore_staged_artifacts(staged: list[tuple[Path, Path]]) -> None:
        errors: list[Exception] = []
        for original, hidden in reversed(staged):
            try:
                if hidden.exists() and not original.exists():
                    hidden.replace(original)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("删除回滚时有文件无法恢复") from errors[0]

    @staticmethod
    def _purge_staged_artifacts(staged: list[tuple[Path, Path]]) -> None:
        for _original, hidden in staged:
            try:
                if hidden.is_dir():
                    shutil.rmtree(hidden)
                else:
                    hidden.unlink(missing_ok=True)
            except Exception:
                # DB 已提交后不能复活业务数据；隐藏残留可由运维稍后清理。
                logger.warning("无法清理删除隔离文件：%s", hidden, exc_info=True)

    def _character_artifact_paths(
        self,
        session_id: str,
        character_id: str,
        character_key: str,
    ) -> list[Path]:
        paths = [avatar_file_path(self, session_id, character_id)]
        if hasattr(self, "_character_checkpoint_dir"):
            paths.append(self._character_checkpoint_dir(session_id, character_key))
        return paths

    def _session_artifact_paths(self, session_id: str) -> list[Path]:
        paths: list[Path] = [avatar_session_dir(self, session_id)]
        if hasattr(self, "_character_checkpoint_root") and hasattr(self, "_safe_checkpoint_part"):
            paths.append(
                self._character_checkpoint_root()
                / self._safe_checkpoint_part(session_id, "session")
            )
        if hasattr(self, "_user_log_path") and hasattr(self, "_log_all_paths"):
            paths.extend(self._log_all_paths(self._user_log_path(session_id)))
        return paths

    @staticmethod
    def _perform_delete_saga(
        paths: list[Path],
        database_delete: Callable[[], dict[str, int]],
    ) -> dict[str, int]:
        staged = DeletionRuntimeMixin._stage_delete_artifacts(paths)
        try:
            deleted = database_delete()
        except Exception:
            DeletionRuntimeMixin._restore_staged_artifacts(staged)
            raise
        DeletionRuntimeMixin._purge_staged_artifacts(staged)
        return deleted

    async def _run_delete_saga(
        self,
        paths: list[Path],
        database_delete: Callable[[], dict[str, int]],
    ) -> dict[str, int]:
        """shield 完整文件+DB saga，避免 to_thread 已提交后外层取消误回滚。"""
        task = asyncio.create_task(
            asyncio.to_thread(self._perform_delete_saga, paths, database_delete),
            name="delete-saga",
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    async def _quiesce_delete_scope(
        self,
        session_id: str,
        character_key: str | None,
        *,
        timeout: float,
    ) -> None:
        if hasattr(self, "_cancel_background_scope"):
            stopped = await self._cancel_background_scope(
                session_id,
                character_key,
                timeout=timeout,
            )
            if not stopped:
                raise DeletionBusyError("目标仍有后台任务未在时限内停止")
        if character_key is None:
            await self._quiesce_telegram_updates_for_delete(
                session_id,
                drop_queued=True,
            )

    async def _quiesce_telegram_updates_for_delete(
        self,
        session_id: str,
        *,
        drop_queued: bool,
    ) -> None:
        """会话删除时停住既有 Telegram update；当前删除命令自身不会取消。"""
        chat_key = str(self.chat_id_from_session(session_id))
        current = asyncio.current_task()
        active = getattr(self, "_telegram_active_update_tasks", {}).get(chat_key)
        if active is not None and active is not current and not active.done():
            task_name = active.get_name()
            if task_name.startswith("telegram-update:"):
                try:
                    getattr(self, "_telegram_queued_update_ids", set()).discard(
                        int(task_name.removeprefix("telegram-update:"))
                    )
                except ValueError:
                    pass
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if not drop_queued:
            return
        queue = getattr(self, "_telegram_update_queues", {}).get(chat_key)
        queued_ids = getattr(self, "_telegram_queued_update_ids", set())
        if isinstance(queue, asyncio.Queue):
            while True:
                try:
                    update_id, _update, _attempts, _available_at = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queued_ids.discard(int(update_id))
                queue.task_done()
        worker = getattr(self, "_telegram_update_workers", {}).get(chat_key)
        if (
            worker is not None
            and worker is not current
            and active is not current
            and not worker.done()
        ):
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def _prepare_character_delete_state(
        self,
        session_id: str,
        character_id: str,
    ) -> tuple[dict[str, Any], str, bool]:
        live_state = self.sessions.get(session_id)
        if not isinstance(live_state, dict):
            live_state = self.app_store.load_session_state(session_id)
        if not isinstance(live_state, dict):
            raise DeletionNotFoundError("会话不存在")
        state = copy.deepcopy(live_state)
        if hasattr(self, "_snapshot_character"):
            self._snapshot_character(state)
        saved = session_schema.get_saved_characters(state)
        existing = saved.get(character_id)
        default_id = str(self._default_character_payload().get("id") or "").strip()
        if character_id == default_id and (
            not isinstance(existing, dict) or existing.get("is_default") is True
        ):
            raise DeletionForbiddenError("系统默认角色不能删除")
        active_id = str(
            session_schema.get_character_value(state, "custom_character", "") or ""
        ).strip()
        if not isinstance(existing, dict) and active_id != character_id:
            raise DeletionNotFoundError("角色不存在")

        # 角色 key 必须在 pop 前冻结；与系统默认同名的自定义卡仍使用具名空间。
        character_key = str(character_id or "").strip()
        is_active = active_id == character_id
        saved.pop(character_id, None)
        session_schema.get_character_contexts(state).pop(character_key, None)
        if is_active:
            if hasattr(self, "_clear_transient_state"):
                self._clear_transient_state(state, keep_appearance=False)
            if hasattr(self, "_apply_selected_character_payload"):
                self._apply_selected_character_payload(
                    state,
                    self._default_character_payload(),
                )
            state.pop("life_profile", None)
        return state, character_key, is_active

    def _evict_character_runtime(
        self,
        session_id: str,
        character_key: str,
        *,
        was_active: bool,
    ) -> None:
        scope = f"{session_id}\n{character_key}"
        for name in ("_checkpoint_tasks", "_checkpoint_locks", "_dream_tasks"):
            bucket = getattr(self, name, None)
            if isinstance(bucket, dict):
                bucket.pop(scope, None)
        if was_active:
            for name in (
                "_last_prompt_slots_by_session",
                "_last_generated_nltag_by_session",
                "_semistable_context_signatures",
                "_chat_world_conditions_cache",
                "_world_conditions_context_signatures",
                "_dynamic_context_signatures",
            ):
                bucket = getattr(self, name, None)
                if isinstance(bucket, dict):
                    bucket.pop(session_id, None)
        for name in ("_background_generations", "_background_retry_state"):
            bucket = getattr(self, name, None)
            if isinstance(bucket, dict):
                for key in [
                    key
                    for key in bucket
                    if len(key) >= 3
                    and key[1] == session_id
                    and key[2] == character_key
                ]:
                    bucket.pop(key, None)

    def _evict_session_runtime(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        dirty = getattr(self, "_dirty_sessions", None)
        if isinstance(dirty, set):
            dirty.discard(session_id)
        active_pushes = getattr(self, "_active_pushes", None)
        if isinstance(active_pushes, set):
            active_pushes.discard(session_id)
        for name in (
            "_push_locks",
            "_weather_caches",
            "_interruptible_tasks",
            "_life_plan_tasks",
            "_post_chat_push_tasks",
            "_pending_photo_inputs",
            "_pending_photo_history_messages",
            "_pending_wardrobe_history_messages",
            "_last_prompt_slots_by_session",
            "_last_generated_nltag_by_session",
            "_semistable_context_signatures",
            "_chat_world_conditions_cache",
            "_world_conditions_context_signatures",
            "_dynamic_context_signatures",
        ):
            bucket = getattr(self, name, None)
            if isinstance(bucket, dict):
                bucket.pop(session_id, None)
        chat_key = str(self.chat_id_from_session(session_id))
        for name in (
            "_telegram_update_queues",
            "_telegram_update_workers",
            "_telegram_active_update_tasks",
        ):
            bucket = getattr(self, name, None)
            if isinstance(bucket, dict):
                bucket.pop(chat_key, None)
        for name in ("_checkpoint_tasks", "_checkpoint_locks", "_dream_tasks"):
            bucket = getattr(self, name, None)
            if isinstance(bucket, dict):
                for key in [key for key in bucket if key == session_id or key.startswith(session_id + "\n")]:
                    bucket.pop(key, None)
        media_groups = getattr(self, "_pending_media_group_inputs", None)
        if isinstance(media_groups, dict):
            for key in [key for key in media_groups if key.startswith(session_id + "\n")]:
                media_groups.pop(key, None)
        debug_buffer = getattr(self, "_llm_debug_buffer", None)
        if isinstance(debug_buffer, list):
            debug_buffer[:] = [
                item
                for item in debug_buffer
                if not isinstance(item, dict) or item.get("session_id") != session_id
            ]
        for name in ("_background_generations", "_background_retry_state"):
            bucket = getattr(self, name, None)
            if isinstance(bucket, dict):
                for key in [key for key in bucket if len(key) >= 2 and key[1] == session_id]:
                    bucket.pop(key, None)

    def set_session_hidden(self, session_id: str, hidden: bool) -> dict[str, Any]:
        """只调整 Web 列表可见性，不删除任何会话业务数据。"""
        state = self.sessions.get(session_id)
        if not isinstance(state, dict):
            state = self.app_store.load_session_state(session_id)
        if not isinstance(state, dict):
            raise DeletionNotFoundError("会话不存在")
        next_state = copy.deepcopy(state)
        session_schema.set_web_hidden(next_state, hidden)
        self.sessions[session_id] = next_state
        self._save_session_state(session_id, next_state)
        return next_state

    async def delete_character(
        self,
        session_id: str,
        character_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        character_id = str(character_id or "").strip()
        if not character_id:
            raise DeletionNotFoundError("角色不存在")
        async with self._deletion_lock(session_id):
            next_state, character_key, was_active = self._prepare_character_delete_state(
                session_id,
                character_id,
            )
            scope = (session_id, character_key)
            self._deleting_characters.add(scope)
            try:
                await self._quiesce_delete_scope(
                    session_id,
                    character_key,
                    timeout=timeout,
                )
                paths = self._character_artifact_paths(
                    session_id,
                    character_id,
                    character_key,
                )
                deleted = await self._run_delete_saga(
                    paths,
                    lambda: self.app_store.delete_character_bundle(
                        session_id,
                        character_key,
                        next_state,
                    ),
                )
                self.sessions[session_id] = next_state
                dirty = getattr(self, "_dirty_sessions", None)
                if isinstance(dirty, set):
                    dirty.discard(session_id)
                self._evict_character_runtime(
                    session_id,
                    character_key,
                    was_active=was_active,
                )
                return {
                    "session_id": session_id,
                    "character_id": character_id,
                    "character_key": character_key,
                    "active_id": str(
                        session_schema.get_character_value(
                            next_state,
                            "custom_character",
                            "",
                        ) or ""
                    ),
                    "characters": session_schema.get_saved_characters(next_state),
                    "deleted": deleted,
                }
            finally:
                self._deleting_characters.discard(scope)

    async def delete_session(
        self,
        session_id: str,
        *,
        purge_identity: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        async with self._deletion_lock(session_id):
            state = self.sessions.get(session_id)
            if not isinstance(state, dict):
                state = self.app_store.load_session_state(session_id)
            if not isinstance(state, dict):
                raise DeletionNotFoundError("会话不存在")
            self._deleting_sessions.add(session_id)
            try:
                await self._quiesce_delete_scope(
                    session_id,
                    None,
                    timeout=timeout,
                )
                deleted = await self._run_delete_saga(
                    self._session_artifact_paths(session_id),
                    lambda: self.app_store.delete_session_bundle(
                        session_id,
                        purge_identity=purge_identity,
                    ),
                )
                self._evict_session_runtime(session_id)
                return {
                    "session_id": session_id,
                    "purge_identity": bool(purge_identity),
                    "deleted": deleted,
                }
            finally:
                self._deleting_sessions.discard(session_id)
