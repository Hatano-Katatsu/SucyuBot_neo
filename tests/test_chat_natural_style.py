from __future__ import annotations

import unittest

from telegram_comfyui_selfie import session_schema
from telegram_comfyui_selfie.chat_context import CHAT_FOCUS_RULES
from telegram_comfyui_selfie.prompt_layout import CHAT_SYSTEM_STATIC_RULES
from tests.support import ServiceFixtureMixin


class ChatNaturalStyleRulesTestCase(unittest.TestCase):
    def test_static_rules_include_natural_style_directives(self):
        rules = CHAT_SYSTEM_STATIC_RULES
        # 静态规则整体预算见 AGENTS.md（≤700 字）。
        self.assertLessEqual(len(rules), 700)
        # 纯台词为主、示例一不带动作段。
        self.assertIn("像真人发消息一样", rules)
        self.assertIn("示例一：\n「", rules)
        # 反 AI 腔约束：附和说完就停、问事先说核心、高频小动作词表。
        self.assertIn("说完就停", rules)
        self.assertIn("说清核心", rules)
        self.assertIn("顿了顿", rules)

    def test_focus_rules_include_topic_follow_through(self):
        self.assertIn("顺着用户当前的话题往下聊", CHAT_FOCUS_RULES)


class RecentReplyClicheReminderTestCase(ServiceFixtureMixin, unittest.TestCase):
    def test_reminder_injected_into_dynamic_tail_when_terms_repeat(self):
        svc = self.make_service()
        sid = "telegram:9527"
        state = svc._get_session_state(sid)
        session_schema.set_chat_history(state, [
            {"role": "user", "content": "在干嘛"},
            {"role": "assistant", "content": "（她顿了顿，把手机扣在桌上。）\n\n「没干嘛。」"},
            {"role": "user", "content": "哦"},
            {"role": "assistant", "content": "（顿了顿。）\n\n「就发呆。」"},
        ])
        note = svc._recent_reply_cliche_reminder(state)
        self.assertIn("顿了顿", note)
        messages = svc._build_chat_messages(sid, "吃饭了吗")
        dynamic = messages[-2]["content"]
        self.assertEqual(messages[-1], {"role": "user", "content": "吃饭了吗"})
        self.assertIn("语气提醒", dynamic)
        # 退避提醒在对话推进规则之前，推进规则仍然紧贴本轮 user。
        self.assertLess(dynamic.index("语气提醒"), dynamic.index("对话推进规则"))

    def test_no_reminder_without_repetition(self):
        svc = self.make_service()
        sid = "telegram:9528"
        state = svc._get_session_state(sid)
        session_schema.set_chat_history(state, [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "「早啊。」"},
        ])
        self.assertEqual(svc._recent_reply_cliche_reminder(state), "")
        messages = svc._build_chat_messages(sid, "今天干嘛")
        self.assertNotIn("语气提醒", messages[-2]["content"])

    def test_short_context_reset_restarts_counting(self):
        svc = self.make_service()
        sid = "telegram:9529"
        state = svc._get_session_state(sid)
        session_schema.set_chat_history(state, [
            {"role": "assistant", "content": "（顿了顿。）\n\n「嗯。」"},
            {"role": "assistant", "content": "（顿了顿。）\n\n「好。」"},
        ])
        # 切场景后旧回复不计入退避统计。
        session_schema.set_short_context_start(state, 2)
        self.assertEqual(svc._recent_reply_cliche_reminder(state), "")


if __name__ == "__main__":
    unittest.main()
