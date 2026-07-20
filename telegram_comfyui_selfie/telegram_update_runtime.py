from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any


logger = logging.getLogger(__name__)


class TelegramUpdateRuntimeMixin:
    """提供持久 Telegram update 收件箱、按会话顺序与全局背压。"""

    def _init_telegram_update_runtime(self) -> None:
        self._telegram_update_queues: dict[str, asyncio.Queue] = {}
        self._telegram_update_workers: dict[str, asyncio.Task] = {}
        self._telegram_active_update_tasks: dict[str, asyncio.Task] = {}
        self._telegram_preempted_update_tasks: set[asyncio.Task] = set()
        self._telegram_queued_update_ids: set[int] = set()
        self._telegram_update_semaphore: asyncio.Semaphore | None = None
        self._telegram_update_stopping = False

    def _telegram_update_int_config(self, key: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return max(minimum, default)

    def _telegram_update_float_config(
        self,
        key: str,
        default: float,
        minimum: float = 0.0,
    ) -> float:
        try:
            return max(minimum, float(self.config.get(key, default)))
        except (TypeError, ValueError):
            return max(minimum, default)

    @staticmethod
    def _telegram_update_chat_key(update: dict[str, Any], update_id: int) -> str:
        message = update.get("message") if isinstance(update, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        return str(chat_id) if chat_id is not None else f"update:{update_id}"

    async def _start_telegram_update_runtime(self) -> None:
        self._telegram_update_stopping = False
        concurrency = self._telegram_update_int_config(
            "telegram_update_max_concurrency",
            4,
        )
        self._telegram_update_semaphore = asyncio.Semaphore(concurrency)
        durable_offset, pending = await asyncio.gather(
            asyncio.to_thread(self.app_store.telegram_update_offset),
            asyncio.to_thread(self.app_store.list_pending_telegram_updates),
        )
        self._offset = max(int(getattr(self, "_offset", 0) or 0), int(durable_offset or 0))
        for item in pending:
            update_id = int(item.get("update_id") or 0)
            update = item.get("update") if isinstance(item.get("update"), dict) else {}
            chat_key = str(item.get("chat_key") or self._telegram_update_chat_key(update, update_id))
            await self._queue_persisted_telegram_update(
                update_id,
                chat_key,
                update,
                attempts=int(item.get("attempts") or 0),
                available_at=float(item.get("available_at") or 0),
                preempt=False,
            )

    async def _stage_and_queue_telegram_update(self, update: dict[str, Any]) -> bool:
        """先持久化更新与 offset，再进入有界会话队列。"""
        try:
            update_id = int(update["update_id"])
        except (KeyError, TypeError, ValueError):
            logger.error("Telegram update 缺少合法 update_id: %r", update)
            return False
        chat_key = self._telegram_update_chat_key(update, update_id)
        inserted, durable_offset = await asyncio.to_thread(
            self.app_store.stage_telegram_update,
            update_id,
            chat_key,
            update,
        )
        self._offset = max(int(getattr(self, "_offset", 0) or 0), int(durable_offset))
        if not inserted:
            return False
        await self._queue_persisted_telegram_update(
            update_id,
            chat_key,
            update,
            preempt=True,
        )
        return True

    async def _queue_persisted_telegram_update(
        self,
        update_id: int,
        chat_key: str,
        update: dict[str, Any],
        *,
        attempts: int = 0,
        available_at: float = 0.0,
        preempt: bool,
    ) -> None:
        if update_id in self._telegram_queued_update_ids:
            return
        queue = self._telegram_update_queues.get(chat_key)
        if queue is None:
            queue = asyncio.Queue(
                maxsize=self._telegram_update_int_config("telegram_update_queue_size", 32)
            )
            self._telegram_update_queues[chat_key] = queue
        worker = self._telegram_update_workers.get(chat_key)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                self._telegram_chat_update_worker(chat_key, queue),
                name=f"telegram-update-worker:{chat_key}",
            )
            self._telegram_update_workers[chat_key] = worker
            worker.add_done_callback(
                lambda done, key=chat_key: self._telegram_update_worker_done(key, done)
            )
        await queue.put((update_id, update, attempts, available_at))
        self._telegram_queued_update_ids.add(update_id)
        if preempt:
            active = self._telegram_active_update_tasks.get(chat_key)
            if active is not None and not active.done():
                self._telegram_preempted_update_tasks.add(active)
                active.cancel()

    def _telegram_update_worker_done(self, chat_key: str, task: asyncio.Task) -> None:
        if self._telegram_update_workers.get(chat_key) is task:
            self._telegram_update_workers.pop(chat_key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Telegram update worker 异常退出: chat=%s", chat_key)

    async def _complete_telegram_update(self, update_id: int) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await asyncio.to_thread(self.app_store.complete_telegram_update, update_id)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.05 * (2**attempt))
        raise RuntimeError(f"无法确认 Telegram update {update_id} 已完成") from last_error

    async def _telegram_chat_update_worker(
        self,
        chat_key: str,
        queue: asyncio.Queue,
    ) -> None:
        while True:
            update_id, update, attempts, available_at = await queue.get()
            terminal = False
            try:
                while True:
                    delay = max(0.0, float(available_at or 0) - time.time())
                    if delay:
                        await asyncio.sleep(delay)
                    semaphore = self._telegram_update_semaphore
                    if semaphore is None:
                        semaphore = asyncio.Semaphore(
                            self._telegram_update_int_config(
                                "telegram_update_max_concurrency",
                                4,
                            )
                        )
                        self._telegram_update_semaphore = semaphore
                    process_task: asyncio.Task | None = None
                    try:
                        async with semaphore:
                            process_task = asyncio.create_task(
                                self.handle_update(update),
                                name=f"telegram-update:{update_id}",
                            )
                            self._telegram_active_update_tasks[chat_key] = process_task
                            await process_task
                        await self._complete_telegram_update(update_id)
                        terminal = True
                        break
                    except asyncio.CancelledError:
                        if (
                            process_task is not None
                            and process_task in self._telegram_preempted_update_tasks
                        ):
                            self._telegram_preempted_update_tasks.discard(process_task)
                            await self._complete_telegram_update(update_id)
                            terminal = True
                            break
                        raise
                    except Exception as exc:
                        attempts += 1
                        base = self._telegram_update_float_config(
                            "telegram_update_retry_base_seconds",
                            1.0,
                        )
                        available_at = time.time() + min(30.0, base * (2 ** max(0, attempts - 1)))
                        max_attempts = self._telegram_update_int_config(
                            "telegram_update_max_attempts",
                            3,
                        )
                        attempts, status = await asyncio.to_thread(
                            self.app_store.fail_telegram_update,
                            update_id,
                            str(exc),
                            max_attempts=max_attempts,
                            retry_at=available_at,
                        )
                        if status == "failed":
                            logger.error(
                                "Telegram update %s 处理失败 %s 次，已保留为死信: %s",
                                update_id,
                                attempts,
                                exc,
                                exc_info=True,
                            )
                            terminal = True
                            break
                        logger.warning(
                            "Telegram update %s 处理失败，将在 %.1fs 后重试: %s",
                            update_id,
                            max(0.0, available_at - time.time()),
                            exc,
                        )
                    finally:
                        if (
                            process_task is not None
                            and self._telegram_active_update_tasks.get(chat_key) is process_task
                        ):
                            self._telegram_active_update_tasks.pop(chat_key, None)
                        if process_task is not None:
                            self._telegram_preempted_update_tasks.discard(process_task)
            finally:
                if terminal:
                    self._telegram_queued_update_ids.discard(update_id)
                    queue.task_done()

    async def poll_loop(self) -> None:
        while not self._telegram_update_stopping:
            try:
                data = {
                    "timeout": "55",
                    "offset": str(self._offset),
                    "allowed_updates": json.dumps(["message"]),
                }
                payload = await self.tg_api("getUpdates", data)
                updates = payload.get("result", [])
                for update in sorted(updates, key=lambda item: int(item.get("update_id", 0))):
                    await self._stage_and_queue_telegram_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("poll loop error: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def _stop_telegram_update_runtime(self, *, timeout: float | None = None) -> bool:
        """停止拉取后排空更新队列；超时则取消 worker 并保留持久 pending。"""
        self._telegram_update_stopping = True
        if timeout is None:
            timeout = self._telegram_update_float_config(
                "telegram_update_drain_timeout_seconds",
                30.0,
            )
        queues = list(self._telegram_update_queues.values())
        drained = True
        if queues:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(queue.join() for queue in queues)),
                    timeout=max(0.0, float(timeout)),
                )
            except asyncio.TimeoutError:
                drained = False
                logger.warning("Telegram update 队列在 %.1fs 内未排空，将保留待办并取消 worker", timeout)

        tasks = {
            *self._telegram_update_workers.values(),
            *self._telegram_active_update_tasks.values(),
        }
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._telegram_update_workers.clear()
        self._telegram_active_update_tasks.clear()
        self._telegram_preempted_update_tasks.clear()
        self._telegram_update_queues.clear()
        self._telegram_queued_update_ids.clear()
        self._telegram_update_semaphore = None
        return drained

    async def _drain_protected_image_tasks(self, timeout: float) -> bool:
        """关闭 HTTP 前等待受保护发图链，超时后显式取消。"""
        tasks = {
            task
            for task in getattr(self, "_protected_image_tasks", set())
            if isinstance(task, asyncio.Task) and not task.done()
        }
        if not tasks:
            return True
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning("停机时取消 %d 个超时的受保护发图任务", len(pending))
        for task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
        return not pending
