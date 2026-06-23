from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from . import session_schema
from .defaults import WEEKDAY_NAMES

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
SHORT_CONTEXT_RESET_RE = re.compile(
    r"(换个话题|换话题|换一?个场景|新场景|下一幕|下一段|另起|说点别的|聊点别的|不说这个|先不说|不聊这个|别提这个|跳过这个|结束这个|这个话题到此|算了|重新开始|从头来|回到正题)"
)

class ChatContextMixin:
    async def handle_chat(self, chat_id: int | str, session_id: str, text: str):
        state = self._get_session_state(session_id)
        previous_interaction = state.get("last_interaction", 0)
        reset_reason = self._short_context_reset_reason(text, previous_interaction)
        self._touch(session_id)
        self._schedule_weather_refresh(session_id)  # 天气缓存过期则后台刷新，避免聊天天气停在早安推送那次
        if reset_reason:
            self._reset_short_context(state, reset_reason)
        self._update_user_place_from_text(session_id, text)
        state["last_message_text"] = text
        state["last_message_time"] = time.time()
        state["recent_message_history"] = (state.get("recent_message_history", []) + [{"text": text, "time": time.time()}])[-5:]

        if state.get("last_sent_selfie_time", 0) and not state.get("last_sent_selfie_replied", False):
            if self._within(state["last_sent_selfie_time"], 12 * 3600):
                state["replying_to_selfie"] = True
            state["last_sent_selfie_replied"] = True

        state["rounds_since_image"] = state.get("rounds_since_image", 0) + 1
        # "距上次确认位置的轮数"：每轮 +1，由 _set_character_place（角色再次明确位置时）清零。
        # 用来给陈旧 pin 降权——多轮没再提及地点时，该 pin 不再锁死生图。
        session_schema.increment_rounds_since_location(state)
        if state.get("ntr_affection_reset"):
            self._tick_ntr_reconcile(state)

        self._save_session_state(session_id, state)

        if not self.has_llm_config("chat", session_id):
            await self.send_message(chat_id, "聊天与角色扮演模型未配置，聊天和工具触发不可用。命令功能仍可使用。")
            return

        await self.send_action(chat_id, "typing")
        reply = await self.run_roleplay_chat(chat_id, session_id, text)
        if reply:
            self._ulog(session_id, "BOT", reply)
            split = str(self.config.get("chat_split_paragraphs", "true")).lower() in ("true", "1", "yes")
            await self.send_message(chat_id, reply, split_paragraphs=split)
        else:
            await self.send_message(chat_id, "回复生成失败，请稍后重试。")

    async def run_roleplay_chat(self, chat_id: int | str, session_id: str, user_text: str) -> str:
        state = self._get_session_state(session_id)
        if hasattr(self, "_ensure_life_profile"):
            # 角色生活档案（年龄段/白天职场）按人设推断并缓存：命中缓存时无开销，仅人设变动才重算。
            try:
                await self._ensure_life_profile(session_id)
            except Exception:
                logger.debug("ensure life profile failed", exc_info=True)
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
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("LLM request failed: %s", exc)
            return ""

        assistant = result.get("choices", [{}])[0].get("message", {})
        content = (assistant.get("content") or "").strip()
        tool_calls = assistant.get("tool_calls") or []

        explicit_image_req = self._user_requested_image(user_text)

        image_emitted = False
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
                    continue
                tool_result = await self._execute_tool_call(chat_id, session_id, call)
                if fn_name == "generate_roleplay_image":
                    image_emitted = True
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
                    session_id=session_id,
                )
                final_msg = final.get("choices", [{}])[0].get("message", {})
                content = (final_msg.get("content") or content or "").strip()
            except Exception as exc:
                logger.warning("final chat completion after tool call failed: %s", exc)

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
        if not image_emitted:
            judge_decision = await self._judge_image_moment(
                session_id, user_text, content, explicit=explicit_image_req
            )

        # 自动抽取角色自述位置并持久化（工具 update_location 是显式高置信路径，这里是 LLM 判定兜底）。
        # fire-and-forget：不阻塞回复返回，抽取结果下一轮生效。
        if content:
            async def _bg_extract():
                try:
                    await self._update_character_place_from_text(session_id, content)
                except Exception:
                    logger.warning("background location extract failed", exc_info=True)
            asyncio.create_task(_bg_extract())

        history = state.get("chat_history", [])
        new_messages = [{"role": "user", "content": user_text}]
        if content:
            new_messages.append({"role": "assistant", "content": content})
        history.extend(new_messages)
        try:
            self.app_store.append_messages(session_id, self._context_character_key(session_id), new_messages)
        except Exception:
            logger.warning("chat message sqlite append failed", exc_info=True)
        full_snapshot = list(history)
        # 仅做存储兜底裁剪（远高于 checkpoint 周期，正常不触及），并同步 short_context_start。
        # 发给模型的窗口由 _chat_prompt_history（checkpoint 锚定）决定，不在这里逐轮滑动。
        self._apply_history_trim(state, self._history_storage_cap())
        self._save_session_state(session_id, state)
        self._queue_checkpoint_if_needed(session_id, full_snapshot)
        if judge_decision:
            self._ulog(session_id, "JUDGE", f"配图时机=发 intent={judge_decision.get('intent','')[:60]}")
            asyncio.create_task(self.tool_generate_image(
                chat_id, session_id,
                intent=judge_decision.get("intent", ""),
                mood=judge_decision.get("mood", ""),
                prompt=content,
                view=judge_decision.get("view", ""),
            ))
        return content

    def _build_chat_messages(self, session_id: str, user_text: str) -> list[dict[str, Any]]:
        state = self._get_session_state(session_id)
        now = self._session_now(session_id)
        weekday = WEEKDAY_NAMES[now.weekday()]
        time_ctx = self._get_time_context(session_id, now=now)
        time_period = time_ctx.get("period") or self._get_time_period(now.hour)
        time_light = self._format_time_context(session_id, now=now)
        light_guard = self._format_light_guard(session_id, now=now)
        # 静态前缀不含穿搭（中频变化），避免换装作废整条历史前缀缓存；穿搭见下方动态层 visual_context。
        persona = self._get_effective_persona(session_id, include_appearance=False)
        role_name, bot_name, bot_self_name = self._session_role_identity(session_id)
        relationship = self._get_session_cfg(session_id, "spatial_relationship", "")
        rel_line = f"你和用户的关系: {str(relationship).strip()}。\n" if str(relationship).strip() else ""

        # ── 静态前缀（变化极低频：角色切换/配置变更才动）──
        # 放在 messages[0]，最大化 DeepSeek 服务端 prefix cache 命中率。
        system_static = (
            f"{persona}\n\n"
            f"你当前扮演的角色是「{bot_name}」（{role_name}）。除非用户明确要求换角色，否则你就是「{bot_name}」，"
            f"不要声称自己是其他角色或默认角色。对话中按角色习惯使用「{bot_self_name}」或自然第一人称作为自称，"
            "不要不自然地反复报全名。\n"
            f"{rel_line}"
            "当用户明示或暗示想看你的样子、照片、穿着或当前场景时，应调用 generate_roleplay_image。"
            "工具调用只需要描述这张图要回应的对话意图、情绪和必要元素；"
            "最终画面会由生图辅助模型结合完整上下文整合。不要把工具名、函数调用或内部指令写进聊天文字。"
            "\n换装持久化（重要）：当剧情里角色【换上新的服装/配饰】或【换了一套不同的穿搭】时，必须调用 change_appearance 工具记录这次变化，"
            "这样之后的配图和对话会保持一致。"
            "但注意：性爱/亲密行为/洗澡中的【临时脱衣/裸体】只是暂时的，场景结束后衣服会穿回来——不要为此调用 change_appearance，"
            "配图系统会自动处理临时裸露。脱掉外层（如外套、开衫）准备换上另一套时仍要走 change_appearance。"
            "不要只在文字里描述换装却不调用工具。"
            "\n位置持久化：当剧情里角色移动到新地点、或你明确交代了此刻在哪（出门、到公司、回家、到了某店等）时，调用 update_location 工具记录，"
            "这样之后的配图和推送会和你说的位置保持一致，不会无理由瞬移。位置没变就不用调。"
        )

        # ── 动态后缀（每请求变化：时间/光线/频率/世界状态/记忆）──
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

        system_dynamic = (
            f"当前时间: {now.strftime('%H:%M')} ({weekday}) {time_period}。\n"
            f"季节与自然光: {time_light}。\n"
            f"{light_guard}\n"
            f"纯度指令: {self._purity_directive(self._get_purity(session_id))}\n"
            f"外貌修改权限: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}。\n"
            f"发图频率: {freq_inst}\n"
        )
        length_directive = self._reply_length_directive()
        if length_directive:
            system_dynamic += f"{length_directive}\n"
        visual_context = self._chat_visible_appearance_context(session_id)
        if visual_context:
            system_dynamic += (
                "当前可见外型与配饰（这是你此刻身上真实可见的状态；用户问到外貌、穿搭、配饰或随身物时优先依据这里，"
                "不要编造不存在的配饰）：\n"
                f"{visual_context}\n"
            )
        closet_context = self._wardrobe_closet_context(session_id) if hasattr(self, "_wardrobe_closet_context") else ""
        if closet_context:
            system_dynamic += (
                "你的衣橱里收藏着这些穿过的衣服（你清楚自己有哪些）：\n"
                f"{closet_context}\n"
                "用户点名某件、或剧情/场合自然需要时（出门、睡前、洗澡后、约会等），可以让角色换上其中一件；不要无缘无故频繁换装。\n"
            )
        # 对话进行中：对话已建立的场景优先，动线只作背景；只有冷启动/刚换场景才以动线引导，
        # 避免角色随现实时间被算法"传送"（家→公园这类飘移）。对话态不钉死时钟地点（pin_location=False）。
        active_dialog = bool(self._chat_prompt_history(state))
        world_context = self._format_world_context(
            session_id, user_text, mode="chat", pin_location=not active_dialog
        )
        if world_context:
            if active_dialog:
                system_dynamic += (
                    f"\n{world_context}\n"
                    "以上是你的日常动线背景参考。当前正在进行的对话场景优先级最高："
                    "如果对话里你已经处在某个地点（在家、在车站、在仓库等），或刚说过自己在哪，就保持那个地点不变，"
                    "不要因为上面动线显示的时间点不同，就擅自把自己挪到别处。"
                    "只有在开启全新话题、对话出现明显时间跳跃、或需要交代你独自近况时，才依据动线更新所在地。"
                    "无论如何不要无理由瞬移；与用户不在同一地点时，用消息、自拍、电话或约定见面推进。\n"
                )
            else:
                system_dynamic += (
                    f"\n{world_context}\n"
                    "聊天时可参考这个世界状态自然提及所在与去向（例如“我现在在公司”、“等会儿要去逛商场”），但不要机械地报地点。"
                    "不要让角色无理由瞬移；如果用户和角色不在同一地点，优先用消息、自拍、电话或约定见面推进。\n"
                )
        if state.get("replying_to_selfie"):
            photos = state.get("sent_photos_history", [])
            last_photo = photos[-1] if photos else {}
            scene = (last_photo.get("scene") or "").strip()
            caption = (last_photo.get("caption") or "").strip()
            parts = []
            if scene:
                parts.append(f"画面: {scene}")
            if caption:
                parts.append(f"你给这张图配的台词: {caption}")
            if parts:
                system_dynamic += f"你刚向用户发了一张图。{'；'.join(parts)}。用户现在说:\n"
            else:
                fallback = state.get("last_sent_selfie_source_description") or ""
                if fallback:
                    system_dynamic += f"你刚向用户发了一张图，描述: {fallback}。用户现在说:\n"
            state["replying_to_selfie"] = False
        if state.get("short_context_start", 0):
            system_dynamic += (
                "短期注意规则: 用户已经切换过话题或场景。切换点之前的聊天、地点、动作、服装、冲突和图片只作历史背景，"
                "不要主动带入当前场景；只有用户明确说继续刚才、上一张、那个话题时才引用。\n"
            )

        # ── 稳定上下文（仅在 checkpoint / dream 时更新，变化低频，适合作前缀缓存）──
        # 包含：checkpoint 摘要、角色历史提要、长期记忆。放在历史之后、动态 system 之前，
        # 使 [静态 system] + [历史] + [稳定上下文] 构成可缓存前缀；仅动态部分（时间/光线/世界状态）逐轮冲刷。
        stable_parts: list[str] = []
        checkpoint_context = self._checkpoint_context(session_id)
        if checkpoint_context:
            stable_parts.append(
                "Checkpoint summary of earlier dialogue "
                "(use this as continuity background; do not reveal it):\n"
                f"{checkpoint_context}"
            )
        history_summary = self._character_history_summary_context(session_id)
        if history_summary:
            stable_parts.append(
                "角色历史提要（长期发展脉络，仅在相关时自然引用）:\n"
                f"{history_summary}"
            )
        memory_context = self._long_term_memory_context(session_id, user_text)
        if memory_context:
            stable_parts.append(
                "长期记忆（仅在相关时自然使用，不要逐条复述，不要暴露记忆系统）:\n"
                f"{memory_context}"
            )

        # 拼接顺序刻意为前缀缓存优化：
        #   [静态 system] + [聊天历史] + [照片注入] + [稳定上下文] + [动态 system] + [本轮 user]
        # 静态前缀与逐轮追加的历史构成稳定前缀；照片注入每隔几轮就变，必须放在历史之后，
        # 否则它一变就会作废后面整段历史的前缀缓存（checkpoint 攒下的命中红利会被清零）。
        # 稳定上下文（checkpoint/历史提要/记忆）变化低频，放在照片之后、动态之前，
        # 使 [静态+历史+照片+稳定] 在两次 checkpoint/dream 之间几乎不动，最大化前缀缓存命中。
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_static}]
        messages.extend(self._chat_prompt_history(state))
        self._inject_photo_history_messages(messages, state)
        if stable_parts:
            messages.append({"role": "system", "content": "\n\n".join(stable_parts)})
        messages.append({"role": "system", "content": system_dynamic})
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
                            "view": {"type": "string", "enum": ["selfie", "mirror", "pov", "third", "portrait"], "description": "用户明确要求视角时填写；否则留空交给生图辅助模型判断。selfie 是前摄自拍（伸手举手机，但画面不出现手机和手机 UI）；portrait 是别人帮角色拍的照片（用户/他人在画面外拍摄、角色看向镜头），仅在同处一地且角色明说要别人帮拍、或 NTR 场景时用；只有 mirror 对镜自拍才允许镜子和手机同时出现。"},
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
                        "当剧情里角色换上新的服装、换了一套穿搭、脱下外套/鞋袜等外层衣物时调用，持续生效。支持分层换装："
                        "上衣、下装、连衣裙、外套、内衣(胸罩/内裤)、袜、鞋可分别更换；同类自动替换，连衣裙会覆盖上下装；"
                        "也可脱掉某层或摘掉配饰。description 用自然语言描述这次变化即可（如「换上红色旗袍」「脱掉外套」「换黑色蕾丝内衣」）。"
                        "mode 一般用 merge；只有要整套从头换时才用 replace。"
                        "注意：性爱、亲密行为、洗澡中的临时脱衣/裸体，不要调用此工具——配图系统会自动处理临时裸露，场景结束后角色会自动恢复原着装。"
                        "只有确实换上了不同的衣服（且会继续穿着）时才调用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string", "description": "这次外观/换装变化的自然语言描述。"},
                            "mode": {"type": "string", "enum": ["merge", "replace"]},
                        },
                        "required": ["description"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_location",
                    "description": (
                        "当剧情里角色移动到新地点、或明确交代了自己此刻在哪时调用，持续生效，之后的配图和推送会据此保持一致。"
                        "place 用自然语言写角色当前所在（如“家里”“公司”“楼下咖啡店”“商场”“在路上”）。只在位置确实变化或首次确立时调用，不要每句都报。"
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
        if fn == "change_appearance":
            return await self.tool_change_appearance(session_id, args.get("description", ""), args.get("mode", "merge"))
        if fn == "update_location":
            return await self.tool_update_location(session_id, args.get("place", ""))
        return f"未知工具: {fn}"

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
        rounds = self._get_session_state(session_id).get("rounds_since_image", 0)
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
        rounds = state.get("rounds_since_image", 0)
        if not explicit and rounds < self._image_min_gap(freq):
            return None  # 刚发过图，留白（用户明确要图时不受冷却约束）
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
            "view": (parsed.get("view") or "").strip(),
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
        return str(state.get("checkpoint_summary") or "").strip()

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
        return str(state.get("character_history_summary") or "").strip()

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

        # ── 稳定上下文 ──
        stable_parts: list[str] = []
        checkpoint_context = self._checkpoint_context(session_id)
        if checkpoint_context:
            stable_parts.append(f"Checkpoint summary of earlier dialogue:\n{checkpoint_context}")
        history_summary = self._character_history_summary_context(session_id)
        if history_summary:
            stable_parts.append(f"角色历史提要:\n{history_summary}")
        memory_context = self._long_term_memory_context(session_id)
        if memory_context:
            stable_parts.append(f"长期记忆:\n{memory_context}")

        # ── 动态上下文 ──
        time_light = self._format_time_context(session_id, now=now, weather=weather)
        light_guard = self._format_light_guard(session_id, now=now, weather=weather)
        dynamic = self._effective_dynamic_appearance(session_id)
        prompt_prefs = self._prompt_scene_preferences(session_id) if hasattr(self, "_prompt_scene_preferences") else {}
        purity = self._get_purity(session_id)
        safety = self._get_effective_safety(session_id)
        system_dynamic = (
            f"当前时间: {now.strftime('%H:%M')} ({weekday}) {time_period}。\n"
            f"季节与自然光: {time_light}。\n"
            f"{light_guard}\n"
            f"当前附加外貌: {dynamic or '无'}\n"
            f"用户画面偏好: 场景偏好={prompt_prefs.get('scene_preference') or '无'}；自拍偏好={prompt_prefs.get('selfie_preference') or '无'}。\n"
            f"角色性观念: {self._purity_directive(purity)}\n"
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
        if stable_parts:
            parts.append("\n\n".join(stable_parts))
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

        发给模型的窗口由 checkpoint 折叠 + _chat_prompt_history 决定，这里只在 checkpoint
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

    def _apply_history_trim(self, state: dict[str, Any], limit: int):
        """把 chat_history 从头部裁到 limit 条，并同步下移 short_context_start。

        short_context_start 是 chat_history 的下标（短期场景起点）。从头部删消息会让
        所有下标左移，必须同量下移这个起点，否则 _chat_prompt_history 的切片会错位
        （丢掉本应保留的当前场景消息，甚至取到空窗口）。
        """
        history = state.get("chat_history", [])
        if len(history) <= limit:
            return
        trimmed = self._trim_history_preserve_turns(history, limit)
        removed = len(history) - len(trimmed)
        state["chat_history"] = trimmed
        if removed > 0:
            start = int(state.get("short_context_start", 0) or 0)
            state["short_context_start"] = max(0, start - removed)

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
        self._checkpoint_tasks[scope] = asyncio.create_task(self._run_context_checkpoint(session_id, key, keep))

    async def _run_context_checkpoint(self, session_id: str, character_key: str, keep: int):
        try:
            checkpoint = self.app_store.get_checkpoint(session_id, character_key)
            pending = self.app_store.list_messages(session_id, character_key, after_id=int(checkpoint.get("source_until_id") or 0))
            if len(pending) <= self._context_window_message_limit() and sum(len(str(m.get("content") or "")) for m in pending) <= 30000:
                return
            overflow = pending[:-keep]
            if not overflow:
                return
            previous = checkpoint.get("summary") or ""
            merged = await self._summarize_checkpoint(session_id, previous, overflow)
            hard = self._checkpoint_hard_limit_chars()
            if len(merged) > hard:
                merged = merged[-hard:]
            until_id = int(overflow[-1]["id"])
            # Extract stable long-term memories from the overflow before committing checkpoint.
            try:
                await self._extract_long_term_memories_from_messages(session_id, overflow, source_type="checkpoint")
            except Exception:
                logger.warning("checkpoint memory extraction failed", exc_info=True)
            self.app_store.upsert_checkpoint(session_id, character_key, merged, until_id)
            state = self._get_session_state(session_id)
            state["checkpoint_summary"] = merged
            state["checkpoint_message_id"] = until_id
            state["last_checkpoint_at"] = time.time()
            # Trim only after the summary has been committed (同步 short_context_start)。
            self._apply_history_trim(state, keep)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "CHECKPOINT", f"until=#{until_id} chars={len(merged)}")
        except Exception:
            logger.warning("context checkpoint failed", exc_info=True)

    def _checkpoint_hard_limit_chars(self) -> int:
        try:
            return max(500, int(self.config.get("checkpoint_hard_limit_chars", "3000") or 3000))
        except Exception:
            return 3000

    async def _summarize_checkpoint(self, session_id: str, previous: str, messages: list[dict[str, Any]]) -> str:
        soft = str(self.config.get("checkpoint_soft_limit_chars", "2000") or "2000")
        dialog = self._format_store_messages(messages, limit_chars=18000)
        if not self.has_llm_config("image", session_id):
            if not self.has_llm_config("chat", session_id):
                combined = (previous + "\n" if previous else "") + dialog
                return combined[-int(soft):]
            # 回退到 chat 模型
            system = (
                "You are a checkpoint summarizer for a long roleplay chat. Merge the existing checkpoint "
                "and the overflowed dialogue into one continuous background summary. Keep relationships, "
                "promises, unresolved events, important emotions, places, and confirmed facts. "
                f"Soft limit: {soft} Chinese characters. Output only the summary text."
            )
            user = f"Existing checkpoint:\n{previous or 'none'}\n\nOverflow dialogue:\n{dialog}"
            return await self._call_llm(system, user, temp=0.1, tag="checkpoint", purpose="chat", disable_thinking=True, session_id=session_id)
        system = (
            "You are a checkpoint summarizer for a long roleplay chat. Merge the existing checkpoint "
            "and the overflowed dialogue into one continuous background summary. Keep relationships, "
            "promises, unresolved events, important emotions, places, and confirmed facts. "
            f"Soft limit: {soft} Chinese characters. Output only the summary text."
        )
        user = f"Existing checkpoint:\n{previous or 'none'}\n\nOverflow dialogue:\n{dialog}"
        return await self._call_llm(system, user, temp=0.1, tag="checkpoint", purpose="image", disable_thinking=True, session_id=session_id)

    @staticmethod
    def _format_store_messages(messages: list[dict[str, Any]], limit_chars: int = 50000) -> str:
        lines = []
        for msg in messages:
            role = msg.get("role") or ""
            name = "User" if role == "user" else "Assistant" if role == "assistant" else role
            content = str(msg.get("content") or "").strip()
            if content:
                lines.append(f"{name}: {content}")
        text = "\n".join(lines)
        return text[-limit_chars:] if len(text) > limit_chars else text

    async def _extract_long_term_memories_from_messages(self, session_id: str, messages: list[dict[str, Any]], source_type: str = "checkpoint"):
        if not messages or not hasattr(self, "_extract_long_term_memories"):
            return
        dialog = self._format_store_messages(messages, limit_chars=20000)
        await self._extract_long_term_memories(session_id, f"[{source_type}]\n{dialog}", "")

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

    def _reset_short_context(self, state: dict[str, Any], reason: str):
        # 短期/新场景重置：注意力边界前移 + 清空轻量近期 buffer，但**位置不硬清空**（连续而非瞬移）：
        #   · user_place：交给 4h TTL 自然老化（B 方案）——换话题不代表用户物理移动；
        #   · character_place：降级为 weak（见 _demote_character_place），新场景不钉死生图、仍作背景，
        #     等新场景的位置声明覆盖或 TTL 过期。
        # 两个位置字段在本路径**对称处理**（都不清空），消除原先“SR 清 user 不清 character”的不对称。
        state["short_context_start"] = len(state.get("chat_history", []))
        state["short_context_reset_time"] = time.time()
        state["short_context_reset_reason"] = reason
        state["recent_message_history"] = []
        self._demote_character_place(state)
        session_schema.clear_nudity(state)  # 新场景：不再续上上一幕的裸体态

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
    def _chat_prompt_history(state: dict[str, Any]) -> list[dict[str, Any]]:
        """聊天 prompt 的历史窗口：checkpoint 锚定，取短期场景起点之后的全部未折叠消息。

        故意不做 [-N:] 逐轮滑动——长度由 checkpoint 折叠（每周期把溢出折进摘要、尾部裁到
        keep）与 _history_storage_cap 兜底共同约束。这样两次 checkpoint 之间历史前缀只增不
        移，最大化服务端 prefix cache 命中；只有 checkpoint 落地那一刻才发生一次前缀归位。
        """
        history = state.get("chat_history", [])
        try:
            start = int(state.get("short_context_start", 0) or 0)
        except Exception:
            start = 0
        if start < 0 or start > len(history):
            start = 0
        return history[start:]

    def _inject_photo_history_messages(self, messages: list[dict[str, Any]], state: dict[str, Any]):
        photos = state.get("sent_photos_history", [])
        if not photos:
            return
        existing = "\n".join(m.get("content", "") for m in state.get("chat_history", []) if isinstance(m.get("content"), str))
        reset_time = float(state.get("short_context_reset_time", 0) or 0)
        for photo in photos[-3:]:
            # 12h 内 且 晚于短期重置边界（视觉时效绑定注意力边界——见 ④，刻意为之）。
            if not self._within(photo.get("timestamp", 0), 12 * 3600, since=reset_time):
                continue
            scene = photo.get("scene", "")
            if scene and scene in existing:
                continue
            content = f"*（你最近一次出现在用户眼前的样子：{scene}）*"
            caption = (photo.get("caption") or "").strip()
            if caption and caption != scene:
                content += f"\n你给这张图配的文字：{caption}"
            source = (photo.get("source_description") or "").strip()
            if source and source != scene:
                content += f"\n这张图当时要回应的原始描写：{source}"
            messages.append({"role": "assistant", "content": content})

