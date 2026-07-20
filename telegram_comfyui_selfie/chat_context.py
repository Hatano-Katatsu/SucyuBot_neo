from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import time
from typing import Any

from . import session_schema
from .defaults import WEEKDAY_NAMES
from .memory import format_memory_lines

logger = logging.getLogger(__name__)

FREQ_MAX_ROUNDS = {"极频繁": 2, "频繁": 3, "适度": 5, "偶尔": 8}
# 各档位的最小配图间隔（轮）：发图后至少留白这么多轮才允许下一张主动配图。
# 用户明确开口要图时不受此约束（见 _user_requested_image / _should_block_chat_image）。
FREQ_MIN_GAP = {"极频繁": 1, "频繁": 2, "适度": 3, "偶尔": 5}
# 用户“明确想看图”的意图检测：命中则即便在冷却期内也放行配图。
# 只是冷却期的放行旁路；漏判最多让用户等满冷却或改用 /自拍，故偏向覆盖常见说法、控制误判。
IMAGE_REQUEST_RE = re.compile(
    r"(自拍|selfie|\bpic\b|\bphoto\b|"
    r"拍(?:一|两|几)?[张个]|拍照|拍来|"
    r"看看?你|瞧瞧你|想看你|让我(?:看|瞧)(?:看|瞧)?|给我?(?:看|瞧)(?:看|瞧)?|"
    r"看你(?:的|现在|此刻|那边|那儿|长什么|长啥)|看(?:一)?下你|"
    r"发(?:我)?(?:张|个|一张|几张|一下)?(?:图|照|照片|自拍)|来(?:张|个|一张)(?:图|照|照片)?|"
    r"照片|图片|什么样[子貌]|啥样[子貌]?|长(?:什么|啥)样|(?:现在|此刻|这会儿)的样[子貌]|"
    r"你的(?:样子|穿搭|打扮|照片)|镜子里|对镜|你那(?:边|儿)(?:啥|什么|怎))",
    re.IGNORECASE,
)
CHAT_WORLD_TRIGGER_RE = re.compile(
    r"(在哪|哪里|位置|地点|见面|过来|过去|出门|回家|到家|在家|路上|街上|"
    r"(?:要|想|准备|现在|马上|一会儿|今晚|明天|今天)?(?:去|回到|来到|抵达|离开)(?:家|公司|学校|餐厅|咖啡|商场|车站|地铁|机场|公园|医院|酒店|图书馆|电影院|影院|便利店|超市|海边|你那|我这|那里|这里)|"
    r"公司|学校|餐厅|咖啡|商场|车站|地铁|机场|"
    r"公园|医院|酒店|图书馆|电影院|影院|便利店|超市|天气|下雨|下雪|雨天|雪天|"
    r"冷吗|热吗|温度|刮风|有风|大风|起雾|天黑|天亮|日落|夕阳|光线|几点|"
    r"拍照|自拍|照片|图片|配图|画图|绘图|生图|发图|镜头|photo|pic|selfie|"
    r"weather|rain|snow|cold|hot|where|location|arrive|home|office|school|restaurant|cafe|mall|station|airport|hotel)",
    re.IGNORECASE,
)
IMAGE_JUDGE_TRIGGER_RE = re.compile(
    r"(照片|图片|自拍|配图|画图|绘图|生图|发图|镜头|拍照|看看|看你|样子|"
    r"穿|换|脱|戴|摘|裙|衣|外套|眼镜|头发|发色|妆|表情|脸红|坐|躺|站|跪|"
    r"抱|靠|贴|凑|牵|亲|吻|摸|腿|脚|肩|怀里|身后|床|被子|枕头|晚安|睡|沙发|浴室|镜子|窗|"
    r"雨|雪|阳光|夜景|photo|pic|selfie|look|wear|dress|pose|camera)",
    re.IGNORECASE,
)
SHORT_CONTEXT_RESET_RE = re.compile(
    r"(换个话题|换话题|换一?个场景|新场景|下一幕|下一段|另起|说点别的|聊点别的|不说这个|先不说|不聊这个|别提这个|跳过这个|结束这个|这个话题到此|算了|重新开始|从头来|回到正题)"
)
DSML_MARK = r"[|｜]{2}DSML[|｜]{2}"
DSML_TOOL_BLOCK_RE = re.compile(rf"<{DSML_MARK}tool_calls\b[^>]*>.*?</{DSML_MARK}tool_calls>", re.IGNORECASE | re.DOTALL)
DSML_INVOKE_RE = re.compile(rf"<{DSML_MARK}invoke\b(?P<attrs>[^>]*)>(?P<body>.*?)</{DSML_MARK}invoke>", re.IGNORECASE | re.DOTALL)
DSML_PARAMETER_RE = re.compile(rf"<{DSML_MARK}parameter\b(?P<attrs>[^>]*)>(?P<body>.*?)</{DSML_MARK}parameter>", re.IGNORECASE | re.DOTALL)
DSML_ATTR_RE = re.compile(r"""([A-Za-z_][\w.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
DSML_ANY_TAG_RE = re.compile(rf"</?{DSML_MARK}[^>]*>", re.IGNORECASE | re.DOTALL)
JUDGE_VALID_VIEWS = {"selfie", "mirror", "pov", "third", "portrait"}
JUDGE_EXPLICIT_SELF_CAMERA_RE = re.compile(
    r"(自拍|selfie|对镜|镜子|mirror|前摄|前置|拿着手机|举着手机|手机自拍|自拍分享|自拍道晚安|录视频|录像)",
    re.IGNORECASE,
)
JUDGE_EXPLICIT_PORTRAIT_RE = re.compile(
    r"(帮(?:我|她|忙)?拍|给(?:我|她)拍|替(?:我|她)拍|让我拍|请你拍|请人拍|摆拍|拍一张照片|拍张照片|来一张照片|再拍一张|再来一张)",
    re.IGNORECASE,
)

CHAT_INTIMATE_LANGUAGE_RULES = (
    "\n文爱/性爱语言规则（仅在明确进入文爱、性爱、插入、抽插、高潮或同等性行为描写时启用；"
    "普通调情、拥抱、亲吻、日常亲密不要套用本段）：核心目标是让台词像身体反应的一部分，不像写好的剧本。"
    "台词是呼吸和身体反应的延伸，不是旁白分析；越兴奋，语言越破碎；能用拟声词或动作表达的，不要用完整句子。"
    "语言密度：挑逗/前戏台词总量不超过40字，进入/前中期不超过25字，激烈抽插不超过15字，"
    "高潮前/高潮中不写完整句，只保留拟声词、叫喊、单个词。挑逗阶段单句不超过20字，进入之后单句不超过12字，"
    "超过就用喘息、拟声词或动作打断。"
    "兴奋度破碎度必须匹配当前阶段：1挑逗可用完整短句和轻调侃；2前戏句子缩短并插入拟声；"
    "3进入瞬间只用极短句；4抽插中只剩单词、短语、喘息和拟声；5高潮前几乎无完整语言；"
    "6高潮后才用喘息加短句逐步恢复。禁止在抽插中使用挑逗阶段的完整调侃句。"
    "台词只做三类事：引导/回应（如别停、慢点、就是那里）、感受陈述（好深、蹭到了、太紧了）、"
    "情绪表达（受不了了、还要）。每轮只做一件事，不要同时塞入调侃、分析、宣告和反问。"
    "节奏优先是「说 -> 停 -> 动 -> 说」，被爽到或意外刺激时先写身体反应和短暂失语，再考虑极短台词。"
    "回复结构要不规则化，并参考上一轮避免连续重复：可用纯动作+拟声、短台词->动作打断->拟声、"
    "动作->单词->动作->拟声、一句台词+动作收尾、只有拟声词+身体反应。"
    "拟声词优先于解释，每轮至少1个拟声词，激烈阶段至少2个；可用咕啾、咕叽、噗嗤、噗滋、滋溜、啵、啪、啪啪、"
    "啪啪啪、哈啊、嘶——、呼……、嗯、啊、唔、咿、啊啊啊啊——等，拟声词可以单独成句。"
    "拟声词可以独占「」段或（）段，密度要求高于 chat_reply_length 配置；回到日常对话后本段停止适用。"
    "禁用句式：不要写评论员口吻或翻旧账（刚才还在、你上次等），"
    "不要写完整逻辑推演句，不要写「不是……而是……」句式，不要在性行为中做长篇解释、复盘或元评论。"
    "本规则关于语言密度、禁用句式、破碎度和失语优先的要求高于角色性格；强势角色也应该说更少的字，"
    "用更短、更准的语言体现性格。"
)


def _sanitize_judge_view_hint(view: str | None, *sources: str) -> str:
    """自动配图判断器的 view 只保留硬相机约束，普通场景交给后续规划器决策。"""
    normalized = (view or "").strip().lower()
    if normalized not in JUDGE_VALID_VIEWS:
        return ""
    combined = "\n".join(part for part in sources if part)
    if normalized in {"selfie", "mirror"}:
        return normalized if JUDGE_EXPLICIT_SELF_CAMERA_RE.search(combined) else ""
    if normalized == "portrait":
        return normalized if JUDGE_EXPLICIT_PORTRAIT_RE.search(combined) else ""
    # 自动判断阶段不强推 pov/third，避免把“近景/看向镜头”误解释成硬视角。
    return ""

class ChatContextMixin:
    def _append_chat_history_messages(self, session_id: str, messages: list[dict[str, str]]) -> None:
        if not session_id or not messages:
            return
        state = self._get_session_state(session_id)
        history = session_schema.get_chat_history(state)
        clean = [
            {"role": str(msg.get("role") or ""), "content": str(msg.get("content") or "").strip()}
            for msg in messages
            if str(msg.get("role") or "").strip() and str(msg.get("content") or "").strip()
        ]
        if not clean:
            return
        history.extend(clean)
        try:
            self.app_store.append_messages(session_id, self._context_character_key(session_id), clean)
        except Exception:
            logger.warning("chat message sqlite append failed", exc_info=True)
        full_snapshot = list(history)
        self._apply_history_trim(state, self._history_storage_cap())
        self._save_session_state(session_id, state)
        self._queue_checkpoint_if_needed(session_id, full_snapshot)

    def _ensure_user_history_committed(self, session_id: str, user_text: str) -> None:
        """取消发生在 LLM 返回前时，仍保留已经收到的用户输入。"""
        text = self._sanitize_user_history_text(user_text)
        if not text:
            return
        state = self._get_session_state(session_id)
        for msg in reversed(session_schema.get_chat_history(state)[-6:]):
            if msg.get("role") == "user" and msg.get("content") == text:
                return
        self._append_chat_history_messages(session_id, [{"role": "user", "content": text}])

    def _flush_pending_photo_history_messages(self, session_id: str) -> None:
        if not hasattr(self, "_take_pending_photo_history_messages"):
            return
        pending = self._take_pending_photo_history_messages(session_id)
        if not pending:
            return
        state = self._get_session_state(session_id)
        for msg in pending:
            self._append_photo_history_message(session_id, msg, state=state)
        self._save_session_state(session_id, state)

    def _flush_pending_wardrobe_history_messages(self, session_id: str) -> None:
        if not hasattr(self, "_take_pending_wardrobe_history_messages"):
            return
        pending = self._take_pending_wardrobe_history_messages(session_id)
        if pending:
            self._append_chat_history_messages(session_id, pending)

    def _trim_last_assistant_history_to_sent(self, session_id: str, full_text: str, sent_text: str) -> None:
        """发送被取消时，只保留 Telegram 已确认发出的 assistant 内容。"""
        full_text = str(full_text or "").strip()
        sent_text = str(sent_text or "").strip()
        if not full_text:
            return
        state = self._get_session_state(session_id)
        history = session_schema.get_chat_history(state)
        changed = False
        for idx in range(len(history) - 1, -1, -1):
            msg = history[idx]
            if msg.get("role") == "assistant" and str(msg.get("content") or "").strip() == full_text:
                if sent_text:
                    history[idx] = {"role": "assistant", "content": sent_text}
                else:
                    del history[idx]
                changed = True
                break
        if changed:
            session_schema.set_chat_history(state, history)
            try:
                self.app_store.update_latest_matching_message(
                    session_id,
                    self._context_character_key(session_id),
                    "assistant",
                    full_text,
                    sent_text,
                )
            except Exception:
                logger.warning("chat message sqlite trim failed", exc_info=True)
            self._save_session_state(session_id, state)

    async def _send_chat_reply_tracked(
        self,
        chat_id: int | str,
        text: str,
        *,
        split_paragraphs: bool,
        on_progress,
    ) -> None:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] if split_paragraphs else [text]
        sent_paragraphs: list[str] = []
        current_chunks: list[str] = []

        def report() -> None:
            parts = list(sent_paragraphs)
            if current_chunks:
                parts.append("\n".join(current_chunks))
            on_progress("\n\n".join(part for part in parts if part).strip())

        for i, para in enumerate(paragraphs):
            if i > 0:
                await asyncio.sleep(1)
            current_chunks = []
            for chunk in self._split_text(para, 3900):
                await self.tg_api("sendMessage", {"chat_id": str(chat_id), "text": chunk})
                current_chunks.append(chunk)
                report()
            if current_chunks:
                sent_paragraphs.append("\n".join(current_chunks))
                current_chunks = []
                report()

    def _build_chat_final_recovery_messages(
        self,
        messages: list[dict[str, Any]],
        tool_results: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """构造不含 tool 协议消息的最终回复恢复 prompt。"""
        recovered: list[dict[str, str]] = []
        for msg in messages:
            role = str(msg.get("role") or "")
            if role == "tool":
                continue
            if role not in {"system", "user", "assistant"}:
                continue
            content = (msg.get("content") or "").strip()
            if role == "assistant" and msg.get("tool_calls"):
                # 去掉 assistant.tool_calls，避免兼容端点继续粘在工具模式。
                if content:
                    recovered.append({"role": role, "content": self._strip_dsml_tool_markup(content)})
                continue
            if content:
                recovered.append({"role": role, "content": content})

        labels = {
            "change_appearance": "外观记录",
            "update_location": "角色位置记录",
            "update_user_location": "用户位置记录",
            "generate_roleplay_image": "配图处理",
        }
        result_lines = []
        for item in tool_results[-6:]:
            name = labels.get(item.get("name") or "", item.get("name") or "工具")
            result = (item.get("result") or "").strip()
            if result:
                result_lines.append(f"- {name}: {result[:160]}")
        result_text = "\n".join(result_lines) if result_lines else "- 无可用工具结果。"
        recovered.append({
            "role": "system",
            "content": (
                "工具阶段已经结束。上方如有 assistant/tool 调用记录，已在本请求中改写为普通上下文；"
                "现在只能输出要发给用户的自然语言角色回复，禁止再调用、补调用或修正任何工具。"
                "如果地点工具曾提示无法识别，也不要继续尝试改参数；按剧情把当前位置自然写成路上、街道或目标地点附近即可。\n"
                f"已执行工具结果:\n{result_text}"
            ),
        })
        return recovered

    async def handle_chat(self, chat_id: int | str, session_id: str, text: str):
        sent_reply = ""
        reply = ""
        try:
            state = self._get_session_state(session_id)
            previous_interaction = session_schema.get_last_interaction(state)
            reset_reason = self._short_context_reset_reason(text, previous_interaction)
            self._touch(session_id)
            if self._should_include_chat_world_context(text):
                self._schedule_weather_refresh(session_id)  # 只在本轮可能用到天气/动线时后台刷新
            if reset_reason:
                self._reset_short_context(state, reset_reason, session_id=session_id)
            session_schema.set_last_message_text(state, text)
            session_schema.set_last_message_time(state, time.time())
            session_schema.set_recent_message_history(state, (session_schema.get_recent_message_history(state) + [{"text": text, "time": time.time()}])[-5:])

            if session_schema.get_last_sent_selfie_time(state) and not session_schema.get_last_sent_selfie_replied(state):
                if self._within(session_schema.get_last_sent_selfie_time(state), 12 * 3600):
                    session_schema.set_replying_to_selfie(state, True)
                session_schema.set_last_sent_selfie_replied(state, True)

            session_schema.set_rounds_since_image(state, session_schema.get_rounds_since_image(state) + 1)
            # "距上次确认位置的轮数"：每轮 +1，由 _set_character_place（角色再次明确位置时）清零。
            # 用来给陈旧 pin 降权——多轮没再提及地点时，该 pin 不再锁死生图。
            session_schema.increment_rounds_since_location(state)
            if session_schema.get_ntr_affection_reset(state):
                self._tick_ntr_reconcile(state)

            self._save_session_state(session_id, state)
            if hasattr(self, "queue_life_plan_refresh_if_needed"):
                try:
                    self.queue_life_plan_refresh_if_needed(session_id, reason="lazy-chat")
                except Exception:
                    logger.debug("queue life plan refresh failed", exc_info=True)
            if not self.has_llm_config("chat", session_id):
                await self.send_message(chat_id, "聊天与角色扮演模型未配置，聊天和工具触发不可用。命令功能仍可使用。")
                return

            await self.send_action(chat_id, "typing")
            reply = await self.run_roleplay_chat(chat_id, session_id, text)
            if reply:
                self._ulog(session_id, "BOT", reply)
                split = str(self.config.get("chat_split_paragraphs", "true")).lower() in ("true", "1", "yes")
                def update_sent(value: str) -> None:
                    nonlocal sent_reply
                    sent_reply = value
                await self._send_chat_reply_tracked(
                    chat_id,
                    reply,
                    split_paragraphs=split,
                    on_progress=update_sent,
                )
                if hasattr(self, "_schedule_post_chat_push"):
                    try:
                        self._schedule_post_chat_push(session_id)
                    except Exception:
                        logger.debug("schedule post-chat push failed", exc_info=True)
            else:
                await self.send_message(chat_id, "回复生成失败，请稍后重试。")
        except asyncio.CancelledError:
            self._ensure_user_history_committed(session_id, text)
            self._flush_pending_wardrobe_history_messages(session_id)
            self._flush_pending_photo_history_messages(session_id)
            if reply:
                self._trim_last_assistant_history_to_sent(session_id, reply, sent_reply)
            raise

    async def run_roleplay_chat(
        self,
        chat_id: int | str,
        session_id: str,
        user_text: str,
        *,
        extra_system_prompt: str = "",
        history_user_text: str | None = None,
    ) -> str:
        state = self._get_session_state(session_id)
        if hasattr(self, "_ensure_life_profile"):
            # 角色生活档案（年龄段/白天职场）按人设推断并缓存：命中缓存时无开销，仅人设变动才重算。
            try:
                await self._ensure_life_profile(session_id)
            except Exception:
                logger.debug("ensure life profile failed", exc_info=True)
        if hasattr(self, "_record_external_wardrobe_change_before_user"):
            self._record_external_wardrobe_change_before_user(session_id)
        messages = self._build_chat_messages(session_id, user_text)
        extra_system_prompt = str(extra_system_prompt or "").strip()
        if extra_system_prompt:
            messages.insert(max(0, len(messages) - 1), {"role": "system", "content": extra_system_prompt})
        tools = self._chat_tools_schema()
        chat_request_body = {
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        final = None
        final_request_body = None
        empty_error_logged = False
        try:
            result = await self._call_llm_messages(
                messages,
                tools=tools,
                tool_choice="auto",
                tag="chat",
                purpose="chat",
                temp=float(self._get_llm_value("chat", "temperature", "0.9")),
                session_id=session_id,
                sampling=True,
            )
        except Exception as exc:
            logger.warning("LLM request failed: %s", exc)
            self._record_llm_error_log(
                session_id=session_id,
                purpose="chat",
                tag="chat",
                request_body=chat_request_body,
                response=None,
                error=f"initial chat exception: {exc}",
            )
            return ""

        # 记录 chat 请求的 usage 到会话日志
        usage = result.get("usage") or {}
        if usage:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = self._cached_tokens_from_usage(usage, prompt_tokens=prompt_tokens)
            self._ulog(session_id, "USAGE", f"prompt={prompt_tokens} completion={completion_tokens} cached={cached_tokens}")

        assistant = result.get("choices", [{}])[0].get("message", {})
        content = (assistant.get("content") or "").strip()
        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            dsml_tool_calls, cleaned_content = self._extract_dsml_tool_calls(content)
            if dsml_tool_calls:
                tool_calls = dsml_tool_calls
                content = cleaned_content
                assistant = dict(assistant)
                assistant["content"] = content
                assistant["tool_calls"] = tool_calls

        explicit_image_req = self._user_requested_image(user_text)

        image_emitted = False
        tool_results: list[dict[str, str]] = []
        if tool_calls:
            messages.append(assistant)
            for call in tool_calls:
                fn_name = (call.get("function") or {}).get("name")
                if fn_name == "generate_roleplay_image" and self._should_block_chat_image(
                    session_id, user_text, explicit=explicit_image_req
                ):
                    # 冷却期内模型主动配图：跳过生图，仅保留文字回复（用户明确要图时不会走到这里）。
                    self._ulog(session_id, "IMG", "冷却期内抑制模型主动配图")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", "tool"),
                        "content": "Skipped: image cooldown.",
                    })
                    tool_results.append({"name": fn_name or "", "result": "Skipped: image cooldown."})
                    continue
                tool_result = await self._execute_tool_call(chat_id, session_id, call)
                if fn_name == "generate_roleplay_image":
                    image_emitted = True
                tool_results.append({"name": fn_name or "", "result": str(tool_result or "")})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "tool"),
                    "content": tool_result,
                })
            final_request_body = {"messages": messages}
            try:
                final = await self._call_llm_messages(
                    messages,
                    tag="chat-final",
                    purpose="chat",
                    temp=float(self._get_llm_value("chat", "temperature", "0.9")),
                    session_id=session_id,
                    sampling=True,
                )
                final_msg = final.get("choices", [{}])[0].get("message", {})
                final_tool_calls = final_msg.get("tool_calls") or []
                final_text = (final_msg.get("content") or "").strip()
                final_dsml_tool_calls: list[dict[str, Any]] = []
                final_cleaned_text = final_text
                if final_text:
                    final_dsml_tool_calls, final_cleaned_text = self._extract_dsml_tool_calls(final_text)
                    final_cleaned_text = final_cleaned_text.strip()
                final_tool_only = (final_tool_calls and not final_text) or (final_dsml_tool_calls and not final_cleaned_text)
                if final_tool_only:
                    retry_messages = messages + [{
                        "role": "system",
                        "content": (
                            "上一轮模型错误地在最终回复阶段返回了工具调用。工具阶段已经结束，"
                            "现在禁止调用任何工具；请只输出要发给用户的自然语言回复，"
                            "承接已经执行的工具结果，不要提到工具调用或内部错误。"
                            "即使上方工具结果提示地点未更新，也不要再尝试修正地点参数。"
                        ),
                    }]
                    retry_request_body = {"messages": retry_messages}
                    try:
                        retry = await self._call_llm_messages(
                            retry_messages,
                            tag="chat-final-retry",
                            purpose="chat",
                            temp=float(self._get_llm_value("chat", "temperature", "0.9")),
                            session_id=session_id,
                            sampling=True,
                        )
                        retry_msg = retry.get("choices", [{}])[0].get("message", {})
                        retry_text = (retry_msg.get("content") or "").strip()
                        retry_tool_calls = retry_msg.get("tool_calls") or []
                        retry_cleaned_text = retry_text
                        if retry_text:
                            _retry_dsml_tool_calls, retry_cleaned_text = self._extract_dsml_tool_calls(retry_text)
                            retry_cleaned_text = retry_cleaned_text.strip()
                        if retry_cleaned_text:
                            self._ulog(session_id, "WARN", "chat-final returned tool_calls without content; retried text-only")
                            final = retry
                            final_request_body = retry_request_body
                            final_msg = dict(retry_msg)
                            final_msg["content"] = retry_cleaned_text
                            final_text = retry_cleaned_text
                            final_tool_calls = retry_tool_calls
                        else:
                            recovery_messages = self._build_chat_final_recovery_messages(messages, tool_results)
                            recovery_request_body = {"messages": recovery_messages}
                            recovery = await self._call_llm_messages(
                                recovery_messages,
                                tag="chat-final-recovery",
                                purpose="chat",
                                temp=float(self._get_llm_value("chat", "temperature", "0.9")),
                                session_id=session_id,
                                sampling=True,
                            )
                            recovery_msg = recovery.get("choices", [{}])[0].get("message", {})
                            recovery_text = (recovery_msg.get("content") or "").strip()
                            recovery_cleaned_text = recovery_text
                            if recovery_text:
                                _recovery_dsml_tool_calls, recovery_cleaned_text = self._extract_dsml_tool_calls(recovery_text)
                                recovery_cleaned_text = recovery_cleaned_text.strip()
                            if recovery_cleaned_text:
                                self._ulog(session_id, "WARN", "chat-final retry still returned empty tool_calls; recovered text-only")
                                final = recovery
                                final_request_body = recovery_request_body
                                final_msg = dict(recovery_msg)
                                final_msg["content"] = recovery_cleaned_text
                                final_text = recovery_cleaned_text
                                final_tool_calls = recovery_msg.get("tool_calls") or []
                            else:
                                self._record_llm_error_log(
                                    session_id=session_id,
                                    purpose="chat",
                                    tag="chat-final-recovery",
                                    request_body=recovery_request_body,
                                    response=recovery,
                                    status=200,
                                    error="chat-final recovery returned empty content after unexpected tool_calls",
                                )
                                empty_error_logged = True
                    except Exception as exc:
                        self._record_llm_error_log(
                            session_id=session_id,
                            purpose="chat",
                            tag="chat-final-retry",
                            request_body=retry_request_body,
                            response=None,
                            error=f"chat-final retry exception after unexpected tool_calls: {exc}",
                        )
                        empty_error_logged = True
                if final_tool_only and not final_text and not empty_error_logged:
                    self._record_llm_error_log(
                        session_id=session_id,
                        purpose="chat",
                        tag="chat-final",
                        request_body=final_request_body,
                        response=final,
                        status=200,
                        error="chat-final returned tool_calls without content",
                    )
                    empty_error_logged = True
                content = (final_msg.get("content") or content or "").strip()
                content = self._strip_dsml_tool_markup(content)
            except Exception as exc:
                logger.warning("final chat completion after tool call failed: %s", exc)
                self._record_llm_error_log(
                    session_id=session_id,
                    purpose="chat",
                    tag="chat-final",
                    request_body=final_request_body or {"messages": messages},
                    response=None,
                    error=f"final chat exception: {exc}",
                )
        else:
            content = self._strip_dsml_tool_markup(content)

        scene = self._handle_leaked_image_text(content)
        if scene:
            content = self._strip_leaked_image_text(content)
            if self._should_block_chat_image(session_id, user_text, explicit=explicit_image_req):
                # 冷却期内模型把图片描述泄漏进文字：清掉痕迹但不发图。
                self._ulog(session_id, "IMG", "冷却期内抑制模型泄漏的配图")
                content = self._strip_photo_memory_echo(content)
            else:
                image_emitted = True
                asyncio.create_task(self._push_image_from_text(session_id, scene))
        else:
            content = self._strip_photo_memory_echo(content)

        # 模型没主动配图时，用独立的"配图时机判断器"按对话内容决定是否补一张。
        # 只先做判断；真正发图放到本轮对话入库之后，避免图片规划看不到刚才的用户/角色文本。
        judge_decision = None
        if not content and not empty_error_logged:
            self._record_llm_error_log(
                session_id=session_id,
                purpose="chat",
                tag="chat-final" if final is not None else "chat",
                request_body=final_request_body if final is not None else chat_request_body,
                response=final if final is not None else result,
                status=200,
                error="LLM returned empty chat content",
            )
        if content and not image_emitted:
            judge_decision = await self._judge_image_moment(
                session_id, user_text, content, explicit=explicit_image_req
            )

        # 自动抽取角色自述位置并持久化（工具 update_location 是显式高置信路径，这里是 LLM 判定兜底）。
        # fire-and-forget：不阻塞回复返回，抽取结果下一轮生效。
        should_extract_location = True
        if hasattr(self, "_should_run_location_extract"):
            should_extract_location = self._should_run_location_extract(user_text, content)
        if content and should_extract_location:
            async def _bg_extract():
                try:
                    await self._update_character_place_from_text(session_id, content)
                except Exception:
                    logger.warning("background location extract failed", exc_info=True)
            asyncio.create_task(_bg_extract())

        history = session_schema.get_chat_history(state)
        stored_user_text = self._sanitize_user_history_text(history_user_text if history_user_text is not None else user_text)
        new_messages = [{"role": "user", "content": stored_user_text}]
        if content:
            new_messages.append({"role": "assistant", "content": content})
        if hasattr(self, "_take_pending_wardrobe_history_messages"):
            new_messages.extend(self._take_pending_wardrobe_history_messages(session_id))
        if hasattr(self, "_take_pending_photo_history_messages"):
            new_messages.extend(self._take_pending_photo_history_messages(session_id))
        history.extend(new_messages)
        try:
            self.app_store.append_messages(session_id, self._context_character_key(session_id), new_messages)
        except Exception:
            logger.warning("chat message sqlite append failed", exc_info=True)
        full_snapshot = list(history)
        # 仅做存储兜底裁剪（远高于 checkpoint 周期，正常不触及），并同步 short_context_start。
        # 发给模型的窗口由 _active_chat_history 固定窗口 + checkpoint 前置决定。
        self._apply_history_trim(state, self._history_storage_cap())
        self._save_session_state(session_id, state)
        self._queue_checkpoint_if_needed(session_id, full_snapshot)
        if judge_decision:
            judge_view = (judge_decision.get("view") or "").strip()
            if judge_view:
                self._ulog(session_id, "JUDGE", f"配图时机=发 view={judge_view} intent={judge_decision.get('intent','')[:60]}")
            else:
                self._ulog(session_id, "JUDGE", f"配图时机=发 intent={judge_decision.get('intent','')[:60]}")
            asyncio.create_task(self._run_background_roleplay_image(
                chat_id, session_id,
                intent=judge_decision.get("intent", ""),
                mood=judge_decision.get("mood", ""),
                prompt=content,
                view=judge_decision.get("view", ""),
            ))
        return content

    def _build_chat_messages(
        self,
        session_id: str,
        user_text: str,
        *,
        include_history: bool = True,
        include_dynamic_tail: bool = True,
    ) -> list[dict[str, Any]]:
        state = self._get_session_state(session_id)
        now = self._session_now(session_id)
        weekday = WEEKDAY_NAMES[now.weekday()]
        explicit_image_req = self._user_requested_image(user_text)
        include_world_dynamic = self._should_include_chat_world_context(user_text, explicit_image=explicit_image_req)
        time_ctx = self._get_time_context(session_id, now=now)
        time_period = time_ctx.get("period") or self._get_time_period(now.hour)
        # 静态前缀不含穿搭（中频变化），避免换装作废整条历史前缀缓存；穿搭见下方动态层 visual_context。
        persona = self._get_effective_persona(session_id, include_appearance=False)
        role_name, bot_name, bot_self_name = self._session_role_identity(session_id)
        relationship = self._get_session_cfg(session_id, "spatial_relationship", "")
        rel_line = f"你和用户的关系: {str(relationship).strip()}。\n" if str(relationship).strip() else ""
        user_address = self._get_session_cfg(session_id, "user_address", "")
        address_line = f"你通常称呼用户为「{str(user_address).strip()}」。\n" if str(user_address).strip() else ""

        # ── 静态前缀（变化极低频：角色切换/配置变更才动）──
        # 放在 messages[0]，最大化 DeepSeek 服务端 prefix cache 命中率。
        system_static = (
            f"{persona}\n\n"
            f"你当前扮演的角色是「{bot_name}」（{role_name}）。除非用户明确要求换角色，否则你就是「{bot_name}」，"
            f"不要声称自己是其他角色或默认角色。对话中按角色习惯使用「{bot_self_name}」或自然第一人称作为自称，"
            "不要不自然地反复报全名。\n"
            f"{rel_line}"
            f"{address_line}"
            "当用户明示或暗示想看你的样子、照片、穿着或当前场景时，应调用 generate_roleplay_image。"
            "工具调用只需要描述这张图要回应的对话意图、情绪和必要元素；"
            "若场景涉及性爱，intent 与 must_include 必须如实描述性行为，露骨度由评级系统控制，不需要你自行淡化或省略。"
            "最终画面会由生图辅助模型结合完整上下文整合。不要把工具名、函数调用或内部指令写进聊天文字。"
            "\n换装持久化（重要）：当剧情里角色换上/移除服装配饰、一次更换多件衣物，或衣物变为半脱/破损/脱下/恢复正常时，必须调用 change_appearance；"
            "每件变化分别写入 items，工具会返回完整最新着装，之后的配图和对话以工具结果及历史中的衣橱状态 system 记录为准。"
            "性爱/亲密/洗澡中的临时衣物状态也用 set_state 记录，事件结束时再恢复 normal；全裸/脱光用 clear_all。"
            "明确摘掉并继续不戴的配饰用 remove，换整套才用 mode=replace。"
            "不要只在文字里描述换装却不调用工具。"
            "\n位置持久化：当剧情里角色移动到新地点、或你明确交代了此刻在哪（出门、到公司、回家、到了某店等）时，调用 update_location 工具记录，"
            "这样之后的配图和推送会和你说的位置保持一致，不会无理由瞬移。位置没变就不用调。"
            "当用户消息里明确透露了自己当前在哪（如「我刚到家」「在公司加班」「和你在一起」等），可调用 update_user_location 工具辅助记录。"
            "\n照片历史规则：历史中 role=system 且以「照片历史」开头的内容，是你之前发给用户的照片记录。"
            "当用户紧接照片历史回复，或提到“刚才那张/照片/图/自拍/画面/出来看看”等内容时，优先理解为用户在回应最近一张照片；"
            "依据照片历史自然承接，但不要主动复述系统记录。"
            "\n发图节奏规则：用户明确要图或动态提醒要求补图时优先调用 generate_roleplay_image；其余频率细节以下方对话控制为准。"
            "\n语言理解规则：用户的日常表述（如自夸、调侃、闲聊、陈述事实）默认是普通对话，不是表白或调情。"
            "只有当用户明确使用恋爱/亲密相关词汇（喜欢你、想你、爱你、亲一下、抱抱等），或委婉/隐喻的性暗示（融为一体、想要你、今晚别走、给我、交给你等）时才理解为亲密信号。"
            "不要把「我是好人」「今天天气不错」「我吃饭了」等日常表述曲解为暗示或直球表白。"
            "\n回复格式规则：角色说出口的语言必须单独放在中文直角引号「」中；动作、神态、姿态、心理、环境和状态描写必须单独放在全角括号（）中。"
            "同一自然段不要混写台词和状态描写；需要同时写状态和台词时，用空行分成独立段落。示例：\n（她抬眼看过来。）\n\n「怎么突然这么问？」\n"
            "不要使用英文引号、冒号旁白或括号外裸叙述来表示动作状态。"
            f"{CHAT_INTIMATE_LANGUAGE_RULES}"
            "\n对话推进规则：优先回应用户本轮话题、情绪和问题，不要因为某条长期记忆很重要就主动跳出用户正在聊的内容。"
            "如果用户本轮发起的话题与前文、旧场景或旧动作明显无关，请直接接续用户的新话题，不要为了显得连续而强行呼应上一场景。"
            "用户一句话里可能包含寒暄、抱怨、解释、问题和转折；先判断核心意图和最需要被接住的情绪，再自然推进对话，不要逐句逐点机械回应。"
            "长期记忆只在与本轮话题直接相关时自然融入，不要逐条复述。"
            "不要连续几轮发出结构、语义或情绪走向都类似的信息；不要反复提及同一个具体物件、食物、配饰或旧事件，"
            "除非用户本轮主动提起或上下文确实需要。"
            "\n事实来源优先级：用户本轮明确输入 > 最近真实对话 > checkpoint > 长期记忆 > 世界/动线背景。"
            "低优先级背景不能覆盖高优先级事实；不确定时承认不确定或轻描淡写，不要把推测说成已经发生。"
        )

        active_dialog = bool(self._active_chat_history(state))

        # ── 半稳定状态快照（外型/衣橱/世界模板：中低频变化，独立放在 checkpoint 前）──
        semistable_parts: list[str] = []
        wardrobe_semistable = session_schema.get_wardrobe_semistable_snapshot(state)
        visual_context = (
            wardrobe_semistable.get("visual_context", "")
            if wardrobe_semistable
            else self._chat_visible_appearance_context(session_id)
        )
        if visual_context:
            semistable_parts.append(
                "当前可见外型与配饰（这是你此刻身上真实可见的状态；用户问到外貌、穿搭、配饰或随身物时优先依据这里，"
                "不要编造不存在的配饰）：\n"
                f"{visual_context}"
            )
        closet_context = (
            wardrobe_semistable.get("closet_context", "")
            if wardrobe_semistable
            else (self._wardrobe_closet_context(session_id) if hasattr(self, "_wardrobe_closet_context") else "")
        )
        if closet_context:
            semistable_parts.append(
                "你的衣橱里收藏着这些穿过的衣服（你清楚自己有哪些）：\n"
                f"{closet_context}\n"
                "用户点名某件、或剧情/场合自然需要时（出门、睡前、洗澡后、约会等），可以让角色换上其中一件；不要无缘无故频繁换装。"
            )
        if hasattr(self, "_format_world_semistable_context"):
            world_semistable = self._format_world_semistable_context(
                session_id, mode="chat", now=now, pin_location=not active_dialog
            )
            if world_semistable:
                semistable_parts.append(world_semistable)
        # 自然光硬规则随 light_phase（日间/黄昏/暮色/入夜）低频变化，单独放进后面的
        # world_conditions 半稳定槽，避免把稳定世界规则和外观槽一起改掉。
        semistable_context = "\n\n".join(semistable_parts)
        self._track_semistable_context_change(session_id, semistable_context)
        world_conditions_context = self._chat_world_conditions_context(session_id, now=now)
        self._track_world_conditions_context_change(session_id, world_conditions_context)

        # ── 动态后缀（每请求变化：精确时间/本轮位置判断/发图 overdue）──
        # 城市/天气/季节自然光与自然光硬规则是低频变化，放在 checkpoint 前的独立半稳定槽。
        freq = self.config.get("selfie_frequency", "频繁")
        image_nudge_due = self._image_nudge_due(freq, session_schema.get_rounds_since_image(state))
        # 场景断档感知：距离上次对话超过阈值时提醒 LLM 旧场景可能已自然结束
        try:
            stale_minutes = float(self.config.get("scene_stale_minutes", "30") or 0)
        except Exception:
            stale_minutes = 30
        previous_interaction = session_schema.get_last_interaction(state)
        scene_stale = bool(stale_minutes > 0 and previous_interaction and time.time() - previous_interaction > stale_minutes * 60)
        system_dynamic = f"当前时间: {now.strftime('%H:%M')} ({weekday}) {time_period}。\n"
        if image_nudge_due:
            system_dynamic += "发图提醒: 已有多轮未配图，本轮请优先调用 generate_roleplay_image。\n"
        if scene_stale:
            system_dynamic += (
                "距离上次对话已过超过半小时，之前的日常场景可能已自然结束；请优先依据结束前场景和动作的特征判断其是否延续。"
                "重新思考你和用户的位置关系。\n"
            )
        # 对话进行中：对话已建立的场景优先，动线只作背景。低频世界模板在 semistable，
        # 这里只保留本轮用户位置/空间关系等高频尾部。
        world_dynamic = ""
        if include_world_dynamic and hasattr(self, "_format_world_dynamic_context"):
            world_dynamic = self._format_world_dynamic_context(
                session_id, user_text, mode="chat", now=now, pin_location=not active_dialog
            )
            if world_dynamic:
                system_dynamic += f"\n{world_dynamic}\n"
        dynamic_signature = "\n".join([
            f"image_nudge={int(image_nudge_due)}",
            f"scene_stale={int(scene_stale)}",
            f"world_dynamic={world_dynamic or 'off'}",
        ])
        if include_dynamic_tail:
            self._track_dynamic_context_change(session_id, dynamic_signature)
        if session_schema.get_replying_to_selfie(state):
            session_schema.set_replying_to_selfie(state, False)
        # ── 天级/低频稳定上下文（角色历史、长期记忆、配置控制）──
        # 这些比半稳定外型更低频，放在半稳定状态快照之前。
        durable_parts: list[str] = []
        control_context = self._chat_low_frequency_context(session_id, state=state)
        if control_context:
            durable_parts.append(control_context)
        history_summary = self._character_history_summary_context(session_id)
        if history_summary:
            durable_parts.append(
                "角色历史提要（宏观关系与剧情发展脉络；用于理解长期阶段变化，不复述近期细节，不替代长期记忆）:\n"
                f"{history_summary}"
            )
        life_context = self._life_plan_chat_context(session_id, now=now) if hasattr(self, "_life_plan_chat_context") else ""
        if life_context:
            durable_parts.append(life_context)
        memory_context = self._long_term_memory_context(session_id)
        if memory_context:
            durable_parts.append(
                "长期记忆（高重要度稳定事实/偏好/边界/纠正；比 checkpoint 更像硬约束，仅在相关时自然使用，不要逐条复述）:\n"
                f"{memory_context}"
            )
        checkpoint_context = self._checkpoint_context(session_id)
        checkpoint_part = (
            "Checkpoint（近期已折叠对话连续性；只用于承接当前/最近场景、未完成动作、承诺、情绪和地点，不是长期设定；不要主动暴露）:\n"
            f"{checkpoint_context}"
        ) if checkpoint_context else ""

        # 拼接顺序：
        #   [静态 system] + [天级/低频稳定层] + [半稳定状态/世界模板] + [checkpoint 会话连续性] + [历史(checkpoint 锚定，含照片 system 记录)] + [动态 system] + [本轮 user]
        # 静态 + 低频稳定 + 半稳定 + checkpoint + 未折叠历史构成只追加不左移的前缀；checkpoint 落地时才整体归位。
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_static}]
        durable_context = "\n\n".join(durable_parts) if durable_parts else ""
        if durable_context:
            messages.append({"role": "system", "content": durable_context})
        if semistable_context:
            messages.append({"role": "system", "content": semistable_context})
        if world_conditions_context:
            messages.append({"role": "system", "content": world_conditions_context})
        if checkpoint_part:
            messages.append({"role": "system", "content": checkpoint_part})
        history = self._chat_prompt_history(state) if include_history else []
        if include_history:
            messages.extend(history)
        if include_dynamic_tail:
            messages.append({"role": "system", "content": system_dynamic})
        messages.append({"role": "user", "content": user_text})
        self._log_prefix_slot_signatures(
            session_id,
            static=system_static,
            durable=durable_context,
            semistable=semistable_context,
            conditions=world_conditions_context,
            checkpoint=checkpoint_part,
            history_count=len(history),
        )
        return messages

    def _build_chat_context_messages_for_push(self, session_id: str, marker: str = "【系统事件】后台上下文前缀占位") -> list[dict[str, Any]]:
        """复用聊天 prompt 前缀和 checkpoint 后历史，并去掉占位 user。

        推送前 checkpoint 会把未折叠窗口收敛到最近一轮用户消息及之后；
        这些保留下来的对话和照片记录应像正常聊天一样进入 planner 前缀，
        这样用户继续对话或连续推送时都能共享同一段上下文前缀。
        """
        messages = self._build_chat_messages(
            session_id,
            marker,
            include_history=True,
            include_dynamic_tail=False,
        )
        if messages and messages[-1].get("role") == "user" and messages[-1].get("content") == marker:
            messages = messages[:-1]
        return messages

    @staticmethod
    def _slot_hash(text: str) -> str:
        """常驻前缀槽的稳定短哈希；只用于缓存作废点定位（Tier 3 计测）。"""
        if not text:
            return "----"
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

    def _log_prefix_slot_signatures(
        self,
        session_id: str,
        *,
        static: str,
        durable: str,
        semistable: str,
        checkpoint: str,
        history_count: int,
        conditions: str = "",
    ) -> None:
        """把各常驻前缀槽的哈希写进用户日志，便于和 USAGE 的 cached 命中对照，
        定位某轮命中率下降到底是哪个槽（static/durable/semistable/checkpoint）变了。
        前缀缓存在第一个变化的槽处断开，故越靠前的槽变化代价越大。"""
        if not session_id:
            return
        self._ulog(
            session_id,
            "CACHE",
            "prefix "
            f"static={self._slot_hash(static)} "
            f"durable={self._slot_hash(durable)} "
            f"semistable={self._slot_hash(semistable)} "
            f"conditions={self._slot_hash(conditions)} "
            f"ckpt={self._slot_hash(checkpoint)} "
            f"hist={history_count}",
        )

    @staticmethod
    def _parse_dsml_attrs(raw: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for match in DSML_ATTR_RE.finditer(raw or ""):
            attrs[match.group(1)] = html.unescape(match.group(2) if match.group(2) is not None else match.group(3) or "")
        return attrs

    @classmethod
    def _strip_dsml_tool_markup(cls, text: str) -> str:
        if not text:
            return ""
        cleaned = DSML_TOOL_BLOCK_RE.sub("", text)
        cleaned = DSML_INVOKE_RE.sub("", cleaned)
        cleaned = DSML_ANY_TAG_RE.sub("", cleaned)
        return cleaned.strip()

    @classmethod
    def _extract_dsml_tool_calls(cls, text: str) -> tuple[list[dict[str, Any]], str]:
        """兼容部分 OpenAI 兼容端点把工具调用以 DSML 文本塞进 content 的情况。"""
        if not text or "DSML" not in text or "invoke" not in text:
            return [], text.strip() if text else ""
        calls: list[dict[str, Any]] = []
        for index, invoke in enumerate(DSML_INVOKE_RE.finditer(text), start=1):
            attrs = cls._parse_dsml_attrs(invoke.group("attrs"))
            name = (attrs.get("name") or "").strip()
            if not name:
                continue
            args: dict[str, str] = {}
            for param in DSML_PARAMETER_RE.finditer(invoke.group("body") or ""):
                p_attrs = cls._parse_dsml_attrs(param.group("attrs"))
                p_name = (p_attrs.get("name") or "").strip()
                if not p_name:
                    continue
                value = DSML_ANY_TAG_RE.sub("", param.group("body") or "")
                args[p_name] = html.unescape(value).strip()
            calls.append({
                "id": f"dsml_tool_{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
        return calls, cls._strip_dsml_tool_markup(text)

    def _chat_tools_schema(self) -> list[dict[str, Any]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "generate_roleplay_image",
                    "description": "用图片回应本轮角色扮演时调用；只给意图/情绪/必要元素，最终画面由生图辅助模型结合上下文整合。回应性爱场景配图时，intent/must_include 必须如实描述性行为，露骨度由评级系统控制，不由你省略。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string", "description": "本图回应的对话意图，如用户想看角色下班后在家等他的样子。"},
                            "mood": {"type": "string", "description": "图中承载的情绪或关系推进，如安抚、调情、撒娇、展示、挑逗。"},
                            "must_include": {"type": "string", "description": "用户明确要求必须出现的服装、动作、地点或物件；没有则留空。"},
                            "prompt": {"type": "string", "description": "可选简短画面草案；不要写英文标签，生图辅助模型会重写。"},
                            "view": {"type": "string", "enum": ["selfie", "mirror", "pov", "third", "portrait"], "description": "仅用户明确要求视角时填写；否则留空。selfie=前摄自拍，伸手举手机但画面无手机和手机UI；portrait=别人帮角色拍，拍摄者在画面外、角色看镜头，仅同处一地且角色明说要别人帮拍或NTR场景时用；只有mirror允许镜子和手机同框。"},
                        },
                        "required": ["intent"],
                    },
            },
        },
        {
            "type": "function",
                "function": {
                    "name": "change_appearance",
                    "description": (
                        "角色穿上、移除或改变衣物状态时调用。一次调用可在 items 中处理多件；"
                        "wear=穿上/替换该槽，remove=真正移除，set_state=半脱/破损/临时脱下/恢复。"
                        "服装 tags 写简短英文作图标签，name 写中文衣橱名；连衣裙自动覆盖上下装。"
                        "临时脱衣也必须 set_state，结束时设 normal；全裸/脱光用 clear_all。整套重换才用 mode=replace。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "enum": ["merge", "replace"]},
                            "clear_all": {"type": "boolean", "description": "全裸/脱光时为 true，清空全部当前衣物。"},
                            "items": {
                                "type": "array",
                                "description": "本轮全部衣物/配饰操作；每件单独一项。",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "slot": {
                                            "type": "string",
                                            "enum": ["dress", "top", "bottom", "outerwear", "bra", "panties", "legwear", "footwear", "accessory", "hair", "eyes", "other"],
                                        },
                                        "action": {"type": "string", "enum": ["wear", "remove", "set_state", "restore"]},
                                        "tags": {"type": "string", "description": "wear/remove 的英文视觉标签；remove 整槽时可空。"},
                                        "name": {"type": "string", "description": "wear 新衣物的简短中文名称。"},
                                        "state": {"type": "string", "enum": ["normal", "half_off", "damaged", "removed"]},
                                    },
                                    "required": ["slot", "action"],
                                },
                            },
                            "description": {"type": "string", "description": "仅兼容旧调用；能写 items 时不要使用。"},
                        },
                        "required": ["items"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_location",
                    "description": (
                        "角色移动到新地点或明确交代此刻在哪时调用，持续生效，之后配图/推送据此保持一致。"
                        "place 写角色当前所在，如“家里”“公司”“楼下咖啡店”“商场”“在路上”。只在位置确实变化或首次确立时调用，不要每句都报。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "place": {"type": "string", "description": "角色当前所在的自然语言描述。"},
                        },
                        "required": ["place"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_user_location",
                    "description": (
                        "能从用户消息/上下文合理推断用户当前在哪时调用，如「我刚到家」「在公司加班」「在路上」。"
                        "place 写用户当前所在，如\"家里\"\"公司\"\"商场\"\"咖啡店\"\"与角色同处\"等；只有能推断或用户明确交代时调用，无法判断不要编造。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "place": {"type": "string", "description": "用户当前所在的自然语言描述，或「与角色同处」。"},
                        },
                        "required": ["place"],
                    },
                },
            },
        ]
        # 搜索工具只在配置开启且有 API Key 时挂载：模型看不到就不会尝试调用。
        # 注意 tools schema 属于请求静态前缀，开关切换会一次性作废旧前缀缓存（可接受）。
        if self._web_search_enabled():
            tools.append({
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": (
                        "角色遇到自己确实不了解、或需要最新信息的话题时调用联网搜索：新闻时事、"
                        "最新的游戏/番剧/影视/产品、赛事、价格行情等时效性内容，或用户明确让你去查的资料。"
                        "角色扮演剧情、情感交流、常识和人设内已有的话题不要调用。"
                        "query 写简短的搜索关键词（中文或英文皆可），不要写整句对话。"
                        "结果会以资料形式返回，之后由你用角色口吻自然转述，不要照抄。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜索关键词，如「英雄联盟 S16 冠军」「东京 樱花 见顷 2026」。"},
                        },
                        "required": ["query"],
                    },
                },
            })
        return tools

    async def _execute_tool_call(self, chat_id: int | str, session_id: str, call: dict[str, Any]) -> str:
        fn = (call.get("function") or {}).get("name", "")
        raw_args = (call.get("function") or {}).get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        if fn == "generate_roleplay_image":
            return await self._await_protected_image_task(
                session_id,
                self.tool_generate_image(
                    chat_id,
                    session_id,
                    prompt=args.get("prompt", ""),
                    view=args.get("view", ""),
                    intent=args.get("intent", ""),
                    mood=args.get("mood", ""),
                    must_include=args.get("must_include", ""),
                    defer_photo_history=True,
                ),
                label="聊天生图任务",
                after_cancel_done=lambda: self._flush_pending_photo_history_messages(session_id),
            )
        if fn == "change_appearance":
            clear_all_raw = args.get("clear_all")
            clear_all = clear_all_raw is True or str(clear_all_raw or "").strip().lower() in {"1", "true", "yes", "on"}
            return await self.tool_change_appearance(
                session_id,
                args.get("description", ""),
                args.get("mode", "merge"),
                items=args.get("items"),
                clear_all=clear_all,
            )
        if fn == "update_location":
            return await self.tool_update_location(session_id, args.get("place", ""))
        if fn == "update_user_location":
            return await self.tool_update_user_location(session_id, args.get("place", ""))
        if fn == "search_web":
            return await self.tool_search_web(session_id, args.get("query", ""))
        return f"未知工具: {fn}"

    async def _run_background_roleplay_image(self, chat_id: int | str, session_id: str, **kwargs):
        """运行后台自动配图任务，并把异常写入用户日志。"""
        try:
            await self.tool_generate_image(chat_id, session_id, **kwargs)
        except Exception as exc:
            self._ulog(session_id, "ERROR", f"后台自动配图异常: {exc}")
            logger.error("background roleplay image failed: %s", exc, exc_info=True)

    @staticmethod
    def _image_nudge_due(freq: str, rounds_since: int) -> bool:
        if freq == "关闭":
            return False
        return rounds_since >= FREQ_MAX_ROUNDS.get(freq, 5)

    def _reply_length_directive(self) -> str:
        """按配置给聊天回复加长度约束（提示词层面，不截断）。空=不限制。"""
        preset = str(self.config.get("chat_reply_length", "") or "").strip()
        return {
            "简短": "回复长度：保持简短自然，通常 1 到 2 句、40 到 80 字；动作或神态描写最多一处，不要长段独白或铺陈。",
            "适中": "回复长度：控制在 2 到 4 句、约 120 字以内，避免大段独白和过度铺陈。",
            "详细": "回复长度：可以适当展开，但单次不要超过约 300 字。",
        }.get(preset, "")

    @staticmethod
    def _image_frequency_instruction(freq: str) -> str:
        return {
            "极频繁": "原则上每 1 到 2 轮对话至少触发一次配图。",
            "频繁": "原则上每 2 到 3 轮对话触发一次配图。",
            "适度": "每 3 到 5 轮可触发一次配图。",
            "偶尔": "每 5 到 8 轮在精彩时刻触发配图。",
            "关闭": "本次对话中请勿触发配图。",
        }.get(freq, "原则上每 2 到 3 轮对话触发一次配图。")

    def _chat_low_frequency_context(self, session_id: str, *, state: dict[str, Any] | None = None) -> str:
        """低频对话控制：配置/角色设置变化时才动，放在历史前稳定层。"""
        if state is None:
            state = self._get_session_state(session_id)
        freq = self.config.get("selfie_frequency", "频繁")
        lines = [
            f"纯度指令: {self._purity_directive(self._get_purity(session_id))}",
            f"外貌修改权限: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}。",
            f"发图频率: {self._image_frequency_instruction(freq)}",
        ]
        length_directive = self._reply_length_directive()
        if length_directive:
            lines.append(length_directive)
        if session_schema.get_short_context_start(state) or session_schema.get_short_context_reset_reason(state):
            lines.append(
                "短期注意规则: 用户已经切换过话题或场景。切换点之前的短期聊天和 checkpoint 已从当前模型上下文移除，"
                "不要主动延续旧地点、旧动作、旧冲突或旧图片；只有用户明确说继续刚才、上一张、那个话题时才引用长期背景。"
            )
        return "对话控制（低频配置；变化时才会影响历史前缀）:\n" + "\n".join(lines)

    def _scene_low_frequency_context(self, session_id: str) -> str:
        """低频场景控制：角色偏好/纯度类设置，放在场景 prompt 的时间动态信息之前。"""
        prompt_prefs = self._prompt_scene_preferences(session_id) if hasattr(self, "_prompt_scene_preferences") else {}
        return (
            "场景控制（低频配置；变化时才会影响场景前缀）:\n"
            f"角色性观念: {self._purity_directive(self._get_purity(session_id))}\n"
            f"用户画面偏好: 场景偏好={prompt_prefs.get('scene_preference') or '无'}；自拍偏好={prompt_prefs.get('selfie_preference') or '无'}。"
        )

    def _track_semistable_context_change(self, session_id: str, context: str):
        """半稳定状态变化后，如果历史已经足够长，异步 checkpoint 一次来收敛缓存前缀。"""
        if not session_id:
            return
        bucket = getattr(self, "_semistable_context_signatures", None)
        if not isinstance(bucket, dict):
            bucket = {}
            self._semistable_context_signatures = bucket
        previous = bucket.get(session_id)
        bucket[session_id] = context
        if previous is None or previous == context:
            return
        self._queue_checkpoint_if_pending_half(session_id, force=True)

    def _chat_world_conditions_context(self, session_id: str, *, now: Any = None) -> str:
        """低频世界条件槽：按日期/天气/光线阶段更新，不随每分钟滚动。"""
        if hasattr(self, "_world_runtime_enabled") and not self._world_runtime_enabled():
            return ""
        now = now or self._session_now(session_id)
        weather = None
        cached = getattr(self, "_weather_caches", {}).get(session_id or "__default__")
        if isinstance(cached, dict):
            weather = cached.get("data")
        city = self._get_session_cfg(session_id, "location", self.config.get("location", "上海"))
        day = self._day_type(now).get("label", "工作日") if hasattr(self, "_day_type") else ""
        weather_text = self._weather_text(weather) if hasattr(self, "_weather_text") else str(weather or "未知")
        time_ctx = self._get_time_context(session_id, now=now, weather=weather)
        light_guard = self._format_light_guard(session_id, now=now, weather=weather)
        sunrise = time_ctx.get("sunrise")
        sunset = time_ctx.get("sunset")
        sun_key = ""
        if hasattr(sunrise, "strftime") and hasattr(sunset, "strftime"):
            sun_key = f"{sunrise.strftime('%H:%M')}-{sunset.strftime('%H:%M')}"
        signature = json.dumps({
            "city": city,
            "date": now.strftime("%Y-%m-%d") if hasattr(now, "strftime") else "",
            "day": day,
            "weather": weather_text,
            "season": time_ctx.get("season") or "",
            "light_phase": time_ctx.get("light_phase") or "",
            "sun": sun_key,
            "guard": light_guard,
        }, ensure_ascii=False, sort_keys=True)
        cache = getattr(self, "_chat_world_conditions_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._chat_world_conditions_cache = cache
        cached_entry = cache.get(session_id)
        if isinstance(cached_entry, tuple) and len(cached_entry) == 2 and cached_entry[0] == signature:
            return cached_entry[1]
        lines = [
            "世界当前条件（半稳定；仅日期/天气/自然光阶段变化时更新，不随每分钟滚动）:",
            f"- 城市/日期: {city}，{now.strftime('%Y-%m-%d')}，{day}",
            f"- 天气: {weather_text}",
            f"- 季节/自然光: {self._format_time_context(session_id, now=now, weather=weather)}",
        ]
        if light_guard:
            lines.append(light_guard)
        content = "\n".join(lines)
        cache[session_id] = (signature, content)
        return content

    def _track_world_conditions_context_change(self, session_id: str, context: str):
        """世界条件半稳定槽变化后，未折叠历史过半则 checkpoint。"""
        if not session_id:
            return
        bucket = getattr(self, "_world_conditions_context_signatures", None)
        if not isinstance(bucket, dict):
            bucket = {}
            self._world_conditions_context_signatures = bucket
        previous = bucket.get(session_id)
        bucket[session_id] = context
        if previous is None or previous == context:
            return
        self._queue_checkpoint_if_pending_half(session_id, force=True)

    def _track_dynamic_context_change(self, session_id: str, signature: str):
        """动态尾部结构变化时，历史足够长则异步 checkpoint，避免长窗口一直拖着旧场景。

        signature 不包含精确分钟时间，只包含发图提醒、场景断档和本轮动线这类结构性动态信息，
        避免时钟每分钟滚动造成无意义 checkpoint。
        """
        if not session_id:
            return
        bucket = getattr(self, "_dynamic_context_signatures", None)
        if not isinstance(bucket, dict):
            bucket = {}
            self._dynamic_context_signatures = bucket
        previous = bucket.get(session_id)
        bucket[session_id] = signature
        if previous is None or previous == signature:
            return
        self._queue_checkpoint_if_pending_half(session_id, force=True)

    def _queue_checkpoint_if_pending_half(self, session_id: str, *, force: bool = False) -> bool:
        """未折叠历史达到窗口一半时排一次 checkpoint；用于上下文结构变化后的前缀收敛。"""
        if not session_id:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        key = self._context_character_key(session_id)
        try:
            checkpoint = self.app_store.get_checkpoint(session_id, key)
            pending = self.app_store.list_messages(session_id, key, after_id=int(checkpoint.get("source_until_id") or 0))
        except Exception:
            return False
        threshold = max(2, self._context_window_message_limit() // 2)
        if len(pending) < threshold:
            return False
        scope = f"{session_id}\n{key}"
        task = getattr(self, "_checkpoint_tasks", {}).get(scope)
        if task and not task.done():
            return False
        self._checkpoint_tasks[scope] = loop.create_task(
            self._run_context_checkpoint(session_id, key, self._checkpoint_keep_message_limit(), force=force)
        )
        return True

    def _image_min_gap(self, freq: str | None = None) -> int:
        """最小配图间隔（轮）：刚发过图后留白几轮再考虑，避免连刷。随频率档位变化。

        全局 image_min_gap_rounds 若显式配置，则作为下限地板（取与档位间隔的较大者）。
        """
        if freq is None:
            freq = self.config.get("selfie_frequency", "频繁")
        tier = FREQ_MIN_GAP.get(freq, 2)
        try:
            cfg = self.config.get("image_min_gap_rounds")
            if cfg is not None and str(cfg).strip() != "":
                return max(1, max(tier, int(cfg)))
        except Exception:
            pass
        return max(1, tier)

    def _user_requested_image(self, text: str) -> bool:
        """用户是否明确开口要看图/自拍/照片：命中则配图不受冷却期约束。"""
        return bool(IMAGE_REQUEST_RE.search(text or ""))

    def _should_include_chat_world_context(self, text: str, *, explicit_image: bool | None = None) -> bool:
        """普通聊天默认不注入天气/光线/动线；只有本轮真的碰到世界状态才展开。"""
        if explicit_image is None:
            explicit_image = self._user_requested_image(text)
        if explicit_image:
            return True
        return bool(CHAT_WORLD_TRIGGER_RE.search(text or ""))

    def _should_run_image_judge(self, user_text: str, draft_reply: str, *, explicit: bool = False) -> bool:
        """自动配图 judge 的便宜意图门控，避免寒暄/纯问答每轮再跑一次小模型。"""
        if explicit:
            return True
        combined = "\n".join(part for part in (user_text, draft_reply) if part)
        return bool(IMAGE_JUDGE_TRIGGER_RE.search(combined))

    def _should_block_chat_image(self, session_id: str, user_text: str, *, explicit: bool | None = None) -> bool:
        """聊天触发的配图（模型主动调工具或泄漏图片描述）是否应被冷却拦截。

        关闭档位一律拦截；其余档位在冷却期内拦截，但用户明确要图时放行。
        """
        freq = self.config.get("selfie_frequency", "频繁")
        if freq == "关闭":
            return True
        if explicit is None:
            explicit = self._user_requested_image(user_text)
        if explicit:
            return False
        rounds = session_schema.get_rounds_since_image(self._get_session_state(session_id))
        return rounds < self._image_min_gap(freq)

    def _recent_dialog_for_judge(self, state: dict[str, Any], limit: int = 6) -> str:
        lines = []
        for msg in self._active_chat_history(state, limit):
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            role = "用户" if msg.get("role") == "user" else "角色"
            lines.append(f"{role}: {content[:200]}")
        return "\n".join(lines)

    async def _judge_image_moment(self, session_id: str, user_text: str, draft_reply: str, *, explicit: bool = False) -> dict[str, Any] | None:
        """独立的配图时机判断器：由对话内容决定此刻是否自然地补一张图。

        故意做成一个干净、专注的小判断（关 thinking、只输出 JSON），这样模型会按内容
        老实判断，而不是像主 RP 调用那样沉浸在文字里不肯配图。最小间隔做硬约束，
        “很久没配图”只做软倾向，避免固定轮次的机械感。
        """
        if not self.has_llm_config("chat"):
            return None
        freq = self.config.get("selfie_frequency", "频繁")
        if freq == "关闭":
            return None
        state = self._get_session_state(session_id)
        rounds = session_schema.get_rounds_since_image(state)
        if not explicit and rounds < self._image_min_gap(freq):
            return None  # 刚发过图，留白（用户明确要图时不受冷却约束）
        if not self._should_run_image_judge(user_text, draft_reply, explicit=explicit):
            return None
        overdue = self._image_nudge_due(freq, rounds)
        tendency = {
            "极频繁": "门槛很低：稍微有点画面感、场景或情绪就发。",
            "频繁": "门槛偏低：有一定画面感、或用户想看时就发。",
            "适度": "门槛中等：画面感明确、展示穿搭/场景、或在推进暧昧/关系时才发。",
            "偶尔": "门槛较高：只在特别有画面感、或用户明显想看时才发。",
        }.get(freq, "门槛中等。")
        recent = self._recent_dialog_for_judge(state)
        now = self._session_now(session_id)
        light_guard = self._format_light_guard(session_id, now=now)
        system = (
            "你是角色扮演配图时机判断器。判断“此刻给用户发一张角色的自拍/场景图”是否自然且加分。\n"
            "适合发：用户想看角色、聊到穿搭/外貌/场景、画面感强、调情或推进氛围的时刻。\n"
            "不适合发：纯逻辑问答、简单寒暄确认、话题与画面无关、角色刚回复没有可拍的动作/地点/穿搭、或刚刚才发过图。\n"
            "若决定发图，intent 必须严格贴合“角色刚回复”的地点、动作、情绪和用户刚说的话；不要另起一个新场景，不要改写成角色刚才没提到的地点。\n"
            "view 字段只有在用户或角色刚刚明确提出了硬视角/拍摄方式时才填写：例如自拍、对镜、拿手机拍、帮忙拍一张照片。\n"
            "像“凑近镜头”“看向镜头”“分享一下现在的样子”这类普通画面感，不等于自拍要求；同空间陪伴、一起看东西、递咖啡、坐在沙发上说话等日常场景，view 留空交给后续规划器判断。\n"
            f"{light_guard}\n"
            f"发图门槛: {tendency}\n"
            + ("已经较久没有配图了，如有合适时机可适当倾向于发。\n" if overdue else "")
            + "只输出严格 JSON: {\"send\": true/false, \"intent\": \"这张图要回应的对话意图(中文,具体)\", "
            "\"mood\": \"情绪或关系推进\", \"view\": \"selfie|mirror|pov|third 或留空\"}。"
            "send 为 false 时其余可留空。不要输出 JSON 以外的任何内容。"
        )
        user = (
            f"最近对话:\n{recent or '(无)'}\n\n"
            f"用户刚说: {user_text}\n"
            f"角色刚回复: {(draft_reply or '(无文字回复)')[:500]}"
        )
        try:
            text = await self._call_llm(system, user, temp=0.2, tag="image-judge", purpose="chat", disable_thinking=True, session_id=session_id)
            parsed = json.loads(re.sub(r"```json\s*|```\s*$", "", text).strip())
        except Exception as exc:
            logger.warning("image moment judge failed: %s", exc)
            return None
        if not isinstance(parsed, dict) or not parsed.get("send"):
            return None
        intent = (parsed.get("intent") or "").strip() or (user_text or "").strip()[:80] or "根据当前对话氛围自然配一张贴合的图"
        return {
            "intent": intent,
            "mood": (parsed.get("mood") or "").strip(),
            "view": _sanitize_judge_view_hint(
                parsed.get("view"),
                intent,
                parsed.get("mood") or "",
                user_text,
                draft_reply,
            ),
        }

    def _checkpoint_context(self, session_id: str) -> str:
        try:
            cp = self.app_store.get_checkpoint(session_id, self._context_character_key(session_id))
            summary = (cp.get("summary") or "").strip()
            if summary:
                return summary
        except Exception:
            logger.debug("checkpoint lookup failed", exc_info=True)
        state = self._get_session_state(session_id)
        return session_schema.get_checkpoint_summary(state)

    def _character_history_summary_context(self, session_id: str) -> str:
        key = self._context_character_key(session_id)
        try:
            meta = self.app_store.get_context_meta(session_id, key)
            summary = (meta.get("character_history_summary") or "").strip()
            if summary:
                return summary
        except Exception:
            logger.debug("character history summary lookup failed", exc_info=True)
        state = self._get_session_state(session_id)
        return session_schema.get_character_history_summary(state)

    def _checkpoint_summary_durable_context(self, session_id: str, character_key: str | None = None) -> str:
        """给 checkpoint 摘要器看的长期依据，只用于去重和归属判断。"""
        key = self._context_character_key(session_id) if character_key is None else str(character_key or "").strip()
        active_key = self._context_character_key(session_id)
        parts: list[str] = []
        history_summary = ""
        try:
            history_summary = (self.app_store.get_context_meta(session_id, key).get("character_history_summary") or "").strip()
        except Exception:
            logger.debug("character history summary lookup for checkpoint failed", exc_info=True)
        if not history_summary and key == active_key:
            history_summary = session_schema.get_character_history_summary(self._get_session_state(session_id))
        if history_summary:
            parts.append(f"角色历史提要（宏观关系/重大事件/个人轨迹，不要在 checkpoint 中复述）:\n{history_summary}")
        try:
            if key == active_key:
                memory_context = self._long_term_memory_context(session_id)
            else:
                memories = self.memory.context_memories(
                    session_id,
                    "",
                    character=key,
                    limit=self._long_memory_limit(),
                )
                memory_context = format_memory_lines(memories, with_ids=False) if memories else ""
        except Exception:
            memory_context = ""
            logger.debug("long memory context lookup for checkpoint failed", exc_info=True)
        if memory_context:
            parts.append(f"长期记忆（稳定事实/偏好/边界/纠正，不要在 checkpoint 中复述）:\n{memory_context}")
        return "\n\n".join(parts)

    def _build_scene_system_prompt(self, session_id: str, *, weather: Any = None, mode: str = "image", now: Any = None) -> str:
        """构建场景生成用的完整 system prompt，复用聊天侧的静态 + 稳定 + 动态上下文。

        返回拼好的单个 system 字符串，供 _llm_write_scene / plan_roleplay_image 使用。
        调用方只需追加场景特定的模式指令和 JSON 输出要求。
        """
        state = self._get_session_state(session_id)
        if now is None:
            now = self._session_now(session_id)
        weekday = WEEKDAY_NAMES[now.weekday()]
        time_ctx = self._get_time_context(session_id, now=now, weather=weather)
        time_period = time_ctx.get("period") or self._get_time_period(now.hour)

        # ── 静态前缀 ──（不含穿搭：见下方动态层「当前附加外貌」，避免双注入+毒化前缀缓存）
        persona = self._get_effective_persona(session_id, include_appearance=False)
        role_name, bot_name, bot_self_name = self._session_role_identity(session_id)
        relationship = self._get_session_cfg(session_id, "spatial_relationship", "")
        rel_line = f"你和用户的关系: {str(relationship).strip()}。\n" if str(relationship).strip() else ""
        system_static = (
            f"{persona}\n\n"
            f"你当前扮演的角色是「{bot_name}」（{role_name}）。对话中按角色习惯使用「{bot_self_name}」或自然第一人称作为自称。\n"
            f"{rel_line}"
        )

        # ── 天级/低频稳定上下文 + 半稳定快照 + checkpoint ──
        durable_parts: list[str] = []
        scene_control = self._scene_low_frequency_context(session_id)
        if scene_control:
            durable_parts.append(scene_control)
        history_summary = self._character_history_summary_context(session_id)
        if history_summary:
            durable_parts.append(f"角色历史提要（宏观关系与剧情发展脉络）:\n{history_summary}")
        memory_context = self._long_term_memory_context(session_id)
        if memory_context:
            durable_parts.append(f"长期记忆（高重要度稳定事实/偏好/边界）:\n{memory_context}")

        semistable_parts: list[str] = []
        dynamic = self._effective_dynamic_appearance(session_id)
        if dynamic:
            semistable_parts.append(f"当前附加外貌: {dynamic}")

        checkpoint_context = self._checkpoint_context(session_id)
        checkpoint_part = f"Checkpoint（近期已折叠对话连续性，仅承接当前/最近场景）:\n{checkpoint_context}" if checkpoint_context else ""

        # ── 动态上下文 ──
        time_light = self._format_time_context(session_id, now=now, weather=weather)
        light_guard = self._format_light_guard(session_id, now=now, weather=weather)
        safety = self._get_effective_safety(session_id)
        system_dynamic = (
            f"当前时间: {now.strftime('%H:%M')} ({weekday}) {time_period}。\n"
            f"季节与自然光: {time_light}。\n"
            f"{light_guard}\n"
            f"当前场合: {time_period}, {weekday}, {safety.get('context', '')}。\n"
        )

        # 对话场景连续性（近 3h 内的对话 + 照片）
        continuity = self._format_scene_continuity_context(state, session_id, now=now) if hasattr(self, "_format_scene_continuity_context") else ""

        # 世界状态
        world_context = ""
        if hasattr(self, "_format_world_context"):
            try:
                world_context = self._format_world_context(session_id, "", weather=weather, mode=mode, now=now)
            except Exception:
                logger.debug("world context build failed for scene prompt", exc_info=True)

        # ── 拼接 ──
        parts = [system_static]
        if durable_parts:
            parts.append("\n\n".join(durable_parts))
        if semistable_parts:
            parts.append("\n\n".join(semistable_parts))
        if checkpoint_part:
            parts.append(checkpoint_part)
        parts.append(system_dynamic)
        if world_context:
            parts.append(world_context)
        if continuity:
            parts.append(continuity)
        return "\n\n".join(parts)

    def _context_character_key(self, session_id: str) -> str:
        return self._memory_character(session_id) if hasattr(self, "_memory_character") else ""

    def _context_window_message_limit(self) -> int:
        try:
            return max(10, int(self.config.get("context_window_message_limit", "50") or 50))
        except Exception:
            return 50

    def _history_storage_cap(self) -> int:
        """chat_history 的存储兜底上限：取 checkpoint 周期阈值的 3 倍，远大于正常运行所需。

        发给模型的窗口由 _active_chat_history 固定窗口决定，这里只在 checkpoint
        长期失联（任务 hang、反复异常）时防止 chat_history 无限膨胀，正常运行永不触及。
        """
        return self._context_window_message_limit() * 3

    def _checkpoint_keep_message_limit(self) -> int:
        try:
            return max(2, int(self.config.get("checkpoint_keep_message_limit", "10") or 10))
        except Exception:
            return 10

    @staticmethod
    def _trim_history_preserve_turns(history: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if len(history) <= limit:
            return list(history)
        trimmed = list(history[-limit:])
        while trimmed and trimmed[0].get("role") != "user":
            trimmed.pop(0)
        return trimmed or list(history[-limit:])

    @classmethod
    def _split_checkpoint_overflow(cls, history: list[dict[str, Any]], keep: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        kept = cls._trim_history_preserve_turns(history, keep)
        overflow_count = max(0, len(history) - len(kept))
        return list(history[:overflow_count]), kept

    def _apply_history_trim(self, state: dict[str, Any], limit: int):
        """把 chat_history 从头部裁到 limit 条，并同步下移 short_context_start。

        short_context_start 是 chat_history 的下标（短期场景起点）。从头部删消息会让
        所有下标左移，必须同量下移这个起点，否则 _active_chat_history 的切片会错位
        （丢掉本应保留的当前场景消息，甚至取到空窗口）。
        """
        history = session_schema.get_chat_history(state)
        if len(history) <= limit:
            return
        trimmed = self._trim_history_preserve_turns(history, limit)
        removed = len(history) - len(trimmed)
        session_schema.set_chat_history(state, trimmed)
        if removed > 0:
            start = session_schema.get_short_context_start(state)
            session_schema.set_short_context_start(state, max(0, start - removed))

    @staticmethod
    def _sanitize_user_history_text(text: str) -> str:
        """入历史前去掉 Telegram 输入增强里的当前输入标题，保留真实文本/引用/图片描述。"""
        text = str(text or "").strip()
        if not text:
            return ""
        return re.sub(r"(?m)^【用户当前输入】\s*\n?", "", text).strip()

    @classmethod
    def _sanitize_history_message(cls, msg: dict[str, Any]) -> dict[str, Any]:
        if (msg.get("role") or "") != "user":
            return dict(msg)
        cleaned = dict(msg)
        cleaned["content"] = cls._sanitize_user_history_text(str(cleaned.get("content") or ""))
        return cleaned

    def _queue_checkpoint_if_needed(self, session_id: str, history_snapshot: list[dict[str, Any]] | None = None):
        if not session_id:
            return
        key = self._context_character_key(session_id)
        scope = f"{session_id}\n{key}"
        task = getattr(self, "_checkpoint_tasks", {}).get(scope)
        if task and not task.done():
            return
        limit = self._context_window_message_limit()
        keep = self._checkpoint_keep_message_limit()
        try:
            checkpoint = self.app_store.get_checkpoint(session_id, key)
            pending = self.app_store.list_messages(session_id, key, after_id=int(checkpoint.get("source_until_id") or 0))
            msg_over = len(pending) > limit
            char_over = not msg_over and sum(len(str(m.get("content") or "")) for m in pending) > 30000
            if not msg_over and not char_over:
                return
        except Exception:
            if not history_snapshot:
                return
            msg_over = len(history_snapshot) > limit
            char_over = not msg_over and sum(len(str(m.get("content") or "")) for m in history_snapshot) > 30000
            if not msg_over and not char_over:
                return
        # 只排后台任务，不 await；checkpoint 摘要和记忆提取不能阻塞本轮聊天回复。
        self._checkpoint_tasks[scope] = asyncio.create_task(self._run_context_checkpoint(session_id, key, keep))

    async def _run_context_checkpoint(
        self,
        session_id: str,
        character_key: str,
        keep: int,
        *,
        force: bool = False,
        extract_memory: bool = True,
    ):
        try:
            checkpoint = self.app_store.get_checkpoint(session_id, character_key)
            pending = self.app_store.list_messages(session_id, character_key, after_id=int(checkpoint.get("source_until_id") or 0))
            if (
                not force
                and len(pending) <= self._context_window_message_limit()
                and sum(len(str(m.get("content") or "")) for m in pending) <= 30000
            ):
                return
            overflow, _kept = self._split_checkpoint_overflow(pending, keep)
            if not overflow:
                return
            previous = checkpoint.get("summary") or ""
            merged = await self._summarize_checkpoint(session_id, previous, overflow, character_key=character_key)
            hard = self._checkpoint_hard_limit_chars()
            if len(merged) > hard:
                merged = merged[-hard:]
            until_id = int(overflow[-1]["id"])
            # Extract stable long-term memories from the overflow before committing checkpoint.
            if extract_memory:
                try:
                    await self._extract_long_term_memories_from_messages(session_id, overflow, source_type="checkpoint", character=character_key)
                except Exception:
                    logger.warning("checkpoint memory extraction failed", exc_info=True)
            if self._context_character_key(session_id) != character_key:
                # 摘要/提取期间用户已切换角色：SQLite 按旧 key 落库是安全的，
                # 但不能把旧角色摘要写进新角色的 live state，更不能裁剪新角色的 chat_history。
                self.app_store.upsert_checkpoint(session_id, character_key, merged, until_id)
                self._ulog(session_id, "CHECKPOINT", f"until=#{until_id} chars={len(merged)} (角色已切换, 仅落库)")
                return
            state = self._get_session_state(session_id)
            if hasattr(self, "_sync_wardrobe_checkpoint_events"):
                self._sync_wardrobe_checkpoint_events(session_id, state, pending, overflow)
            self.app_store.upsert_checkpoint(session_id, character_key, merged, until_id)
            session_schema.set_checkpoint_summary(state, merged)
            session_schema.set_checkpoint_message_id(state, until_id)
            session_schema.set_last_checkpoint_at(state, time.time())
            # Trim only after the summary has been committed (同步 short_context_start)。
            self._apply_history_trim(state, keep)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "CHECKPOINT", f"until=#{until_id} chars={len(merged)}")
        except Exception:
            logger.warning("context checkpoint failed", exc_info=True)

    async def _checkpoint_context_before_push(self, session_id: str) -> bool:
        """推送前把未折叠上下文收敛到最近一个用户起点之后。

        普通 checkpoint 按窗口长度保留尾部 N 条；推送 planner 更看重前缀稳定，所以这里
        特化为：checkpoint 掉最近一条 user 之前的全部消息，只保留“上一句用户消息以及
        从这一句开始的回复/照片记录”。若当前未折叠窗口已经从这条 user 开始，则不改动。
        """
        if not session_id:
            return False
        key = self._context_character_key(session_id)
        scope = f"{session_id}\n{key}"
        task = getattr(self, "_checkpoint_tasks", {}).get(scope)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("cancelled pending checkpoint before push", exc_info=True)
        try:
            checkpoint = self.app_store.get_checkpoint(session_id, key)
            source_until = int(checkpoint.get("source_until_id") or 0)
            pending = self.app_store.list_messages(session_id, key, after_id=source_until)
            if not pending:
                return False
            keep_start = -1
            for idx in range(len(pending) - 1, -1, -1):
                if pending[idx].get("role") == "user":
                    keep_start = idx
                    break
            if keep_start <= 0:
                return False
            overflow = pending[:keep_start]
            kept = pending[keep_start:]
            previous = checkpoint.get("summary") or ""
            merged = await self._summarize_checkpoint(session_id, previous, overflow, character_key=key)
            hard = self._checkpoint_hard_limit_chars()
            if len(merged) > hard:
                merged = merged[-hard:]
            until_id = int(overflow[-1]["id"])
            try:
                await self._extract_long_term_memories_from_messages(session_id, overflow, source_type="push-checkpoint", character=key)
            except Exception:
                logger.warning("push checkpoint memory extraction failed", exc_info=True)
            if self._context_character_key(session_id) != key:
                # 摘要期间角色已切换：只按旧 key 落库，不动新角色的 live state 与历史。
                self.app_store.upsert_checkpoint(session_id, key, merged, until_id)
                return True
            state = self._get_session_state(session_id)
            if hasattr(self, "_sync_wardrobe_checkpoint_events"):
                self._sync_wardrobe_checkpoint_events(session_id, state, pending, overflow)
            self.app_store.upsert_checkpoint(session_id, key, merged, until_id)
            session_schema.set_checkpoint_summary(state, merged)
            session_schema.set_checkpoint_message_id(state, until_id)
            session_schema.set_last_checkpoint_at(state, time.time())
            session_schema.set_chat_history(state, [
                {"role": str(msg.get("role") or ""), "content": str(msg.get("content") or "")}
                for msg in kept
                if str(msg.get("role") or "").strip() and str(msg.get("content") or "").strip()
            ])
            session_schema.set_short_context_start(state, 0)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "CHECKPOINT", f"push-prep until=#{until_id} kept={len(kept)} chars={len(merged)}")
            return True
        except Exception:
            logger.warning("push pre-checkpoint failed", exc_info=True)
            return False

    def _checkpoint_hard_limit_chars(self) -> int:
        try:
            return max(500, int(self.config.get("checkpoint_hard_limit_chars", "3000") or 3000))
        except Exception:
            return 3000

    async def _summarize_checkpoint(
        self,
        session_id: str,
        previous: str,
        messages: list[dict[str, Any]],
        *,
        character_key: str | None = None,
    ) -> str:
        soft = str(self.config.get("checkpoint_soft_limit_chars", "2000") or "2000")
        dialog = self._format_store_messages(messages, limit_chars=18000)
        role_legend = self._dialog_role_legend()
        durable_context = self._checkpoint_summary_durable_context(session_id, character_key)
        durable_rules = (
            "Use durable context only as a de-duplication and ownership reference. "
            "Stable user facts, preferences, boundaries, and corrections belong to long-term memory; "
            "macro relationship arcs, major event ledger, character trajectory, and acting direction belong to character history. "
            "Checkpoint should keep only short-term current-scene continuity, unresolved near-term actions, latest explicit state, and immediate promises. "
            "Drop expired, resolved, superseded, or no-longer-actionable short-term facts. "
            "Do not restate durable context unless a tiny pointer is necessary for continuity. "
        )
        if not self.has_llm_config("image", session_id):
            if not self.has_llm_config("chat", session_id):
                combined = (previous + "\n" if previous else "") + dialog
                return combined[-int(soft):]
            # 回退到 chat 模型
            system = (
                "You are a checkpoint summarizer for a long roleplay chat. Merge the existing checkpoint "
                "and the overflowed dialogue into one short-term continuity summary for the next few turns only. "
                "Focus on the current or most recent scene, unfinished actions, immediate emotions, near-term promises, "
                "current places, and the latest photo only when the dialogue is directly responding to it. "
                "Actively preserve explicit time anchors mentioned by the user, such as dates, clock times, deadlines, "
                "appointments, countdowns, and relative time nodes, with their related event and status when stated. "
                f"{durable_rules}"
                "Do not duplicate character-history arcs, durable relationship progress, permanent profile facts, "
                "stable preferences, boundaries, corrections, or memory-worthy facts. "
                f"Soft limit: {soft} Chinese characters. Output only the summary text. "
                "Do not invent, infer, or add details not explicitly present in the source dialogue. "
                "Only include rules, promises, constraints, or events that were literally stated by the user or character. "
                "If uncertain, omit rather than fabricate. "
                f"{role_legend} Keep ownership clear: Assistant is the bot character's speech/actions; User is the human user's speech/actions. "
                "Do not swap their perspective, emotions, promises, or physical actions."
            )
            user = (
                f"Durable context for de-duplication only:\n{durable_context or 'none'}\n\n"
                f"Existing checkpoint:\n{previous or 'none'}\n\n"
                f"Dialogue role legend:\n{role_legend}\n\nOverflow dialogue:\n{dialog}"
            )
            return await self._call_llm(system, user, temp=0.1, tag="checkpoint", purpose="chat", disable_thinking=True, session_id=session_id)
        system = (
            "You are a checkpoint summarizer for a long roleplay chat. Merge the existing checkpoint "
            "and the overflowed dialogue into one short-term continuity summary for the next few turns only. "
            "Focus on the current or most recent scene, unfinished actions, immediate emotions, near-term promises, "
            "current places, and the latest photo only when the dialogue is directly responding to it. "
            "Actively preserve explicit time anchors mentioned by the user, such as dates, clock times, deadlines, "
            "appointments, countdowns, and relative time nodes, with their related event and status when stated. "
            f"{durable_rules}"
            "Do not duplicate character-history arcs, durable relationship progress, permanent profile facts, "
            "stable preferences, boundaries, corrections, or memory-worthy facts. "
            f"Soft limit: {soft} Chinese characters. Output only the summary text. "
            "Do not invent, infer, or add details not explicitly present in the source dialogue. "
            "Only include rules, promises, constraints, or events that were literally stated by the user or character. "
            "If uncertain, omit rather than fabricate. "
            f"{role_legend} Keep ownership clear: Assistant is the bot character's speech/actions; User is the human user's speech/actions. "
            "Do not swap their perspective, emotions, promises, or physical actions."
        )
        user = (
            f"Durable context for de-duplication only:\n{durable_context or 'none'}\n\n"
            f"Existing checkpoint:\n{previous or 'none'}\n\n"
            f"Dialogue role legend:\n{role_legend}\n\nOverflow dialogue:\n{dialog}"
        )
        return await self._call_llm(system, user, temp=0.1, tag="checkpoint", purpose="image", disable_thinking=True, session_id=session_id)

    @staticmethod
    def _dialog_role_legend() -> str:
        return "User = human user; Assistant = the current bot roleplay character."

    @staticmethod
    def _format_store_messages(messages: list[dict[str, Any]], limit_chars: int = 50000, roles: set[str] | None = None) -> str:
        entries: list[tuple[str, str]] = []
        for msg in messages:
            role = msg.get("role") or ""
            if roles is not None and role not in roles:
                continue
            name = "User" if role == "user" else "Assistant" if role == "assistant" else role
            content = str(msg.get("content") or "").strip()
            if role == "user":
                content = ChatContextMixin._sanitize_user_history_text(content)
            if content:
                entries.append((role, f"{name}: {content}"))
        if not entries:
            return ""
        groups: list[list[str]] = []
        current: list[str] = []
        for role, line in entries:
            if role == "user" and current:
                groups.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            groups.append(current)

        group_texts = ["\n".join(group) for group in groups]
        text = "\n".join(group_texts)
        if len(text) <= limit_chars:
            return text

        selected: list[str] = []
        total = 0
        for group in reversed(group_texts):
            extra = len(group) + (1 if selected else 0)
            if total + extra <= limit_chars:
                selected.append(group)
                total += extra
                continue
            if not selected:
                return group[-limit_chars:]
            break
        selected.reverse()
        return "\n".join(selected)

    async def _extract_long_term_memories_from_messages(self, session_id: str, messages: list[dict[str, Any]], source_type: str = "checkpoint", character: str | None = None):
        if not messages or not hasattr(self, "_extract_long_term_memories"):
            return
        dialog = self._format_store_messages(messages, limit_chars=20000)
        await self._extract_long_term_memories(session_id, f"[{source_type}]\n{dialog}", "", character=character)

    async def _checkpoint_current_context_before_reset(self, session_id: str) -> int:
        """新场景/短期硬切换前，先把当前未折叠上下文过一遍 checkpoint 侧的摘要与记忆提取。"""
        if not session_id:
            return 0
        key = self._context_character_key(session_id)
        scope = f"{session_id}\n{key}"
        task = getattr(self, "_checkpoint_tasks", {}).get(scope)
        if task and not task.done():
            task.cancel()
        latest_id = 0
        try:
            checkpoint = self.app_store.get_checkpoint(session_id, key)
            source_until = int(checkpoint.get("source_until_id") or 0)
            latest_id = self.app_store.latest_message_id(session_id, key)
            pending = self.app_store.list_messages(session_id, key, after_id=source_until, before_or_equal_id=latest_id)
            if not pending:
                return latest_id
            previous = checkpoint.get("summary") or ""
            merged = await self._summarize_checkpoint(session_id, previous, pending, character_key=key)
            hard = self._checkpoint_hard_limit_chars()
            if len(merged) > hard:
                merged = merged[-hard:]
            until_id = int(pending[-1]["id"])
            try:
                await self._extract_long_term_memories_from_messages(session_id, pending, source_type="checkpoint", character=key)
            except Exception:
                logger.warning("pre-reset checkpoint memory extraction failed", exc_info=True)
            self.app_store.upsert_checkpoint(session_id, key, merged, until_id)
            state = self._get_session_state(session_id)
            session_schema.set_checkpoint_summary(state, merged)
            session_schema.set_checkpoint_message_id(state, until_id)
            session_schema.set_last_checkpoint_at(state, time.time())
            self._save_session_state(session_id, state)
            self._ulog(session_id, "CHECKPOINT", f"pre-reset until=#{until_id} chars={len(merged)}")
            return until_id
        except Exception:
            logger.warning("pre-reset context checkpoint failed", exc_info=True)
            try:
                return latest_id or self.app_store.latest_message_id(session_id, key)
            except Exception:
                return latest_id

    def _short_context_reset_reason(self, text: str, previous_interaction: float = 0) -> str:
        if SHORT_CONTEXT_RESET_RE.search(text or ""):
            return "用户显式切换或结束上一话题/场景"
        try:
            gap_hours = float(self.config.get("short_context_reset_gap_hours", "2") or 0)
        except Exception:
            gap_hours = 6
        if gap_hours > 0 and previous_interaction and time.time() - previous_interaction > gap_hours * 3600:
            return f"距离上次互动超过 {gap_hours:g} 小时，开启新的短期上下文"
        return ""

    def _reset_short_context(self, state: dict[str, Any], reason: str, *, session_id: str = ""):
        # 短期/新场景重置：清空模型侧未折叠历史 + 轻量近期 buffer，但**位置不硬清空**（连续而非瞬移）：
        #   · user_place：交给 4h TTL 自然老化（B 方案）——换话题不代表用户物理移动；
        #   · character_place：降级为 weak（见 _demote_character_place），新场景不钉死生图、仍作背景，
        #     等新场景的位置声明覆盖或 TTL 过期。
        # 两个位置字段在本路径**对称处理**（都不清空），消除原先“SR 清 user 不清 character”的不对称。
        session_schema.set_chat_history(state, [])
        session_schema.set_short_context_start(state, 0)
        session_schema.set_short_context_reset_time(state, time.time())
        session_schema.set_short_context_reset_reason(state, reason)
        session_schema.set_recent_message_history(state, [])
        latest_id = 0
        if session_id:
            key = self._context_character_key(session_id)
            scope = f"{session_id}\n{key}"
            task = getattr(self, "_checkpoint_tasks", {}).get(scope)
            if task and not task.done():
                task.cancel()
            try:
                latest_id = self.app_store.latest_message_id(session_id, key)
                self.app_store.clear_checkpoint(session_id, key, source_until_id=latest_id)
            except Exception:
                logger.warning("short context checkpoint clear failed", exc_info=True)
        session_schema.set_checkpoint_summary(state, "")
        session_schema.set_checkpoint_message_id(state, latest_id)
        session_schema.set_last_checkpoint_at(state, time.time() if session_id else 0)
        session_schema.clear_wardrobe_semistable_snapshot(state)
        session_schema.clear_wardrobe_observed_snapshot(state)
        self._demote_character_place(state)
        session_schema.clear_nudity(state)  # 新场景：不再续上上一幕的裸体态

    @staticmethod
    def _active_chat_history(state: dict[str, Any], limit: int = 16) -> list[dict[str, Any]]:
        history = session_schema.get_chat_history(state)
        try:
            start = session_schema.get_short_context_start(state)
        except Exception:
            start = 0
        if start < 0 or start > len(history):
            start = 0
        return [ChatContextMixin._sanitize_history_message(msg) for msg in history[start:][-limit:]]

    @staticmethod
    def _chat_prompt_history(state: dict[str, Any]) -> list[dict[str, Any]]:
        """聊天 prompt 的历史窗口：checkpoint 锚定，取短期场景起点之后的全部未折叠消息。

        故意不做 [-N:] 逐轮滑动——长度由 checkpoint 折叠（每周期把溢出折进摘要、尾部裁到
        keep）与 _history_storage_cap 兜底共同约束。这样两次 checkpoint 之间历史前缀只增不
        移，最大化服务端 prefix cache 命中；只有 checkpoint 落地那一刻才发生一次前缀归位。
        """
        history = session_schema.get_chat_history(state)
        try:
            start = session_schema.get_short_context_start(state)
        except Exception:
            start = 0
        if start < 0 or start > len(history):
            start = 0
        return [ChatContextMixin._sanitize_history_message(msg) for msg in history[start:]]

    def _inject_photo_history_messages(self, messages: list[dict[str, Any]], state: dict[str, Any]):
        # 兼容旧调用点：照片视觉记录现在在 _record_sent_photo 时写入 chat_history
        # 的 system 消息，并随普通历史一起保留/裁剪；这里不再做每轮动态注入。
        return

