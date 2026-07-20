from __future__ import annotations

import asyncio
import unittest

from tests.support import ServiceFixtureMixin


def _update(update_id: int, chat_id: int, text: str = "消息") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False},
            "text": text,
        },
    }


class TelegramUpdateWorkerTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_recovery_uses_durable_offset_and_processes_pending_update(self):
        async def run():
            service = self.make_service()
            pending = _update(50, 1001)
            inserted, offset = service.app_store.stage_telegram_update(50, "1001", pending)
            self.assertTrue(inserted)
            self.assertEqual(offset, 51)

            service._offset = 0
            service._init_telegram_update_runtime()
            seen: list[int] = []

            async def handle(update):
                seen.append(update["update_id"])

            service.handle_update = handle
            await service._start_telegram_update_runtime()
            await asyncio.wait_for(
                asyncio.gather(*(queue.join() for queue in service._telegram_update_queues.values())),
                timeout=1,
            )

            self.assertEqual(service._offset, 51)
            self.assertEqual(seen, [50])
            self.assertEqual(service.app_store.list_pending_telegram_updates(), [])
            await service._stop_telegram_update_runtime(timeout=1)

        asyncio.run(run())

    def test_workers_preserve_chat_order_and_enforce_global_concurrency(self):
        async def run():
            service = self.make_service()
            service.config["telegram_update_max_concurrency"] = "2"
            updates = [
                _update(1, 101),
                _update(2, 202),
                _update(3, 101),
                _update(4, 303),
            ]
            for update in updates:
                service.app_store.stage_telegram_update(
                    update["update_id"],
                    str(update["message"]["chat"]["id"]),
                    update,
                )

            release = asyncio.Event()
            two_started = asyncio.Event()
            active = 0
            max_active = 0
            events: list[tuple[str, int, int]] = []

            async def handle(update):
                nonlocal active, max_active
                chat_id = update["message"]["chat"]["id"]
                active += 1
                max_active = max(max_active, active)
                events.append(("start", chat_id, update["update_id"]))
                if active >= 2:
                    two_started.set()
                await release.wait()
                events.append(("end", chat_id, update["update_id"]))
                active -= 1

            service.handle_update = handle
            await service._start_telegram_update_runtime()
            await asyncio.wait_for(two_started.wait(), timeout=1)
            self.assertEqual(max_active, 2)
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*(queue.join() for queue in service._telegram_update_queues.values())),
                timeout=2,
            )

            chat_101 = [(kind, update_id) for kind, chat, update_id in events if chat == 101]
            self.assertEqual(
                chat_101,
                [("start", 1), ("end", 1), ("start", 3), ("end", 3)],
            )
            self.assertLessEqual(max_active, 2)
            self.assertEqual(service.app_store.list_pending_telegram_updates(), [])
            await service._stop_telegram_update_runtime(timeout=1)

        asyncio.run(run())

    def test_new_update_preempts_active_same_chat_without_losing_queue(self):
        async def run():
            service = self.make_service()
            service.config["telegram_update_retry_base_seconds"] = "0"
            first_started = asyncio.Event()
            second_finished = asyncio.Event()
            cancelled: list[int] = []
            seen: list[int] = []

            async def handle(update):
                update_id = update["update_id"]
                seen.append(update_id)
                if update_id == 10:
                    first_started.set()
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        cancelled.append(update_id)
                        raise
                second_finished.set()

            service.handle_update = handle
            await service._start_telegram_update_runtime()
            await service._stage_and_queue_telegram_update(_update(10, 404))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await service._stage_and_queue_telegram_update(_update(11, 404))
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            await asyncio.wait_for(service._telegram_update_queues["404"].join(), timeout=1)

            self.assertEqual(seen, [10, 11])
            self.assertEqual(cancelled, [10])
            self.assertEqual(service.app_store.list_pending_telegram_updates(), [])
            await service._stop_telegram_update_runtime(timeout=1)

        asyncio.run(run())

    def test_poll_uses_offset_only_after_update_is_persisted(self):
        async def run():
            service = self.make_service()
            calls: list[int] = []

            async def telegram_api(method, data=None):
                self.assertEqual(method, "getUpdates")
                calls.append(int(data["offset"]))
                if len(calls) == 1:
                    return {"ok": True, "result": [_update(7, 707)]}
                raise asyncio.CancelledError()

            async def handle(update):
                return None

            service.tg_api = telegram_api
            service.handle_update = handle
            await service._start_telegram_update_runtime()
            with self.assertRaises(asyncio.CancelledError):
                await service.poll_loop()

            self.assertEqual(calls, [0, 8])
            self.assertEqual(service.app_store.telegram_update_offset(), 8)
            await service._stop_telegram_update_runtime(timeout=1)

        asyncio.run(run())

    def test_stop_timeout_cancels_worker_but_keeps_pending_update_for_restart(self):
        async def run():
            service = self.make_service()
            started = asyncio.Event()

            async def handle(update):
                started.set()
                await asyncio.sleep(10)

            service.handle_update = handle
            await service._start_telegram_update_runtime()
            await service._stage_and_queue_telegram_update(_update(80, 808))
            await asyncio.wait_for(started.wait(), timeout=1)

            drained = await service._stop_telegram_update_runtime(timeout=0.01)

            self.assertFalse(drained)
            pending = service.app_store.list_pending_telegram_updates()
            self.assertEqual([item["update_id"] for item in pending], [80])
            self.assertFalse(service._telegram_update_workers)
            self.assertFalse(service._telegram_active_update_tasks)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
