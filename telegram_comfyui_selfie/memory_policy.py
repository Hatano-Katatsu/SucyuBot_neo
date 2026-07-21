from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import session_schema
from .memory import USER_PROFILE_KIND, format_memory_lines, normalize_kind

logger = logging.getLogger(__name__)

LONG_MEMORY_STABLE_CUE_RE = re.compile(
    r"(喜欢|偏好|偏爱|更喜欢|讨厌|不喜欢|不要|别|禁止|不希望|希望|以后|长期|一直|总是|通常|习惯|约定|边界|禁忌|称呼|记住|重要|更愿意|避免|关系|恋人|同居|女友|男友|伴侣)"
)
LONG_MEMORY_TRANSIENT_CUE_RE = re.compile(
    r"(当前|现在|今天|今晚|这次|本轮|刚才|刚刚|上一张|这张图|这张照片|正在|临时|这一次|此刻|时段|天气|星期|自拍|照片|画面)"
)
LONG_MEMORY_STRUCTURED_CUE_RE = re.compile(
    r"(当前角色|角色是|当前人设|人设是|身体特征|物种特征|positive_prefix|纯良度|纯度|地点|城市|时区|画风|当前外观|当前穿搭|临时外型|dynamic_appearance|每日推送)"
)

class MemoryPolicyMixin:
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

    def _long_term_memory_context(self, session_id: str, limit: int | None = None) -> str:
        if not session_id or not self._long_memory_enabled():
            return ""
        memories = self.memory.context_memories(
            session_id, character=self._memory_character(session_id), limit=limit or self._long_memory_limit()
        )
        if not memories:
            return ""
        return format_memory_lines(memories, with_ids=False)

    def _long_memory_structured_fields(self, session_id: str, character: str | None = None) -> list[tuple[str, Any]]:
        """返回指定角色的结构化边界，后台任务不得借用另一活动角色的 live state。"""
        state = self._get_session_state(session_id)
        active_key = self._memory_character(session_id)
        character_key = active_key if character is None else str(character or "").strip()
        if character_key == active_key:
            return [
                ("当前角色", session_schema.get_character_value(state, "custom_character", "") or ""),
                ("当前作品", session_schema.get_character_value(state, "custom_series", "") or ""),
                ("当前人设", (session_schema.get_character_value(state, "custom_scheduled_persona", "") or "")[:120]),
                ("当前身体特征", (session_schema.get_character_value(state, "custom_positive_prefix", "") or "")[:120]),
                ("当前临时外型", session_schema.get_outfit(state)),
                ("当前地点", self._get_session_cfg(session_id, "location", "")),
                ("当前时区", self._get_session_cfg(session_id, "timezone_offset", "")),
                ("当前画风", self._get_current_style(session_id)),
                ("当前纯良度", str(self._get_purity(session_id))),
                ("当前空间关系", self._get_session_cfg(session_id, "spatial_relationship", "")),
            ]

        saved = session_schema.get_saved_characters(state)
        card = dict(saved.get(character_key) or {}) if character_key else dict(self._default_character_payload())
        context_key = character_key or "__default__"
        frozen = session_schema.get_character_contexts(state).get(context_key)
        clothing = frozen.get("clothing") if isinstance(frozen, dict) and isinstance(frozen.get("clothing"), dict) else {}
        outfit = clothing.get("dynamic_appearance") if isinstance(clothing, dict) else ""
        if not outfit:
            outfit = card.get("outfit") or ""
        return [
            ("当前角色", card.get("character") or card.get("bot_name") or character_key),
            ("当前作品", card.get("series") or ""),
            ("当前人设", str(card.get("persona") or "")[:120]),
            ("当前身体特征", str(card.get("appearance") or "")[:120]),
            ("当前临时外型", outfit),
            ("当前画风", card.get("style") or ""),
            ("当前纯良度", "" if card.get("purity") is None else str(card.get("purity"))),
            ("当前空间关系", card.get("relationship") or ""),
        ]

    def _long_memory_structured_boundary_text(self, session_id: str, character: str | None = None) -> str:
        fields = self._long_memory_structured_fields(session_id, character)
        return "\n".join(f"- {label}: {value}" for label, value in fields if str(value).strip())

    def _is_long_memory_in_scope(
        self,
        session_id: str,
        kind: str,
        summary: str,
        tags: Any = None,
        character: str | None = None,
    ) -> bool:
        kind = normalize_kind(kind)
        summary = (summary or "").strip()
        if not summary:
            return False
        if kind in ("manual", "correction", "boundary", USER_PROFILE_KIND):
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
            current_values = [value for _label, value in self._long_memory_structured_fields(session_id, character)]
            for value in current_values:
                value = str(value or "").strip()
                if value and len(value) >= 2 and value in text:
                    return False

        tag_text = " ".join(str(tag) for tag in (tags or []))
        if re.search(r"(当前|临时|本轮|这次)", tag_text) and not stable:
            return False
        return True

    def _queue_long_memory_extraction(self, session_id: str, user_text: str, assistant_text: str):
        """旧版每轮聊天记忆提取入口。

        现在长期记忆只在 checkpoint 折叠阶段从溢出的真实对话中异步提取，避免普通聊天每轮额外跑
        LLM，也避免未稳定的即时剧情被过早固化为长期记忆。
        """
        return

    async def _extract_long_term_memories(self, session_id: str, user_text: str, assistant_text: str, character: str | None = None):
        # character 为 None 时沿用当前活动角色；后台任务应在启动时捕获 key 并显式透传，
        # 避免摘要 LLM 等待期间用户切换角色导致记忆写进新角色的记忆空间。
        character_key = self._memory_character(session_id) if character is None else str(character or "").strip()
        existing = ""
        if self._long_memory_enabled():
            contextual = self.memory.context_memories(
                session_id, character=character_key, limit=10
            )
            stable = [
                memory
                for memory in self.memory.list_memories(session_id, character=character_key, limit=200)
                if memory.get("kind") in {"user_profile", "boundary", "correction", "manual"}
            ]
            seen_ids: set[int] = set()
            mems = []
            for memory in [*stable, *contextual]:
                mid = int(memory.get("id") or 0)
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    mems.append(memory)
            if mems:
                existing = format_memory_lines(mems, with_ids=False)
        structured = self._long_memory_structured_boundary_text(session_id, character_key)
        system = (
            "你是长期记忆提取器。请从一轮用户与角色的对话中提取值得长期保存的信息。\n"
            "只保存稳定偏好、明确设定、关系状态变化、重要事件、视觉/穿搭偏好、边界或禁忌。\n"
            "用户画像 user_profile 专门保存人类用户相关的稳定信息：兴趣爱好、行为方式、外貌、自我描述、长期偏好和边界；"
            "不要把 bot 角色的人设、动作、身体状态或短期剧情写进用户画像。用户画像按当前角色独立维护，不跨角色共享。\n"
            "长期记忆负责可跨场景复用的高重要度事实/偏好/边界/纠正，不负责承接刚才发生到哪一步；近期连续性交给 checkpoint，宏观关系阶段交给角色历史提要。\n"
            "长期记忆不是第二套人设系统，不要保存已有结构化状态负责的内容。\n"
            "不要保存当前角色、当前人设、当前身体特征、当前地点/时区、当前纯良度、当前画风、当前临时穿搭或最近图片内容；"
            "除非用户明确表达了长期偏好、边界、约定、纠正或重要关系变化。\n"
            "当输入来自 checkpoint，用户明确提到未来或待完成的时间节点（日期、几点、期限、倒计时、约定时间、相对时间）时，"
            "如果该节点会跨场景影响后续互动，可以作为 event 记忆保存，并写清时间节点、关联事件与已知状态；不要为未明确的时间自行换算或补全。\n"
            "视角映射：User/用户 是人类用户；Assistant/角色 是当前 bot 角色。不要把双方的动作、情绪、承诺、偏好或身体状态互换。"
            "如果输入是 checkpoint/current window 形式的多轮对话，必须按其中每行的 User/Assistant 标签判断归属，不要把整段当成用户发言。\n"
            "不要保存普通寒暄、临时情绪、重复信息、无长期价值的台词。\n"
            "严格来源约束（最重要）：只从对话原文提取信息，不要推断、补充、联想或编造对话中没有明确出现的规则、约定、偏好或事件。"
            "例如：用户说「我迟到了」→ 不要推断出「迟到要请吃东西」；用户说「送你一个发卡」→ 不要推断出「发卡是某种约定的象征」。\n"
            "如果已有相关记忆已经覆盖，不要重复输出。\n"
            "必须输出严格 JSON: {\"memories\":[{\"kind\":\"user_profile|profile|preference|relationship|setting|boundary|visual|event|correction\","
            "\"summary\":\"一句中文记忆摘要\",\"importance\":1-5,\"tags\":[\"标签\"]}]}。没有值得保存的内容时 memories 为空数组。"
        )
        if assistant_text:
            source_block = f"本轮对话:\n用户/User: {user_text}\n角色/Assistant: {assistant_text or '（无文字回复）'}"
        else:
            source_block = (
                "来源对话（按行读取；User=人类用户，Assistant=当前 bot 角色；不要把整段当成用户发言）:\n"
                f"{user_text or '无'}"
            )
        user = (
            f"当前结构化状态（不要作为长期记忆重复保存）:\n{structured or '无'}\n\n"
            f"已有高重要度记忆（避免重复，必要时只输出真正新增/修正的信息）:\n{existing or '无'}\n\n"
            f"{source_block}"
        )
        try:
            if hasattr(self, "_call_memory_json_llm") and (
                self.has_llm_config("chat", session_id) or self.has_llm_config("image", session_id)
            ):
                text, parsed, _purpose, _attempts = await self._call_memory_json_llm(
                    session_id, system, user, tag="memory-extract", temp=0.1
                )
            else:
                text = await self._call_llm(system, user, temp=0.1, tag="memory-extract", purpose="chat", session_id=session_id)
                parsed = self._parse_llm_json(text) if hasattr(self, "_parse_llm_json") else json.loads(
                    re.sub(r"```json\s*|```\s*$", "", text).strip()
                )
        except Exception as exc:
            logger.warning("long memory extraction failed: %s", exc)
            try:
                payload = {
                    "stage": "memory-extract",
                    "request": {"system": system, "user": user},
                    "result": {"error": str(exc)},
                }
                self._ulog(session_id, "ERROR", f"MEMORY_OP_FAILED {json.dumps(payload, ensure_ascii=False, default=str)}")
            except Exception:
                logger.debug("long memory extraction failure log failed", exc_info=True)
            return
        memories = parsed.get("memories") if isinstance(parsed, dict) else None
        if not isinstance(memories, list):
            try:
                payload = {
                    "stage": "memory-extract",
                    "request": {"system": system, "user": user},
                    "result": {"raw": text, "parsed": parsed},
                }
                self._ulog(session_id, "ERROR", f"MEMORY_OP_FAILED {json.dumps(payload, ensure_ascii=False, default=str)}")
            except Exception:
                logger.debug("long memory extraction invalid result log failed", exc_info=True)
            return
        if assistant_text:
            source = f"用户: {user_text[:240]}\n角色: {(assistant_text or '')[:240]}"
        else:
            source = f"来源对话(User=用户, Assistant=角色): {user_text[:360]}"
        for item in memories[:8]:
            if not isinstance(item, dict):
                continue
            summary = (item.get("summary") or "").strip()
            if not summary:
                continue
            if not self._is_long_memory_in_scope(
                session_id,
                item.get("kind", "event"),
                summary,
                item.get("tags") or [],
                character_key,
            ):
                logger.info("skip out-of-scope long memory: %s", summary)
                continue
            mid = self.memory.add_memory(
                session_id,
                item.get("kind", "event"),
                summary,
                character=character_key,
                importance=item.get("importance", 3),
                tags=item.get("tags") or [],
                source=source,
            )
            self._ulog(session_id, "MEM+", f"#{mid} 自动[{normalize_kind(item.get('kind', 'event'))}]: {summary}")

