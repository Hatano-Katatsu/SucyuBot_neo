from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from . import appearance as appearance_rules
from . import generation as image_generation
from .defaults import DEFAULT_CONFIG, MENU_BODY, SCENES, WEEKDAY_NAMES
from .image_planning import VALID_VIEWS, plan_roleplay_image
from .memory import LongTermMemoryStore, format_memory_lines, normalize_kind

logger = logging.getLogger(__name__)

IMG_CALL_LEAK_RE = re.compile(
    r"\*{0,2}\s*[（(]\s*(?:调用|使用|call)?\s*`?generate_roleplay_image`?\s*[:：，,]?\s*(.*?)\s*[)）]\s*\*{0,2}",
    re.DOTALL | re.IGNORECASE,
)
IMG_NARRATION_LEAK_RE = re.compile(
    r"\*{0,2}\s*[（(]\s*[^（()）]*?(?:照片|画面|展示|呈现|出现在用户眼前)[^（()）]*?[:：]\s*(?:[^（()）]*?)[)）]\s*\*{0,2}",
    re.DOTALL,
)
FREQ_MAX_ROUNDS = {"极频繁": 2, "频繁": 3, "适度": 5, "偶尔": 8}
LONG_MEMORY_STABLE_CUE_RE = re.compile(
    r"(喜欢|偏好|偏爱|更喜欢|讨厌|不喜欢|不要|别|禁止|不希望|希望|以后|长期|一直|总是|通常|习惯|约定|边界|禁忌|称呼|记住|重要|更愿意|避免|关系|恋人|同居|女友|男友|伴侣)"
)
LONG_MEMORY_TRANSIENT_CUE_RE = re.compile(
    r"(当前|现在|今天|今晚|这次|本轮|刚才|刚刚|上一张|这张图|这张照片|正在|临时|这一次|此刻|时段|天气|星期|自拍|照片|画面)"
)
LONG_MEMORY_STRUCTURED_CUE_RE = re.compile(
    r"(当前角色|角色是|当前人设|人设是|身体特征|物种特征|positive_prefix|纯良度|纯度|地点|城市|时区|画风|当前外观|当前穿搭|临时外型|dynamic_appearance|每日推送)"
)
SHORT_CONTEXT_RESET_RE = re.compile(
    r"(换个话题|换话题|换一?个场景|新场景|下一幕|下一段|另起|说点别的|聊点别的|不说这个|先不说|不聊这个|别提这个|跳过这个|结束这个|这个话题到此|算了|重新开始|从头来|回到正题)"
)

# /人设重置、/角色 reset、/个性设置 reset 共用这一套清理逻辑和文案。
SESSION_CUSTOM_RESET_KEYS = (
    "custom_scheduled_persona", "custom_role_name", "custom_bot_name", "custom_bot_self_name",
    "custom_spatial_relationship", "custom_location", "custom_timezone_offset",
    "custom_positive_prefix", "custom_default_hair", "custom_default_eyes",
    "custom_current_style", "custom_character", "custom_series",
)
RESET_DONE_MSG = (
    "已恢复全局默认：本会话的人设、角色、身体特征、外型、称呼、地区时区、推送频率、纯良度覆盖，"
    "以及全部角色档案均已清空，并已重置对话上下文。\n"
    "下一句起将以默认人设回应。"
)


class TelegramComfyUIService:
    def __init__(self, config_path: str | Path = "data/config.json", state_path: str | Path = "data/state.json"):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.config = self._load_config()
        self.memory = LongTermMemoryStore(self._memory_db_path())
        self.sessions: dict[str, dict[str, Any]] = {}
        self._load_state()

        self.http: aiohttp.ClientSession | None = None
        self.comfy_session: aiohttp.ClientSession | None = None
        self._gen_lock = asyncio.Lock()
        self._generating = False
        self._active_pushes: set[str] = set()
        self._dirty_sessions: set[str] = set()
        self._last_state_write = 0.0
        self._state_write_interval = 30.0
        self._weather_caches: dict[str, dict[str, Any]] = {}
        self._skill_reference_cache: str | None = None
        self._bot_username = ""
        self._offset = 0
        self._bot_tasks: list[asyncio.Task] = []
        self._web_runner: Any = None
        self._stop_event: asyncio.Event | None = None

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------
    async def run(self):
        self._stop_event = asyncio.Event()
        if self.config.get("web_enabled", True):
            await self.start_web_console()

        if self.config.get("telegram_bot_token", ""):
            await self.start_bot()
        elif not self.config.get("web_enabled", True):
            raise RuntimeError("telegram_bot_token 未配置，请先复制 config.example.json 并填写 token")
        else:
            logger.warning("telegram_bot_token 未配置；仅启动本地 Web 控制台。")

        try:
            await self._stop_event.wait()
        finally:
            await self.stop_bot()
            await self.stop_web_console()
            await self.close()

    async def start_bot(self):
        if self.is_bot_running:
            return
        token = self.config.get("telegram_bot_token", "")
        if not token:
            raise RuntimeError("telegram_bot_token 未配置")
        timeout = aiohttp.ClientTimeout(total=620)
        self.http = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        me = await self.tg_api("getMe")
        self._bot_username = (me.get("result") or {}).get("username", "")
        self._bot_tasks = [
            asyncio.create_task(self.poll_loop(), name="telegram-poll-loop"),
            asyncio.create_task(self.scheduler_loop(), name="selfie-scheduler-loop"),
        ]
        logger.info("Telegram bot connected as @%s", self._bot_username or "?")

    async def stop_bot(self):
        for task in list(self._bot_tasks):
            if not task.done():
                task.cancel()
        for task in list(self._bot_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("bot task stopped with error: %s", exc)
        self._bot_tasks.clear()
        if self.http is not None and not self.http.closed:
            await self.http.close()
        self.http = None

    async def start_web_console(self):
        if self._web_runner is not None:
            return
        from aiohttp import web
        from .webui import create_web_app

        host = str(self.config.get("web_host", "127.0.0.1"))
        port = int(self.config.get("web_port", 8787))
        app = create_web_app(self)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._web_runner = runner
        logger.info("Web console listening on http://%s:%s", host, port)

    async def stop_web_console(self):
        runner = self._web_runner
        self._web_runner = None
        if runner is not None:
            await runner.cleanup()

    @property
    def is_bot_running(self) -> bool:
        return bool(self.http and not self.http.closed and self._bot_tasks and all(not task.done() for task in self._bot_tasks))

    async def close(self):
        self._flush_sessions(force=True)
        if self.comfy_session and not self.comfy_session.closed:
            await self.comfy_session.close()

    # ---------------------------------------------------------------------
    # Config / state
    # ---------------------------------------------------------------------
    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception as exc:
                raise RuntimeError(f"读取配置失败: {self.config_path}: {exc}") from exc
        else:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.warning("配置文件不存在，已生成默认配置: %s", self.config_path)
        return cfg

    def save_config(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _memory_db_path(self) -> Path:
        raw = str(self.config.get("long_memory_db_path") or "").strip()
        if not raw:
            return self.state_path.with_name("memory.sqlite3")
        path = Path(raw)
        if not path.is_absolute():
            path = self.config_path.parent / path
        return path

    def _load_state(self):
        self.sessions = {}
        if not self.state_path.exists():
            return
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            if not raw.strip():
                return
            data = json.loads(raw)
            sessions = data.get("sessions", {})
            if isinstance(sessions, dict):
                self.sessions = sessions
                logger.info("Loaded %d sessions from %s", len(self.sessions), self.state_path)
        except Exception as exc:
            logger.warning("加载状态失败，使用空状态: %s", exc)

    def _write_state(self):
        state = {
            "sessions": self.sessions,
            "character_registry": self._build_character_registry(),
            "location_registry": self._build_location_registry(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _mark_dirty(self, session_id: str):
        if session_id:
            self._dirty_sessions.add(session_id)

    def _flush_sessions(self, force=False):
        now = time.time()
        if not force and now - self._last_state_write < self._state_write_interval:
            return
        if not self._dirty_sessions and self.state_path.exists():
            return
        self._write_state()
        self._last_state_write = now
        self._dirty_sessions.clear()

    def _save_session_state(self, session_id: str, state: dict[str, Any]):
        if not session_id:
            return
        self.sessions[session_id] = state
        self._mark_dirty(session_id)
        self._flush_sessions(force=True)

    @staticmethod
    def session_id_for_chat(chat_id: int | str) -> str:
        return f"telegram:{chat_id}"

    @staticmethod
    def chat_id_from_session(session_id: str) -> int | str:
        raw = session_id.removeprefix("telegram:")
        try:
            return int(raw)
        except ValueError:
            return raw

    def _get_session_state(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            self.sessions[session_id] = {}
        state = self.sessions[session_id]
        defaults = {
            "last_interaction": time.time(),
            "last_morning_greet_date": "",
            "daily_trigger_times": [],
            "daily_trigger_date": "",
            "daily_triggered_times": [],
            "recent_message_history": [],
            "chat_history": [],
            "sent_photos_history": [],
            "dynamic_appearance": "",
            "replying_to_selfie": False,
            "last_sent_selfie_time": 0,
            "last_sent_selfie_caption": "",
            "last_sent_selfie_source_description": "",
            "last_sent_selfie_replied": False,
            "custom_scheduled_persona": "",
            "custom_role_name": "",
            "custom_bot_name": "",
            "custom_bot_self_name": "",
            "custom_spatial_relationship": "",
            "custom_location": "",
            "custom_timezone_offset": "",
            "custom_positive_prefix": "",
            "custom_default_hair": "",
            "custom_default_eyes": "",
            "custom_current_style": "",
            "custom_allow_llm_change_appearance": None,
            "custom_character": "",
            "custom_series": "",
            "persona_user_set": False,
            "saved_characters": {},
            "purity": None,
            "purity_user_set": False,
            "ntr_stage_reached": 0,
            "ntr_reconcile_count": 0,
            "rounds_since_image": 0,
            "short_context_start": 0,
            "short_context_reset_time": 0,
            "short_context_reset_reason": "",
        }
        for key, val in defaults.items():
            state.setdefault(key, val)
        return state

    def _get_session_cfg(self, session_id: str, key: str, default=None):
        if session_id:
            state = self._get_session_state(session_id)
            override = state.get(f"custom_{key}")
            if override not in (None, ""):
                return override
        return self.config.get(key, default)

    # ---------------------------------------------------------------------
    # Telegram API
    # ---------------------------------------------------------------------
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

        allowed = self.config.get("allowed_chat_ids") or []
        if allowed and str(chat_id) not in {str(x) for x in allowed}:
            logger.info("ignored chat_id not in allowlist: %s", chat_id)
            return

        session_id = self.session_id_for_chat(chat_id)
        cmd, arg = self.parse_command(text)
        try:
            if cmd is not None:
                await self.dispatch_command(chat_id, session_id, cmd, arg)
            else:
                await self.handle_chat(chat_id, session_id, text)
        except Exception as exc:
            logger.error("message handling failed: %s", exc, exc_info=True)
            await self.send_message(chat_id, f"发生异常: {exc}")

    def parse_command(self, text: str) -> tuple[str | None, str]:
        if text == "/":
            return "菜单", ""
        if not text.startswith("/"):
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

    # ---------------------------------------------------------------------
    # Message / command handling
    # ---------------------------------------------------------------------
    async def dispatch_command(self, chat_id: int | str, session_id: str, command: str, arg: str):
        aliases = {
            "start": "菜单",
            "help": "菜单",
            "menyu": "菜单",
            "外貌": "外型",
            "外形": "外型",
            "外貌自动": "外貌自动",
            "人设定义": "人格",
            "人设取消": "人设重置",
            "添加画风": "添加画风",
            "删除画风": "删除画风",
            "切换画风": "切换画风",
            "memory": "记忆",
            "remember": "记住",
            "forget": "忘记",
            "resetcontext": "新场景",
            "上下文重置": "新场景",
            "清空上下文": "新场景",
        }
        command = aliases.get(command, command)
        handlers = {
            "菜单": self.cmd_menu,
            "自拍": self.cmd_selfie,
            "天气": self.cmd_weather,
            "天气设置": self.cmd_set_location,
            "测试推送": self.cmd_test_push,
            "画风": self.cmd_style,
            "添加画风": self.cmd_add_style,
            "删除画风": self.cmd_del_style,
            "切换画风": self.cmd_switch_style,
            "turbo": self.cmd_turbo,
            "提示词": self.cmd_show_prompt,
            "生图状态": self.cmd_status,
            "测试生图": self.cmd_test,
            "人设查看": self.cmd_persona_show,
            "人格": self.cmd_persona_define,
            "人设重置": self.cmd_persona_cancel,
            "纯良度": self.cmd_purity,
            "推送频率": self.cmd_push_frequency,
            "角色": self.cmd_character,
            "个性设置": self.cmd_personalize,
            "外型": self.cmd_appearance,
            "外貌自动": self.cmd_auto_appearance,
            "记忆": self.cmd_memory,
            "记住": self.cmd_remember,
            "忘记": self.cmd_forget,
            "新场景": self.cmd_new_scene,
            "调度": self.cmd_sched,
            "管理": self.cmd_management,
        }
        handler = handlers.get(command)
        if not handler:
            await self.send_message(chat_id, f"未知命令: /{command}\n发送 /菜单 查看可用命令。")
            return
        await handler(chat_id, session_id, arg)

    async def handle_chat(self, chat_id: int | str, session_id: str, text: str):
        state = self._get_session_state(session_id)
        previous_interaction = state.get("last_interaction", 0)
        reset_reason = self._short_context_reset_reason(text, previous_interaction)
        self._touch(session_id)
        if reset_reason:
            self._reset_short_context(state, reset_reason)
        state["last_message_text"] = text
        state["last_message_time"] = time.time()
        state["recent_message_history"] = (state.get("recent_message_history", []) + [{"text": text, "time": time.time()}])[-5:]

        if state.get("last_sent_selfie_time", 0) and not state.get("last_sent_selfie_replied", False):
            if time.time() - state["last_sent_selfie_time"] < 12 * 3600:
                state["replying_to_selfie"] = True
            state["last_sent_selfie_replied"] = True

        state["rounds_since_image"] = state.get("rounds_since_image", 0) + 1
        if state.get("ntr_affection_reset"):
            self._tick_ntr_reconcile(state)

        self._save_session_state(session_id, state)

        if not self.has_llm_config("chat"):
            await self.send_message(chat_id, "聊天与角色扮演模型未配置，聊天和工具触发不可用。命令功能仍可使用。")
            return

        await self.send_action(chat_id, "typing")
        reply = await self.run_roleplay_chat(chat_id, session_id, text)
        if reply:
            await self.send_message(chat_id, reply)

    async def run_roleplay_chat(self, chat_id: int | str, session_id: str, user_text: str) -> str:
        state = self._get_session_state(session_id)
        messages = self._build_chat_messages(session_id, user_text)
        tools = self._chat_tools_schema()
        try:
            result = await self._call_llm_messages(
                messages,
                tools=tools,
                tool_choice="auto",
                tag="chat",
                purpose="chat",
                temp=float(self._get_llm_value("chat", "temperature", "0.9")),
            )
        except Exception as exc:
            return f"LLM 请求失败: {exc}"

        assistant = result.get("choices", [{}])[0].get("message", {})
        content = (assistant.get("content") or "").strip()
        tool_calls = assistant.get("tool_calls") or []

        if tool_calls:
            messages.append(assistant)
            for call in tool_calls:
                tool_result = await self._execute_tool_call(chat_id, session_id, call)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "tool"),
                    "content": tool_result,
                })
            try:
                final = await self._call_llm_messages(
                    messages,
                    tools=tools,
                    tool_choice="none",
                    tag="chat-final",
                    purpose="chat",
                    temp=float(self._get_llm_value("chat", "temperature", "0.9")),
                )
                final_msg = final.get("choices", [{}])[0].get("message", {})
                content = (final_msg.get("content") or content or "").strip()
            except Exception as exc:
                logger.warning("final chat completion after tool call failed: %s", exc)

        scene = self._handle_leaked_image_text(content)
        if scene:
            content = self._strip_leaked_image_text(content)
            asyncio.create_task(self._push_image_from_text(session_id, scene))
        else:
            content = self._strip_photo_memory_echo(content)

        history = state.get("chat_history", [])
        history.append({"role": "user", "content": user_text})
        if content:
            history.append({"role": "assistant", "content": content})
        state["chat_history"] = history[-50:]
        self._save_session_state(session_id, state)
        self._queue_long_memory_extraction(session_id, user_text, content)
        return content

    def _build_chat_messages(self, session_id: str, user_text: str) -> list[dict[str, Any]]:
        state = self._get_session_state(session_id)
        now = self._session_now(session_id)
        weekday = WEEKDAY_NAMES[now.weekday()]
        time_period = self._get_time_period(now.hour)
        persona = self._get_effective_persona(session_id)
        role_name = self._get_session_cfg(session_id, "role_name", "魅魔")
        bot_name = self._get_session_cfg(session_id, "bot_name", "蕾伊")
        bot_self_name = self._get_session_cfg(session_id, "bot_self_name", "我")
        if self._is_character_set(session_id):
            role_name = state.get("custom_role_name", "") or role_name
            bot_name = state.get("custom_bot_name", "") or bot_name
            bot_self_name = state.get("custom_bot_self_name", "") or bot_self_name

        freq = self.config.get("selfie_frequency", "频繁")
        freq_inst = {
            "极频繁": "原则上每 1 到 2 轮对话至少触发一次配图。",
            "频繁": "原则上每 2 到 3 轮对话触发一次配图。",
            "适度": "每 3 到 5 轮可触发一次配图。",
            "偶尔": "每 5 到 8 轮在精彩时刻触发配图。",
            "关闭": "本次对话中请勿触发配图。",
        }.get(freq, "原则上每 2 到 3 轮对话触发一次配图。")
        if self._image_nudge_due(freq, state.get("rounds_since_image", 0)):
            freq_inst += " 已有多轮未配图，本轮请优先调用 generate_roleplay_image。"

        system = (
            f"{persona}\n\n"
            f"你正在与用户进行{role_name}角色扮演。角色名参考是「{bot_name}」，不要强行把角色名当自称；"
            f"对话中优先使用「{bot_self_name}」作为自称。\n"
            f"当前时间: {now.strftime('%H:%M')} ({weekday}) {time_period}。\n"
            f"纯度指令: {self._purity_directive(self._get_purity(session_id))}\n"
            f"外貌修改权限: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}。\n"
            f"发图频率: {freq_inst}\n"
            "当用户明示或暗示想看你的样子、照片、穿着或当前场景时，应调用 generate_roleplay_image。"
            "工具调用只需要描述这张图要回应的对话意图、情绪和必要元素；"
            "最终画面会由生图辅助模型结合完整上下文整合。不要把工具名、函数调用或内部指令写进聊天文字。"
        )
        if state.get("dynamic_appearance"):
            system += f"\n当前附加外貌特征: {state['dynamic_appearance']}。"
        if state.get("replying_to_selfie"):
            source = state.get("last_sent_selfie_source_description") or state.get("last_sent_selfie_caption", "")
            system += f"\n用户这句话是在回应你刚才发出的画面，上一张图片的原始描述是: {source}"
            state["replying_to_selfie"] = False
        if state.get("short_context_start", 0):
            system += (
                "\n短期注意规则: 用户已经切换过话题或场景。切换点之前的聊天、地点、动作、服装、冲突和图片只作历史背景，"
                "不要主动带入当前场景；只有用户明确说继续刚才、上一张、那个话题时才引用。"
            )
        memory_context = self._long_term_memory_context(session_id, user_text)
        if memory_context:
            system += (
                "\n\n长期记忆（仅在相关时自然使用，不要逐条复述，不要暴露记忆系统）：\n"
                f"{memory_context}"
            )

        messages = [{"role": "system", "content": system}]
        self._inject_photo_history_messages(messages, state)
        messages.extend(self._active_chat_history(state, self._short_context_history_limit()))
        messages.append({"role": "user", "content": user_text})
        return messages

    def _chat_tools_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "generate_roleplay_image",
                    "description": "当需要用图片回应当前角色扮演对话时调用。你负责给出生图意图，最终画面由生图辅助模型结合上下文整合。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string", "description": "这张图要回应的对话意图，例如用户想看角色下班后在家等他的样子。"},
                            "mood": {"type": "string", "description": "图片应承载的情绪或关系推进，例如安抚、调情、撒娇、展示、挑逗。"},
                            "must_include": {"type": "string", "description": "用户明确要求必须出现的服装、动作、地点或物件；没有则留空。"},
                            "prompt": {"type": "string", "description": "可选的简短画面草案。不要写英文标签，生图辅助模型会重写。"},
                            "view": {"type": "string", "enum": ["selfie", "mirror", "pov", "third"], "description": "用户明确要求视角时填写；否则留空交给生图辅助模型判断。"},
                        },
                        "required": ["intent"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "roleplay_selfie",
                    "description": "生成一张随机自拍并带台词发送。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "change_appearance",
                    "description": "持续修改角色外貌、穿搭或配饰。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "mode": {"type": "string", "enum": ["merge", "replace"]},
                        },
                        "required": ["description"],
                    },
                },
            },
        ]

    async def _execute_tool_call(self, chat_id: int | str, session_id: str, call: dict[str, Any]) -> str:
        fn = (call.get("function") or {}).get("name", "")
        raw_args = (call.get("function") or {}).get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        if fn == "generate_roleplay_image":
            return await self.tool_generate_image(
                chat_id,
                session_id,
                prompt=args.get("prompt", ""),
                view=args.get("view", ""),
                intent=args.get("intent", ""),
                mood=args.get("mood", ""),
                must_include=args.get("must_include", ""),
            )
        if fn == "roleplay_selfie":
            return await self.tool_generate_selfie(chat_id, session_id)
        if fn == "change_appearance":
            return await self.tool_change_appearance(session_id, args.get("description", ""), args.get("mode", "merge"))
        return f"未知工具: {fn}"

    # ---------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------
    async def cmd_menu(self, chat_id, session_id, arg):
        await self.send_message(chat_id, "ComfyUI 自拍服务 - 命令菜单\n\n" + MENU_BODY)

    async def cmd_memory(self, chat_id, session_id, arg):
        text = (arg or "").strip()
        char = self._memory_character(session_id)
        if not text or text in ("查看", "list", "ls", "show"):
            memories = self.memory.list_memories(session_id, character=char, limit=15)
            if not memories:
                await self.send_message(chat_id, "当前会话还没有长期记忆。\n可用 /记住 内容 手动写入。")
                return
            await self.send_message(chat_id, "当前会话长期记忆:\n\n" + format_memory_lines(memories))
            return

        action, _, rest = text.partition(" ")
        action = action.strip().lower()
        rest = rest.strip()
        if action in ("搜索", "search", "find"):
            if not rest:
                await self.send_message(chat_id, "用法: /记忆 搜索 黑色吊带裙")
                return
            memories = self.memory.search_memories(session_id, rest, character=char, limit=15)
            if not memories:
                await self.send_message(chat_id, "没有找到相关长期记忆。")
                return
            await self.send_message(chat_id, f"搜索「{rest}」:\n\n" + format_memory_lines(memories))
            return

        if action in ("删除", "delete", "del", "remove"):
            if not rest.isdigit():
                await self.send_message(chat_id, "用法: /记忆 删除 12")
                return
            ok = self.memory.deactivate_memory(session_id, int(rest), character=char)
            await self.send_message(chat_id, "已删除。" if ok else "没有找到这条当前会话的有效记忆。")
            return

        if action in ("清空", "clear"):
            if rest != "确认":
                await self.send_message(chat_id, "这会删除当前会话全部长期记忆。确认请发送: /记忆 清空 确认")
                return
            n = self.memory.clear_session(session_id, character=char)
            await self.send_message(chat_id, f"已清空当前角色的长期记忆，共 {n} 条。")
            return

        if action in ("统计", "stats", "count"):
            await self.send_message(chat_id, f"当前角色有效长期记忆: {self.memory.count_active(session_id, character=char)} 条。")
            return

        await self.send_message(
            chat_id,
            "长期记忆用法:\n"
            "/记忆 查看\n"
            "/记忆 搜索 关键词\n"
            "/记忆 删除 ID\n"
            "/记忆 清空 确认\n"
            "/记住 内容\n"
            "/忘记 ID",
        )

    async def cmd_remember(self, chat_id, session_id, arg):
        text = (arg or "").strip()
        if not text:
            await self.send_message(chat_id, "用法: /记住 我喜欢你用温柔一点的语气")
            return
        kind_map = {
            "偏好": "preference",
            "资料": "profile",
            "关系": "relationship",
            "设定": "setting",
            "边界": "boundary",
            "外观": "visual",
            "事件": "event",
        }
        first, _, rest = text.partition(" ")
        kind = kind_map.get(first, "manual")
        summary = rest.strip() if kind != "manual" and rest.strip() else text
        memory_id = self.memory.add_memory(
            session_id, kind, summary,
            character=self._memory_character(session_id), importance=5, tags=["手动"], source="manual command",
        )
        await self.send_message(chat_id, f"已记住 #{memory_id}: {summary}")

    async def cmd_forget(self, chat_id, session_id, arg):
        text = (arg or "").strip()
        if not text:
            await self.send_message(chat_id, "用法: /忘记 12")
            return
        char = self._memory_character(session_id)
        if text.isdigit():
            ok = self.memory.deactivate_memory(session_id, int(text), character=char)
            await self.send_message(chat_id, "已忘记。" if ok else "没有找到这条当前角色的有效记忆。")
            return
        memories = self.memory.search_memories(session_id, text, character=char, limit=5)
        if not memories:
            await self.send_message(chat_id, "没有找到相关记忆。若要删除，请用 /记忆 查看 找到 ID 后发送 /忘记 ID。")
            return
        await self.send_message(chat_id, "找到这些可能相关的记忆，请用 /忘记 ID 删除:\n\n" + format_memory_lines(memories))

    async def cmd_new_scene(self, chat_id, session_id, arg):
        state = self._get_session_state(session_id)
        self._reset_short_context(state, "用户手动开启新短期场景")
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, "已开启新的短期场景。之后默认不会主动延续切换前的话题、画面和动作。")

    async def cmd_weather(self, chat_id, session_id, arg):
        city = arg.strip()
        w = await self._fetch_weather(city, session_id=session_id)
        if not w:
            await self.send_message(chat_id, "天气获取失败，请确认城市名称或稍后再试。")
            return
        show_city = city or self._get_session_cfg(session_id, "location", "上海")
        await self.send_message(
            chat_id,
            f"城市: {w.get('city', show_city)} ({show_city})\n"
            f"温度: {w['temp']} C\n"
            f"天气: {w['desc']}\n"
            f"恶劣天气: {'是' if self._is_bad_weather(w) else '否'}",
        )

    async def cmd_set_location(self, chat_id, session_id, arg):
        city = arg.strip()
        if not city:
            await self.send_message(chat_id, "用法: /天气设置 北京 或 /天气设置 Tokyo")
            return
        w = await self._fetch_weather(city, session_id=session_id)
        if not w:
            await self.send_message(chat_id, f"无法获取城市 {city} 的天气，设置失败。")
            return
        state = self._get_session_state(session_id)
        state["custom_location"] = city
        off = await self._resolve_city_timezone(city, w.get("lon"))
        note = ""
        if off is not None:
            state["custom_timezone_offset"] = str(off)
            note = f"\n已自动识别本会话时区为 UTC{off:+g}（忽略夏令时）"
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, f"本会话天气城市已设置为: {city}\n当前天气: {w['desc']}，{w['temp']} C{note}")

    async def cmd_selfie(self, chat_id, session_id, arg):
        if self._gen_lock.locked():
            await self.send_message(chat_id, "正在拍照中，请稍后再试。")
            return
        await self.send_action(chat_id, "upload_photo")
        state = self._get_session_state(session_id)
        now = self._session_now(session_id)
        w = await self._fetch_weather(session_id=session_id)
        weather = f"{w['desc']} {w['temp']} C" if w else "未知"
        time_period = self._get_time_period(now.hour)
        recent = self._get_recent_chat_history(state, session_id)
        scene, caption, new_app, view = await self._llm_write_scene(
            "normal", weather, WEEKDAY_NAMES[now.weekday()], time_period, recent, session_id
        )
        if not scene:
            scene, caption = random.choice(SCENES)
            view = "selfie"
        if new_app and self._allow_llm_change_appearance(session_id):
            state["dynamic_appearance"] = new_app
            self._save_session_state(session_id, state)
        english = await self._translate_to_tags(scene, session_id=session_id, view=view)
        ok, imgs, err = await self._do_generate(english, session_id=session_id)
        if not ok or not imgs:
            await self.send_message(chat_id, f"生图失败: {err}")
            return
        await self.send_photo(chat_id, imgs[0], caption or "")
        for extra in imgs[1:]:
            await self.send_photo(chat_id, extra)
        source = self._format_image_source_description(
            intent=f"自拍命令生成的 normal 模式画面，时段: {time_period}，天气: {weather}",
            prompt=recent or "",
        )
        self._record_sent_photo(
            session_id,
            scene,
            caption or "",
            appearance=state.get("dynamic_appearance", ""),
            view=view,
            source_description=source,
        )

    async def cmd_test_push(self, chat_id, session_id, arg):
        mode = (arg or "normal").strip() or "normal"
        await self.send_message(chat_id, f"正在强制触发 {mode} 模式推送。")
        asyncio.create_task(self._sched_fire(session_id, self._session_now(session_id), mode_override=mode, skip_active_check=True))

    async def cmd_style(self, chat_id, session_id, arg):
        sub = arg.strip()
        if not sub or sub.lower() in ("查看", "list", "ls", "show"):
            await self.send_message(chat_id, self._style_list_text(session_id))
            return
        parts = sub.split(None, 1)
        action = parts[0].lower()
        val = parts[1].strip() if len(parts) > 1 else ""
        if action in ("添加", "add"):
            await self.cmd_add_style(chat_id, session_id, val)
        elif action in ("删除", "del", "remove"):
            await self.cmd_del_style(chat_id, session_id, val)
        elif action in ("切换", "switch"):
            await self.cmd_switch_style(chat_id, session_id, val)
        elif sub.startswith("@"):
            await self.cmd_add_style(chat_id, session_id, sub)
        else:
            await self.cmd_switch_style(chat_id, session_id, sub)

    def _style_list_text(self, session_id: str) -> str:
        pool = self._normalize_style_pool()
        current = self._get_current_style(session_id)
        global_current = self.config.get("current_style", pool[0])
        lines = ["画风池:"]
        for i, style in enumerate(pool, 1):
            marks = []
            if style == current:
                marks.append("当前")
            if style == global_current:
                marks.append("全局")
            marker = " <- " + " / ".join(marks) if marks else ""
            lines.append(f"{i}. {style}{marker}")
        lines.append("\n用法: /画风 添加 @xxx | /画风 删除 序号 | /画风 切换 序号")
        return "\n".join(lines)

    async def cmd_add_style(self, chat_id, session_id, arg):
        style = arg.strip()
        if not style:
            await self.send_message(chat_id, "用法: /添加画风 @xxx")
            return
        pool = self._normalize_style_pool()
        if style in pool:
            await self.send_message(chat_id, f"{style} 已在池中。")
            return
        pool.append(style)
        self.config["style_pool"] = "\n".join(pool)
        self.config.setdefault("current_style", pool[0])
        self.save_config()
        await self.send_message(chat_id, f"已添加 {style}，当前池共 {len(pool)} 个。")

    async def cmd_del_style(self, chat_id, session_id, arg):
        pool = self._normalize_style_pool()
        target = arg.strip()
        if not target:
            await self.send_message(chat_id, "用法: /删除画风 序号 或 /删除画风 画风名")
            return
        if len(pool) <= 1:
            await self.send_message(chat_id, "画风池至少保留一个画风。")
            return
        removed = None
        try:
            idx = int(target) - 1
            if 0 <= idx < len(pool):
                removed = pool.pop(idx)
        except ValueError:
            pass
        if removed is None and target in pool:
            removed = target
            pool.remove(target)
        if removed is None:
            await self.send_message(chat_id, f"未找到 {target}")
            return
        self.config["style_pool"] = "\n".join(pool)
        if self.config.get("current_style") == removed:
            self.config["current_style"] = pool[0]
        for state in self.sessions.values():
            if state.get("custom_current_style") == removed:
                state["custom_current_style"] = ""
        self.save_config()
        self._write_state()
        await self.send_message(chat_id, f"已删除 {removed}。")

    async def cmd_switch_style(self, chat_id, session_id, arg):
        target = arg.strip()
        pool = self._normalize_style_pool()
        if not target or target.lower() in ("查看", "list", "ls", "show"):
            await self.send_message(chat_id, self._style_list_text(session_id))
            return
        if target.lower() in ("style reset", "clear", "默认", "全局"):
            state = self._get_session_state(session_id)
            state["custom_current_style"] = ""
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已清除当前会话画风覆盖，当前画风: {self._get_current_style(session_id)}")
            return
        chosen = None
        try:
            idx = int(target) - 1
            if 0 <= idx < len(pool):
                chosen = pool[idx]
        except ValueError:
            pass
        if chosen is None:
            matches = [s for s in pool if target.lower() in s.lower()]
            if len(matches) == 1:
                chosen = matches[0]
            elif len(matches) > 1:
                await self.send_message(chat_id, "匹配多个:\n" + "\n".join(f"{i+1}. {m}" for i, m in enumerate(matches)))
                return
        if not chosen:
            await self.send_message(chat_id, "未找到画风，用 /画风 查看全部。")
            return
        self._set_current_style(session_id, chosen)
        await self.send_message(chat_id, f"当前会话画风已切换到 {chosen}")

    async def cmd_turbo(self, chat_id, session_id, arg):
        val = arg.strip().lower()
        if val in ("on", "1", "开", "启用"):
            self.config["turbo_mode"] = True
            self.config["steps"] = "8"
            self.config["cfg"] = "2.5"
            self.save_config()
            await self.send_message(chat_id, "Turbo 模式已开启（8 steps / CFG 2.5）。")
        elif val in ("off", "0", "关", "禁用"):
            self.config["turbo_mode"] = False
            self.save_config()
            await self.send_message(chat_id, "Turbo 模式已关闭。")
        else:
            await self.send_message(chat_id, f"Turbo: {'开启' if self.config.get('turbo_mode') else '关闭'}\n强度: {self.config.get('turbo_strength', '0.6')}")

    async def cmd_show_prompt(self, chat_id, session_id, arg):
        pos, neg = self._build_prompt("{场景描述}", session_id=session_id)
        await self.send_message(
            chat_id,
            f"当前画风\n{self._get_current_style(session_id)}\n\n"
            f"角色设定\n{self._get_session_cfg(session_id, 'positive_prefix', '')[:300]}\n\n"
            f"示例 Positive\n{pos[:800]}\n\nNegative\n{neg[:500]}",
        )

    async def cmd_status(self, chat_id, session_id, arg):
        try:
            self._ensure_comfy_session()
            async with self.comfy_session.get(f"{self.comfyui_url}/system_stats") as resp:
                stats = await resp.json()
            sys = stats.get("system", {})
            await self.send_message(
                chat_id,
                f"ComfyUI {sys.get('comfyui_version', '?')}\n"
                f"RAM: {sys.get('ram_total', 0)//(1024**3)}GB (free {sys.get('ram_free', 0)//(1024**3)}GB)\n"
                f"{self.config.get('width')}x{self.config.get('height')} / {self.config.get('steps')} steps / {self.config.get('sampler')}",
            )
        except Exception as exc:
            await self.send_message(chat_id, f"无法连接 ComfyUI: {exc}")

    async def cmd_test(self, chat_id, session_id, arg):
        prompt = arg.strip()
        if not prompt:
            await self.send_message(chat_id, "用法: /测试生图 <prompt>")
            return
        await self.send_action(chat_id, "upload_photo")
        ok, imgs, err = await self._do_generate(prompt, session_id=session_id)
        if not ok or not imgs:
            await self.send_message(chat_id, f"生图失败: {err}")
            return
        for img in imgs:
            await self.send_photo(chat_id, img)

    async def cmd_persona_show(self, chat_id, session_id, arg):
        state = self._get_session_state(session_id)
        lines = ["当前会话个性化设置"]
        lines.append(f"人设: {state.get('custom_scheduled_persona') or '（默认）'}")
        ch = state.get("custom_character", "")
        if ch:
            lines.append(f"角色: {ch}{'（' + state.get('custom_series', '') + '）' if state.get('custom_series') else ''}")
        lines.append(f"身体特征: {self._get_session_cfg(session_id, 'positive_prefix', '')[:300] or '（未设置）'}")
        lines.append(f"画风: {self._get_current_style(session_id)}")
        if state.get("dynamic_appearance"):
            lines.append(f"外型覆盖: {state['dynamic_appearance']}")
        purity = self._get_purity(session_id)
        lines.append(f"纯良度: {purity}/10 | NTR 周期: {self._compute_ntr_threshold(purity)}天")
        lines.append(f"城市: {self._get_session_cfg(session_id, 'location', '上海')} | 时区: UTC{float(self._get_session_cfg(session_id, 'timezone_offset', '8')):+g}")
        await self.send_message(chat_id, "\n".join(lines))

    async def cmd_persona_define(self, chat_id, session_id, arg):
        text = arg.strip()
        if not text:
            await self.send_message(chat_id, f"当前人格:\n{self._get_effective_persona(session_id)}\n\n用法: /人格 <文本>")
            return
        state = self._get_session_state(session_id)
        state["custom_scheduled_persona"] = text
        state["persona_user_set"] = True
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, "人格已更新。")

    def _reset_session_customization(self, state: dict[str, Any]):
        """把单个会话恢复到"未设角色、走全局默认人设"的干净状态。

        清空所有 custom_* 覆盖、临时外型、人格/角色标记位、纯良度覆盖、整个角色档案池，
        并重置对话上下文。重置对话上下文是必须的：否则 chat_history 里旧角色口吻的历史
        发言会被重新喂回模型，导致即使系统提示已换成默认人设，模型仍沿用旧人设回应。
        """
        for key in SESSION_CUSTOM_RESET_KEYS:
            state[key] = ""
        state.pop("custom_daily_selfie_limit", None)
        state["custom_allow_llm_change_appearance"] = None
        state["dynamic_appearance"] = ""
        state["persona_user_set"] = False
        state["purity"] = None
        state["purity_user_set"] = False
        state["daily_trigger_date"] = ""  # 让随机推送计划按全局默认重新生成
        state["saved_characters"] = {}  # 清空本会话角色池
        self._clear_conversation_context(state)

    @staticmethod
    def _clear_conversation_context(state: dict[str, Any]):
        """清掉会带着旧人设/旧画面回流进提示词的对话上下文。"""
        state["chat_history"] = []
        state["recent_message_history"] = []
        state["sent_photos_history"] = []
        state["short_context_start"] = 0
        state["short_context_reset_time"] = 0
        state["short_context_reset_reason"] = ""
        state["replying_to_selfie"] = False
        state["last_sent_selfie_time"] = 0
        state["last_sent_selfie_caption"] = ""
        state["last_sent_selfie_source_description"] = ""
        state["last_sent_selfie_replied"] = False
        state["rounds_since_image"] = 0

    async def cmd_persona_cancel(self, chat_id, session_id, arg):
        state = self._get_session_state(session_id)
        self._reset_session_customization(state)
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, RESET_DONE_MSG)

    async def cmd_purity(self, chat_id, session_id, arg):
        text = arg.strip().lower()
        state = self._get_session_state(session_id)
        if not text:
            p = self._get_purity(session_id)
            src = "手动设定" if state.get("purity_user_set") else "自动/默认"
            await self.send_message(chat_id, f"当前纯良度: {p}/10（{src}）\nNTR 触发周期: {self._compute_ntr_threshold(p)}天\n用法: /纯良度 0~10 或 /纯良度 auto")
            return
        if text in ("auto", "默认", "reset", "自动"):
            state["purity"] = None
            state["purity_user_set"] = False
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已恢复自动/默认纯良度: {self._get_purity(session_id)}/10")
            return
        try:
            val = max(0, min(10, int(text)))
        except ValueError:
            await self.send_message(chat_id, "请输入 0~10 的整数，或 auto。")
            return
        state["purity"] = val
        state["purity_user_set"] = True
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, f"纯良度已设定为 {val}/10\nNTR 触发周期: {self._compute_ntr_threshold(val)}天")

    async def cmd_push_frequency(self, chat_id, session_id, arg):
        text = arg.strip().lower()
        state = self._get_session_state(session_id)

        def cur_limit():
            try:
                return int(str(self._get_session_cfg(session_id, "daily_selfie_limit", "3")).strip())
            except ValueError:
                return 3

        if not text:
            times = state.get("daily_trigger_times", [])
            plan = "已关闭随机推送" if cur_limit() == 0 else ("今日推送点: " + "、".join(times) if times else "今日推送点将在下一轮调度生成")
            await self.send_message(chat_id, f"每日主动推送次数: {cur_limit()} 次/天\n{plan}\n用法: /推送频率 <0~20> 或 /推送频率 默认")
            return
        if text in ("默认", "全局", "reset", "auto"):
            state.pop("custom_daily_selfie_limit", None)
            state["daily_trigger_date"] = ""
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已恢复全局默认: {cur_limit()} 次/天")
            return
        try:
            val = int(text)
            if val < 0 or val > 20:
                raise ValueError
        except ValueError:
            await self.send_message(chat_id, "请输入 0~20 的整数。")
            return
        state["custom_daily_selfie_limit"] = str(val)
        state["daily_trigger_date"] = ""
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, "已关闭本会话随机推送。" if val == 0 else f"每日主动推送次数已设为 {val} 次/天。")

    async def cmd_character(self, chat_id, session_id, arg):
        text = arg.strip()
        state = self._get_session_state(session_id)
        saved = state.setdefault("saved_characters", {})
        lower = text.lower()
        if not text or lower in ("查看", "show"):
            lines = ["当前角色设定"]
            lines.append(f"角色: {state.get('custom_character') or '（未设定）'}")
            if state.get("custom_series"):
                lines.append(f"作品: {state['custom_series']}")
            lines.append(f"人设: {(state.get('custom_scheduled_persona') or '（未设定）')[:300]}")
            lines.append(f"身体特征: {(state.get('custom_positive_prefix') or '（未设定）')[:300]}")
            lines.append(f"已保存角色: {', '.join(saved.keys()) or '无'}")
            lines.append("\n用法: /角色 <角色名> | /角色 load <名称> | /角色 list | /角色 delete <名称>")
            lines.append("/角色 reset 一键恢复全局默认（含角色池与对话） | /角色 clearup 仅清空角色池")
            await self.send_message(chat_id, "\n".join(lines))
            return
        if lower in ("reset", "clear", "重置", "恢复默认", "清除"):
            self._reset_session_customization(state)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, RESET_DONE_MSG)
            return
        parts = text.split(None, 1)
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if sub == "clearup":
            count = len(saved)
            saved.clear()
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已清空全部 {count} 个角色档案。")
            return
        if sub in ("list", "ls"):
            if not saved:
                await self.send_message(chat_id, "暂无已保存角色。")
                return
            await self.send_message(chat_id, "已保存角色\n" + "\n".join(f"{k}: {v.get('character', k)}" for k, v in saved.items()))
            return
        if sub == "load" and sub_arg:
            data = saved.get(sub_arg)
            if not data:
                await self.send_message(chat_id, f"未找到角色 {sub_arg}。")
                return
            switching = (data.get("character", "") or "") != (state.get("custom_character") or "")
            state["custom_character"] = data.get("character", "")
            state["custom_series"] = data.get("series", "")
            state["custom_scheduled_persona"] = data.get("persona", "")
            state["custom_positive_prefix"] = data.get("appearance", "")
            if data.get("purity") is not None and not state.get("purity_user_set"):
                state["purity"] = data.get("purity")
            if switching:
                self._clear_conversation_context(state)  # 换角色：避免上一个角色的对话/画面串味
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已载入角色 {sub_arg}。")
            return
        if sub == "delete" and sub_arg:
            if sub_arg not in saved:
                await self.send_message(chat_id, f"未找到角色 {sub_arg}。")
                return
            del saved[sub_arg]
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已删除角色 {sub_arg}。")
            return
        if sub in ("load", "delete"):
            await self.send_message(chat_id, f"用法: /角色 {sub} <名称>")
            return

        if not self.has_llm_config("image"):
            if "," in text or re.search(r"[a-zA-Z]{3,}", text):
                state["custom_positive_prefix"] = text
                self._save_session_state(session_id, state)
                await self.send_message(chat_id, "LLM 未配置，已按英文 tags 写入身体特征。")
            else:
                await self.send_message(chat_id, "LLM 未配置，无法自动分析角色。可直接输入英文 tags。")
            return

        try:
            result = await self._llm_classify_character(text)
        except Exception as exc:
            await self.send_message(chat_id, f"LLM 分析失败: {exc}")
            return
        if result.get("type") == "character":
            name = result.get("name", text)
            switching = name != (state.get("custom_character") or "")
            state["custom_character"] = name
            state["custom_series"] = result.get("series", "")
            state["custom_scheduled_persona"] = result.get("persona", "")
            state["custom_positive_prefix"] = result.get("appearance", "")
            if result.get("purity") is not None and not state.get("purity_user_set"):
                try:
                    state["purity"] = max(0, min(10, int(result["purity"])))
                except (TypeError, ValueError):
                    pass
            saved[name] = {
                "character": state["custom_character"],
                "series": state["custom_series"],
                "persona": state["custom_scheduled_persona"],
                "appearance": state["custom_positive_prefix"],
                "purity": state.get("purity"),
            }
            if switching:
                self._clear_conversation_context(state)  # 换角色：避免上一个角色的对话/画面串味
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已设定角色: {name}\n作品: {state['custom_series'] or '（未指定）'}\n人设: {state['custom_scheduled_persona'][:200]}\n身体特征: {state['custom_positive_prefix'][:250]}")
        elif result.get("type") == "appearance":
            state["custom_positive_prefix"] = result.get("tags", "")
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"身体特征已更新:\n{state['custom_positive_prefix']}")
        else:
            await self.send_message(chat_id, f"LLM 返回了无法识别的结果: {result}")

    async def cmd_personalize(self, chat_id, session_id, arg):
        state = self._get_session_state(session_id)
        parts = arg.split(None, 1) if arg else []
        action = parts[0] if parts else ""
        value = parts[1] if len(parts) > 1 else ""
        mapping = {
            "人格": "custom_scheduled_persona", "人设": "custom_scheduled_persona",
            "称呼": "custom_role_name", "角色类型": "custom_role_name",
            "角色名": "custom_bot_name", "名字": "custom_bot_name",
            "自称": "custom_bot_self_name", "关系": "custom_spatial_relationship",
        }
        if not action:
            lines = ["其余设置（空=使用全局默认）"]
            labels = [("custom_scheduled_persona", "人格文本"), ("custom_role_name", "角色类型"), ("custom_bot_name", "角色名"), ("custom_bot_self_name", "自称"), ("custom_spatial_relationship", "关系设定")]
            for key, label in labels:
                lines.append(f"{label}: {state.get(key, '') or '（默认）'}")
            lines.append("\n用法: /个性设置 <项> <值> 覆盖单项 | /个性设置 reset 一键恢复全局默认")
            await self.send_message(chat_id, "\n".join(lines))
            return
        if action == "reset":
            self._reset_session_customization(state)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, RESET_DONE_MSG)
            return
        key = mapping.get(action)
        if not key:
            await self.send_message(chat_id, f"未知设置项: {action}，可用: {', '.join(mapping.keys())}")
            return
        if not value:
            await self.send_message(chat_id, f"当前 {action}: {state.get(key, '') or '（默认）'}")
            return
        state[key] = value
        if key == "custom_scheduled_persona":
            # 与 /人格 保持一致：自定义人设即进入角色态。
            state["persona_user_set"] = True
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, f"{action} 已覆盖为: {value[:200]}")

    async def cmd_appearance(self, chat_id, session_id, arg):
        tags = arg.strip()
        state = self._get_session_state(session_id)
        if not tags:
            await self.send_message(
                chat_id,
                "当前外型设置\n"
                f"穿搭/配饰/临时发型瞳色: {state.get('dynamic_appearance') or '（默认）'}\n"
                f"物种特征: {(state.get('custom_positive_prefix') or '（默认）')[:200]}\n"
                f"默认发色: {state.get('custom_default_hair') or self.config.get('default_hair')}\n"
                f"默认瞳色: {state.get('custom_default_eyes') or self.config.get('default_eyes')}\n"
                f"模型自主改外型: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}\n\n"
                "用法: /外型 <标签> | /外型 特征 <标签> | /外型 发色 <标签> | /外型 瞳色 <标签> | /外型 自动变装 on/off | /外型 reset",
            )
            return
        parts = tags.split(None, 1)
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("特征", "traits"):
            if not sub_arg:
                await self.send_message(chat_id, f"当前物种特征: {state.get('custom_positive_prefix') or '（默认）'}")
                return
            if re.search(r"[\u4e00-\u9fff]", sub_arg):
                sub_arg = await self._translate_appearance_tags(sub_arg)
            state["custom_positive_prefix"] = sub_arg
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"物种特征已更新: {sub_arg[:300]}")
            return
        if sub in ("发色", "hair"):
            if not sub_arg:
                await self.send_message(chat_id, f"当前默认发色: {state.get('custom_default_hair') or self.config.get('default_hair')}")
                return
            if re.search(r"[\u4e00-\u9fff]", sub_arg):
                sub_arg = await self._translate_appearance_tags(sub_arg)
            state["custom_default_hair"] = sub_arg
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"默认发色已更新: {sub_arg}")
            return
        if sub in ("瞳色", "eyes"):
            if not sub_arg:
                await self.send_message(chat_id, f"当前默认瞳色: {state.get('custom_default_eyes') or self.config.get('default_eyes')}")
                return
            if re.search(r"[\u4e00-\u9fff]", sub_arg):
                sub_arg = await self._translate_appearance_tags(sub_arg)
            state["custom_default_eyes"] = sub_arg
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"默认瞳色已更新: {sub_arg}")
            return
        if sub in ("自动", "auto", "自动变装"):
            await self._set_auto_appearance(chat_id, session_id, sub_arg)
            return
        if tags.lower() in ("无", "clear", "重置", "reset", "none"):
            state["dynamic_appearance"] = ""
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "已重置为默认外型。")
            return
        new_tags = tags
        if re.search(r"[\u4e00-\u9fff]", tags):
            new_tags = await self._translate_appearance_tags(tags)
        state["dynamic_appearance"] = self._merge_appearance(state.get("dynamic_appearance", ""), new_tags)
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, f"外型已临时更改为: {state['dynamic_appearance']}")

    async def cmd_auto_appearance(self, chat_id, session_id, arg):
        await self._set_auto_appearance(chat_id, session_id, arg.strip())

    async def _set_auto_appearance(self, chat_id, session_id, val):
        state = self._get_session_state(session_id)
        val = (val or "").strip().lower()
        if val in ("on", "1", "开", "允许", "启用", "true", "yes"):
            state["custom_allow_llm_change_appearance"] = True
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "已允许模型自主修改外型。")
        elif val in ("off", "0", "关", "禁止", "禁用", "false", "no"):
            state["custom_allow_llm_change_appearance"] = False
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "已禁止模型自主修改外型。")
        elif val in ("reset", "clear", "默认", "全局"):
            state["custom_allow_llm_change_appearance"] = None
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已恢复全局设置: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}")
        else:
            await self.send_message(chat_id, f"当前模型自主改外型: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}\n用法: /外貌自动 on | off | reset")

    async def cmd_sched(self, chat_id, session_id, arg):
        now = self._session_now(session_id)
        state = self._get_session_state(session_id)
        last = state.get("last_interaction", 0)
        days = (time.time() - last) / 86400 if last else 0
        purity = self._get_purity(session_id)
        threshold = self._compute_ntr_threshold(purity)
        stage = self._compute_ntr_stage(days, threshold)
        names = {0: "无", 1: "不安(25%)", 2: "难受(50%)", 3: "幽怨(75%)", 4: "好感归零(90%)", 5: "背叛(100%)"}
        await self.send_message(
            chat_id,
            f"当前本地时间: {now.strftime('%H:%M')} (UTC{now.strftime('%z')})\n"
            f"今日触发日期: {state.get('daily_trigger_date') or '无'}\n"
            f"今日随机推送点: {', '.join(state.get('daily_trigger_times', [])) or '未生成'}\n"
            f"已完成随机推送点: {', '.join(state.get('daily_triggered_times', [])) or '无'}\n"
            f"今日早安推送: {'已发送' if state.get('last_morning_greet_date') == now.strftime('%Y-%m-%d') else '待发送'}\n"
            f"纯良度: {purity}/10 | NTR 触发周期: {threshold}天\n"
            f"NTR 阶段: {names.get(stage, '?')} | 已冷落: {days:.1f}天",
        )

    async def cmd_management(self, chat_id, session_id, arg):
        sub = arg.strip()
        if sub == "角色池":
            await self.send_message(chat_id, self._mgmt_characters())
        elif sub == "位置":
            await self.send_message(chat_id, self._mgmt_locations())
        elif sub == "会话":
            await self.send_message(chat_id, self._mgmt_sessions())
        elif sub:
            await self.send_message(chat_id, "未知管理面板，可用: 角色池、位置、会话")
        else:
            total_chars = sum(len(s.get("saved_characters", {})) for s in self.sessions.values())
            active = sum(1 for s in self.sessions.values() if s.get("last_interaction", 0) > time.time() - 86400)
            await self.send_message(
                chat_id,
                "管理仪表盘\n\n"
                f"活跃会话: {active} / 总会话数: {len(self.sessions)}\n"
                f"角色档案池: {total_chars} 个角色\n"
                f"ComfyUI: {self.config.get('comfyui_url')}\n"
                f"聊天模型: {self._get_llm_value('chat', 'model', '未配置')} @ {self._get_llm_value('chat', 'api_base', '未配置')}\n"
                f"生图辅助模型: {self._get_llm_value('image', 'model', '未配置')} @ {self._get_llm_value('image', 'api_base', '未配置')}\n"
                f"全局画风: {self.config.get('current_style')}\n"
                f"默认城市: {self.config.get('location')}\n\n"
                "可用子面板: /管理 角色池 | /管理 位置 | /管理 会话",
            )

    # ---------------------------------------------------------------------
    # Session-derived behavior
    # ---------------------------------------------------------------------
    @property
    def comfyui_url(self) -> str:
        return self.config.get("comfyui_url", "http://127.0.0.1:8188").rstrip("/")

    @property
    def _local_tz(self):
        return timezone(timedelta(hours=float(self.config.get("timezone_offset", "8.0"))))

    def _session_tz(self, session_id: str = ""):
        raw = self._get_session_cfg(session_id, "timezone_offset", self.config.get("timezone_offset", "8.0"))
        try:
            return timezone(timedelta(hours=float(raw)))
        except (TypeError, ValueError):
            return self._local_tz

    def _session_now(self, session_id: str = ""):
        return datetime.now(self._session_tz(session_id))

    @staticmethod
    def _offset_from_lon(lon):
        try:
            return float(round(float(lon) / 15))
        except (TypeError, ValueError):
            return None

    def _is_character_set(self, session_id: str) -> bool:
        state = self._get_session_state(session_id)
        return bool(state.get("custom_character") or state.get("persona_user_set"))

    def _get_purity(self, session_id: str) -> int:
        state = self._get_session_state(session_id) if session_id else {}
        if state.get("purity") is not None:
            return max(0, min(10, int(state["purity"])))
        default = self.config.get("default_purity", "")
        if default not in ("", None):
            try:
                return max(0, min(10, int(default)))
            except (TypeError, ValueError):
                pass
        return 1

    @staticmethod
    def _compute_ntr_threshold(purity: int) -> int:
        if purity <= 0:
            return 1
        if purity >= 10:
            return 99999
        return int(7 + (purity - 1) * (120 - 7) / 8)

    @staticmethod
    def _compute_ntr_stage(days_since: float, threshold_days: int) -> int:
        if threshold_days <= 0 or days_since <= 0:
            return 0
        ratio = days_since / threshold_days
        if ratio >= 1.0:
            return 5
        if ratio >= 0.9:
            return 4
        if ratio >= 0.75:
            return 3
        if ratio >= 0.5:
            return 2
        if ratio >= 0.25:
            return 1
        return 0

    def _get_effective_safety(self, session_id: str) -> dict[str, Any]:
        purity = self._get_purity(session_id)
        now = self._session_now(session_id)
        period = self._get_time_period(now.hour)
        effective = purity
        context = ""
        if period == "深夜":
            effective -= 3
            context = "深夜时段，氛围更私密暧昧"
        elif period == "傍晚":
            effective -= 1
            context = "傍晚时段，一天工作结束"
        elif period == "早晨":
            effective += 1
            context = "早晨时段，适合保持清新"
        if now.weekday() >= 5:
            effective -= 1
            context = (context + "；周末放松模式") if context else "周末放松模式"
        effective = max(0, min(10, effective))
        tag = "nsfw" if effective <= 2 else "safe" if effective >= 8 else None
        return {"level": effective, "tag": tag, "context": context}

    def _get_effective_persona(self, session_id: str) -> str:
        state = self._get_session_state(session_id)
        if self._is_character_set(session_id):
            base = state.get("custom_scheduled_persona", "")
        else:
            base = self._get_session_cfg(session_id, "scheduled_persona", DEFAULT_CONFIG["scheduled_persona"])
        if not base:
            # 兜底：角色态但人设被清空（半重置残留）时回退全局默认，绝不返回空人设。
            base = self.config.get("scheduled_persona") or DEFAULT_CONFIG["scheduled_persona"]
        additional = state.get("dynamic_appearance") or self.config.get("dynamic_appearance", "")
        return f"{base}\n\n[当前附加人设/短期穿搭与配饰: {additional}]" if additional else base

    def _allow_llm_change_appearance(self, session_id: str) -> bool:
        state = self._get_session_state(session_id)
        override = state.get("custom_allow_llm_change_appearance")
        if isinstance(override, bool):
            return override
        if isinstance(override, str) and override.strip():
            return override.strip().lower() in ("true", "1", "yes", "on", "开", "允许", "启用")
        return bool(self.config.get("allow_llm_change_appearance", True))

    def _normalize_style_pool(self) -> list[str]:
        raw = self.config.get("style_pool") or self.config.get("style_prefix") or "@00 gx4"
        if isinstance(raw, str):
            parts = re.split(r"[\n;；]+", raw)
        elif isinstance(raw, list):
            parts = raw
        else:
            parts = []
        pool, seen = [], set()
        for item in parts:
            style = str(item).strip()
            if style and style.lower() not in seen:
                pool.append(style)
                seen.add(style.lower())
        if not pool:
            pool = ["@00 gx4"]
        current = str(self.config.get("current_style", "")).strip()
        if current not in pool:
            self.config["current_style"] = pool[0]
        self.config["style_pool"] = "\n".join(pool)
        return pool

    def _get_current_style(self, session_id: str = "") -> str:
        pool = self._normalize_style_pool()
        if session_id:
            state = self._get_session_state(session_id)
            custom = str(state.get("custom_current_style", "")).strip()
            if custom in pool:
                return custom
            if custom:
                state["custom_current_style"] = ""
                self._mark_dirty(session_id)
        current = str(self.config.get("current_style", "")).strip()
        return current if current in pool else pool[0]

    def _set_current_style(self, session_id: str, style: str):
        if style not in self._normalize_style_pool():
            raise ValueError(f"未知画风: {style}")
        if session_id:
            state = self._get_session_state(session_id)
            state["custom_current_style"] = style
            self._save_session_state(session_id, state)
        else:
            self.config["current_style"] = style
            self.save_config()

    @staticmethod
    def _purity_directive(purity: int) -> str:
        if purity <= 0:
            return "你是纯粹的性欲化身：无条件且主动地发情，频繁发送挑逗自拍，也可能主动制造 NTR 危机感。"
        if purity <= 2:
            return "你对性始终保持开放，任意场合、任意时间都不设防，语言风格挑逗暧昧。"
        if purity <= 4:
            return "你多数情况下较为开放、乐于主动挑逗，但会视场合气氛适当收敛。"
        if purity == 5:
            return "在合适的私密空间你对亲密保持开放，在公开或不合适的场合保持得体。"
        if purity <= 7:
            return "只有在合适的私密场合和合适时间，氛围到位时你才会逐渐放开。"
        if purity <= 9:
            return "你性格保守，不轻易越界；只有在对方持续引导下才一点点打开心扉。"
        return "你是纯洁的天使，完全不涉及任何性相关内容，言行永远纯真 safe。"

    @staticmethod
    def _image_nudge_due(freq: str, rounds_since: int) -> bool:
        if freq == "关闭":
            return False
        return rounds_since >= FREQ_MAX_ROUNDS.get(freq, 5)

    def _short_context_history_limit(self) -> int:
        try:
            return max(4, min(40, int(self.config.get("short_context_history_limit", 16) or 16)))
        except Exception:
            return 16

    def _short_context_reset_reason(self, text: str, previous_interaction: float = 0) -> str:
        if SHORT_CONTEXT_RESET_RE.search(text or ""):
            return "用户显式切换或结束上一话题/场景"
        try:
            gap_hours = float(self.config.get("short_context_reset_gap_hours", "6") or 0)
        except Exception:
            gap_hours = 6
        if gap_hours > 0 and previous_interaction and time.time() - previous_interaction > gap_hours * 3600:
            return f"距离上次互动超过 {gap_hours:g} 小时，开启新的短期上下文"
        return ""

    @staticmethod
    def _reset_short_context(state: dict[str, Any], reason: str):
        state["short_context_start"] = len(state.get("chat_history", []))
        state["short_context_reset_time"] = time.time()
        state["short_context_reset_reason"] = reason
        state["recent_message_history"] = []

    @staticmethod
    def _active_chat_history(state: dict[str, Any], limit: int = 16) -> list[dict[str, Any]]:
        history = state.get("chat_history", [])
        try:
            start = int(state.get("short_context_start", 0) or 0)
        except Exception:
            start = 0
        if start < 0 or start > len(history):
            start = 0
        return history[start:][-limit:]

    @staticmethod
    def _get_time_period(hour: int) -> str:
        if 5 <= hour < 11:
            return "早晨"
        if 11 <= hour < 17:
            return "下午"
        if 17 <= hour < 21:
            return "傍晚"
        return "深夜"

    def _tick_ntr_reconcile(self, state: dict[str, Any]) -> bool:
        if not state.get("ntr_affection_reset"):
            return False
        cnt = state.get("ntr_reconcile_count", 0) + 1
        if cnt >= 5:
            state["ntr_affection_reset"] = False
            state["ntr_reconcile_count"] = 0
            return True
        state["ntr_reconcile_count"] = cnt
        return False

    def _touch(self, session_id: str):
        if session_id:
            state = self._get_session_state(session_id)
            state["last_interaction"] = time.time()
            state["ntr_stage_reached"] = 0
            self._save_session_state(session_id, state)

    @staticmethod
    def _format_image_source_description(intent: str = "", mood: str = "", must_include: str = "", prompt: str = "") -> str:
        parts = []
        for label, value in (
            ("意图", intent),
            ("情绪/关系推进", mood),
            ("必须包含", must_include),
            ("原始草案/上下文", prompt),
        ):
            value = (value or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        return "；".join(parts)

    def _record_sent_photo(
        self,
        session_id: str,
        scene: str,
        caption: str = "",
        appearance: str | None = None,
        view: str = "",
        source_description: str = "",
    ):
        state = self._get_session_state(session_id)
        history = state.get("sent_photos_history", [])
        source_description = (source_description or "").strip()
        history.append({
            "timestamp": time.time(),
            "scene": scene,
            "caption": caption,
            "appearance": appearance if appearance is not None else state.get("dynamic_appearance", ""),
            "view": (view or "").strip().lower(),
            "source_description": source_description,
        })
        state["sent_photos_history"] = history[-10:]
        state["last_sent_selfie_time"] = time.time()
        state["last_sent_selfie_caption"] = caption
        state["last_sent_selfie_source_description"] = source_description or scene
        state["last_sent_selfie_replied"] = False
        state["rounds_since_image"] = 0
        self._save_session_state(session_id, state)

    def _inject_photo_history_messages(self, messages: list[dict[str, Any]], state: dict[str, Any]):
        photos = state.get("sent_photos_history", [])
        if not photos:
            return
        existing = "\n".join(m.get("content", "") for m in state.get("chat_history", []) if isinstance(m.get("content"), str))
        now = time.time()
        reset_time = float(state.get("short_context_reset_time", 0) or 0)
        for photo in photos[-3:]:
            if now - photo.get("timestamp", 0) > 12 * 3600:
                continue
            if reset_time and photo.get("timestamp", 0) < reset_time:
                continue
            scene = photo.get("scene", "")
            if scene and scene in existing:
                continue
            content = f"*（你最近一次出现在用户眼前的样子：{scene}）*"
            source = (photo.get("source_description") or "").strip()
            if source and source != scene:
                content += f"\n这张图当时要回应的原始描述：{source}"
            messages.append({"role": "assistant", "content": content})

    def _get_recent_chat_history(self, state: dict[str, Any], session_id: str = "") -> str | None:
        recent = []
        now_ts = time.time()
        for msg in state.get("recent_message_history", []):
            if now_ts - msg.get("time", 0) < 3 * 3600:
                dt = datetime.fromtimestamp(msg["time"], self._session_tz(session_id))
                recent.append(f"[{dt.strftime('%H:%M')}] 用户: {msg.get('text', '')}")
        return "\n".join(recent) if recent else None

    def _long_memory_enabled(self) -> bool:
        value = self.config.get("long_memory_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def _long_memory_extract_enabled(self) -> bool:
        value = self.config.get("long_memory_extract_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def _long_memory_limit(self, default: int = 8) -> int:
        try:
            return max(1, min(20, int(self.config.get("long_memory_context_limit", default) or default)))
        except Exception:
            return default

    def _memory_character(self, session_id: str) -> str:
        """长期记忆的角色维度键：当前具名角色，未设角色（默认人设）用空串。

        所有读写都按此键隔离，换角色即换记忆空间，换回来记忆复原。
        """
        if not session_id:
            return ""
        return (self._get_session_state(session_id).get("custom_character") or "").strip()

    def _long_term_memory_context(self, session_id: str, query: str = "", limit: int | None = None) -> str:
        if not session_id or not self._long_memory_enabled():
            return ""
        memories = self.memory.context_memories(
            session_id, query, character=self._memory_character(session_id), limit=limit or self._long_memory_limit()
        )
        if not memories:
            return ""
        self.memory.mark_used([int(memory["id"]) for memory in memories])
        return format_memory_lines(memories, with_ids=False)

    def _long_memory_structured_boundary_text(self, session_id: str) -> str:
        state = self._get_session_state(session_id)
        fields = [
            ("当前角色", state.get("custom_character") or ""),
            ("当前作品", state.get("custom_series") or ""),
            ("当前人设", (state.get("custom_scheduled_persona") or "")[:120]),
            ("当前身体特征", (state.get("custom_positive_prefix") or "")[:120]),
            ("当前临时外型", state.get("dynamic_appearance") or ""),
            ("当前地点", self._get_session_cfg(session_id, "location", "")),
            ("当前时区", self._get_session_cfg(session_id, "timezone_offset", "")),
            ("当前画风", self._get_current_style(session_id)),
            ("当前纯良度", str(self._get_purity(session_id))),
            ("当前空间关系", self._get_session_cfg(session_id, "spatial_relationship", "")),
        ]
        return "\n".join(f"- {label}: {value}" for label, value in fields if str(value).strip())

    def _is_long_memory_in_scope(self, session_id: str, kind: str, summary: str, tags: Any = None) -> bool:
        kind = normalize_kind(kind)
        summary = (summary or "").strip()
        if not summary:
            return False
        if kind in ("manual", "correction", "boundary"):
            return True

        text = summary
        stable = bool(LONG_MEMORY_STABLE_CUE_RE.search(text))
        transient = bool(LONG_MEMORY_TRANSIENT_CUE_RE.search(text))
        structured = bool(LONG_MEMORY_STRUCTURED_CUE_RE.search(text))

        if structured and not stable:
            return False
        if transient and not stable and kind != "event":
            return False
        if kind == "visual" and transient and not stable:
            return False
        if kind in ("profile", "setting", "relationship") and not stable:
            state = self._get_session_state(session_id)
            current_values = [
                state.get("custom_character", ""),
                state.get("custom_series", ""),
                state.get("custom_scheduled_persona", ""),
                state.get("custom_positive_prefix", ""),
                state.get("dynamic_appearance", ""),
                self._get_session_cfg(session_id, "location", ""),
                self._get_current_style(session_id),
            ]
            for value in current_values:
                value = str(value or "").strip()
                if value and len(value) >= 2 and value in text:
                    return False

        tag_text = " ".join(str(tag) for tag in (tags or []))
        if re.search(r"(当前|临时|本轮|这次)", tag_text) and not stable:
            return False
        return True

    def _queue_long_memory_extraction(self, session_id: str, user_text: str, assistant_text: str):
        if not session_id or not self._long_memory_extract_enabled() or not self.has_llm_config("chat"):
            return
        if not (user_text or assistant_text):
            return
        asyncio.create_task(self._extract_long_term_memories(session_id, user_text, assistant_text))

    async def _extract_long_term_memories(self, session_id: str, user_text: str, assistant_text: str):
        existing = self._long_term_memory_context(session_id, f"{user_text}\n{assistant_text}", limit=10)
        structured = self._long_memory_structured_boundary_text(session_id)
        system = (
            "你是长期记忆提取器。请从一轮用户与角色的对话中提取值得长期保存的信息。\n"
            "只保存稳定偏好、明确设定、关系状态变化、重要事件、视觉/穿搭偏好、边界或禁忌。\n"
            "长期记忆不是第二套人设系统，不要保存已有结构化状态负责的内容。\n"
            "不要保存当前角色、当前人设、当前身体特征、当前地点/时区、当前纯良度、当前画风、当前临时穿搭或最近图片内容；"
            "除非用户明确表达了长期偏好、边界、约定、纠正或重要关系变化。\n"
            "不要保存普通寒暄、临时情绪、重复信息、无长期价值的台词。不要编造。\n"
            "如果已有相关记忆已经覆盖，不要重复输出。\n"
            "必须输出严格 JSON: {\"memories\":[{\"kind\":\"profile|preference|relationship|setting|boundary|visual|event|correction\","
            "\"summary\":\"一句中文记忆摘要\",\"importance\":1-5,\"tags\":[\"标签\"]}]}。没有值得保存的内容时 memories 为空数组。"
        )
        user = (
            f"当前结构化状态（不要作为长期记忆重复保存）:\n{structured or '无'}\n\n"
            f"已有相关记忆:\n{existing or '无'}\n\n"
            f"本轮对话:\n用户: {user_text}\n角色: {assistant_text or '（无文字回复）'}"
        )
        try:
            text = await self._call_llm(system, user, temp=0.1, tag="memory-extract", purpose="chat")
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
        except Exception as exc:
            logger.warning("long memory extraction failed: %s", exc)
            return
        memories = parsed.get("memories") if isinstance(parsed, dict) else None
        if not isinstance(memories, list):
            return
        source = f"用户: {user_text[:240]}\n角色: {(assistant_text or '')[:240]}"
        for item in memories[:8]:
            if not isinstance(item, dict):
                continue
            summary = (item.get("summary") or "").strip()
            if not summary:
                continue
            if not self._is_long_memory_in_scope(session_id, item.get("kind", "event"), summary, item.get("tags") or []):
                logger.info("skip out-of-scope long memory: %s", summary)
                continue
            self.memory.add_memory(
                session_id,
                item.get("kind", "event"),
                summary,
                character=self._memory_character(session_id),
                importance=item.get("importance", 3),
                tags=item.get("tags") or [],
                source=source,
            )

    def _build_character_registry(self) -> dict[str, Any]:
        registry = {}
        for sid, state in self.sessions.items():
            if state.get("saved_characters"):
                registry[sid] = state["saved_characters"]
        return registry

    def _build_location_registry(self) -> dict[str, Any]:
        registry = {}
        for sid, state in self.sessions.items():
            if state.get("custom_location") or state.get("custom_timezone_offset"):
                registry[sid] = {
                    "city": state.get("custom_location") or f"(全局: {self.config.get('location', '—')})",
                    "timezone": state.get("custom_timezone_offset") or f"(全局: {self.config.get('timezone_offset', '—')})",
                }
        return registry

    # ---------------------------------------------------------------------
    # Appearance / prompt
    # ---------------------------------------------------------------------
    def _load_keywords(self, key: str, defaults: list[str]) -> list[str]:
        return appearance_rules.load_keywords(self.config, key, defaults)

    @property
    def _outfit_kw(self):
        if not hasattr(self, "_cached_outfit_kw"):
            self._cached_outfit_kw = appearance_rules.outfit_keywords(self.config)
        return self._cached_outfit_kw

    @property
    def _accessory_kw(self):
        if not hasattr(self, "_cached_accessory_kw"):
            self._cached_accessory_kw = appearance_rules.accessory_keywords(self.config)
        return self._cached_accessory_kw

    def _parse_appearance(self, appearance: str) -> dict[str, list[str]]:
        return appearance_rules.parse_appearance(appearance, self._outfit_kw, self._accessory_kw)

    @staticmethod
    def _slots_to_string(slots: dict[str, list[str]]) -> str:
        return appearance_rules.slots_to_string(slots)

    @staticmethod
    def _remove_tag(text: str, tag: str) -> str:
        return appearance_rules.remove_tag(text, tag)

    def _merge_appearance(self, current_tags: str, new_tags: str, mode: str = "merge") -> str:
        return appearance_rules.merge_appearance(current_tags, new_tags, self._outfit_kw, self._accessory_kw, mode=mode)

    def _inject_appearance(self, char: str, session_id: str = "") -> str:
        return appearance_rules.inject_appearance(self, char, session_id)

    @staticmethod
    def _infer_gender_from_prefix(prefix: str) -> str:
        return appearance_rules.infer_gender_from_prefix(prefix)

    @staticmethod
    def _view_opener(view: str, gender: str = "girl") -> str:
        return image_generation.view_opener(view, gender)

    def _build_prompt(self, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[str, str]:
        return image_generation.build_prompt(self, scene_desc, is_ntr, session_id)

    def _build_workflow(self, positive: str, negative: str, seed: int) -> dict[str, Any]:
        return image_generation.build_workflow(self, positive, negative, seed)

    def _build_anima_workflow(self, positive: str, negative: str, seed: int) -> dict[str, Any]:
        return image_generation.build_anima_workflow(self, positive, negative, seed)

    # ---------------------------------------------------------------------
    # LLM / ComfyUI
    # ---------------------------------------------------------------------
    def _get_llm_value(self, purpose: str, name: str, default=None):
        prefix = "chat_llm" if purpose == "chat" else "image_llm"
        value = self.config.get(f"{prefix}_{name}")
        if value not in ("", None):
            return value
        legacy_map = {
            "api_base": "llm_api_base",
            "api_key": "llm_api_key",
            "model": "llm_model",
            "max_tokens": "llm_max_tokens",
            "disable_thinking": "llm_disable_thinking",
            "temperature": "llm_temperature_scene",
            "temperature_scene": "llm_temperature_scene",
            "temperature_translate": "llm_temperature_translate",
            "temperature_classify": "llm_temperature_classify",
        }
        legacy_key = legacy_map.get(name)
        if legacy_key:
            legacy_value = self.config.get(legacy_key)
            if legacy_value not in ("", None):
                return legacy_value
        return default

    def has_llm_config(self, purpose: str) -> bool:
        return bool(self._get_llm_value(purpose, "api_key", ""))

    async def _call_llm_messages(self, messages: list[dict[str, Any]], tools=None, tool_choice=None, tag: str = "", temp: float | None = None, purpose: str = "image") -> dict[str, Any]:
        api_base = (self._get_llm_value(purpose, "api_base", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1").rstrip("/")
        api_key = self._get_llm_value(purpose, "api_key", "")
        if not api_key:
            label = "聊天与角色扮演模型" if purpose == "chat" else "生图辅助模型"
            raise RuntimeError(f"{label} API Key 未配置")
        body = {
            "model": self._get_llm_value(purpose, "model", "deepseek-chat") or "deepseek-chat",
            "messages": messages,
            "max_tokens": int(self._get_llm_value(purpose, "max_tokens", "4096") or "4096"),
            "temperature": float(self._get_llm_value(purpose, "temperature", "0.95")) if temp is None else temp,
        }
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        disable = self._get_llm_value(purpose, "disable_thinking", False)
        if isinstance(disable, str):
            disable = disable.lower() in ("true", "1", "yes", "on")
        if disable:
            body["thinking"] = {"type": "disabled"}
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.post(
                f"{api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"LLM 请求失败: {resp.status} {await resp.text()}")
                return await resp.json()

    async def _call_llm(self, system: str, user: str, temp: float = 0.3, tag: str = "", purpose: str = "image") -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        data = await self._call_llm_messages(messages, tag=tag, temp=temp, purpose=purpose)
        msg = data.get("choices", [{}])[0].get("message", {})
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
        if not text:
            raise RuntimeError("LLM 返回空内容")
        return text

    async def _translate_to_tags(self, natural: str, session_id: str = "", view: str = "") -> str:
        if not self.has_llm_config("image"):
            return natural
        view = (view or "").strip().lower()
        if view not in VALID_VIEWS:
            view = ""
        char_prefix = self._get_session_cfg(session_id, "positive_prefix", "")
        opener = self._view_opener(view, self._infer_gender_from_prefix(char_prefix)) if view else ""
        if view:
            system = (
                "你是专业的 Anima3 提示词工程师。Anima3 支持英文自然语言与 danbooru 标签混编。"
                "把中文画面重构为一句英文自然语言画面描述，后接少量 danbooru 补强标签。"
                "不要输出 JSON、不要前缀、不要解释；不要压缩成纯标签列表。"
                "不要输出自拍/POV/镜子/手机/主语/1girl/1boy 等视角词，系统会统一添加。"
                "自然语言句子尽量不要使用逗号；重点保留动作、表情、姿态、服装、环境光线、空间关系和氛围。"
                "输出格式: English visual sentence. key tag, key tag, key tag"
            )
        else:
            system = (
                "你是专业的 Anima3 提示词工程师。Anima3 支持英文自然语言与 danbooru 标签混编。"
                "将中文场景描述重构为一句英文自然语言画面描述，后接少量 danbooru 补强标签。"
                "直接输出英文提示词，不要 JSON、不要解释，不要压缩成纯标签列表。"
                "根据物理距离判断自拍、对镜、POV 或第三人称视角。"
                "自然语言句子尽量不要使用逗号。输出格式: English visual sentence. key tag, key tag, key tag"
            )
        text = await self._call_llm(system, f"请翻译: {natural}", temp=float(self._get_llm_value("image", "temperature_translate", "0.3")), tag="translate", purpose="image")
        body = text.strip().strip(",")
        if opener:
            return opener if not body or body == natural else f"{opener}, {body}"
        return body

    async def _translate_appearance_tags(self, text: str) -> str:
        if not self.has_llm_config("image"):
            return text
        system = "你是 danbooru 标签翻译器。把中文外观、穿搭、发型、瞳色、配饰描述翻译成英文标签，逗号分隔。只输出标签。"
        try:
            return (await self._call_llm(system, text, temp=0.3, tag="appearance-translate", purpose="image")).strip() or text
        except Exception as exc:
            logger.warning("外观标签翻译失败: %s", exc)
            return text

    async def _llm_classify_character(self, user_text: str) -> dict[str, Any]:
        system = (
            "你是角色设定助手。判断用户描述属于既有作品角色或外观体貌特征，只输出 JSON。\n"
            "既有作品角色输出 {\"type\":\"character\",\"name\":\"角色名\",\"series\":\"作品名\",\"persona\":\"中文人设\",\"appearance\":\"英文prompt标签\",\"purity\":0到10整数}。\n"
            "外观描述输出 {\"type\":\"appearance\",\"tags\":\"英文prompt标签\"}。\n"
            "appearance 必须包含性别标签开头，例如 1girl 或 1boy。"
        )
        text = await self._call_llm(system, user_text, temp=float(self._get_llm_value("image", "temperature_classify", "0.1")), tag="classify", purpose="image")
        return json.loads(text)

    async def _llm_infer_timezone(self, city: str):
        if not city or not self.has_llm_config("image"):
            return None
        system = "根据城市名，只输出该城市 UTC 标准时区偏移小时数，忽略夏令时。例如 北京 8，东京 9，纽约 -5。只输出数字。"
        try:
            text = await self._call_llm(system, city, temp=0.0, tag="tz", purpose="image")
            m = re.search(r"-?\d+(?:\.\d+)?", text)
            if not m:
                return None
            val = float(m.group(0))
            return val if -12 <= val <= 14 else None
        except Exception:
            return None

    async def _resolve_city_timezone(self, city: str, lon=None):
        off = await self._llm_infer_timezone(city)
        return off if off is not None else self._offset_from_lon(lon)

    async def _llm_write_scene(self, mode, weather, weekday, time_period, recent_chat=None, session_id=""):
        if not self.has_llm_config("image"):
            return None, None, None, None
        persona = self._get_effective_persona(session_id)
        spatial = self._get_session_cfg(session_id, "spatial_relationship", DEFAULT_CONFIG["spatial_relationship"])
        bot_name = self._get_session_cfg(session_id, "bot_name", "蕾伊")
        bot_self_name = self._get_session_cfg(session_id, "bot_self_name", "我")
        role_name = self._get_session_cfg(session_id, "role_name", "魅魔")
        state = self._get_session_state(session_id)
        dynamic = state.get("dynamic_appearance") or self.config.get("dynamic_appearance", "")
        purity = self._get_purity(session_id)
        safety = self._get_effective_safety(session_id)
        quirk = self._get_session_cfg(session_id, "character_quirk_rule", "")

        system = (
            f"{persona}\n\n"
            f"角色身份: 角色名参考「{bot_name}」，角色类型「{role_name}」，优先使用「{bot_self_name}」作为自称。\n"
            f"当前附加外貌: {dynamic or '无'}\n"
            f"角色性观念: {self._purity_directive(purity)}\n"
            f"当前场合: {time_period}, {weekday}, {safety.get('context', '')}。\n"
            "你只需要构思发送给用户的画面，输出简短中文画面描述 scene 和一句中文台词 caption，不要输出英文画图标签。\n"
            "户外、办公室等公开场景必须穿着得体；深夜和私密场合可更放松。"
        )
        if quirk:
            system += f"\n角色专属画面修补规则: {quirk}"
        system += (
            "\n模式要求:\n"
            "morning: 必须使用 pov，刚睡醒、厨房或卧室早安场景。\n"
            f"normal: 根据默认物理空间设定（{spatial}）和近期对话判断，身处同一空间用 pov，异地或上班时段用 selfie/mirror。\n"
            f"ntr: 用户超过 {self._compute_ntr_threshold(purity)} 天没有互动时的冷落惩罚推送，强烈 NTR 危机感，通常 selfie 或分屏。\n"
            "必须输出严格 JSON: {\"scene\":\"...\",\"caption\":\"...\",\"view\":\"selfie|mirror|pov|third\"}。"
        )
        prompt = f"当前时段: {time_period}，星期: {weekday}，天气: {weather}，推送模式: {mode}。"
        if recent_chat:
            prompt += f"\n近期对话:\n{recent_chat}\n请呼应这些上下文。"
        proactive = self._allow_llm_change_appearance(session_id) and random.random() < 0.15
        if proactive:
            prompt += "\n本次可以惊喜换造型，并添加 new_appearance_tags 字段，英文标签，逗号分隔。"
        try:
            text = await self._call_llm(system, prompt, temp=float(self._get_llm_value("image", "temperature_scene", "0.95")), tag="scene", purpose="image")
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
            view = (parsed.get("view") or "").strip().lower()
            if view not in VALID_VIEWS:
                view = "pov" if mode == "morning" else "selfie"
            return parsed.get("scene"), parsed.get("caption") or "", parsed.get("new_appearance_tags"), view
        except Exception as exc:
            logger.error("LLM scene generation failed: %s", exc)
            return None, None, None, None

    def _ensure_comfy_session(self):
        image_generation.ensure_comfy_session(self)

    async def _do_generate(self, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate(self, scene_desc, is_ntr, session_id)

    async def _do_generate_locked(self, scene_desc: str, is_ntr: bool = False, session_id: str = "") -> tuple[bool, list[bytes], str]:
        return await image_generation.do_generate_locked(self, scene_desc, is_ntr, session_id)

    # ---------------------------------------------------------------------
    # Weather / scheduler / tools
    # ---------------------------------------------------------------------
    async def _fetch_weather(self, location: str = "", session_id: str = ""):
        if not location:
            now = time.time()
            loc = self._get_session_cfg(session_id, "location", self.config.get("location", "上海"))
            key = session_id or "__default__"
            cached = self._weather_caches.get(key)
            if cached and now - cached["ts"] < 1800:
                return cached["data"]
            location = loc
            cache_key = key
        else:
            cache_key = None
        try:
            encoded = urllib.parse.quote(location.strip())
            async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(f"https://wttr.in/{encoded}?format=j1&lang=zh-cn", headers={"User-Agent": "curl/7.81.0"}) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
            cur = data.get("current_condition", [{}])[0]
            desc = ""
            for key in ("lang_zh-cn", "lang_zh", "lang_zh_cn"):
                items = cur.get(key, [])
                if items:
                    desc = items[0].get("value", "")
                    break
            if not desc:
                desc = cur.get("weatherDesc", [{}])[0].get("value", "")
            nearest = data.get("nearest_area", [])
            city, lon = location, None
            if nearest:
                names = nearest[0].get("areaName", [])
                if names:
                    city = names[0].get("value", location)
                lon = nearest[0].get("longitude")
            weather = {"desc": desc, "code": cur.get("weatherCode", "0"), "temp": cur.get("temp_C", "?"), "city": city, "lon": lon}
            if cache_key:
                self._weather_caches[cache_key] = {"data": weather, "ts": time.time()}
            return weather
        except Exception as exc:
            logger.warning("weather fetch failed: %s", exc)
            return None

    @staticmethod
    def _is_bad_weather(w) -> bool:
        return bool(w and w.get("code", "0") in {"200", "299", "300", "399", "500", "599", "600", "699", "700", "799"})

    async def scheduler_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                for session_id in list(self.sessions.keys()):
                    state = self._get_session_state(session_id)
                    if self._is_recently_active(state):
                        continue
                    now = self._session_now(session_id)
                    today = now.strftime("%Y-%m-%d")
                    time_str = now.strftime("%H:%M")
                    if state.get("daily_trigger_date") != today:
                        try:
                            limit = int(str(self._get_session_cfg(session_id, "daily_selfie_limit", "3")).strip())
                        except ValueError:
                            limit = 3
                        times = []
                        if limit > 0:
                            start, end = 8 * 60 + 30, 23 * 60 + 50
                            slot = (end - start) / limit
                            for i in range(limit):
                                minute = random.randint(int(start + i * slot), int(start + (i + 1) * slot))
                                times.append(f"{minute // 60:02d}:{minute % 60:02d}")
                        state["daily_trigger_times"] = sorted(times)
                        state["daily_trigger_date"] = today
                        state["daily_triggered_times"] = []
                        self._mark_dirty(session_id)

                    if now.hour == 8 and now.minute < 5 and state.get("last_morning_greet_date") != today:
                        state["last_morning_greet_date"] = today
                        self._mark_dirty(session_id)
                        if not self._check_goodnight_inhibition(state) and session_id not in self._active_pushes:
                            asyncio.create_task(self._sched_fire(session_id, now, mode_override="morning"))

                    triggered = state.get("daily_triggered_times", [])
                    for t in state.get("daily_trigger_times", []):
                        if t <= time_str and t not in triggered:
                            triggered.append(t)
                            state["daily_triggered_times"] = triggered
                            self._mark_dirty(session_id)
                            t_min = int(t.split(":")[0]) * 60 + int(t.split(":")[1])
                            now_min = now.hour * 60 + now.minute
                            if now_min - t_min <= 5 and not self._check_goodnight_inhibition(state) and session_id not in self._active_pushes:
                                asyncio.create_task(self._sched_fire(session_id, now, mode_override="normal"))

                    await self._check_ntr_stage(session_id, state)
                self._flush_sessions(force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("scheduler error: %s", exc, exc_info=True)
            await asyncio.sleep(60)

    async def _check_ntr_stage(self, session_id: str, state: dict[str, Any]):
        last = state.get("last_interaction", 0)
        if not last:
            return
        days = (time.time() - last) / 86400
        threshold = self._compute_ntr_threshold(self._get_purity(session_id))
        current = self._compute_ntr_stage(days, threshold)
        reached = state.get("ntr_stage_reached", 0)
        if current <= reached:
            return
        for stage in range(reached + 1, current + 1):
            if stage <= 3:
                asyncio.create_task(self._fire_ntr_stage_message(session_id, stage, int(days)))
            elif stage == 4:
                state["ntr_affection_reset"] = True
                state["ntr_reconcile_count"] = 0
            elif stage == 5:
                logger.info("session %s reached NTR stage 5", session_id)
        state["ntr_stage_reached"] = current
        self._mark_dirty(session_id)

    async def _sched_fire(self, session_id: str, local_dt: datetime, mode_override=None, skip_active_check=False):
        if not session_id or (not skip_active_check and session_id in self._active_pushes):
            return
        self._active_pushes.add(session_id)
        chat_id = self.chat_id_from_session(session_id)
        try:
            state = self._get_session_state(session_id)
            if self._check_goodnight_inhibition(state):
                return
            if not skip_active_check and self._is_recently_active(state):
                return
            last = state.get("last_interaction", 0)
            purity = self._get_purity(session_id)
            mode = mode_override or "normal"
            if last and time.time() - last > self._compute_ntr_threshold(purity) * 86400:
                mode = "ntr"
            if mode == "normal" and purity <= 0 and random.random() < 0.4:
                mode = "ntr"
            w = await self._fetch_weather(session_id=session_id)
            weather = f"{w['desc']} {w['temp']} C" if w else "未知"
            recent = self._get_recent_chat_history(state, session_id)
            time_period = self._get_time_period(local_dt.hour)
            scene, caption, new_app, view = await self._llm_write_scene(mode, weather, WEEKDAY_NAMES[local_dt.weekday()], time_period, recent, session_id)
            if not scene:
                return
            if new_app and self._allow_llm_change_appearance(session_id):
                state["dynamic_appearance"] = new_app
                self._save_session_state(session_id, state)
            english = await self._translate_to_tags(scene, session_id=session_id, view=view)
            ok, imgs, err = await self._do_generate(english, is_ntr=(mode == "ntr"), session_id=session_id)
            if ok and imgs:
                await self.send_photo(chat_id, imgs[0], caption or "")
                source = self._format_image_source_description(
                    intent=f"{mode} 模式自动推送，时段: {time_period}，天气: {weather}",
                    prompt=recent or "",
                )
                self._record_sent_photo(session_id, scene, caption or "", view=view, source_description=source)
            else:
                logger.error("scheduled generate failed: %s", err)
        finally:
            self._active_pushes.discard(session_id)

    def _check_goodnight_inhibition(self, state: dict[str, Any]) -> bool:
        text = (state.get("last_message_text") or "").lower()
        ts = state.get("last_message_time", 0)
        return time.time() - ts < 3600 and any(word in text for word in ("晚安", "睡觉", "睡了", "去睡", "good night", "sleep"))

    @staticmethod
    def _is_recently_active(state: dict[str, Any]) -> bool:
        last = state.get("last_interaction", 0)
        return last > 0 and time.time() - last < 30 * 60

    async def _fire_ntr_stage_message(self, session_id: str, stage: int, days: int):
        persona = self._get_effective_persona(session_id)
        desc = {
            1: ("不安", "角色感到孤独和不安，开始担心用户是不是不在乎自己。"),
            2: ("难受", "角色越来越难受，开始怀疑自己的魅力。"),
            3: ("幽怨", "角色充满幽怨和不满，开始考虑是否放下。"),
        }.get(stage, ("不安", "角色感到不安。"))
        try:
            msg = await self._call_llm(
                f"你正在扮演以下角色:\n{persona}\n\n用户已经 {days} 天没有互动。现在状态: {desc[0]}。{desc[1]}",
                "用第一人称写 30-60 字角色台词，不要解释。",
                temp=float(self._get_llm_value("image", "temperature_scene", "0.95")),
                tag="ntr-stage",
                purpose="image",
            )
        except Exception:
            msg = {1: "已经好几天没和我说过话了……是不是我哪里做得不好？", 2: "又是没等到你的一天。你是不是已经忘了这里还有一个我。", 3: "如果你真的不在乎我，那我也该学着不等了。"}[stage]
        await self.send_message(self.chat_id_from_session(session_id), msg)

    async def tool_generate_image(
        self,
        chat_id,
        session_id: str,
        prompt: str = "",
        view: str = "",
        intent: str = "",
        mood: str = "",
        must_include: str = "",
    ) -> str:
        if not any((prompt, intent, must_include)):
            return "缺少图片意图"
        await self.send_action(chat_id, "upload_photo")
        source_description = self._format_image_source_description(
            intent=intent,
            mood=mood,
            must_include=must_include,
            prompt=prompt,
        )
        plan = await plan_roleplay_image(
            self,
            session_id,
            intent=intent,
            mood=mood,
            must_include=must_include,
            prompt=prompt,
            view=view,
        )
        scene = (plan.get("scene") or "").strip()
        if not scene:
            return "缺少图片意图"
        caption = (plan.get("caption") or "").strip()
        final_view = (plan.get("view") or "").strip()
        new_app = (plan.get("new_appearance_tags") or "").strip()
        state = self._get_session_state(session_id)
        if new_app and self._allow_llm_change_appearance(session_id):
            state["dynamic_appearance"] = new_app
            self._save_session_state(session_id, state)
        english = await self._translate_to_tags(scene, session_id=session_id, view=final_view)
        ok, imgs, err = await self._do_generate(english, session_id=session_id)
        if not ok or not imgs:
            return f"生图失败: {err}"
        await self.send_photo(chat_id, imgs[0], caption)
        self._record_sent_photo(
            session_id,
            scene,
            caption,
            appearance=state.get("dynamic_appearance", ""),
            view=final_view,
            source_description=source_description,
        )
        detail = f"图片已生成并发送。画面: {scene}"
        if caption:
            detail += f"；配文: {caption}"
        return detail

    async def tool_generate_selfie(self, chat_id, session_id: str) -> str:
        await self.cmd_selfie(chat_id, session_id, "")
        return "自拍已发送"

    async def tool_change_appearance(self, session_id: str, description: str = "", mode: str = "merge") -> str:
        state = self._get_session_state(session_id)
        if not self._allow_llm_change_appearance(session_id):
            return "当前会话已关闭模型自主修改外型，dynamic_appearance 未改变。"
        desc = (description or "").strip()
        if desc.lower() in ("reset", "none", "clear", "无", "", "重置", "恢复", "默认"):
            state["dynamic_appearance"] = ""
            self._save_session_state(session_id, state)
            return "外貌已重置为默认"
        if re.search(r"[a-zA-Z]{3,}", desc) and not re.search(r"[\u4e00-\u9fff]", desc):
            tags = desc
        else:
            tags = await self._translate_appearance_tags(desc)
        state["dynamic_appearance"] = self._merge_appearance(state.get("dynamic_appearance", ""), tags, mode=mode)
        self._save_session_state(session_id, state)
        return f"外貌已改变: {state['dynamic_appearance']}"

    async def _push_image_from_text(self, session_id: str, scene: str):
        chat_id = self.chat_id_from_session(session_id)
        try:
            english = await self._translate_to_tags(scene, session_id=session_id)
            ok, imgs, err = await self._do_generate(english, session_id=session_id)
            if ok and imgs:
                await self.send_photo(chat_id, imgs[0])
                self._record_sent_photo(
                    session_id,
                    scene,
                    "",
                    source_description=self._format_image_source_description(intent="聊天模型文字中泄漏出的配图描述", prompt=scene),
                )
            else:
                logger.error("leak fallback generate failed: %s", err)
        except Exception as exc:
            logger.error("leak fallback failed: %s", exc, exc_info=True)

    @staticmethod
    def _handle_leaked_image_text(text: str) -> str | None:
        if not text or "generate_roleplay_image" not in text:
            return None
        match = IMG_CALL_LEAK_RE.search(text)
        if not match:
            return None
        scene = (match.group(1) or "").strip()
        scene = re.sub(r"^\s*(画面中你|画面|场景|内容|prompt)\s*[:：]\s*", "", scene, flags=re.IGNORECASE).strip()
        return scene or None

    @staticmethod
    def _strip_leaked_image_text(text: str) -> str:
        return IMG_CALL_LEAK_RE.sub("", text or "").strip()

    @staticmethod
    def _strip_photo_memory_echo(text: str) -> str:
        if not text or not any(k in text for k in ("照片", "画面", "展示", "呈现", "出现在用户眼前")):
            return text
        return IMG_NARRATION_LEAK_RE.sub("", text).strip()

    def _mgmt_characters(self) -> str:
        lines, total = ["角色档案池", ""], 0
        for sid, state in self.sessions.items():
            saved = state.get("saved_characters", {})
            if not saved:
                continue
            lines.append(f"会话: {sid}")
            for key, data in saved.items():
                total += 1
                lines.append(f"  - {key}: {data.get('character', key)} {('(' + data.get('series', '') + ')') if data.get('series') else ''}")
        if total == 0:
            lines.append("暂无已保存角色档案。")
        lines.append(f"\n共 {total} 个角色。")
        return "\n".join(lines)

    def _mgmt_locations(self) -> str:
        lines = [f"地区设定总览\n全局默认城市: {self.config.get('location')} | 时区: UTC+{self.config.get('timezone_offset')}", ""]
        found = False
        for sid, state in self.sessions.items():
            if state.get("custom_location") or state.get("custom_timezone_offset"):
                found = True
                lines.append(f"{sid}: {state.get('custom_location') or '(全局)'} | UTC{state.get('custom_timezone_offset') or self.config.get('timezone_offset')}")
        if not found:
            lines.append("所有会话均使用全局默认地区设置。")
        return "\n".join(lines)

    def _mgmt_sessions(self) -> str:
        lines = ["会话概况", ""]
        if not self.sessions:
            return "会话概况\n\n暂无会话记录。"
        for sid, state in self.sessions.items():
            last = state.get("last_interaction", 0)
            ago = "无记录"
            if last:
                sec = time.time() - last
                ago = f"{int(sec // 60)}分钟前" if sec < 3600 else f"{int(sec // 3600)}小时前" if sec < 86400 else f"{int(sec // 86400)}天前"
            push = f"{len(state.get('daily_triggered_times', []))}/{len(state.get('daily_trigger_times', []))}" if state.get("daily_trigger_times") else "关闭"
            lines.append(f"{sid}\n  角色: {state.get('custom_character') or '(未设定)'} | 纯良度: {self._get_purity(sid)}/10\n  上次互动: {ago} | 今日推送: {push}")
        return "\n".join(lines)
