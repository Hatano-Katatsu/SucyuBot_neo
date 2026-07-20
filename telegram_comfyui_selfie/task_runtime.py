from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Literal


logger = logging.getLogger(__name__)

BackgroundStopPolicy = Literal["cancel", "drain"]


@dataclass(frozen=True, slots=True)
class BackgroundTaskRecord:
    """统一登记的后台任务元数据。"""

    name: str
    session_id: str
    character_key: str
    scope: str
    generation: int
    stop_policy: BackgroundStopPolicy
    created_at: float


class TaskRuntimeMixin:
    """统一管理业务后台任务、作用域取消、停机排空与失败退避。"""

    def _init_task_runtime(self) -> None:
        self._background_tasks: dict[asyncio.Task[Any], BackgroundTaskRecord] = {}
        self._background_generations: dict[tuple[str, str, str], int] = {}
        self._background_retry_state: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._background_stopping = False

    @staticmethod
    def _background_key(scope: str, session_id: str = "", character_key: str = "") -> tuple[str, str, str]:
        return (str(scope or "background"), str(session_id or ""), str(character_key or ""))

    def _spawn_background(
        self,
        coro: Awaitable[Any],
        *,
        name: str,
        session_id: str = "",
        character_key: str = "",
        scope: str = "background",
        generation: int | None = None,
        drain: bool = False,
        stop_policy: BackgroundStopPolicy | None = None,
    ) -> asyncio.Task[Any]:
        """创建并登记后台任务；完成回调始终取出异常并清理 registry。"""
        if getattr(self, "_background_stopping", False):
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise RuntimeError("后台任务运行时正在停止")
        if not isinstance(getattr(self, "_background_tasks", None), dict):
            self._init_task_runtime()
        policy: BackgroundStopPolicy = stop_policy or ("drain" if drain else "cancel")
        if policy not in ("cancel", "drain"):
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise ValueError("stop_policy must be 'cancel' or 'drain'")
        key = self._background_key(scope, session_id, character_key)
        if generation is None:
            generation = int(self._background_generations.get(key, 0)) + 1
        generation = max(0, int(generation))
        self._background_generations[key] = max(int(self._background_generations.get(key, 0)), generation)
        try:
            task = asyncio.create_task(coro, name=name)
        except Exception:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise
        record = BackgroundTaskRecord(
            name=name,
            session_id=str(session_id or ""),
            character_key=str(character_key or ""),
            scope=str(scope or "background"),
            generation=generation,
            stop_policy=policy,
            created_at=time.time(),
        )
        self._background_tasks[task] = record
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        record = getattr(self, "_background_tasks", {}).pop(task, None)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("读取后台任务结果失败: %s", getattr(record, "name", task.get_name()))
            return
        if error is not None:
            logger.error(
                "后台任务异常: name=%s scope=%s session=%s character=%s generation=%s",
                getattr(record, "name", task.get_name()),
                getattr(record, "scope", "unknown"),
                getattr(record, "session_id", ""),
                getattr(record, "character_key", ""),
                getattr(record, "generation", 0),
                exc_info=(type(error), error, error.__traceback__),
            )

    @staticmethod
    def _bind_background_task_slot(
        bucket: dict[Any, asyncio.Task[Any]],
        key: Any,
        task: asyncio.Task[Any],
    ) -> asyncio.Task[Any]:
        """让兼容任务 map 只保留仍在运行且属于当前 generation 的 task。"""
        bucket[key] = task

        def clear_slot(done: asyncio.Task[Any]) -> None:
            if bucket.get(key) is done:
                bucket.pop(key, None)

        task.add_done_callback(clear_slot)
        return task

    def _find_background_task(
        self,
        *,
        scope: str,
        session_id: str = "",
        character_key: str = "",
    ) -> asyncio.Task[Any] | None:
        key = self._background_key(scope, session_id, character_key)
        matches = [
            (record.generation, task)
            for task, record in getattr(self, "_background_tasks", {}).items()
            if not task.done()
            and self._background_key(record.scope, record.session_id, record.character_key) == key
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1]

    async def _cancel_background_scope(
        self,
        session_id: str,
        character_key: str | None = None,
        *,
        timeout: float = 30.0,
        scopes: set[str] | None = None,
    ) -> bool:
        """取消并等待指定会话/角色的任务，供删除事务在清数据前调用。"""
        current = asyncio.current_task()
        selected: set[asyncio.Task[Any]] = set()
        for task, record in list(getattr(self, "_background_tasks", {}).items()):
            if task is current or task.done() or record.session_id != session_id:
                continue
            if character_key is not None and record.character_key != character_key:
                continue
            if scopes is not None and record.scope not in scopes:
                continue
            selected.add(task)
        for task in selected:
            task.cancel()
        pending: set[asyncio.Task[Any]] = set()
        if selected:
            _done, pending = await asyncio.wait(selected, timeout=max(0.0, float(timeout)))
            if pending:
                logger.warning(
                    "作用域后台任务取消超时: session=%s character=%s count=%d",
                    session_id,
                    character_key or "*",
                    len(pending),
                )
        retries = getattr(self, "_background_retry_state", {})
        for key in list(retries):
            _scope, retry_session, retry_character = key
            if retry_session != session_id:
                continue
            if character_key is not None and retry_character != character_key:
                continue
            if scopes is not None and _scope not in scopes:
                continue
            retries.pop(key, None)
        return not pending

    async def _shutdown_background_tasks(self, timeout: float = 30.0, *, final: bool = False) -> bool:
        """先取消可取消任务、排空需完成任务；超时后显式取消剩余任务。"""
        self._background_stopping = True
        current = asyncio.current_task()
        records = [
            (task, record)
            for task, record in list(getattr(self, "_background_tasks", {}).items())
            if task is not current and not task.done()
        ]
        for task, record in records:
            if record.stop_policy == "cancel":
                task.cancel()
        tasks = {task for task, _record in records}
        pending: set[asyncio.Task[Any]] = set()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))
        if pending:
            for task in pending:
                task.cancel()
            _cancelled, stubborn = await asyncio.wait(pending, timeout=min(1.0, max(0.0, float(timeout))))
            if stubborn:
                logger.error("停机后仍有 %d 个后台任务未响应取消", len(stubborn))
            else:
                logger.warning("停机时取消 %d 个超时的 drain 后台任务", len(pending))
            pending = stubborn
        if not final:
            self._background_stopping = False
        return not pending

    def _background_retry_ready(
        self,
        scope: str,
        session_id: str = "",
        character_key: str = "",
        *,
        now: float | None = None,
    ) -> bool:
        state = getattr(self, "_background_retry_state", {}).get(
            self._background_key(scope, session_id, character_key)
        )
        return not state or float(state.get("next_retry") or 0.0) <= float(time.time() if now is None else now)

    def _record_background_retry_failure(
        self,
        scope: str,
        session_id: str = "",
        character_key: str = "",
        *,
        error: Any = "",
        base_seconds: float = 60.0,
        max_seconds: float = 3600.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        key = self._background_key(scope, session_id, character_key)
        previous = getattr(self, "_background_retry_state", {}).get(key) or {}
        attempts = int(previous.get("attempts") or 0) + 1
        base = max(0.0, float(base_seconds))
        cap = max(base, float(max_seconds))
        delay = min(cap, base * (2 ** max(0, attempts - 1)))
        current = float(time.time() if now is None else now)
        state = {
            "attempts": attempts,
            "next_retry": current + delay,
            "delay": delay,
            "last_error": str(error or ""),
        }
        self._background_retry_state[key] = state
        return state

    def _clear_background_retry(self, scope: str, session_id: str = "", character_key: str = "") -> None:
        getattr(self, "_background_retry_state", {}).pop(
            self._background_key(scope, session_id, character_key),
            None,
        )

    def _background_retry_info(self, scope: str, session_id: str = "", character_key: str = "") -> dict[str, Any]:
        return dict(
            getattr(self, "_background_retry_state", {}).get(
                self._background_key(scope, session_id, character_key)
            )
            or {}
        )
