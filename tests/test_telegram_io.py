from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from tests.support import ServiceFixtureMixin


def private_update(
    *,
    chat_id: int = 123,
    sender_id: int | None = None,
    message_id: int = 1,
    text: str = "",
    photo_id: str = "",
) -> dict:
    """构造包含真实私聊身份字段的 Telegram update。"""
    if sender_id is None:
        sender_id = chat_id
    message = {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": sender_id, "is_bot": False},
        "message_id": message_id,
    }
    if text:
        message["text"] = text
    if photo_id:
        message["photo"] = [{"file_id": photo_id, "width": 100, "height": 100}]
    return {"message": message}


class TelegramPrivateChatBoundaryTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_group_and_topic_messages_never_enter_session_state(self):
        async def run():
            svc = self.make_service()
            original_sessions = dict(svc.sessions)
            svc.session_id_for_chat = Mock(wraps=svc.session_id_for_chat)
            svc._ulog = Mock(wraps=svc._ulog)
            svc._process_incoming_message = AsyncMock()

            updates = [
                {
                    "message": {
                        "chat": {"id": -1001, "type": "group"},
                        "from": {"id": 11, "is_bot": False},
                        "message_id": 1,
                        "text": "群聊消息",
                    }
                },
                {
                    "message": {
                        "chat": {"id": -1002, "type": "supergroup"},
                        "from": {"id": 22, "is_bot": False},
                        "message_id": 2,
                        "message_thread_id": 99,
                        "photo": [{"file_id": "topic-photo", "width": 100, "height": 100}],
                    }
                },
            ]

            for update in updates:
                await svc.handle_update(update)

            self.assertEqual(svc.sessions, original_sessions)
            svc.session_id_for_chat.assert_not_called()
            svc._ulog.assert_not_called()
            svc._process_incoming_message.assert_not_awaited()
            self.assertFalse(getattr(svc, "_pending_photo_inputs", {}))
            self.assertFalse(getattr(svc, "_pending_media_group_inputs", {}))

        asyncio.run(run())

    def test_private_chat_requires_matching_non_bot_sender(self):
        async def run():
            svc = self.make_service()
            svc.session_id_for_chat = Mock(wraps=svc.session_id_for_chat)
            svc._process_incoming_message = AsyncMock()
            invalid_messages = [
                private_update(chat_id=123, sender_id=456, text="身份不一致"),
                {
                    "message": {
                        "chat": {"id": 123, "type": "private"},
                        "message_id": 2,
                        "text": "缺少发送者",
                    }
                },
                {
                    "message": {
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": 123, "is_bot": True},
                        "message_id": 3,
                        "text": "机器人发送者",
                    }
                },
            ]

            for update in invalid_messages:
                await svc.handle_update(update)

            svc.session_id_for_chat.assert_not_called()
            svc._process_incoming_message.assert_not_awaited()
            self.assertNotIn("telegram:123", svc.sessions)

        asyncio.run(run())

    def test_valid_private_chat_routes_to_its_own_session(self):
        async def run():
            svc = self.make_service()
            svc._process_incoming_message = AsyncMock()
            update = private_update(chat_id=123, text="正常私聊")

            await svc.handle_update(update)

            svc._process_incoming_message.assert_awaited_once_with(
                123,
                "telegram:123",
                update["message"],
                "正常私聊",
            )

        asyncio.run(run())


class TelegramPendingPhotoBatchTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_two_single_photos_still_merge_after_event_loop_yield(self):
        async def run():
            svc = self.make_service()
            svc.config["photo_caption_wait_seconds"] = "0.02"
            svc.has_llm_config = lambda purpose, session_id="": purpose == "vision"
            svc._ulog = Mock()
            processed = asyncio.Event()
            captured: dict = {}

            async def fake_process(chat_id, session_id, msg, text):
                captured.update({"chat_id": chat_id, "session_id": session_id, "msg": msg, "text": text})
                processed.set()

            svc._process_incoming_message = AsyncMock(side_effect=fake_process)
            first = private_update(message_id=1, photo_id="photo-1")
            second = private_update(message_id=2, photo_id="photo-2")

            try:
                await svc.handle_update(first)
                batch_future = svc._pending_photo_inputs["telegram:123"]["future"]
                await asyncio.sleep(0)
                await svc.handle_update(second)

                await asyncio.wait_for(processed.wait(), timeout=1)
                await asyncio.sleep(0)

                self.assertFalse(batch_future.cancelled())
                self.assertEqual(captured["chat_id"], 123)
                self.assertEqual(captured["session_id"], "telegram:123")
                self.assertEqual(captured["text"], "")
                grouped = captured["msg"]["_grouped_photos"]
                self.assertEqual([photo[-1]["file_id"] for photo in grouped], ["photo-1", "photo-2"])
                self.assertEqual(captured["msg"]["_media_group_message_count"], 2)
                svc._process_incoming_message.assert_awaited_once()
                self.assertNotIn("telegram:123", svc._pending_photo_inputs)
            finally:
                entries = list(getattr(svc, "_pending_photo_inputs", {}).values())
                tasks = [entry.get("task") for entry in entries if entry.get("task")]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
