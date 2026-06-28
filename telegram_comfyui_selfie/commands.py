from __future__ import annotations

import asyncio
import copy
import json
import random
import re
import time
from typing import Any

from . import appearance as appearance_rules
from . import character_card
from . import prompt_intake
from . import session_schema
from .command_aliases import resolve_command_alias
from .defaults import INIT_GUIDE, MENU_BODY, MENU_TOPICS, MENU_TOPIC_ALIASES, OC_CREATE_HELP, SCENES, WEEKDAY_NAMES
from .memory import format_memory_lines


# /角色 reset uses this full cleanup path. Keep one public hard-reset entry.
SESSION_CUSTOM_RESET_KEYS = (
    "custom_scheduled_persona", "custom_role_name", "custom_bot_name", "custom_bot_self_name",
    "custom_user_address", "custom_spatial_relationship", "custom_location", "custom_timezone_offset",
    "custom_count", "custom_positive_prefix", "custom_default_hair", "custom_default_eyes",
    "custom_current_style", "custom_scene_preference", "custom_selfie_preference",
    "custom_raw_profile_text", "custom_prompt_intake",
    "custom_character", "custom_series",
    "custom_visual_character", "custom_visual_series",
    "custom_character_age_stage", "custom_character_occupation", "custom_character_day_anchor",
)
# ── 会话 state 字段归属分类 ──
# 单一来源在 session_schema.STATE_SCHEMA（每字段声明 归属 + 默认值 + reset 保留）。此处仅
# 再导出，保持调用方/测试的导入路径不变。state 键分三类，切角色时各走各的路：
#   · 会话全局（SESSION_GLOBAL_STATE_KEYS）：属于这场会话/这个人，绝不随角色冻结/清空。
#   · 角色配置（custom_* 前缀 + 少数显式项）：身份/人设/外貌设定，走 saved_characters 卡 schema。
#   · 角色短期态（其余一切）：对话/位置/照片/穿搭等工作记忆，走 character_contexts 冻结/解冻。
# 关键：短期态用「黑名单之外即是」反推——新增字段默认跟角色走，漏配的失败方向是“正确隔离”而非串味。
SESSION_GLOBAL_STATE_KEYS = session_schema.SESSION_GLOBAL_STATE_KEYS
CHARACTER_CONFIG_EXTRA_KEYS = session_schema.CHARACTER_CONFIG_EXTRA_KEYS
RESET_PRESERVED_TRANSIENT_KEYS = session_schema.RESET_PRESERVED_TRANSIENT_KEYS
_is_character_config_key = session_schema.is_character_config_key
_is_transient_state_key = session_schema.is_transient_state_key


RESET_DONE_MSG = "对话上下文与照片历史已清空。角色设定与角色池保持不变。"

CLEARUP_DONE_MSG = (
    "已恢复全局默认：本会话的人设、角色、身体特征、外型、称呼、地区时区、推送频率、纯良度覆盖，"
    "以及全部角色档案均已清空，并已重置对话上下文。\n"
    "下一句起将以默认人设回应。"
)
OC_FIELD_ALIASES = {
    "名字": "name",
    "姓名": "name",
    "名称": "name",
    "角色名": "name",
    "name": "name",
    "角色出处": "source_identity",
    "角色出处与原名": "source_identity",
    "出处": "source_identity",
    "来源": "source_identity",
    "source_identity": "source_identity",
    "source_type": "source_type",
    "作品": "series",
    "原作": "series",
    "出处作品": "series",
    "系列": "series",
    "作品名": "series",
    "series": "series",
    "原名": "original_name",
    "原角色名": "original_name",
    "原作角色名": "original_name",
    "original_name": "original_name",
    "生图角色Tag": "visual_character",
    "生图角色tag": "visual_character",
    "生图角色": "visual_character",
    "prompt_name": "visual_character",
    "visual_character": "visual_character",
    "生图作品Tag": "visual_series",
    "生图作品tag": "visual_series",
    "生图作品": "visual_series",
    "prompt_series": "visual_series",
    "visual_series": "visual_series",
    "角色类型": "role",
    "类型": "role",
    "身份": "role",
    "role": "role",
    "对话称呼": "user_address",
    "对用户称呼": "user_address",
    "称呼用户": "user_address",
    "叫我": "user_address",
    "称呼我": "user_address",
    "user_address": "user_address",
    "年龄段": "age",
    "年龄": "age",
    "age": "age",
    "职业": "occupation",
    "职场": "occupation",
    "白天去向": "occupation",
    "occupation": "occupation",
    "day_anchor": "occupation",
    "性格": "persona",
    "人格": "persona",
    "人设": "persona",
    "persona": "persona",
    "外貌": "appearance",
    "外型": "appearance",
    "身体特征": "appearance",
    "appearance": "appearance",
    "外貌和穿搭": "appearance_outfit",
    "外貌与穿搭": "appearance_outfit",
    "外型和穿搭": "appearance_outfit",
    "外型与穿搭": "appearance_outfit",
    "appearance_outfit": "appearance_outfit",
    "角色设定": "character_setting",
    "设定": "character_setting",
    "character_setting": "character_setting",
    "初始穿搭": "outfit",
    "穿搭": "outfit",
    "衣服": "outfit",
    "服装": "outfit",
    "outfit": "outfit",
    "与你的关系": "relationship",
    "关系": "relationship",
    "空间关系": "relationship",
    "relationship": "relationship",
    "关系和称呼": "relationship_address",
    "关系与称呼": "relationship_address",
    "relationship_address": "relationship_address",
    "所在城市": "city",
    "城市": "city",
    "地点": "city",
    "city": "city",
}

# /个性设置 可调项的单一事实来源：(展示名, state key, 输入别名)。
# 展示列表与别名映射都由这里派生，避免两份清单手动对齐时漂移。
# 人格走专用命令 /人格；关系走专用命令 /关系，均不在此处重复。
PERSONALIZE_FIELDS = [
    ("角色类型", "custom_role_name", ("角色类型",)),
    ("角色名", "custom_bot_name", ("角色名", "名字")),
    ("自称", "custom_bot_self_name", ("自称",)),
    ("对用户称呼", "custom_user_address", ("称呼", "对话称呼", "对用户称呼", "称呼用户", "叫我", "称呼我")),
    ("生图角色Tag", "custom_visual_character", ("生图角色", "生图角色tag")),
    ("生图作品Tag", "custom_visual_series", ("生图作品", "生图作品tag")),
    ("年龄段", "custom_character_age_stage", ("年龄段", "年龄")),
    ("职业", "custom_character_occupation", ("职业", "职场", "白天去向")),
    ("用户性别", "custom_user_gender", ("用户性别", "我的性别")),
]
# 改这些字段后需要让 life_profile 缓存失效，下次重新生成生活档案。
PERSONALIZE_LIFE_PROFILE_KEYS = {
    "custom_role_name", "custom_bot_name", "custom_character_age_stage",
    "custom_character_occupation", "custom_character_day_anchor",
}


class CommandHandlersMixin:
    INIT_FLOW_STEPS = (
        (
            "name",
            "第 1/8 步：新角色卡叫什么名字？\n"
            "这是角色池里的主键和切换名称，必填。例如：小雨、爱丽丝。",
        ),
        (
            "source_identity",
            "第 2/8 步：角色出处是什么？\n"
            "原创角色回复“原创”。现有作品角色请写原作和角色名，最好用英文或罗马音，例如：Blue Archive / Tendou Aris。回复“跳过”按原创处理。",
        ),
        (
            "appearance",
            "第 3/8 步：角色外貌和初始穿搭是什么？\n"
            "例如：黑色短发、蓝眼睛、身材纤细、白衬衫、深色百褶裙。回复“跳过”可使用默认外貌。",
        ),
        (
            "role",
            "第 4/8 步：角色设定是什么？\n"
            "包括角色类型、职业身份、性格、语气和习惯。例如：大学生，温柔慢热，说话简短。回复“跳过”可留空。",
        ),
        (
            "relationship",
            "第 5/8 步：你和角色的关系、以及角色怎么称呼你？\n"
            "例如：同城恋人，称呼我主人；或：同校朋友，叫我老师。回复“跳过”可留空。",
        ),
        (
            "city",
            "第 6/8 步：你所在或故事发生的城市是哪里？\n"
            "例如：上海、东京、纽约。回复“跳过”可之后再用 /天气设置 设置。",
        ),
        (
            "purity",
            "第 7/8 步：纯良度设置为多少？\n"
            "输入 0-10 的整数，数字越高越保守；也可以回复 auto。",
        ),
        (
            "push_frequency",
            "第 8/8 步：每天主动推送几次？\n"
            "输入 0-20 的整数，0 表示关闭；也可以回复“默认”。",
        ),
    )
    INIT_FLOW_SKIP_WORDS = {"跳过", "skip", "略过", "暂不", "不用", "不要", "无", "空"}
    INIT_FLOW_CANCEL_WORDS = {"取消", "取消初始化", "退出", "退出初始化", "cancel", "stop", "结束"}

    async def dispatch_command(self, chat_id: int | str, session_id: str, command: str, arg: str):
        command = resolve_command_alias(command)
        handlers = {
            "初始化": self.cmd_init_guide,
            "菜单": self.cmd_menu,
            "创建OC": self.cmd_create_oc,
            "自拍": self.cmd_selfie,
            "配图": self.cmd_scene_image,
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
            "纯良度": self.cmd_purity,
            "推送频率": self.cmd_push_frequency,
            "角色": self.cmd_character,
            "个性设置": self.cmd_personalize,
            "关系": self.cmd_relationship,
            "外型": self.cmd_appearance,
            "衣橱": self.cmd_closet,
            "外貌自动": self.cmd_auto_appearance,
            "记忆": self.cmd_memory,
            "记住": self.cmd_remember,
            "忘记": self.cmd_forget,
            "新场景": self.cmd_new_scene,
            "回滚": self.cmd_rollback,
            "重答": self.cmd_regenerate,
            "调度": self.cmd_sched,
            "管理": self.cmd_management,
            "完整菜单": self.cmd_full_menu,
            "web密码": self.cmd_web_password,
            "webui": self.cmd_webui,
            "模型": self.cmd_model,
            "修改角色": self.cmd_modify_character,
            "更新": self.cmd_git_update,
        }
        handler = handlers.get(command)
        if not handler:
            await self.send_message(chat_id, f"未知命令: /{command}\n发送 /菜单 查看可用命令。")
            return
        await handler(chat_id, session_id, arg)

    # ---------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------
    async def cmd_init_guide(self, chat_id, session_id, arg):
        text = (arg or "").strip().lower()
        state = self._get_session_state(session_id)
        flow = session_schema.get_init_flow(state)
        if text in self.INIT_FLOW_CANCEL_WORDS:
            flow.clear()
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "初始化向导已取消。")
            return
        flow.clear()
        flow.update({"active": True, "step": 0, "answers": {}, "started_at": time.time(), "mode": "create_character"})
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, INIT_GUIDE + "\n\n" + self._init_flow_question(0))

    def _init_flow_question(self, step: int) -> str:
        if step < 0 or step >= len(self.INIT_FLOW_STEPS):
            return ""
        return self.INIT_FLOW_STEPS[step][1]

    def _init_flow_is_skip(self, text: str) -> bool:
        return (text or "").strip().lower() in self.INIT_FLOW_SKIP_WORDS

    async def handle_init_flow_message(self, chat_id: int | str, session_id: str, text: str) -> bool:
        state = self._get_session_state(session_id)
        flow = session_schema.get_init_flow(state)
        if not (isinstance(flow, dict) and flow.get("active")):
            return False
        answer = (text or "").strip()
        if not answer:
            await self.send_message(chat_id, self._init_flow_question(int(flow.get("step") or 0)))
            return True
        if answer.lower() in self.INIT_FLOW_CANCEL_WORDS:
            flow.clear()
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "初始化向导已取消。")
            return True

        step = int(flow.get("step") or 0)
        if step < 0 or step >= len(self.INIT_FLOW_STEPS):
            flow.clear()
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "初始化向导状态已重置。发送 /初始化 可重新开始。")
            return True

        key, _question = self.INIT_FLOW_STEPS[step]
        if self._init_flow_is_skip(answer):
            if key == "name":
                await self.send_message(chat_id, "角色名是必填项。请发一个角色名，或回复“取消初始化”。")
                return True
        else:
            ok = await self._store_init_flow_answer(chat_id, flow, key, answer)
            if not ok:
                return True

        step += 1
        flow["step"] = step
        if step >= len(self.INIT_FLOW_STEPS):
            self._save_session_state(session_id, state)
            await self._finish_init_flow(chat_id, session_id)
            return True
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, self._init_flow_question(step))
        return True

    async def _store_init_flow_answer(self, chat_id: int | str, flow: dict[str, Any], key: str, answer: str) -> bool:
        if key == "name":
            parsed = self._parse_oc_fields(answer)
            answer = (parsed.get("name") or answer).strip()
            if not answer:
                await self.send_message(chat_id, "角色名不能为空。请发一个角色名，或回复“取消初始化”。")
                return False
        if key == "purity":
            val = answer.strip().lower()
            if val not in ("auto", "默认", "自动", "reset"):
                try:
                    num = int(val)
                    if num < 0 or num > 10:
                        raise ValueError
                except ValueError:
                    await self.send_message(chat_id, "纯良度请输入 0-10 的整数，或 auto。")
                    return False
        if key == "push_frequency":
            val = answer.strip().lower()
            if val not in ("默认", "全局", "reset", "auto"):
                try:
                    num = int(val)
                    if num < 0 or num > 20:
                        raise ValueError
                except ValueError:
                    await self.send_message(chat_id, "推送次数请输入 0-20 的整数，或“默认”。")
                    return False
        answers = flow.setdefault("answers", {})
        if isinstance(answers, dict):
            answers[key] = answer
        return True

    def _init_flow_create_oc_text(self, answers: dict[str, Any]) -> str:
        role = str(answers.get("role") or "").strip()
        lines = [f"名字：{str(answers.get('name') or '').strip()}"]
        source_identity = str(answers.get("source_identity") or "").strip()
        if source_identity:
            lines.append(f"角色出处与原名：{source_identity}")
        appearance = str(answers.get("appearance") or "").strip()
        if appearance:
            lines.append(f"外貌和穿搭：{appearance}")
        if role:
            lines.append(f"角色设定：{role}")
        mapping = (
            ("relationship", "关系和称呼"),
            ("city", "所在城市"),
        )
        for key, label in mapping:
            value = str(answers.get(key) or "").strip()
            if value:
                lines.append(f"{label}：{value}")
        return "\n".join(lines)

    async def _finish_init_flow(self, chat_id: int | str, session_id: str):
        state = self._get_session_state(session_id)
        flow = session_schema.get_init_flow(state)
        answers = dict(flow.get("answers") or {}) if isinstance(flow, dict) else {}
        name = str(answers.get("name") or "").strip()
        if not name:
            flow.clear()
            flow.update({"active": True, "step": 0, "answers": {}, "started_at": time.time(), "mode": "create_character"})
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "初始化需要先创建角色卡。请告诉我角色名。")
            return
        profile_text = self._init_flow_create_oc_text(answers)
        fields = self._parse_oc_fields(profile_text)
        intake = await self._normalize_prompt_intake(profile_text, context="init")
        fields = prompt_intake.merge_oc_fields(fields, intake)
        await self._create_oc_from_fields(chat_id, session_id, profile_text, fields, intake)
        state = self._get_session_state(session_id)
        session_schema.get_init_flow(state).clear()
        purity = str(answers.get("purity") or "").strip().lower()
        if purity:
            if purity in ("auto", "默认", "自动", "reset"):
                session_schema.set_character_value(state, "purity", None)
                session_schema.set_character_value(state, "purity_user_set", False)
            else:
                session_schema.set_character_value(state, "purity", max(0, min(10, int(purity))))
                session_schema.set_character_value(state, "purity_user_set", True)
                self._snapshot_character(state)
        push_frequency = str(answers.get("push_frequency") or "").strip().lower()
        if push_frequency:
            if push_frequency in ("默认", "全局", "reset", "auto"):
                state.pop("custom_daily_selfie_limit", None)
            else:
                state["custom_daily_selfie_limit"] = str(max(0, min(20, int(push_frequency))))
            session_schema.set_daily_trigger_date(state, "")
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, "初始化向导已完成，新的角色卡已经创建。之后可以直接聊天，或发送 /完整菜单 查看全部命令。")

    async def cmd_menu(self, chat_id, session_id, arg):
        topic = (arg or "").strip()
        if not topic:
            await self.send_message(chat_id, "ComfyUI 自拍服务 - 快速菜单\n\n" + MENU_BODY)
            return

        key = MENU_TOPIC_ALIASES.get(topic.lower(), MENU_TOPIC_ALIASES.get(topic, topic))
        body = MENU_TOPICS.get(key)
        if not body:
            available = " / ".join(MENU_TOPICS.keys())
            await self.send_message(chat_id, f"没有这个菜单分区: {topic}\n可用分区: {available}\n例如: /菜单 设置")
            return
        await self.send_message(chat_id, f"菜单 - {key}\n\n{body}")

    @staticmethod
    def _parse_oc_fields(text: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        current = ""
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.lower().strip() in ("/创建角色", "/新建角色", "/创建oc", "/创建OC".lower(), "/oc"):
                continue
            match = re.match(r"^([^:：]{1,20})[:：]\s*(.*)$", line)
            if match:
                label = match.group(1).strip()
                key = OC_FIELD_ALIASES.get(label, OC_FIELD_ALIASES.get(label.lower(), ""))
                if key:
                    current = key
                    value = match.group(2).strip()
                    if value:
                        fields[key] = value
                    else:
                        fields.setdefault(key, "")
                    continue
            if current:
                fields[current] = (fields.get(current, "") + "\n" + line).strip()
        return fields

    @staticmethod
    def _oc_gender_tag(*parts: str) -> str:
        text = " ".join(part or "" for part in parts).lower()
        if re.search(r"\b1boy\b|\bboy\b|\bmale\b|男性|男生|少年|青年|男人|男孩", text):
            return "1boy"
        return "1girl"

    async def _oc_translate_tags(self, text: str) -> str:
        tags = (text or "").strip()
        if not tags:
            return ""
        if re.search(r"[\u4e00-\u9fff]", tags):
            tags = await self._translate_appearance_tags(tags)
        return tags.strip().strip(",")

    @staticmethod
    def _oc_fields_need_intake(text: str, fields: dict[str, str]) -> bool:
        if not fields:
            return bool((text or "").strip())
        if any(key in fields for key in ("source_identity", "appearance_outfit", "character_setting", "relationship_address")):
            return True
        name = fields.get("name", "")
        return not name or "\n" in name

    def _apply_intake_style(self, state: dict[str, Any], intake: dict[str, Any]) -> str:
        style = (intake.get("style") or "").strip()
        if not style:
            return ""
        # Only apply strings that look usable by the image prompt renderer. Chinese-only style hints are kept in
        # custom_prompt_intake for later review instead of being injected into Anima directly.
        if "@" in style or re.search(r"[A-Za-z]", style):
            session_schema.set_character_value(state, "custom_current_style", style)
            return style
        return ""

    async def _apply_oc_city(self, session_id: str, city: str) -> str:
        city = (city or "").strip()
        if not city:
            return ""
        state = self._get_session_state(session_id)
        session_schema.set_character_value(state, "custom_location", city)
        note = f"城市: {city}"
        try:
            weather = await self._fetch_weather(city, session_id=session_id)
        except Exception as exc:
            self._ulog(session_id, "WARN", f"OC 城市天气查询失败: {exc}")
            weather = None
        if weather:
            off = await self._resolve_city_timezone(city, weather.get("lon"))
            if off is not None:
                session_schema.set_character_value(state, "custom_timezone_offset", str(off))
                note += f" / UTC{off:+g}"
            note += f" / {weather.get('desc', '未知')} {weather.get('temp', '?')} C"
        else:
            note += " / 天气暂未验证"
        self._save_session_state(session_id, state)
        if hasattr(self, "_ensure_city_place_catalog"):
            try:
                await self._ensure_city_place_catalog(city)
            except Exception as exc:
                self._ulog(session_id, "WARN", f"OC 城市地点目录生成失败: {exc}")
        return note

    @staticmethod
    def _missing_character_slots(state: dict[str, Any]) -> list[str]:
        """返回角色建档后仍为空、值得提醒用户补的槽位中文名。"""
        checks = [
            ("年龄段", session_schema.get_character_value(state, "custom_character_age_stage")),
            ("职业", session_schema.get_character_value(state, "custom_character_occupation")),
            ("关系", session_schema.get_character_value(state, "custom_spatial_relationship")),
            ("城市", session_schema.get_character_value(state, "custom_location")),
        ]
        return [label for label, val in checks if not str(val or "").strip()]

    @staticmethod
    def _slot_fill_hint(missing: list[str]) -> str:
        if not missing:
            return ""
        examples = {
            "年龄段": "/个性设置 年龄段 adult",
            "职业": "/个性设置 职业 上班族",
            "关系": "/关系 同城恋人",
            "城市": "/天气设置 上海",
        }
        lines = [f"还差这些没填：{'、'.join(missing)}，可按需补上："]
        lines += [f"  {examples[m]}" for m in missing if m in examples]
        return "\n".join(lines)

    async def cmd_full_menu(self, chat_id, session_id, arg):
        sections = []
        for key, body in MENU_TOPICS.items():
            sections.append(f"## {key}\n{body}")
        await self.send_message(chat_id, "Full menu\n\n" + "\n\n".join(sections))

    async def cmd_web_password(self, chat_id, session_id, arg):
        password = (arg or "").strip()
        if not password:
            await self.send_message(chat_id, "用法：/web密码 <密码>")
            return
        user_id = self._user_id_for_session(session_id)
        info = self.app_store.set_web_password(user_id, password)
        url = self._web_access_url(info["token"])
        await self.send_message(
            chat_id,
            f"WebUI 密码已更新。\n账号：{user_id}\n密码：{password}\n持久免登录链接：{url}",
        )

    async def cmd_webui(self, chat_id, session_id, arg):
        user_id = self._user_id_for_session(session_id)
        token = self.app_store.get_or_create_web_token(user_id)
        url = self._web_access_url(token)
        password_hint = "可用 /web密码 <密码> 设置"
        await self.send_message(
            chat_id,
            f"WebUI 访问方式\n账号：{user_id}\n密码：{password_hint}\n持久免登录链接：{url}",
        )

    def _web_access_url(self, token: str) -> str:
        host = str(self.config.get("web_public_host") or self.config.get("web_host", "127.0.0.1") or "127.0.0.1")
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = int(self.config.get("web_port", 8787) or 8787)
        return f"http://{host}:{port}/?token={token}"

    async def cmd_model(self, chat_id, session_id, arg):
        text = (arg or "").strip()
        user_id = self._user_id_for_session(session_id)
        settings = self.app_store.get_user_model_settings(user_id)
        profiles = {**self._global_model_profiles(), **self.app_store.list_model_profiles(user_id)}
        if not text:
            names = ", ".join(profiles.keys()) or "none"
            chat_profile = settings.get("chat_profile_id") or self.config.get("default_chat_model_profile")
            fast_profile = settings.get("fast_profile_id") or self.config.get("default_fast_model_profile")
            vision_profile = settings.get("vision_profile_id") or self.config.get("default_vision_model_profile") or "关闭"
            await self.send_message(
                chat_id,
                "可用模型：" + names +
                f"\nchat={chat_profile} fast={fast_profile} vision={vision_profile}" +
                "\n用法：/模型 chat <id> | /模型 fast <id> | /模型 vision <id|off> | /模型 add <id> <json>",
            )
            return
        parts = text.split(None, 2)
        action = parts[0].lower()
        if action in ("chat", "fast", "vision") and len(parts) >= 2:
            profile_id = parts[1]
            if action == "vision" and profile_id.lower() in {"off", "none", "关闭", "空"}:
                self.app_store.update_user_model_settings(user_id, vision_profile_id="")
                await self.send_message(chat_id, "vision model disabled")
                return
            if profile_id not in profiles:
                await self.send_message(chat_id, f"Unknown profile: {profile_id}")
                return
            if action == "chat":
                self.app_store.update_user_model_settings(user_id, chat_profile_id=profile_id)
            elif action == "fast":
                self.app_store.update_user_model_settings(user_id, fast_profile_id=profile_id)
            else:
                self.app_store.update_user_model_settings(user_id, vision_profile_id=profile_id)
            await self.send_message(chat_id, f"{action} model switched to {profile_id}")
            return
        if action in ("think", "thinking", "fastthink") and len(parts) >= 2:
            await self.send_message(chat_id, "思考开关现在绑定在模型 profile 的 disable_thinking 配置里，不能按任务或用户单独切换。")
            return
        if action == "add" and len(parts) >= 3:
            profile_id = parts[1]
            try:
                data = json.loads(parts[2])
            except Exception as exc:
                await self.send_message(chat_id, f"Invalid JSON: {exc}")
                return
            if not isinstance(data, dict):
                await self.send_message(chat_id, "Profile JSON must be an object.")
                return
            self.app_store.upsert_model_profile(user_id, profile_id, data)
            await self.send_message(chat_id, f"Profile saved: {profile_id}")
            return
        await self.send_message(chat_id, "Usage: /model chat <id> | fast <id> | vision <id|off> | add <id> <json>")

    async def cmd_modify_character(self, chat_id, session_id, arg):
        text = (arg or "").strip()
        if not text:
            await self.send_message(chat_id, "用法：/修改角色 <自然语言修改要求>")
            return
        state = self._get_session_state(session_id)
        before = self._character_export_payload(state)
        allowed = sorted(before.keys())
        if not self.has_llm_config("chat", session_id):
            await self.send_message(chat_id, "聊天模型未配置。")
            return
        system = (
            "你是一名中文角色配置专家，负责按用户自然语言要求修改角色档案。"
            "只输出严格 JSON 对象，键名必须是允许字段之一，只包含需要变更的字段，不要输出未改动的字段。"
            "字段值用中文或英文 tag 写清楚（外观/画风用英文 danbooru tag，人设/关系用中文）。"
            "不要修改用户没提到的字段。允许字段: " + "、".join(allowed)
        )
        user = (
            "当前角色档案 JSON:\n"
            + json.dumps(before, ensure_ascii=False, indent=2)
            + "\n\n用户的修改要求:\n"
            + text
        )
        try:
            raw = await self._call_llm(
                system,
                user,
                temp=0.1,
                tag="modify-character",
                purpose="chat",
                disable_thinking=True,
                session_id=session_id,
            )
            patch = json.loads(raw)
        except Exception as exc:
            await self.send_message(chat_id, f"修改失败：{exc}")
            return
        if not isinstance(patch, dict):
            await self.send_message(chat_id, "模型没有返回 JSON 对象。")
            return
        self._apply_character_payload(state, {**before, **{k: v for k, v in patch.items() if k in before}})
        self._snapshot_character(state)
        self._save_session_state(session_id, state)
        after = self._character_export_payload(state)
        await self.send_message(
            chat_id,
            "修改前：\n"
            + json.dumps(before, ensure_ascii=False, indent=2)[:1800]
            + "\n\n修改后：\n"
            + json.dumps(after, ensure_ascii=False, indent=2)[:1800],
        )

    async def cmd_create_oc(self, chat_id, session_id, arg):
        text = (arg or "").strip()
        if not text:
            await self.cmd_init_guide(chat_id, session_id, "")
            return
        if text.lower() in ("help", "帮助", "模板", "示例", "example"):
            await self.send_message(chat_id, OC_CREATE_HELP)
            return
        fields = self._parse_oc_fields(text)
        intake: dict[str, Any]
        if self._oc_fields_need_intake(text, fields):
            if "\n" in fields.get("name", ""):
                fields["name"] = fields["name"].splitlines()[0].strip()
            intake = await self._normalize_prompt_intake(text, context="oc")
            fields = prompt_intake.merge_oc_fields(fields, intake)
        else:
            intake = prompt_intake.heuristic_intake(text)
        name = (fields.get("name") or "").strip()
        if not name:
            await self.send_message(chat_id, "缺少 OC 名字。\n\n" + OC_CREATE_HELP)
            return
        await self._create_oc_from_fields(chat_id, session_id, text, fields, intake)

    async def _create_oc_from_fields(
        self,
        chat_id: int | str,
        session_id: str,
        text: str,
        fields: dict[str, str],
        intake: dict[str, Any],
    ) -> None:
        name = (fields.get("name") or "").strip()
        if not name:
            await self.send_message(chat_id, "缺少 OC 名字。\n\n" + OC_CREATE_HELP)
            return

        state = self._get_session_state(session_id)
        switching = name != (session_schema.get_character_value(state, "custom_character", "") or "")
        role = (fields.get("role") or "原创角色").strip()
        persona = (fields.get("persona") or "").strip()
        appearance = (fields.get("appearance") or "").strip()
        outfit = (fields.get("outfit") or "").strip()
        relationship = (fields.get("relationship") or "").strip()
        user_address = (fields.get("user_address") or "").strip()
        occupation = (fields.get("occupation") or "").strip()
        city = (fields.get("city") or "").strip()
        source_identity = (fields.get("source_identity") or "").strip()
        source_type = (fields.get("source_type") or "").strip().lower()
        series = (fields.get("series") or "").strip()
        original_name = (fields.get("original_name") or "").strip()
        visual_character = (fields.get("visual_character") or "").strip()
        visual_series = (fields.get("visual_series") or "").strip()
        is_original = (
            source_type in ("original", "oc", "原创", "原创角色")
            or bool(source_identity and re.search(r"\boc\b|原创|自创|原创角色", source_identity, flags=re.IGNORECASE))
        )
        if is_original:
            if series.lower() in ("original", "original character", "oc") or series in ("原创", "原创角色"):
                series = ""
            if original_name.lower() in ("original", "original character", "oc") or original_name in ("原创", "原创角色"):
                original_name = ""
            if visual_series.lower() in ("original", "original character", "oc") or visual_series in ("原创", "原创角色"):
                visual_series = ""
        gender = self._oc_gender_tag(name, role, persona, appearance)

        age = ""
        anchor = ""
        if hasattr(self, "_normalize_age_stage"):
            age = self._normalize_age_stage(fields.get("age")) or self._normalize_age_stage(role)
        if hasattr(self, "_normalize_day_anchor"):
            # 职业是用户填的自由文本，白天去向枚举由职业（其次角色类型）后台派生。
            anchor = (
                self._normalize_day_anchor(fields.get("anchor"))
                or self._normalize_day_anchor(occupation)
                or self._normalize_day_anchor(role)
            )

        appearance_tags = await self._oc_translate_tags(appearance)
        outfit_tags = await self._oc_translate_tags(outfit)
        # 穿搭串归一去重（LLM 可能输出重复标签），避免展示和存储中的脏数据。
        outfit_tags = session_schema.normalize_outfit_string(outfit_tags)
        # 穿搭字段只保留服装/配饰/其他标签，剔除发色/瞳色等稳定外观标签；
        # 防止 LLM 误分类或默认角色外观污染 dynamic_appearance。
        # dynamic_appearance 只存「衣服/配饰 + 本角色显式的临时发/瞳」，稳定外貌走 custom_positive_prefix。
        if outfit_tags:
            parsed = appearance_rules.parse_appearance(outfit_tags, self._outfit_kw, self._accessory_kw)
            filtered: dict[str, list[str]] = {"hair": [], "eyes": [], "outfit": [], "accessory": [], "other": []}
            for k in ("outfit", "accessory", "other"):
                filtered[k] = parsed[k]
            outfit_tags = appearance_rules.slots_to_string(filtered)
        if not appearance_tags:
            appearance_tags = f"{self.config.get('default_hair', 'black long flowing hair')}, {self.config.get('default_eyes', 'purple eyes')}"

        # 人设串只存纯人格描述（性格/语气/习惯）；身份、角色类型、关系、职业都不写时焊接，
        # 由读取侧实时组装：_get_effective_persona 补身份，聊天/生图/推送各自的身份行补角色类型，
        # rel_line 补关系。这样改任一字段即时生效、永不与人设串漂移。
        persona_text = persona.strip()

        if switching:
            self._save_current_character_context(state)
            self._snapshot_character(state)

        character_card.apply_card_to_state(state, {
            "count": gender,
            "character": name,
            "series": series,
            "visual_character": visual_character,
            "visual_series": visual_series,
            "role_name": role,
            "bot_name": original_name or name,
            "user_address": user_address,
            "persona": persona_text,
            "appearance": appearance_tags,
            "relationship": relationship,
            "scene_preference": intake.get("scene_preference", ""),
            "selfie_preference": intake.get("selfie_preference", ""),
            "age_stage": age,
            "occupation": occupation,
            "day_anchor": anchor,
        })
        session_schema.set_character_value(state, "custom_raw_profile_text", text)
        session_schema.set_character_value(state, "custom_prompt_intake", intake)
        session_schema.set_character_value(state, "persona_user_set", True)
        applied_style = self._apply_intake_style(state, intake)
        state.pop("life_profile", None)
        if switching:
            self._restore_character_context(session_id, state)
        # 新 OC 的初始穿搭在 restore（切角色会整体清空短期态）之后再设，避免被清掉。
        session_schema.set_outfit(state, outfit_tags)

        self._snapshot_character(state)
        saved = session_schema.get_saved_characters(state)
        saved[name]["raw_profile"] = text
        if source_identity:
            saved[name]["source_identity"] = source_identity
        if source_type:
            saved[name]["source_type"] = source_type
        if original_name:
            saved[name]["original_name"] = original_name
        self._save_session_state(session_id, state)
        city_note = await self._apply_oc_city(session_id, city) if city else ""
        self._ulog(session_id, "SWITCH", f"创建 OC {name}" + ("（已清空对话上下文）" if switching else ""))

        lines = [
            f"OC 已创建: {name}",
            f"作品: {series or '原创/未指定'}",
            f"角色类型: {role}",
            f"年龄段: {age or '自动推断'}",
            f"职业: {occupation or '自动推断'}",
            f"身体特征: {appearance_tags[:300]}",
        ]
        if original_name and original_name != name:
            lines.append(f"原名: {original_name}")
        if visual_character or visual_series:
            lines.append(f"生图识别: {visual_character or '（空）'}{(' / ' + visual_series) if visual_series else ''}")
        if outfit_tags:
            lines.append(f"初始穿搭: {outfit_tags[:200]}")
        if relationship:
            lines.append(f"关系: {relationship[:200]}")
        if user_address:
            lines.append(f"对用户称呼: {user_address[:80]}")
        if applied_style:
            lines.append(f"画风: {applied_style[:200]}")
        summary = prompt_intake.useful_summary(intake)
        if summary:
            lines.append(f"自动归档: {summary[:300]}")
        if city_note:
            lines.append(city_note)
        hint = self._slot_fill_hint(self._missing_character_slots(state))
        if hint:
            lines.append("\n" + hint)
        lines.append("\n现在可以直接开始聊天，或发送 /自拍 看第一张图。")
        await self.send_message(chat_id, "\n".join(lines))

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
        self._ulog(session_id, "MEM+", f"#{memory_id} 手动[{kind}]: {summary}")
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
        await self._checkpoint_current_context_before_reset(session_id)
        state = self._get_session_state(session_id)
        self._reset_short_context(state, "用户手动开启新短期场景", session_id=session_id)
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, "已开启新的短期场景。之后默认不会主动延续切换前的话题、画面和动作。")

    async def cmd_rollback(self, chat_id, session_id, arg):
        """回退 N 轮对话：从聊天历史尾部删掉最近 N 条角色回复及其对应的用户消息。方便测试时撤回。"""
        state = self._get_session_state(session_id)
        history = session_schema.get_chat_history(state)
        if not history:
            await self.send_message(chat_id, "当前没有可回滚的对话。")
            return
        arg = (arg or "").strip()
        try:
            n = max(1, int(arg)) if arg else 1
        except ValueError:
            await self.send_message(chat_id, "用法: /回滚 [轮数]，例如 /回滚 2（默认 1 轮）。")
            return
        turns = 0
        while turns < n and history:
            popped_assistant = False
            while history:
                if history.pop().get("role") == "assistant":
                    popped_assistant = True
                    break
            if not popped_assistant:
                break
            if history and history[-1].get("role") == "user":
                history.pop()
            turns += 1
        session_schema.set_chat_history(state, history)
        if session_schema.get_short_context_start(state) > len(history):
            session_schema.set_short_context_start(state, 0)
        session_schema.set_replying_to_selfie(state, False)
        self._save_session_state(session_id, state)
        self._ulog(session_id, "ROLLBACK", f"回退 {turns} 轮，剩余 {len(history)} 条上下文")
        tail = next((m.get("content", "") for m in reversed(history) if m.get("role") == "user"), "")
        note = f"已回退 {turns} 轮对话，剩余 {len(history)} 条上下文。"
        if tail:
            note += f"\n当前末尾用户消息: {tail[:80]}"
        await self.send_message(chat_id, note)

    async def cmd_regenerate(self, chat_id, session_id, arg):
        """重答：删掉上一条角色回复，用同一条用户消息重新生成。方便测试对比。"""
        state = self._get_session_state(session_id)
        history = session_schema.get_chat_history(state)
        if history and history[-1].get("role") == "assistant":
            history.pop()
        if not history or history[-1].get("role") != "user":
            await self.send_message(chat_id, "没有可重答的上一条用户消息。")
            return
        last_user = (history.pop().get("content") or "").strip()
        session_schema.set_chat_history(state, history)
        session_schema.set_replying_to_selfie(state, False)
        self._save_session_state(session_id, state)
        if not last_user:
            await self.send_message(chat_id, "上一条用户消息为空，无法重答。")
            return
        if not self.has_llm_config("chat"):
            await self.send_message(chat_id, "聊天模型未配置，无法重答。")
            return
        await self.send_action(chat_id, "typing")
        reply = await self.run_roleplay_chat(chat_id, session_id, last_user)
        if reply:
            self._ulog(session_id, "REGEN", reply)
            split = str(self.config.get("chat_split_paragraphs", "true")).lower() in ("true", "1", "yes")
            await self.send_message(chat_id, reply, split_paragraphs=split)

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
        session_schema.set_character_value(state, "custom_location", city)
        off = await self._resolve_city_timezone(city, w.get("lon"))
        note = ""
        if off is not None:
            session_schema.set_character_value(state, "custom_timezone_offset", str(off))
            note = f"\n已自动识别本会话时区为 UTC{off:+g}（忽略夏令时）"
        self._save_session_state(session_id, state)
        catalog_note = ""
        if hasattr(self, "_ensure_city_place_catalog"):
            try:
                catalog = await self._ensure_city_place_catalog(city)
                status = catalog.get("status")
                count = sum(len(v) for v in (catalog.get("places") or {}).values())
                if status == "generated":
                    catalog_note = f"\n城市地点目录: 已生成增强版（{count} 个地点）"
                elif status == "cached":
                    catalog_note = f"\n城市地点目录: 已使用缓存增强版（{count} 个地点）"
                elif status == "basic":
                    catalog_note = "\n城市地点目录: 生图辅助模型未配置，暂用基础场所模块"
                elif status == "disabled":
                    catalog_note = "\n城市地点目录: 增强地点已关闭，使用基础场所模块"
                elif status == "failed":
                    catalog_note = "\n城市地点目录: 增强生成失败，暂用基础场所模块"
            except Exception as exc:
                self._ulog(session_id, "WARN", f"城市地点目录生成失败: {exc}")
                catalog_note = "\n城市地点目录: 增强生成失败，暂用基础场所模块"
        await self.send_message(chat_id, f"本会话天气城市已设置为 {city}\n当前天气: {w['desc']}，{w['temp']} C{note}{catalog_note}")

    async def cmd_selfie(self, chat_id, session_id, arg):
        if self._gen_lock.locked():
            await self.send_message(chat_id, "正在拍照中，请稍后再试。")
            return
        await self.send_action(chat_id, "upload_photo")
        now = self._session_now(session_id)
        w = await self._fetch_weather(session_id=session_id)
        weather = f"{w['desc']} {w['temp']} C" if w else "未知"
        time_ctx = self._get_time_context(session_id, now=now, weather=w)
        time_period = time_ctx.get("period") or self._get_time_period(now.hour)
        scene, caption, new_app, view, orientation = await self._llm_write_scene(
            "normal", weather, WEEKDAY_NAMES[now.weekday()], time_period, None, session_id, now=now, weather_data=w
        )
        if not scene:
            scene, caption = random.choice(SCENES)
        # /自拍 命令明确要求自拍视角，强制 view=selfie，不受场景生成器偶然返回的 third 影响。
        view = "selfie"
        english = await self._translate_to_tags(scene, session_id=session_id, view=view)
        ok, imgs, err = await self._do_generate(english, session_id=session_id, one_shot_appearance=new_app or "", orientation=orientation or "")
        if not ok or not imgs:
            self._ulog(session_id, "ERROR", f"自拍生图失败: {err}")
            await self.send_message(chat_id, f"生图失败: {err}")
            return
        await self.send_photo(chat_id, imgs[0], caption or "")
        for extra in imgs[1:]:
            await self.send_photo(chat_id, extra)
        state = self._get_session_state(session_id)
        source = self._format_image_source_description(
            intent=f"自拍命令生成的 normal 模式画面，时段: {time_period}，天气: {weather}",
            prompt=caption or "",
        )
        self._record_sent_photo(
            session_id,
            scene,
            caption or "",
            appearance=new_app or session_schema.get_outfit(state),
            view=view,
            source_description=source,
        )

    async def cmd_scene_image(self, chat_id, session_id, arg):
        if self._gen_lock.locked():
            await self.send_message(chat_id, "正在生图中，请稍后再试。")
            return
        text = (arg or "").strip()
        intent = "根据当前聊天场景配一张图"
        if text:
            intent = "根据当前聊天场景配图，并优先满足用户输入的画面参数"
        result = await self.tool_generate_image(
            chat_id,
            session_id,
            prompt=text,
            intent=intent,
            must_include=text,
            planning_mode="illustration",
        )
        if result.startswith("生图失败") or result == "缺少图片意图":
            await self.send_message(chat_id, result)

    async def cmd_test_push(self, chat_id, session_id, arg):
        mode = (arg or "normal").strip() or "normal"
        await self.send_message(chat_id, f"正在强制触发 {mode} 模式推送。")
        now = self._session_now(session_id)
        if hasattr(self, "_create_scheduled_push_task"):
            self._create_scheduled_push_task(
                session_id,
                now,
                mode_override=mode,
                skip_active_check=True,
            )
        else:
            asyncio.create_task(self._sched_fire(session_id, now, mode_override=mode, skip_active_check=True))

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
        lines.append("\n用法: /画风 <画风名> | /画风 清空 | /画风 添加 @xxx | /画风 删除 序号")
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
        for sid, state in self.sessions.items():
            if session_schema.get_character_value(state, "custom_current_style", "") == removed:
                session_schema.set_character_value(state, "custom_current_style", "")
                self._mark_dirty(sid)
        self.save_config()
        self._flush_sessions(force=True)
        await self.send_message(chat_id, f"已删除 {removed}。")

    async def cmd_switch_style(self, chat_id, session_id, arg):
        target = arg.strip()
        pool = self._normalize_style_pool()
        if not target or target.lower() in ("查看", "list", "ls", "show"):
            await self.send_message(chat_id, self._style_list_text(session_id))
            return
        if target.lower() in ("style reset", "clear", "reset", "none", "empty", "默认", "全局", "清空", "留空", "无"):
            self._set_current_style(session_id, "")
            await self.send_message(chat_id, f"已清空当前角色画风字段，当前有效画风: {self._get_current_style(session_id)}")
            return
        chosen = None
        try:
            idx = int(target) - 1
            if 0 <= idx < len(pool):
                chosen = pool[idx]
        except ValueError:
            pass
        if chosen is None:
            chosen = target
        self._set_current_style(session_id, chosen)
        await self.send_message(chat_id, f"当前角色画风已设为 {chosen}")

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
        slots = self._format_last_prompt_slots(session_id)
        await self.send_message(
            chat_id,
            f"当前画风\n{self._get_current_style(session_id)}\n\n"
            f"角色设定\n{self._get_session_cfg(session_id, 'positive_prefix', '')[:300]}\n\n"
            f"Prompt 槽位\n{slots[:1800] if slots else '（暂无）'}\n\n"
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
        lines.append(f"人设: {session_schema.get_character_value(state, 'custom_scheduled_persona', '') or '（默认）'}")
        ch = session_schema.get_character_value(state, "custom_character", "")
        if ch:
            series = session_schema.get_character_value(state, "custom_series", "")
            lines.append(f"角色: {ch}{'（' + series + '）' if series else ''}")
        visual_character = session_schema.get_character_value(state, "custom_visual_character", "")
        visual_series = session_schema.get_character_value(state, "custom_visual_series", "")
        if visual_character or visual_series:
            lines.append(f"生图识别: {visual_character or '（空）'}{('（' + visual_series + '）') if visual_series else ''}")
        lines.append(f"身体特征: {self._get_session_cfg(session_id, 'positive_prefix', '')[:300] or '（未设置）'}")
        lines.append(f"画风: {self._get_current_style(session_id)}")
        if session_schema.get_outfit(state):
            lines.append(f"外型覆盖: {session_schema.get_outfit(state)}")
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
        session_schema.set_character_value(state, "custom_scheduled_persona", text)
        session_schema.set_character_value(state, "persona_user_set", True)
        state.pop("life_profile", None)
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, "人格已更新。")

    def _reset_session_customization(self, state: dict[str, Any]):
        """把单个会话恢复到"未设角色、走全局默认人设"的干净状态。

        清空所有 custom_* 覆盖、临时外型、人格/角色标记位、纯良度覆盖、整个角色档案池，
        并重置对话上下文。重置对话上下文是必须的：否则 chat_history 里旧角色口吻的历史
        发言会被重新喂回模型，导致即使系统提示已换成默认人设，模型仍沿用旧人设回应。
        """
        for key in SESSION_CUSTOM_RESET_KEYS:
            session_schema.set_character_value(state, key, "")
        state.pop("custom_daily_selfie_limit", None)
        session_schema.set_character_value(state, "custom_allow_llm_change_appearance", None)
        session_schema.set_outfit(state, "")
        session_schema.set_wardrobe(state, {})
        session_schema.set_closet(state, {})
        session_schema.clear_nudity(state)
        session_schema.set_character_value(state, "persona_user_set", False)
        session_schema.set_character_value(state, "purity", None)
        session_schema.set_character_value(state, "purity_user_set", False)
        session_schema.set_daily_trigger_date(state, "")  # 让随机推送计划按全局默认重新生成
        session_schema.get_saved_characters(state).clear()  # 清空本会话角色池
        state.pop("life_profile", None)
        self._clear_conversation_context(state)

    def _clear_transient_state(self, state: dict[str, Any], *, keep_appearance: bool = False):
        """把「角色短期态」字段复位到默认值——遍历分类器、按 `_session_state_defaults` 逐字段复位，
        而非手写名单（根治 ③：新增短期字段无需再来这里补一行，漏配也不会串味）。

        keep_appearance=True：保留当前外型/穿搭等（`/reset` 清对话但留外型）；
        keep_appearance=False：整体清空（切角色 restore 前的全清）。
        会话全局与角色配置字段始终不动。
        """
        defaults = self._session_state_defaults()
        for key in list(state.keys()):
            if not _is_transient_state_key(key):
                continue
            if keep_appearance and key in RESET_PRESERVED_TRANSIENT_KEYS:
                continue
            if key in defaults:
                state[key] = copy.deepcopy(defaults[key])
            else:
                # 默认表里没有的动态短期键（life_profile/user_co_located/character_place_name 等）：
                # 删除即回到“未设”自然默认。
                state.pop(key, None)

    def _clear_conversation_context(self, state: dict[str, Any]):
        """清掉会带着旧话题/旧画面回流进提示词的对话上下文（`/reset`、clearup 用），保留当前外型/穿搭。"""
        self._clear_transient_state(state, keep_appearance=True)

    @staticmethod
    def _character_context_key_from_state(state: dict[str, Any]) -> str:
        return (
            session_schema.get_character_value(state, "custom_character", "")
            or session_schema.get_character_value(state, "custom_bot_name", "")
            or "__default__"
        ).strip() or "__default__"

    @staticmethod
    def _conversation_context_payload(state: dict[str, Any]) -> dict[str, Any]:
        """冻结当前角色的全部短期态：黑名单之外即冻，与清空同源，杜绝“冻的和清的不一致”。"""
        return {key: state.get(key) for key in state.keys() if _is_transient_state_key(key)}

    def _save_current_character_context(self, state: dict[str, Any]):
        key = self._character_context_key_from_state(state)
        session_schema.get_character_contexts(state)[key] = self._conversation_context_payload(state)

    def _restore_character_context(self, session_id: str, state: dict[str, Any]):
        key = self._character_context_key_from_state(state)
        payload = session_schema.get_character_contexts(state).get(key)
        # 切角色：整体清空全部短期态（含外型/穿搭），再用目标角色存档覆盖——保证不串味，且切回拿回自己穿搭。
        self._clear_transient_state(state, keep_appearance=False)
        if isinstance(payload, dict):
            for ctx_key, value in payload.items():
                state[ctx_key] = value
        try:
            checkpoint = self.app_store.get_checkpoint(session_id, self._context_character_key(session_id))
            if checkpoint.get("summary"):
                session_schema.set_checkpoint_summary(state, checkpoint.get("summary") or "")
                session_schema.set_checkpoint_message_id(state, int(checkpoint.get("source_until_id") or 0))
        except Exception:
            pass

    @staticmethod
    def _character_export_payload(state: dict[str, Any]) -> dict[str, Any]:
        key = (
            session_schema.get_character_value(state, "custom_character", "")
            or session_schema.get_character_value(state, "custom_bot_name", "")
            or "default"
        ).strip() or "default"
        # 字段表由 character_card 单一来源派生（见模块说明）。
        return {"id": key, **character_card.card_from_state(state)}

    @staticmethod
    def _apply_character_payload(state: dict[str, Any], data: dict[str, Any]):
        character_card.apply_card_to_state(state, data)

    @staticmethod
    def _snapshot_character(state: dict[str, Any]):
        """保存当前角色最新状态到 saved_characters 快照。字段表由 character_card 单一来源派生。"""
        name = (session_schema.get_character_value(state, "custom_character", "") or "").strip()
        if not name:
            return
        card = character_card.card_from_state(state)
        card["character"] = name  # 存档键用 stripped 名，character 字段与键对齐
        session_schema.get_saved_characters(state)[name] = card

    async def cmd_purity(self, chat_id, session_id, arg):
        text = arg.strip().lower()
        state = self._get_session_state(session_id)
        if not text:
            p = self._get_purity(session_id)
            src = "手动设定" if session_schema.get_character_value(state, "purity_user_set", False) else "自动/默认"
            await self.send_message(chat_id, f"当前纯良度: {p}/10（{src}）\nNTR 触发周期: {self._compute_ntr_threshold(p)}天\n用法: /纯良度 0~10 或 /纯良度 auto")
            return
        if text in ("auto", "默认", "reset", "自动"):
            session_schema.set_character_value(state, "purity", None)
            session_schema.set_character_value(state, "purity_user_set", False)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已恢复自动/默认纯良度: {self._get_purity(session_id)}/10")
            return
        try:
            val = max(0, min(10, int(text)))
        except ValueError:
            await self.send_message(chat_id, "请输入 0~10 的整数，或 auto。")
            return
        session_schema.set_character_value(state, "purity", val)
        session_schema.set_character_value(state, "purity_user_set", True)
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
            times = session_schema.get_daily_trigger_times(state)
            plan = "已关闭随机推送" if cur_limit() == 0 else ("今日推送点: " + "、".join(times) if times else "今日推送点将在下一轮调度生成")
            await self.send_message(chat_id, f"每日主动推送次数: {cur_limit()} 次/天\n{plan}\n用法: /推送频率 <0~20> 或 /推送频率 默认")
            return
        if text in ("默认", "全局", "reset", "auto"):
            state.pop("custom_daily_selfie_limit", None)
            session_schema.set_daily_trigger_date(state, "")
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
        session_schema.set_daily_trigger_date(state, "")
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, "已关闭本会话随机推送。" if val == 0 else f"每日主动推送次数已设为 {val} 次/天。")

    async def cmd_character(self, chat_id, session_id, arg):
        text = arg.strip()
        state = self._get_session_state(session_id)
        saved = session_schema.get_saved_characters(state)
        lower = text.lower()
        if not text or lower in ("查看", "show"):
            lines = ["当前角色设定"]
            default_id = self._default_character_payload().get("id") or ""
            current_character = session_schema.get_character_value(state, "custom_character", "")
            on_default = not (current_character or session_schema.get_character_value(state, "persona_user_set", False))
            lines.append(f"角色: {current_character or (f'{default_id}（默认）' if on_default and default_id else '（未设定）')}")
            series = session_schema.get_character_value(state, "custom_series", "")
            if series:
                lines.append(f"作品: {series}")
            lines.append(f"人设: {(session_schema.get_character_value(state, 'custom_scheduled_persona', '') or '（未设定）')[:300]}")
            lines.append(f"身体特征: {(session_schema.get_character_value(state, 'custom_positive_prefix', '') or '（未设定）')[:300]}")
            pool = ([f"{default_id}(默认)"] if default_id and default_id not in saved else []) + list(saved.keys())
            lines.append(f"已保存角色: {', '.join(pool) or '无'}")
            lines.append("\n用法: /角色 <角色名> | /角色 load <名称> | /角色 list | /角色 delete <名称>")
            lines.append("/角色 reset 仅清空对话上下文 | /角色 clearup 恢复全局默认（清角色/人设/角色池）")
            await self.send_message(chat_id, "\n".join(lines))
            return
        if lower == "reset":
            self._clear_conversation_context(state)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "RESET", "/角色 reset 清空对话上下文")
            await self.send_message(chat_id, RESET_DONE_MSG)
            return
        if lower in ("clear", "重置", "恢复默认", "清除"):
            await self.send_message(chat_id, "轻量重置: /角色 reset（仅清对话上下文）\n硬重置: /角色 clearup（恢复全局默认，清角色/人设/角色池）")
            return
        parts = text.split(None, 1)
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if sub == "clearup":
            count = len(saved)
            self._reset_session_customization(state)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "RESET", f"/角色 clearup 恢复全局默认（清角色/人设/对话/角色池，共 {count} 个角色档案）")
            await self.send_message(chat_id, CLEARUP_DONE_MSG)
            return
        if sub in ("list", "ls"):
            default = self._default_character_payload()
            default_id = default.get("id") or ""
            on_default = not (
                session_schema.get_character_value(state, "custom_character", "")
                or session_schema.get_character_value(state, "persona_user_set", False)
            )
            lines = []
            if default_id:
                lines.append(f"{default_id}: 系统默认角色" + ("（当前）" if on_default else ""))
            lines += [f"{k}: {v.get('character', k)}" for k, v in saved.items() if k != default_id]
            await self.send_message(chat_id, "角色列表\n" + "\n".join(lines))
            return
        if sub in ("export", "导出"):
            payload = self._character_export_payload(state)
            await self.send_message(chat_id, json.dumps(payload, ensure_ascii=False, indent=2))
            return
        if sub in ("import", "导入"):
            if not sub_arg:
                await self.send_message(chat_id, "用法：/角色 import <json> 或 /角色 导入 <json>")
                return
            try:
                payload = json.loads(sub_arg)
            except Exception as exc:
                await self.send_message(chat_id, f"JSON 无效：{exc}")
                return
            if not isinstance(payload, dict):
                await self.send_message(chat_id, "角色 JSON 必须是对象。")
                return
            key = str(payload.get("id") or payload.get("character") or payload.get("bot_name") or "").strip()
            if not key:
                await self.send_message(chat_id, "角色 JSON 必须包含 id 或 character。")
                return
            self._save_current_character_context(state)
            self._snapshot_character(state)
            self._apply_character_payload(state, payload)
            if not session_schema.get_character_value(state, "custom_character", ""):
                session_schema.set_character_value(state, "custom_character", key)
            saved[key] = {k: v for k, v in payload.items() if k != "id"}
            self._restore_character_context(session_id, state)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已导入并切换到角色：{key}")
            return
        if sub == "load" and sub_arg:
            data = saved.get(sub_arg)
            if not data and sub_arg == (self._default_character_payload().get("id") or ""):
                # 系统默认角色（蕾伊）不落进 saved_characters：现取现合成，加载即回到隐式默认态。
                data = self._default_character_payload()
            if not data:
                await self.send_message(chat_id, f"未找到角色 {sub_arg}。")
                return
            current_character = session_schema.get_character_value(state, "custom_character", "")
            switching = (data.get("character", "") or "") != (current_character or "")
            if switching:
                self._save_current_character_context(state)
                self._snapshot_character(state)
            payload = dict(data)
            if not switching:
                payload["role_name"] = (
                    session_schema.get_character_value(state, "custom_role_name", "")
                    or data.get("role_name", "")
                )
                payload["bot_self_name"] = (
                    session_schema.get_character_value(state, "custom_bot_self_name", "")
                    or data.get("bot_self_name", "")
                )
                payload["relationship"] = (
                    session_schema.get_character_value(state, "custom_spatial_relationship", "")
                    or data.get("relationship", "")
                )
            if "style" not in data:
                payload.pop("style", None)
            if data.get("purity") is None or session_schema.get_character_value(state, "purity_user_set", False):
                payload.pop("purity", None)
            self._apply_character_payload(state, payload)
            if switching:
                # 短期态（含 dynamic_appearance/wardrobe）现已随 character_contexts 冻结/解冻：
                # 切回的角色拿回自己存档的穿搭，新角色则被清空——不再需要在此硬抹（那会覆盖存档）。
                self._restore_character_context(session_id, state)
            state.pop("life_profile", None)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "SWITCH", f"载入角色 {sub_arg}" + ("（已清空对话上下文）" if switching else ""))
            await self.send_message(chat_id, f"已载入角色 {sub_arg}。")
            return
        if sub == "delete" and sub_arg:
            if sub_arg == (self._default_character_payload().get("id") or ""):
                await self.send_message(chat_id, "系统默认角色不可删除。")
                return
            if sub_arg not in saved:
                await self.send_message(chat_id, f"未找到角色 {sub_arg}。")
                return
            del saved[sub_arg]
            # 删的若是当前角色，必须同步清空当前角色态：否则下次 _snapshot_character（load/
            # 切换/create_oc/设定角色都会触发）会用 state["custom_character"] 把刚删的角色重新
            # 写回 saved_characters，表现为"删了又出现"。这里只复用字段清理原语，不动其它角色。
            is_current = (session_schema.get_character_value(state, "custom_character", "") or "") == sub_arg
            note = ""
            contexts = session_schema.get_character_contexts(state)
            if isinstance(contexts, dict):
                contexts.pop(sub_arg, None)
            if is_current:
                for key in SESSION_CUSTOM_RESET_KEYS:
                    session_schema.set_character_value(state, key, "")
                session_schema.set_outfit(state, "")
                session_schema.set_wardrobe(state, {})
                session_schema.set_closet(state, {})
                session_schema.clear_nudity(state)
                session_schema.set_character_value(state, "persona_user_set", False)
                session_schema.set_character_value(state, "purity", None)
                session_schema.set_character_value(state, "purity_user_set", False)
                self._clear_conversation_context(state)
                self._ulog(session_id, "DELETE", f"删除当前角色 {sub_arg} 并回退全局默认")
                note = "\n当前角色已回退到全局默认。"
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已删除角色 {sub_arg}。{note}")
            return
        if sub in ("load", "delete"):
            await self.send_message(chat_id, f"用法: /角色 {sub} <名称>")
            return

        # /角色 <名称> 或 /切换角色 <名称>：如果名称匹配已保存角色卡，直接加载（不走 LLM 分类）
        match_key = text
        if match_key in saved:
            data = saved[match_key]
            current_character = session_schema.get_character_value(state, "custom_character", "")
            switching = (data.get("character", "") or "") != (current_character or "")
            if switching:
                self._save_current_character_context(state)
                self._snapshot_character(state)
            payload = dict(data)
            if not switching:
                payload["role_name"] = (
                    session_schema.get_character_value(state, "custom_role_name", "")
                    or data.get("role_name", "")
                )
                payload["bot_self_name"] = (
                    session_schema.get_character_value(state, "custom_bot_self_name", "")
                    or data.get("bot_self_name", "")
                )
                payload["relationship"] = (
                    session_schema.get_character_value(state, "custom_spatial_relationship", "")
                    or data.get("relationship", "")
                )
            if "style" not in data:
                payload.pop("style", None)
            if data.get("purity") is None or session_schema.get_character_value(state, "purity_user_set", False):
                payload.pop("purity", None)
            self._apply_character_payload(state, payload)
            if switching:
                self._restore_character_context(session_id, state)
            state.pop("life_profile", None)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "SWITCH", f"载入角色 {match_key}" + ("（已清空对话上下文）" if switching else ""))
            await self.send_message(chat_id, f"已载入角色 {match_key}。")
            return

        if not self.has_llm_config("image"):
            if "," in text or re.search(r"[a-zA-Z]{3,}", text):
                session_schema.set_character_value(state, "custom_positive_prefix", text)
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
            switching = name != (session_schema.get_character_value(state, "custom_character", "") or "")
            if switching:
                self._save_current_character_context(state)
                self._snapshot_character(state)
            raw_appearance = (result.get("appearance") or "").strip()
            count_tag = ""
            m = re.match(r"\b(1girl|1boy)\b", raw_appearance)
            if m:
                count_tag = m.group(1)
                raw_appearance = raw_appearance[m.end():].strip(" ,")
            session_schema.set_character_value(state, "custom_character", name)
            session_schema.set_character_value(state, "custom_series", result.get("series", ""))
            session_schema.set_character_value(state, "custom_role_name", result.get("role_name") or result.get("role") or "")
            session_schema.set_character_value(state, "custom_bot_name", name)
            if switching:
                session_schema.set_character_value(state, "custom_bot_self_name", "")
                session_schema.set_outfit(state, "")  # 既有角色不继承上一个角色/默认的临时穿搭，初始服装交给画面规划器按场景决定
            session_schema.set_character_value(state, "custom_visual_character", result.get("prompt_name") or result.get("visual_name") or result.get("image_name") or "")
            session_schema.set_character_value(state, "custom_visual_series", result.get("prompt_series") or result.get("visual_series") or result.get("image_series") or "")
            session_schema.set_character_value(state, "custom_scheduled_persona", result.get("persona", ""))
            session_schema.set_character_value(state, "custom_positive_prefix", raw_appearance)
            if count_tag:
                session_schema.set_character_value(state, "custom_count", count_tag)
            # 补齐 age/职业/白天去向/关系：大模型认识该角色，可直接判断；anchor 缺失时由职业派生。
            occupation = (result.get("occupation") or "").strip()
            anchor = self._normalize_day_anchor(result.get("anchor")) or self._normalize_day_anchor(occupation)
            session_schema.set_character_value(state, "custom_character_age_stage", self._normalize_age_stage(result.get("age")))
            session_schema.set_character_value(state, "custom_character_occupation", occupation)
            session_schema.set_character_value(state, "custom_character_day_anchor", anchor)
            relationship = (result.get("relationship") or "").strip()
            if relationship and (switching or not session_schema.get_character_value(state, "custom_spatial_relationship", "")):
                session_schema.set_character_value(state, "custom_spatial_relationship", relationship)
            if result.get("purity") is not None and not session_schema.get_character_value(state, "purity_user_set", False):
                try:
                    session_schema.set_character_value(state, "purity", max(0, min(10, int(result["purity"]))))
                except (TypeError, ValueError):
                    pass
            self._snapshot_character(state)
            if switching:
                self._restore_character_context(session_id, state)
            self._save_session_state(session_id, state)
            self._ulog(session_id, "SWITCH", f"设定角色 {name}" + ("（已清空对话上下文）" if switching else ""))
            visual_note = ""
            visual_character = session_schema.get_character_value(state, "custom_visual_character", "")
            visual_series = session_schema.get_character_value(state, "custom_visual_series", "")
            if visual_character or visual_series:
                visual_note = f"\n生图识别: {visual_character or '（空）'}{(' / ' + visual_series) if visual_series else ''}"
            reply = (
                f"已设定角色: {name}\n"
                f"作品: {session_schema.get_character_value(state, 'custom_series', '') or '（未指定）'}{visual_note}\n"
                f"人设: {session_schema.get_character_value(state, 'custom_scheduled_persona', '')[:200]}\n"
                f"身体特征: {session_schema.get_character_value(state, 'custom_positive_prefix', '')[:250]}"
            )
            hint = self._slot_fill_hint(self._missing_character_slots(state))
            if hint:
                reply += "\n\n" + hint
            await self.send_message(chat_id, reply)
        elif result.get("type") == "appearance":
            raw_tags = (result.get("tags") or "").strip()
            count_tag = ""
            m = re.match(r"\b(1girl|1boy)\b", raw_tags)
            if m:
                count_tag = m.group(1)
                raw_tags = raw_tags[m.end():].strip(" ,")
            session_schema.set_character_value(state, "custom_positive_prefix", raw_tags)
            if count_tag:
                session_schema.set_character_value(state, "custom_count", count_tag)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"身体特征已更新:\n{session_schema.get_character_value(state, 'custom_positive_prefix', '')}")
        else:
            await self.send_message(chat_id, f"LLM 返回了无法识别的结果: {result}")

    async def cmd_personalize(self, chat_id, session_id, arg):
        state = self._get_session_state(session_id)
        parts = arg.split(None, 1) if arg else []
        action = parts[0] if parts else ""
        value = parts[1] if len(parts) > 1 else ""
        mapping = {alias: key for _, key, aliases in PERSONALIZE_FIELDS for alias in aliases}
        if not action:
            lines = ["其余设置（空=使用全局默认）"]
            for label, key, _ in PERSONALIZE_FIELDS:
                lines.append(f"{label}: {session_schema.get_character_value(state, key, '') or '（默认）'}")
            lines.append(f"关系设定: {session_schema.get_character_value(state, 'custom_spatial_relationship', '') or '（默认）'}（用 /关系 修改）")
            lines.append("人格文本: 用 /人格 查看或修改")
            lines.append("\n用法: /个性设置 <项> <值> 覆盖单项。/角色 reset 仅清对话上下文，/角色 clearup 硬重置。")
            await self.send_message(chat_id, "\n".join(lines))
            return
        if action == "reset":
            await self.send_message(chat_id, "轻量重置: /角色 reset（仅清对话上下文）\n硬重置: /角色 clearup（恢复全局默认，清角色/人设/角色池）")
            return
        key = mapping.get(action)
        if not key:
            await self.send_message(chat_id, f"未知设置项: {action}，可用: {', '.join(mapping.keys())}")
            return
        if not value:
            await self.send_message(chat_id, f"当前 {action}: {session_schema.get_character_value(state, key, '') or '（默认）'}")
            return
        session_schema.set_character_value(state, key, value)
        if key == "custom_character_occupation":
            # 职业是用户面向字段，白天去向枚举由职业后台派生。
            session_schema.set_character_value(state, "custom_character_day_anchor", self._normalize_day_anchor(value))
        if key in PERSONALIZE_LIFE_PROFILE_KEYS:
            state.pop("life_profile", None)
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, f"{action} 已覆盖为: {value[:200]}")

    async def cmd_relationship(self, chat_id, session_id, arg):
        state = self._get_session_state(session_id)
        value = (arg or "").strip()
        if not value:
            current = session_schema.get_character_value(state, "custom_spatial_relationship", "") or "（默认）"
            await self.send_message(
                chat_id,
                f"当前你和角色的关系: {current}\n\n"
                "用法: /关系 <描述你和角色的关系>\n"
                "例: /关系 同城暧昧对象，周末经常一起出门\n"
                "可写同居、异地、同公司、同学校等；作为高级覆盖项，不替代自动动线。",
            )
            return
        session_schema.set_character_value(state, "custom_spatial_relationship", value)
        self._save_session_state(session_id, state)
        await self.send_message(chat_id, f"你和角色的关系已设为: {value[:200]}")

    def _replace_appearance_slot(self, state: dict[str, Any], slot: str, value: str) -> None:
        """把当前角色 base（custom_positive_prefix）里某个外观槽（hair/eyes）整体替换为 value。
        发/瞳是角色 base 的一部分、per-character；改完由调用方 _snapshot_character 写回卡。"""
        base = session_schema.get_character_value(state, "custom_positive_prefix", "") or ""
        slots = self._parse_appearance(base)
        slots[slot] = [v.strip() for v in str(value or "").split(",") if v.strip()]
        session_schema.set_character_value(state, "custom_positive_prefix", appearance_rules.slots_to_string(slots))

    async def cmd_appearance(self, chat_id, session_id, arg):
        tags = arg.strip()
        state = self._get_session_state(session_id)
        if not tags:
            wardrobe_view = appearance_rules.wardrobe_summary(self._get_wardrobe(state)) or "（默认）"
            await self.send_message(
                chat_id,
                "当前外型设置\n"
                f"衣柜（按槽位）:\n{wardrobe_view}\n"
                f"物种特征: {(session_schema.get_character_value(state, 'custom_positive_prefix', '') or '（默认）')[:200]}\n"
                f"默认发色: {session_schema.get_character_value(state, 'custom_default_hair', '') or self.config.get('default_hair')}\n"
                f"默认瞳色: {session_schema.get_character_value(state, 'custom_default_eyes', '') or self.config.get('default_eyes')}\n"
                f"模型自主改外型: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}\n\n"
                "换装直接说穿/换/脱即可（上衣/下装/连衣裙/外套/内衣/袜/鞋分槽，同槽自动替换，连衣裙覆盖上下装）。\n"
                "用法: /外型 <换装描述，如 换上红色旗袍 / 脱掉外套 / 只换黑色胸罩> | /外型 归档 <描述> | /外型 特征 <标签> | /外型 发色 <标签> | /外型 瞳色 <标签> | /外型 自动变装 on/off | /外型 reset",
            )
            return
        parts = tags.split(None, 1)
        sub = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        auto_intake_requested = sub in ("归档", "自动归档", "intake", "classify")
        if auto_intake_requested:
            if not sub_arg:
                await self.send_message(chat_id, "用法: /外型 归档 <自然描述>")
                return
            tags = sub_arg
            parts = tags.split(None, 1)
            sub = parts[0].lower() if parts else ""
            sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("特征", "traits"):
            if not sub_arg:
                await self.send_message(chat_id, f"当前物种特征: {session_schema.get_character_value(state, 'custom_positive_prefix', '') or '（默认）'}")
                return
            if re.search(r"[\u4e00-\u9fff]", sub_arg):
                sub_arg = await self._translate_appearance_tags(sub_arg)
            session_schema.set_character_value(state, "custom_positive_prefix", sub_arg)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"物种特征已更新: {sub_arg[:300]}")
            return
        if sub in ("发色", "hair"):
            if not sub_arg:
                if self._is_character_set(session_id):
                    w = self._get_wardrobe(state)
                    cur = (w.get("hair") or "").strip() or "（未设定，见身体特征）"
                    await self.send_message(chat_id, f"当前角色发色: {cur}")
                else:
                    await self.send_message(chat_id, f"当前默认发色: {session_schema.get_character_value(state, 'custom_default_hair', '') or self.config.get('default_hair')}")
                return
            if re.search(r"[\u4e00-\u9fff]", sub_arg):
                sub_arg = await self._translate_appearance_tags(sub_arg)
            if self._is_character_set(session_id):
                wardrobe = self._get_wardrobe(state)
                wardrobe["hair"] = sub_arg
                session_schema.set_wardrobe(state, wardrobe)
                session_schema.set_outfit(state, appearance_rules.render_wardrobe(wardrobe))
                self._snapshot_character(state)
                self._save_session_state(session_id, state)
                await self.send_message(chat_id, f"角色发色已更新: {sub_arg}")
            else:
                session_schema.set_character_value(state, "custom_default_hair", sub_arg)
                self._save_session_state(session_id, state)
                await self.send_message(chat_id, f"默认发色已更新: {sub_arg}")
            return
        if sub in ("瞳色", "eyes"):
            if not sub_arg:
                if self._is_character_set(session_id):
                    w = self._get_wardrobe(state)
                    cur = (w.get("eyes") or "").strip() or "（未设定，见身体特征）"
                    await self.send_message(chat_id, f"当前角色瞳色: {cur}")
                else:
                    await self.send_message(chat_id, f"当前默认瞳色: {session_schema.get_character_value(state, 'custom_default_eyes', '') or self.config.get('default_eyes')}")
                return
            if re.search(r"[\u4e00-\u9fff]", sub_arg):
                sub_arg = await self._translate_appearance_tags(sub_arg)
            if self._is_character_set(session_id):
                wardrobe = self._get_wardrobe(state)
                wardrobe["eyes"] = sub_arg
                session_schema.set_wardrobe(state, wardrobe)
                session_schema.set_outfit(state, appearance_rules.render_wardrobe(wardrobe))
                self._snapshot_character(state)
                self._save_session_state(session_id, state)
                await self.send_message(chat_id, f"角色瞳色已更新: {sub_arg}")
            else:
                session_schema.set_character_value(state, "custom_default_eyes", sub_arg)
                self._save_session_state(session_id, state)
                await self.send_message(chat_id, f"默认瞳色已更新: {sub_arg}")
            return
        if sub in ("自动", "auto", "自动变装"):
            await self._set_auto_appearance(chat_id, session_id, sub_arg)
            return
        if tags.lower() in ("无", "clear", "重置", "reset", "none"):
            await self._apply_wardrobe(session_id, "reset")
            await self.send_message(chat_id, "已重置为默认外型（衣柜已清空）。")
            return
        should_intake = auto_intake_requested or bool(re.search(r"[\u4e00-\u9fff]", tags))
        if should_intake:
            intake = await self._normalize_prompt_intake(tags, context="appearance")
            base_src = (intake.get("base_appearance") or "").strip()
            dynamic_src = (intake.get("dynamic_appearance") or "").strip()
            applied = []
            if base_src:
                gender = self._oc_gender_tag(
                    session_schema.get_character_value(state, "custom_character", ""),
                    session_schema.get_character_value(state, "custom_role_name", ""),
                    session_schema.get_character_value(state, "custom_scheduled_persona", ""),
                    base_src,
                )
                base_tags = await self._oc_translate_tags(base_src)
                merged = self._merge_appearance(session_schema.get_character_value(state, "custom_positive_prefix", ""), base_tags)
                session_schema.set_character_value(state, "custom_positive_prefix", merged)
                if not session_schema.get_character_value(state, "custom_count", ""):
                    session_schema.set_character_value(state, "custom_count", gender)
                applied.append(f"基础外观: {base_tags[:180]}")
            if dynamic_src:
                # 服装/配饰走衣柜分槽（同槽替换、连衣裙互斥），不再扁平合并。
                await self._wardrobe_apply_to_state(state, dynamic_src, session_id=session_id)
                applied.append(f"穿搭/配饰: {(state.get('dynamic_appearance') or '')[:180]}")
            style = self._apply_intake_style(state, intake)
            if style:
                applied.append(f"画风: {style[:120]}")
            scene_pref = (intake.get("scene_preference") or "").strip()
            selfie_pref = (intake.get("selfie_preference") or "").strip()
            if scene_pref:
                session_schema.set_character_value(state, "custom_scene_preference", scene_pref)
                applied.append(f"场景偏好: {scene_pref[:120]}")
            if selfie_pref:
                session_schema.set_character_value(state, "custom_selfie_preference", selfie_pref)
                applied.append(f"自拍偏好: {selfie_pref[:120]}")
            session_schema.set_character_value(state, "custom_prompt_intake", intake)
            if applied:
                self._save_session_state(session_id, state)
                await self.send_message(chat_id, "已按槽位自动归档：\n" + "\n".join(applied))
                return
        result = await self._apply_wardrobe(session_id, tags)
        await self.send_message(chat_id, f"外型已临时更改为: {result}")

    async def cmd_closet(self, chat_id, session_id, arg):
        state = self._get_session_state(session_id)
        closet = session_schema.get_closet(state)
        arg = (arg or "").strip()
        parts = arg.split(None, 1)
        sub = parts[0].lower() if parts else ""
        sub_arg = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("删除", "删", "remove", "rm", "del"):
            if sub_arg in closet:
                closet.pop(sub_arg, None)
                session_schema.set_closet(state, closet)
                self._save_session_state(session_id, state)
                await self.send_message(chat_id, f"已从衣橱移除「{sub_arg}」。")
            else:
                await self.send_message(chat_id, f"衣橱里没有「{sub_arg}」。用法: /衣橱 删除 <名称>")
            return
        if sub in ("清空", "clear", "reset"):
            session_schema.set_closet(state, {})
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "已清空衣橱收藏。")
            return
        summary = appearance_rules.closet_summary(closet)
        wearing = appearance_rules.wardrobe_summary(self._get_wardrobe(state)) or "（默认/无）"
        await self.send_message(
            chat_id,
            f"当前穿着:\n{wearing}\n\n"
            f"衣橱收藏（{len(closet)} 件，可点名复穿，如对我说“换上那件碎花连衣裙”）:\n{summary or '（空，角色穿过的衣服会自动收藏）'}\n\n"
            "用法: /衣橱 | /衣橱 删除 <名称> | /衣橱 清空",
        )

    async def cmd_auto_appearance(self, chat_id, session_id, arg):
        await self._set_auto_appearance(chat_id, session_id, arg.strip())

    async def _set_auto_appearance(self, chat_id, session_id, val):
        state = self._get_session_state(session_id)
        val = (val or "").strip().lower()
        if val in ("on", "1", "开", "允许", "启用", "true", "yes"):
            session_schema.set_character_value(state, "custom_allow_llm_change_appearance", True)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "已允许模型自主修改外型。")
        elif val in ("off", "0", "关", "禁止", "禁用", "false", "no"):
            session_schema.set_character_value(state, "custom_allow_llm_change_appearance", False)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, "已禁止模型自主修改外型。")
        elif val in ("reset", "clear", "默认", "全局"):
            session_schema.set_character_value(state, "custom_allow_llm_change_appearance", None)
            self._save_session_state(session_id, state)
            await self.send_message(chat_id, f"已恢复全局设置: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}")
        else:
            await self.send_message(chat_id, f"当前模型自主改外型: {'允许' if self._allow_llm_change_appearance(session_id) else '禁止'}\n用法: /外貌自动 on | off | reset")

    async def cmd_sched(self, chat_id, session_id, arg):
        now = self._session_now(session_id)
        state = self._get_session_state(session_id)
        last = session_schema.get_last_interaction(state)
        days = (time.time() - last) / 86400 if last else 0
        purity = self._get_purity(session_id)
        threshold = self._compute_ntr_threshold(purity)
        stage = self._compute_ntr_stage(days, threshold)
        names = {0: "无", 1: "不安(25%)", 2: "难受(50%)", 3: "幽怨(75%)", 4: "好感归零(90%)", 5: "背叛(100%)"}
        await self.send_message(
            chat_id,
            f"当前本地时间: {now.strftime('%H:%M')} (UTC{now.strftime('%z')})\n"
            f"今日触发日期: {session_schema.get_daily_trigger_date(state) or '无'}\n"
            f"今日随机推送点: {', '.join(session_schema.get_daily_trigger_times(state)) or '未生成'}\n"
            f"已完成随机推送点: {', '.join(session_schema.get_daily_triggered_times(state)) or '无'}\n"
            f"今日早安推送: {'已发送' if session_schema.get_last_morning_greet_date(state) == now.strftime('%Y-%m-%d') else '待发送'}\n"
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
            total_chars = sum(len(session_schema.get_saved_characters(s)) for s in self.sessions.values())
            active = sum(1 for s in self.sessions.values() if session_schema.get_last_interaction(s) > time.time() - 86400)
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
