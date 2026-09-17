from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram_comfyui_selfie.telegram_io import quick_reply_keyboard
from tests.support import ServiceFixtureMixin


class TelegramMenuTests(ServiceFixtureMixin, unittest.TestCase):
    def test_registers_valid_private_commands_that_dispatch(self):
        async def run():
            svc = self.make_service()
            svc.tg_api = AsyncMock(return_value={"ok": True})
            self.assertTrue(await svc._setup_telegram_commands())
            calls = svc.tg_api.await_args_list
            self.assertEqual([c.args[0] for c in calls], ["setMyCommands", "setChatMenuButton"])
            data = calls[0].args[1]
            self.assertEqual(json.loads(data["scope"]), {"type": "all_private_chats"})
            self.assertEqual(data["language_code"], "")
            commands = json.loads(data["commands"])
            self.assertLessEqual(len(commands), 100)
            self.assertEqual(len(commands), len({c["command"] for c in commands}))
            svc.send_message = AsyncMock()
            # 每个已发布菜单项都能进入现有 handler；不依靠源码断言验证路由。
            for name in dir(svc):
                if name.startswith("cmd_"):
                    setattr(svc, name, AsyncMock())
            svc._bot_username = "test_bot"
            for command in commands:
                self.assertRegex(command["command"], r"^[a-z0-9_]{1,32}$")
                self.assertTrue(1 <= len(command["description"]) <= 256)
                canonical, arg = svc.parse_command(f'/{command["command"]}@test_bot')
                await svc.dispatch_command(123, "telegram:123", canonical, arg)
            svc.send_message.assert_not_awaited()
            svc.cmd_quick_reply.assert_awaited_once()
            svc.cmd_hide_keyboard.assert_awaited_once()
            svc.cmd_selfie.assert_awaited_once()
            self.assertEqual(json.loads(calls[1].args[1]["menu_button"]), {"type": "commands"})

        asyncio.run(run())

    def test_menu_failure_does_not_stop_bot_startup(self):
        async def run():
            svc = self.make_service()
            svc.tg_api = AsyncMock(side_effect=[
                {"ok": True, "result": {"username": "test_bot"}},
                RuntimeError("private-token-value"),
                {"ok": True},
            ])
            svc._start_telegram_update_runtime = AsyncMock()
            svc._spawn_background = Mock(side_effect=lambda coro, **kw: (coro.close(), Mock())[1])
            with patch("telegram_comfyui_selfie.service.aiohttp.ClientSession", return_value=Mock()), \
                    self.assertLogs("telegram_comfyui_selfie.telegram_io", level="WARNING") as logs:
                await svc.start_bot()
            self.assertNotIn("private-token-value", "\n".join(logs.output))
            svc._start_telegram_update_runtime.assert_awaited_once()
            self.assertEqual(svc._spawn_background.call_count, 2)
            self.assertEqual(svc.tg_api.await_args_list[-1].args[0], "setChatMenuButton")

        asyncio.run(run())

    def test_registration_timeout_is_bounded_and_cancellation_propagates(self):
        async def run():
            svc = self.make_service()
            svc.tg_api = AsyncMock(side_effect=[asyncio.TimeoutError(), {"ok": True}])
            with self.assertLogs("telegram_comfyui_selfie.telegram_io", level="WARNING"):
                self.assertFalse(await svc._setup_telegram_commands())
            self.assertEqual(svc.tg_api.await_count, 2)
            svc.tg_api = AsyncMock(side_effect=asyncio.CancelledError())
            with self.assertRaises(asyncio.CancelledError):
                await svc._setup_telegram_commands()

        asyncio.run(run())

    def test_menu_keyboard_open_hide_reopen_and_chat_buttons(self):
        async def run():
            svc = self.make_service()
            svc.tg_api = AsyncMock(return_value={"ok": True})
            svc.handle_chat = AsyncMock()

            async def incoming(text):
                await svc.handle_update({"message": {
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123, "is_bot": False}, "text": text,
                }})

            for text in ("/help", "/quickreply", "快捷回复"):
                await incoming(text)
                markup = json.loads(svc.tg_api.await_args.args[1]["reply_markup"])
                self.assertEqual(markup, quick_reply_keyboard())
            for row in markup["keyboard"][:-1]:
                for button in row:
                    await incoming(button["text"])
                    svc.handle_chat.assert_awaited_with(123, "telegram:123", button["text"])
            for text in ("隐藏键盘", "/hidekeyboard", "/quickreply off"):
                await incoming(text)
                self.assertEqual(json.loads(svc.tg_api.await_args.args[1]["reply_markup"]),
                                 {"remove_keyboard": True})
            await svc.send_message(123, "普通回复")
            self.assertNotIn("reply_markup", svc.tg_api.await_args.args[1])
            await incoming("/quickreply")
            self.assertIn("keyboard", json.loads(svc.tg_api.await_args.args[1]["reply_markup"]))

        asyncio.run(run())

    def test_split_message_attaches_markup_once_and_preserves_text(self):
        async def run():
            svc = self.make_service()
            svc.tg_api = AsyncMock(return_value={"ok": True})
            text = "长文本" * 3000
            await svc.send_message(123, text, reply_markup=quick_reply_keyboard())
            data = [call.args[1] for call in svc.tg_api.await_args_list]
            self.assertEqual("".join(d["text"] for d in data), text)
            self.assertEqual(sum("reply_markup" in d for d in data), 1)
            self.assertTrue(all(d["chat_id"] == "123" for d in data))

        asyncio.run(run())

    def test_allowlist_still_blocks_keyboard_commands(self):
        async def run():
            svc = self.make_service()
            svc.config["allowed_chat_ids"] = [999]
            svc.tg_api = AsyncMock()
            await svc.handle_update({"message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "is_bot": False}, "text": "/quickreply",
            }})
            svc.tg_api.assert_not_awaited()

        asyncio.run(run())
