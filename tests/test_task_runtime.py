from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from telegram_comfyui_selfie.task_runtime import TaskRuntimeMixin
from tests.support import ServiceFixtureMixin


class _TaskHarness(TaskRuntimeMixin):
    def __init__(self):
        self._init_task_runtime()


class BackgroundTaskRegistryTestCase(unittest.TestCase):
    def test_task_slot_cleanup_does_not_remove_newer_generation(self):
        async def run():
            runtime = _TaskHarness()
            first_release = asyncio.Event()
            second_release = asyncio.Event()
            bucket: dict[str, asyncio.Task] = {}

            first = runtime._spawn_background(
                first_release.wait(),
                name="first",
                session_id="telegram:1",
                scope="slot",
            )
            runtime._bind_background_task_slot(bucket, "slot", first)
            second = runtime._spawn_background(
                second_release.wait(),
                name="second",
                session_id="telegram:1",
                scope="slot",
            )
            runtime._bind_background_task_slot(bucket, "slot", second)
            records = runtime._background_tasks
            self.assertLess(records[first].generation, records[second].generation)

            first_release.set()
            await first
            await asyncio.sleep(0)
            self.assertIs(bucket.get("slot"), second)
            second_release.set()
            await second
            await asyncio.sleep(0)
            self.assertNotIn("slot", bucket)

        asyncio.run(run())

    def test_done_callback_consumes_exception_and_removes_record(self):
        async def run():
            runtime = _TaskHarness()
            finished = asyncio.Event()

            async def fail():
                finished.set()
                raise RuntimeError("expected background failure")

            with patch("telegram_comfyui_selfie.task_runtime.logger.error") as error_log:
                task = runtime._spawn_background(
                    fail(),
                    name="failure-test",
                    session_id="telegram:1",
                    character_key="role-a",
                    scope="test",
                )
                await asyncio.wait_for(finished.wait(), timeout=1)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            self.assertTrue(task.done())
            self.assertFalse(runtime._background_tasks)
            error_log.assert_called_once()
            self.assertIsInstance(task.exception(), RuntimeError)

        asyncio.run(run())

    def test_scope_cancel_waits_and_only_matches_requested_character(self):
        async def run():
            runtime = _TaskHarness()
            started = {name: asyncio.Event() for name in ("a", "b", "other")}
            finished = {name: asyncio.Event() for name in ("a", "b", "other")}

            async def wait_forever(name: str):
                started[name].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished[name].set()

            task_a = runtime._spawn_background(
                wait_forever("a"),
                name="a",
                session_id="telegram:1",
                character_key="role-a",
                scope="test",
            )
            task_b = runtime._spawn_background(
                wait_forever("b"),
                name="b",
                session_id="telegram:1",
                character_key="role-b",
                scope="test",
            )
            task_other = runtime._spawn_background(
                wait_forever("other"),
                name="other",
                session_id="telegram:2",
                character_key="role-a",
                scope="test",
            )
            await asyncio.gather(*(event.wait() for event in started.values()))

            self.assertTrue(
                await runtime._cancel_background_scope(
                    "telegram:1",
                    "role-a",
                    timeout=1,
                )
            )
            self.assertTrue(task_a.cancelled())
            self.assertTrue(finished["a"].is_set())
            self.assertFalse(task_b.done())
            self.assertFalse(task_other.done())

            self.assertTrue(await runtime._shutdown_background_tasks(1, final=True))
            self.assertTrue(task_b.cancelled())
            self.assertTrue(task_other.cancelled())

        asyncio.run(run())


class BackgroundTaskServiceTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_stop_bot_cancels_cancel_policy_and_drains_before_http_close(self):
        async def run():
            service = self.make_service()
            service.config["telegram_update_drain_timeout_seconds"] = "1"
            drain_finished = asyncio.Event()
            cancel_finished = asyncio.Event()

            class FakeHttp:
                closed = False

                async def close(inner_self):
                    inner_self.closed = True

            http = FakeHttp()
            service.http = http

            async def drain_work():
                await asyncio.sleep(0.02)
                self.assertFalse(http.closed)
                drain_finished.set()

            async def cancel_work():
                try:
                    await asyncio.Event().wait()
                finally:
                    cancel_finished.set()

            service._spawn_background(
                drain_work(),
                name="drain-work",
                scope="test-drain",
                drain=True,
            )
            service._spawn_background(
                cancel_work(),
                name="cancel-work",
                scope="test-cancel",
            )
            await asyncio.sleep(0)

            await service.stop_bot()

            self.assertTrue(drain_finished.is_set())
            self.assertTrue(cancel_finished.is_set())
            self.assertTrue(http.closed)
            self.assertFalse(service._background_tasks)

        asyncio.run(run())

    def test_dream_failure_uses_exponential_next_retry_and_cleans_task_map(self):
        async def run():
            service = self.make_service()
            service.config["dream_retry_base_seconds"] = "5"
            service.config["dream_retry_max_seconds"] = "60"
            session_id = "telegram:dream-retry"
            character_key = service._context_character_key(session_id)
            service._dream_once = AsyncMock(side_effect=RuntimeError("dream offline"))
            now = datetime.now(timezone.utc)

            await service._run_dream(session_id, now, reason="test", force=True)
            await asyncio.sleep(0)
            first = service._background_retry_info("dream", session_id, character_key)
            self.assertEqual(first["attempts"], 1)
            self.assertEqual(first["delay"], 5)
            self.assertGreater(first["next_retry"], 0)
            self.assertNotIn(f"{session_id}\n{character_key}", service._dream_tasks)

            await service._run_dream(session_id, now, reason="blocked", force=False)
            service._dream_once.assert_awaited_once()

            retry_key = service._background_key("dream", session_id, character_key)
            service._background_retry_state[retry_key]["next_retry"] = 0
            await service._run_dream(session_id, now, reason="retry", force=False)
            task = service._dream_tasks[f"{session_id}\n{character_key}"]
            await task
            await asyncio.sleep(0)

            second = service._background_retry_info("dream", session_id, character_key)
            self.assertEqual(second["attempts"], 2)
            self.assertEqual(second["delay"], 10)
            self.assertNotIn(f"{session_id}\n{character_key}", service._dream_tasks)

        asyncio.run(run())

    def test_weather_failure_sets_next_retry_and_suppresses_refresh(self):
        async def run():
            service = self.make_service()
            service.config["weather_retry_base_seconds"] = "7"
            service.config["weather_retry_max_seconds"] = "30"
            session_id = "telegram:weather-retry"

            with patch(
                "telegram_comfyui_selfie.scheduler_runtime.aiohttp.ClientSession",
                side_effect=RuntimeError("weather offline"),
            ):
                self.assertIsNone(await service._fetch_weather(session_id=session_id))

            retry = service._background_retry_info("weather", session_id)
            self.assertEqual(retry["attempts"], 1)
            self.assertEqual(retry["delay"], 7)
            self.assertGreater(retry["next_retry"], 0)
            self.assertFalse(service._schedule_weather_refresh(session_id))
            self.assertIsNone(
                service._find_background_task(scope="weather", session_id=session_id)
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
