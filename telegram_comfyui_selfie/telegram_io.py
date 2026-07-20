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
    def _caption_wait_seconds(self) -> float:
        try:
            raw = self.config.get("photo_caption_wait_seconds", 30)
            if raw is None or raw == "":
                raw = 30
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 30.0

    def _media_group_wait_seconds(self) -> float:
        try:
            raw = self.config.get("telegram_media_group_wait_seconds", 1.0)
            if raw is None or raw == "":
                raw = 1.0
            return max(0.05, float(raw))
        except (TypeError, ValueError):
            return 1.0

    def _interrupt_session_tasks(self, session_id: str, *, reason: str = "", exclude: asyncio.Task | None = None) -> int:
        tasks = getattr(self, "_interruptible_tasks", None)
        if not isinstance(tasks, dict) or not session_id:
            return 0
        current = exclude or asyncio.current_task()
        cancelled = 0
        bucket = tasks.get(session_id) or set()
        for task in list(bucket):
            if task is current or task.done():
                bucket.discard(task)
                continue
            task.cancel()
            cancelled += 1
        if not bucket:
            tasks.pop(session_id, None)
        if cancelled:
            self._ulog(session_id, "INTERRUPT", reason or f"取消旧任务 {cancelled} 个")
        return cancelled

    def _register_interruptible_task(self, session_id: str, task: asyncio.Task | None = None) -> None:
        if not session_id:
            return
        task = task or asyncio.current_task()
        if task is None:
            return
        tasks = getattr(self, "_interruptible_tasks", None)
        if not isinstance(tasks, dict):
            tasks = {}
            self._interruptible_tasks = tasks
        tasks.setdefault(session_id, set()).add(task)

    def _unregister_interruptible_task(self, session_id: str, task: asyncio.Task | None = None) -> None:
        task = task or asyncio.current_task()
        tasks = getattr(self, "_interruptible_tasks", None)
        if not isinstance(tasks, dict) or not session_id or task is None:
            return
        bucket = tasks.get(session_id)
        if not bucket:
            return
        bucket.discard(task)
        if not bucket:
            tasks.pop(session_id, None)

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

        if self._consume_pending_media_group_caption(session_id, msg, text):
            return

        if self._schedule_media_group_input(chat_id, session_id, msg, text):
            return

        if self._consume_pending_photo_caption(session_id, msg, text):
            return

        if self._should_wait_for_photo_caption(session_id, msg, text):
            cancelled = self._interrupt_session_tasks(session_id, reason="用户发送图片，取消旧的文字生成/发送任务")
            if cancelled:
                await asyncio.sleep(0)
            self._schedule_pending_photo_input(chat_id, session_id, msg)
            return

        current_task = asyncio.current_task()
        cancelled = self._interrupt_session_tasks(session_id, reason="用户发来新消息，取消旧的文字生成/发送任务", exclude=current_task)
        if cancelled:
            await asyncio.sleep(0)
        self._register_interruptible_task(session_id, current_task)
        try:
            await self._process_incoming_message(chat_id, session_id, msg, text)
        except asyncio.CancelledError:
            self._ulog(session_id, "INTERRUPT", "当前消息处理被新的用户输入打断")
            raise
        finally:
            self._unregister_interruptible_task(session_id, current_task)

    async def _process_incoming_message(self, chat_id: int | str, session_id: str, msg: dict[str, Any], text: str):
        """在角色操作锁内处理一次完整输入。

        调用方会先把当前任务注册为可中断任务，因此同会话新消息仍可取消正在生成的旧回复；
        旧任务取消后会在 finally 释放本锁，新消息随即接管，不会牺牲原有抢占语义。
        """
        op_lock = self.character_operation_lock(session_id) if hasattr(self, "character_operation_lock") else None
        acquired = False
        if op_lock is not None:
            try:
                await asyncio.wait_for(op_lock.acquire(), timeout=60)
                acquired = True
            except asyncio.TimeoutError:
                await self.send_message(chat_id, "正在切换角色/执行角色操作，请稍后再发一次。")
                return
        try:
            await self._process_incoming_message_locked(chat_id, session_id, msg, text)
        finally:
            if acquired:
                op_lock.release()

    async def _process_incoming_message_locked(self, chat_id: int | str, session_id: str, msg: dict[str, Any], text: str):
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ulog(session_id, "ERROR", f"处理消息异常: {exc}")
            logger.error("message handling failed: %s", exc, exc_info=True)
            await self.send_message(chat_id, f"发生异常: {exc}")

    def _should_wait_for_photo_caption(self, session_id: str, msg: dict[str, Any], text: str) -> bool:
        if text or not (msg.get("photo") or msg.get("_grouped_photos")):
            return False
        if msg.get("reply_to_message") or msg.get("external_reply") or msg.get("quote"):
            return False
        if not self.has_llm_config("vision", session_id):
            return False
        return self._caption_wait_seconds() > 0

    def _media_group_bucket(self) -> dict[str, dict[str, Any]]:
        bucket = getattr(self, "_pending_media_group_inputs", None)
        if not isinstance(bucket, dict):
            bucket = {}
            self._pending_media_group_inputs = bucket
        return bucket

    def _schedule_media_group_input(self, chat_id: int | str, session_id: str, msg: dict[str, Any], text: str) -> bool:
        group_id = str(msg.get("media_group_id") or "").strip()
        if not group_id or not msg.get("photo"):
            return False
        cancelled = self._interrupt_session_tasks(session_id, reason="用户发送图片组，取消旧的文字生成/发送任务")
        if cancelled:
            # 当前函数是同步入口；把取消交还事件循环即可，无需阻塞相册聚合。
            pass
        key = f"{session_id}\n{group_id}"
        bucket = self._media_group_bucket()
        entry = bucket.get(key)
        if not entry:
            entry = {
                "chat_id": chat_id,
                "session_id": session_id,
                "media_group_id": group_id,
                "messages": [],
                "caption_parts": [],
            }
            bucket[key] = entry
        entry["messages"].append(dict(msg))
        if text:
            entry["caption_parts"].append(text)
        old_task = entry.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        entry["task"] = asyncio.create_task(
            self._process_media_group_input_after_delay(key),
            name=f"media-group:{session_id}:{group_id}",
        )
        self._ulog(session_id, "PHOTO", f"收到相册图片 media_group_id={group_id} count={len(entry['messages'])}")
        return True

    def _consume_pending_media_group_caption(self, session_id: str, msg: dict[str, Any], text: str) -> bool:
        if not session_id or not text or msg.get("photo") or msg.get("caption"):
            return False
        if msg.get("reply_to_message") or msg.get("external_reply") or msg.get("quote"):
            return False
        bucket = getattr(self, "_pending_media_group_inputs", None)
        if not isinstance(bucket, dict):
            return False
        candidates = [
            entry for key, entry in bucket.items()
            if key.startswith(session_id + "\n") and isinstance(entry, dict)
        ]
        if not candidates:
            return False
        entry = candidates[-1]
        entry.setdefault("caption_parts", []).append(text)
        old_task = entry.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        key = f"{entry.get('session_id')}\n{entry.get('media_group_id')}"
        entry["task"] = asyncio.create_task(
            self._process_media_group_input_after_delay(key),
            name=f"media-group:{entry.get('session_id')}:{entry.get('media_group_id')}",
        )
        self._ulog(session_id, "PHOTO", "收到相册后续配文，合并后处理")
        return True

    async def _process_media_group_input_after_delay(self, key: str) -> None:
        entry: dict[str, Any] | None = None
        chat_id: int | str | None = None
        session_id = ""
        try:
            await asyncio.sleep(self._media_group_wait_seconds())
            bucket = self._media_group_bucket()
            entry = bucket.pop(key, None)
            if not entry:
                return
            chat_id = entry.get("chat_id")
            session_id = str(entry.get("session_id") or "")
            messages = [m for m in entry.get("messages") or [] if isinstance(m, dict) and m.get("photo")]
            if not messages:
                return
            messages.sort(key=lambda m: int(m.get("message_id") or 0))
            base = dict(messages[0])
            grouped_photos = [m.get("photo") for m in messages if m.get("photo")]
            if len(grouped_photos) > 5:
                self._ulog(session_id, "PHOTO", f"相册图片超过 5 张，仅保留前 5 张 media_group_id={entry.get('media_group_id')}")
            base["_grouped_photos"] = grouped_photos[:5]
            base["_media_group_message_count"] = len(base["_grouped_photos"])
            caption_parts = []
            seen: set[str] = set()
            for part in entry.get("caption_parts") or []:
                part = str(part or "").strip()
                if part and part not in seen:
                    seen.add(part)
                    caption_parts.append(part)
            caption = "\n".join(caption_parts).strip()
            chat_id = entry.get("chat_id")
            session_id = str(entry.get("session_id") or "")
            if self._should_wait_for_photo_caption(session_id, base, caption):
                self._schedule_pending_photo_input(chat_id, session_id, base)
                return
            current_task = asyncio.current_task()
            cancelled = self._interrupt_session_tasks(session_id, reason="处理相册图片，取消旧的文字生成/发送任务", exclude=current_task)
            if cancelled:
                await asyncio.sleep(0)
            self._register_interruptible_task(session_id, current_task)
            try:
                await self._process_incoming_message(chat_id, session_id, base, caption)
            finally:
                self._unregister_interruptible_task(session_id, current_task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                if session_id:
                    self._ulog(session_id, "ERROR", f"处理相册图片异常: {exc}")
                if chat_id is not None:
                    await self.send_message(chat_id, f"发生异常: {exc}")
            finally:
                logger.error("media group handling failed: %s", exc, exc_info=True)

    def _consume_pending_photo_caption(self, session_id: str, msg: dict[str, Any], text: str) -> bool:
        if not session_id or not text or msg.get("photo") or msg.get("caption"):
            return False
        if msg.get("reply_to_message") or msg.get("external_reply") or msg.get("quote"):
            return False
        pending = getattr(self, "_pending_photo_inputs", None)
        if not isinstance(pending, dict):
            return False
        entry = pending.get(session_id)
        if not entry:
            return False
        future = entry.get("future")
        if future and not future.done():
            future.set_result(text)
            self._ulog(session_id, "PHOTO", "收到图片后续配文，合并后处理")
            return True
        return False

    @staticmethod
    def _pending_photo_unit_count(messages: list[dict[str, Any]]) -> int:
        count = 0
        for msg in messages:
            grouped = msg.get("_grouped_photos")
            if isinstance(grouped, list) and grouped:
                count += len(grouped)
            elif msg.get("photo"):
                count += 1
        return count

    @classmethod
    def _pending_photo_grouped_message(cls, messages: list[dict[str, Any]]) -> dict[str, Any]:
        clean = [m for m in messages if isinstance(m, dict) and (m.get("photo") or m.get("_grouped_photos"))]
        if not clean:
            return {}
        photo_groups: list[Any] = []
        for msg in clean:
            grouped = msg.get("_grouped_photos")
            if isinstance(grouped, list) and grouped:
                photo_groups.extend(grouped)
            elif msg.get("photo"):
                photo_groups.append(msg.get("photo"))
            if len(photo_groups) >= 5:
                photo_groups = photo_groups[:5]
                break
        if len(photo_groups) <= 1 and not clean[0].get("_grouped_photos"):
            return dict(clean[0])
        base = dict(clean[0])
        base["_grouped_photos"] = photo_groups
        base["_media_group_message_count"] = len(photo_groups)
        if photo_groups:
            base["photo"] = photo_groups[0]
        return base

    def _schedule_pending_photo_input(self, chat_id: int | str, session_id: str, msg: dict[str, Any]) -> None:
        pending = getattr(self, "_pending_photo_inputs", None)
        if not isinstance(pending, dict):
            pending = {}
            self._pending_photo_inputs = pending
        old = pending.get(session_id)
        loop = asyncio.get_running_loop()
        messages = []
        future = None
        if old:
            old_task = old.get("task")
            if old_task and not old_task.done():
                old_task.cancel()
            if old.get("future") and not old["future"].done():
                future = old["future"]
                messages = list(old.get("messages") or [])
        if future is None:
            future = loop.create_future()
        unit_count = self._pending_photo_unit_count(messages)
        incoming_units = self._pending_photo_unit_count([msg])
        if unit_count >= 5:
            self._ulog(session_id, "PHOTO", "图片等待窗口已满 5 张，丢弃后续图片")
        elif unit_count + incoming_units > 5:
            messages.append(msg)
            self._ulog(session_id, "PHOTO", "图片等待窗口超过 5 张，仅保留前 5 张")
        else:
            messages.append(msg)
        task = asyncio.create_task(
            self._process_pending_photo_input(chat_id, session_id, future),
            name=f"pending-photo-caption:{session_id}",
        )
        pending[session_id] = {"future": future, "task": task, "messages": messages}
        self._ulog(
            session_id,
            "PHOTO",
            f"图片无配文，等待 {self._caption_wait_seconds():.1f}s 合并后续输入 count={min(5, self._pending_photo_unit_count(messages))}",
        )

    async def _process_pending_photo_input(
        self,
        chat_id: int | str,
        session_id: str,
        future: asyncio.Future,
    ) -> None:
        caption = ""
        try:
            try:
                caption = str(await asyncio.wait_for(future, timeout=self._caption_wait_seconds()) or "").strip()
            except asyncio.TimeoutError:
                caption = ""
            pending = getattr(self, "_pending_photo_inputs", {})
            entry = None
            if isinstance(pending, dict):
                entry = pending.get(session_id)
                if entry and entry.get("future") is future:
                    pending.pop(session_id, None)
            messages = list((entry or {}).get("messages") or [])
            msg = self._pending_photo_grouped_message(messages)
            if not msg:
                return
            current_task = asyncio.current_task()
            cancelled = self._interrupt_session_tasks(session_id, reason="处理等待配文后的图片，取消旧的文字生成/发送任务", exclude=current_task)
            if cancelled:
                await asyncio.sleep(0)
            self._register_interruptible_task(session_id, current_task)
            try:
                await self._process_incoming_message(chat_id, session_id, msg, caption)
            finally:
                self._unregister_interruptible_task(session_id, current_task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ulog(session_id, "ERROR", f"处理待配文图片异常: {exc}")
            logger.error("pending photo handling failed: %s", exc, exc_info=True)
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

    async def _download_telegram_photo_sizes(self, photo_sizes: Any) -> tuple[bytes, str] | None:
        photo = self._largest_photo_size(photo_sizes)
        file_id = str(photo.get("file_id") or "")
        if not file_id:
            return None
        if int(photo.get("file_size") or 0) > 20 * 1024 * 1024:
            return None
        return await self._download_telegram_file(file_id)

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
        downloaded = await self._download_telegram_photo_sizes(photo_sizes)
        if not downloaded:
            return ""
        image_bytes, mime_type = downloaded
        return await self._describe_image_for_chat(
            session_id,
            image_bytes,
            mime_type,
            source_label=source_label,
            nearby_text=nearby_text,
        )

    async def _describe_telegram_photo_groups_for_chat(
        self,
        session_id: str,
        photo_groups: Any,
        *,
        source_label: str,
        nearby_text: str = "",
    ) -> str:
        if not hasattr(self, "_describe_images_for_chat") or not self.has_llm_config("vision", session_id):
            return ""
        if not isinstance(photo_groups, list) or not photo_groups:
            return ""
        images: list[tuple[bytes, str]] = []
        for photo_sizes in photo_groups[:5]:
            downloaded = await self._download_telegram_photo_sizes(photo_sizes)
            if downloaded:
                images.append(downloaded)
        if not images:
            return ""
        if len(images) == 1:
            image_bytes, mime_type = images[0]
            return await self._describe_image_for_chat(
                session_id,
                image_bytes,
                mime_type,
                source_label=source_label,
                nearby_text=nearby_text,
            )
        return await self._describe_images_for_chat(
            session_id,
            images,
            source_label=source_label,
            nearby_text=nearby_text,
        )

    async def _augment_chat_text_from_message(self, session_id: str, text: str, msg: dict[str, Any]) -> str:
        """把 Telegram 图片/引用整理成纯文本输入；chat 模型不接收多模态 payload。"""
        text = (text or "").strip()
        reply_context = self._format_telegram_reply_context(msg)
        nearby = "\n".join(part for part in (reply_context, text) if part).strip()
        image_blocks: list[str] = []
        grouped = msg.get("_grouped_photos")
        if grouped:
            count = int(msg.get("_media_group_message_count") or len(grouped) or 0)
            current_desc = await self._describe_telegram_photo_groups_for_chat(
                session_id,
                grouped,
                source_label=f"用户发送的{count}张图片",
                nearby_text=nearby,
            )
        else:
            current_desc = await self._describe_telegram_photo_sizes_for_chat(
                session_id,
                msg.get("photo"),
                source_label="用户发送的图片",
                nearby_text=nearby,
            )
        if current_desc:
            label = "用户发送的多张图片" if grouped else "用户发送的图片"
            image_blocks.append(f"{label}: {current_desc}")
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
            fallback = "用户发送了多张图片。" if grouped else "用户发送了一张图片。"
            blocks.append("【用户当前输入】\n" + fallback)
        return "\n\n".join(blocks).strip()
