from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


BARE_COMMAND_ALIASES = {
    "\u81ea\u62cd": "\u81ea\u62cd",
    "\u62cd\u7167": "\u81ea\u62cd",
    "\u83dc\u5355": "\u83dc\u5355",
    "\u5e2e\u52a9": "\u83dc\u5355",
    "help": "\u83dc\u5355",
    "start": "\u83dc\u5355",
    "\u65b0\u573a\u666f": "\u65b0\u573a\u666f",
    "\u4e0a\u4e0b\u6587\u91cd\u7f6e": "\u65b0\u573a\u666f",
    "\u6e05\u7a7a\u4e0a\u4e0b\u6587": "\u65b0\u573a\u666f",
    "\u8c03\u5ea6": "\u8c03\u5ea6",
    "\u751f\u56fe\u72b6\u6001": "\u751f\u56fe\u72b6\u6001",
    "\u63d0\u793a\u8bcd": "\u63d0\u793a\u8bcd",
}


class TelegramIOMixin:
    async def tg_api(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.http is None:
            raise RuntimeError("HTTP session not initialized")
        token = self.config["telegram_bot_token"]
        url = f"https://api.telegram.org/bot{token}/{method}"
        async with self.http.post(url, data=data or {}) as resp:
            payload = await resp.json(content_type=None)
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram {method} failed: {payload}")
            return payload

    async def send_message(self, chat_id: int | str, text: str):
        chunks = self._split_text(text, 3900)
        for chunk in chunks:
            await self.tg_api("sendMessage", {"chat_id": str(chat_id), "text": chunk})

    async def send_photo(self, chat_id: int | str, image_bytes: bytes, caption: str = ""):
        if self.http is None:
            raise RuntimeError("HTTP session not initialized")
        token = self.config["telegram_bot_token"]
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption[:1024])
        form.add_field("photo", image_bytes, filename="selfie.png", content_type="image/png")
        async with self.http.post(url, data=form) as resp:
            payload = await resp.json(content_type=None)
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram sendPhoto failed: {payload}")

    async def send_action(self, chat_id: int | str, action: str):
        try:
            await self.tg_api("sendChatAction", {"chat_id": str(chat_id), "action": action})
        except Exception:
            logger.debug("sendChatAction failed", exc_info=True)

    @staticmethod
    def _split_text(text: str, limit: int) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            cut = text.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(text[:cut])
            text = text[cut:].lstrip()
        return chunks

    async def poll_loop(self):
        while True:
            try:
                data = {
                    "timeout": "55",
                    "offset": str(self._offset),
                    "allowed_updates": json.dumps(["message"]),
                }
                payload = await self.tg_api("getUpdates", data)
                for update in payload.get("result", []):
                    self._offset = max(self._offset, update["update_id"] + 1)
                    asyncio.create_task(self.handle_update(update))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("poll loop error: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def handle_update(self, update: dict[str, Any]):
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not text:
            return

        session_id = self.session_id_for_chat(chat_id)
        allowed = self.config.get("allowed_chat_ids") or []
        if allowed and str(chat_id) not in {str(x) for x in allowed}:
            self._ulog(session_id, "BLOCKED", f"不在白名单，已忽略: {text}")
            logger.info("ignored chat_id not in allowlist: %s", chat_id)
            return

        cmd, arg = self.parse_command(text)
        if cmd is not None:
            self._ulog(session_id, "CMD", f"/{cmd} {arg}".strip())
        else:
            self._ulog(session_id, "USER", text)
        try:
            if cmd is not None:
                await self.dispatch_command(chat_id, session_id, cmd, arg)
            else:
                await self.handle_chat(chat_id, session_id, text)
        except Exception as exc:
            self._ulog(session_id, "ERROR", f"处理消息异常: {exc}")
            logger.error("message handling failed: %s", exc, exc_info=True)
            await self.send_message(chat_id, f"发生异常: {exc}")

    def parse_command(self, text: str) -> tuple[str | None, str]:
        text = (text or "").strip()
        if text == "/":
            return "菜单", ""
        if not text.startswith("/"):
            first, sep, rest = text.partition(" ")
            alias = BARE_COMMAND_ALIASES.get(first.lower()) or BARE_COMMAND_ALIASES.get(first)
            if alias:
                return alias, rest.strip() if sep else ""
            return None, text
        body = text[1:].strip()
        if not body:
            return "菜单", ""
        first, _, rest = body.partition(" ")
        if "@" in first:
            command, _, mention = first.partition("@")
            if self._bot_username and mention.lower() != self._bot_username.lower():
                return None, text
            first = command
        return first.strip(), rest.strip()
