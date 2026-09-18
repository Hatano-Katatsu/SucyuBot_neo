"""规划提示词的纯组装函数；只消除已在系统事实中逐字存在的重复内容。"""
from __future__ import annotations

from typing import Any


CHAT_SYSTEM_STATIC_RULES = (
    "工具：需要配图、换装、更新位置或联网查询时调用对应工具（何时调用见各工具说明），"
    "不要在文字里描述工具、函数或内部指令。"
    "\n照片记录：历史里以「照片记录」开头的 system 行是你之前发给用户的照片。"
    "用户提到“刚才那张/照片/图/画面”时据此承接，不要主动复述记录内容。"
    "\n回复格式（默认）：台词放中文直角引号「」，动作、神态、心理、环境描写放全角括号（），两者分段、用空行隔开。"
    "日常闲聊的大多数回复只用台词或一两句短消息，像真人发消息一样；动作神态描写不是必须的，"
    "只在有实质动作（起身、递东西、拍照等）时写一处。"
    "不要每条都写成“动作—台词—动作—台词”的固定结构，短回复优于凑满结构。"
    "示例一：\n「怎么突然这么问？」\n示例二：\n（她把手机递过来。）\n\n「你自己看。」\n"
    "不要使用英文引号、冒号旁白或括号外裸叙述来表示动作状态。"
    "\n语言习惯：台词要像真人口语，避免书面腔、文艺腔和解说腔。"
    "不要用浮夸比喻或感叹式开场（“哇，像打翻了蜂蜜”这类文案腔），也不要固定用“行，”“哈哈，”这类开场白；"
    "附和可以极短，说完就停；不要每条回复结尾都抛问题或留话头（“说一声”“要不要我……”）。"
    "别人问事先一句话说清核心，对方追问再展开，不要写成总分总的小作文。"
    "神态小动作不要机械复用同一批词（顿了顿、半晌、轻笑一声、挑眉、勾起嘴角、眼底闪过之类），更不要每轮都写；"
    "不要用“语气软下来”“声音闷闷的”这类元描述凑情绪，情绪通过台词内容体现。"
)

PHOTO_HISTORY_RULES = (
    "照片记录：历史里以「照片记录」开头的 system 行是你之前发给用户的照片。"
    "用户提到“刚才那张/照片/图/画面”时据此承接，不要主动复述记录内容。"
)


def build_image_planner_messages(
    stable: str, dynamic: str, user: str, *,
    context: list[dict[str, Any]] | None = None,
    persona: str = "", memory: str = "", extra_dynamic: str = "",
) -> list[dict[str, Any]]:
    """固定规则在前，保留历史原顺序；当前请求与真实状态仍由调用者完整提供。"""
    context = [dict(message) for message in (context or [])]
    # 图片规划不调用聊天工具、也不生成括号台词；只保留照片历史解释。
    # 严格匹配共享常量，兼容 persona-first，绝不按关键词删除用户历史或角色自定义规则。
    for message in context:
        content = message.get("content")
        if message.get("role") != "system" or not isinstance(content, str):
            continue
        if content == CHAT_SYSTEM_STATIC_RULES:
            message["content"] = PHOTO_HISTORY_RULES
        elif content.endswith("\n\n" + CHAT_SYSTEM_STATIC_RULES):
            message["content"] = content[:-len(CHAT_SYSTEM_STATIC_RULES)] + PHOTO_HISTORY_RULES

    system_facts = [m["content"] for m in context if m.get("role") == "system" and isinstance(m.get("content"), str)]
    if persona and any(persona in content for content in system_facts):
        dynamic = dynamic.replace(f"\n{persona}\n\n", "\n", 1)
    if memory and any(memory in content for content in system_facts):
        user = user.replace(f"\n\n长期记忆:\n{memory}", "", 1)
    messages = [{"role": "system", "content": stable}, *context]
    if dynamic.strip():
        messages.append({"role": "system", "content": dynamic})
    if extra_dynamic:
        messages.append({"role": "system", "content": extra_dynamic})
    messages.append({"role": "user", "content": user})
    return messages
