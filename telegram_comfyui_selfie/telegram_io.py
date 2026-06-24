from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from typing import Any

import aiohttp

from .command_aliases import BARE_COMMAND_ALIASES, resolve_command_alias
from . import session_schema

logger = logging.getLogger(__name__)


class TelegramIOMixin:
    async def tg_api(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.http is None:
            raise RuntimeError("HTTP session not initialized")
        token = self.config["telegram_bot_token"]
        url = f"https://api.telegram.org/bot{token}/{method}"
        kwargs = {"data": data or {}}
        proxy = self._telegram_http_proxy() if hasattr(self, "_telegram_http_proxy") else ""
        if proxy:
            kwargs["proxy"] = proxy
        async with self.http.post(url, **kwargs) as resp:
            payload = await resp.json(content_type=None)
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram {method} failed: {payload}")
            return payload

    async def send_message(self, chat_id: int | str, text: str, *, split_paragraphs: bool = False):
        if split_paragraphs:
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        else:
            paragraphs = [text]
        for i, para in enumerate(paragraphs):
            if i > 0:
                await asyncio.sleep(1)
            for chunk in self._split_text(para, 3900):
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
        kwargs = {"data": form}
        proxy = self._telegram_http_proxy() if hasattr(self, "_telegram_http_proxy") else ""
        if proxy:
            kwargs["proxy"] = proxy
        async with self.http.post(url, **kwargs) as resp:
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
        text = (msg.get("text") or msg.get("caption") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        has_payload = bool(
            text
            or msg.get("photo")
            or msg.get("reply_to_message")
            or msg.get("external_reply")
            or msg.get("quote")
        )
        if chat_id is None or not has_payload:
            return

        session_id = self.session_id_for_chat(chat_id)
        allowed = self.config.get("allowed_chat_ids") or []
        if allowed and str(chat_id) not in {str(x) for x in allowed}:
            self._ulog(session_id, "BLOCKED", f"不在白名单，已忽略: {text or '[非文本消息]'}")
            logger.info("ignored chat_id not in allowlist: %s", chat_id)
            return

        state = self._get_session_state(session_id)
        if session_schema.get_frozen(state):
            session_schema.set_frozen(state, False)
            session_schema.set_frozen_at(state, 0)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "UNFREEZE", "用户发消息，自动解冻")
            logger.info("session %s auto-unfrozen by user message", session_id)

        cmd, arg = self.parse_command(text) if text else (None, "")
        try:
            if cmd is not None:
                self._ulog(session_id, "CMD", f"/{cmd} {arg}".strip())
                await self.dispatch_command(chat_id, session_id, cmd, arg)
            elif hasattr(self, "handle_init_flow_message") and await self.handle_init_flow_message(chat_id, session_id, text):
                self._ulog(session_id, "USER", text)
                return
            else:
                augmented_text = await self._augment_chat_text_from_message(session_id, text, msg)
                if not augmented_text:
                    return
                self._ulog(session_id, "USER", augmented_text)
                await self.handle_chat(chat_id, session_id, augmented_text)
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
        return resolve_command_alias(first.strip()), rest.strip()

    @staticmethod
    def _message_plain_text(msg: dict[str, Any] | None) -> str:
        if not isinstance(msg, dict):
            return ""
        return str(msg.get("text") or msg.get("caption") or "").strip()

    @staticmethod
    def _message_author_label(msg: dict[str, Any] | None) -> str:
        if not isinstance(msg, dict):
            return "被引用消息"
        sender = msg.get("from") or {}
        if isinstance(sender, dict) and sender.get("is_bot"):
            return "机器人消息"
        return "用户消息"

    def _format_telegram_reply_context(self, msg: dict[str, Any]) -> str:
        chunks: list[str] = []
        quote = msg.get("quote") or {}
        if isinstance(quote, dict):
            quote_text = str(quote.get("text") or "").strip()
            if quote_text:
                chunks.append(f"手动引用片段: {quote_text}")
        reply = msg.get("reply_to_message") or {}
        if isinstance(reply, dict):
            reply_text = self._message_plain_text(reply)
            if reply_text:
                chunks.append(f"回复的{self._message_author_label(reply)}: {reply_text}")
        external = msg.get("external_reply") or {}
        if isinstance(external, dict):
            external_text = str(external.get("text") or external.get("caption") or "").strip()
            if external_text:
                chunks.append(f"外部引用消息: {external_text}")
        return "\n".join(chunks)

    @staticmethod
    def _largest_photo_size(photo_sizes: Any) -> dict[str, Any]:
        if not isinstance(photo_sizes, list) or not photo_sizes:
            return {}
        candidates = [p for p in photo_sizes if isinstance(p, dict) and p.get("file_id")]
        if not candidates:
            return {}
        return max(candidates, key=lambda p: int(p.get("file_size") or 0) or int(p.get("width") or 0) * int(p.get("height") or 0))

    async def _download_telegram_file(self, file_id: str) -> tuple[bytes, str]:
        if self.http is None:
            raise RuntimeError("HTTP session not initialized")
        payload = await self.tg_api("getFile", {"file_id": file_id})
        result = payload.get("result") or {}
        file_path = str(result.get("file_path") or "")
        if not file_path:
            raise RuntimeError("Telegram getFile did not return file_path")
        token = self.config["telegram_bot_token"]
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        kwargs: dict[str, Any] = {}
        proxy = self._telegram_http_proxy() if hasattr(self, "_telegram_http_proxy") else ""
        if proxy:
            kwargs["proxy"] = proxy
        async with self.http.get(url, **kwargs) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Telegram file download failed: {resp.status} {text}")
            data = await resp.read()
            mime_type = resp.headers.get("Content-Type") or mimetypes.guess_type(file_path)[0] or "image/jpeg"
        return data, mime_type

    async def _describe_telegram_photo_sizes_for_chat(
        self,
        session_id: str,
        photo_sizes: Any,
        *,
        source_label: str,
        nearby_text: str = "",
    ) -> str:
        if not hasattr(self, "_describe_image_for_chat") or not self.has_llm_config("vision", session_id):
            return ""
        photo = self._largest_photo_size(photo_sizes)
        file_id = str(photo.get("file_id") or "")
        if not file_id:
            return ""
        if int(photo.get("file_size") or 0) > 20 * 1024 * 1024:
            return ""
        image_bytes, mime_type = await self._download_telegram_file(file_id)
        return await self._describe_image_for_chat(
            session_id,
            image_bytes,
            mime_type,
            source_label=source_label,
            nearby_text=nearby_text,
        )

    async def _augment_chat_text_from_message(self, session_id: str, text: str, msg: dict[str, Any]) -> str:
        """把 Telegram 图片/引用整理成纯文本输入；chat 模型不接收多模态 payload。"""
        text = (text or "").strip()
        reply_context = self._format_telegram_reply_context(msg)
        nearby = "\n".join(part for part in (reply_context, text) if part).strip()
        image_blocks: list[str] = []
        current_desc = await self._describe_telegram_photo_sizes_for_chat(
            session_id,
            msg.get("photo"),
            source_label="用户发送的图片",
            nearby_text=nearby,
        )
        if current_desc:
            image_blocks.append(f"用户发送的图片: {current_desc}")
        reply = msg.get("reply_to_message") or {}
        if isinstance(reply, dict):
            reply_desc = await self._describe_telegram_photo_sizes_for_chat(
                session_id,
                reply.get("photo"),
                source_label="被回复消息里的图片",
                nearby_text=nearby,
            )
            if reply_desc:
                image_blocks.append(f"被回复消息里的图片: {reply_desc}")
        external = msg.get("external_reply") or {}
        if isinstance(external, dict):
            external_desc = await self._describe_telegram_photo_sizes_for_chat(
                session_id,
                external.get("photo"),
                source_label="外部引用消息里的图片",
                nearby_text=nearby,
            )
            if external_desc:
                image_blocks.append(f"外部引用消息里的图片: {external_desc}")

        blocks: list[str] = []
        if reply_context:
            blocks.append("【引用内容】\n" + reply_context)
        if image_blocks:
            blocks.append("【图片描述】\n" + "\n".join(image_blocks))
        if text:
            blocks.append("【用户当前输入】\n" + text)
        elif image_blocks:
            blocks.append("【用户当前输入】\n用户发送了一张图片。")
        return "\n\n".join(blocks).strip()
