"""生活照片的结构化取景、成功曝光避重与自然语言暂缓。"""
from __future__ import annotations

import json
import re
import time
from difflib import SequenceMatcher
from typing import Any

from . import session_schema

SUBJECT_MODES = {"character", "detail", "environment"}
PHOTO_FIELDS = (
    "topic_key", "source_ref", "actual_change", "sharing_motive", "main_subject",
    "activity", "place_key", "framing", "angle", "composition", "capture_source", "source_version",
    "source_event_text", "source_mid_id",
)
PHOTO_RULES = """
生活照片协议：分享此刻有依据、值得拍下的一件事。照片本身是中心，配文顺口一句，不设选择题、任务提示或催回复。
在 JSON 增加 subject_mode=character|detail|environment 和 photo_brief 对象。
character 是角色入镜；detail 是物件/活动细节（默认无人，仅明确动作需要时允许角色一只手局部入镜）；environment 是无人静物或环境。
detail/environment 的 view 固定 scene，表示角色眼前的事物，不是用户 POV；不加入角色脸、全身、手机 UI 或无关人物。角色外貌和衣柜不因未入镜改变。
photo_brief 包含 topic_key(稳定主题，不以换同义词制造新主题)、source_ref(有来源时原样引用)、actual_change(真实新增内容，无则空)、
sharing_motive(share|show_result|greeting|waiting)、main_subject、activity、place_key、framing(close|medium|wide|full|detail)、
angle(eye_level|high|low|overhead|side)、composition(简短构图)、capture_source(front_camera|mirror|character_camera|known_photographer|scene)。
自拍必须物理可拍，不一律半身正脸；人物不在场的生活照不套自拍。有人帮拍必须已有依据，不能凭空编出摄影者。
新鲜感来自可见主体、动作和构图，服从当下地点、时刻、衣柜和正在进行的对话。不可为避重改脸、换衣、瞬移。
不必每次都有新故事，普通生活可分享；没人回复也继续自己的生活，不反复准备东西等用户。
成功分享记录中的状态已经向用户表达过，跨日仍有效；没有相关变化证据不得回退或重演。用户建议只是建议，问题不是已完成结果。
候选和旧事项相同必须沿用 source_ref；没有新证据不能靠改 topic_key 或自报 actual_change 制造进展。
""".strip()


class PhotoContentSkipped(Exception):
    """普通推送本窗口没有新鲜内容；不作为模型/网络失败重试。"""


def compact(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_photo_brief(plan: dict[str, Any]) -> dict[str, str]:
    raw = plan.get("photo_brief")
    raw = raw if isinstance(raw, dict) else {}
    mode = str(plan.get("subject_mode") or raw.get("subject_mode") or "character")
    mode = mode if mode in SUBJECT_MODES else "character"
    result = {key: compact(raw.get(key)) for key in PHOTO_FIELDS}
    result.update(subject_mode=mode, view=compact(plan.get("view"), 20), aspect_ratio=compact(plan.get("aspect_ratio"), 8))
    if result["framing"] not in {"close", "medium", "wide", "full", "detail"}:
        result["framing"] = "detail" if mode == "detail" else ""
    if result["angle"] not in {"eye_level", "high", "low", "overhead", "side"}:
        result["angle"] = ""
    if result["capture_source"] not in {"front_camera", "mirror", "character_camera", "known_photographer", "scene"}:
        result["capture_source"] = ""
    text = " ".join((str(plan.get("caption") or ""), str(plan.get("scene") or "")))
    if re.search(r"等你|等着你|留给你|等你回来|waiting for you|wait(?:ing)? for (?:your|a) reply", text, re.I):
        result["sharing_motive"] = "waiting"
    if not result["topic_key"]:
        result["topic_key"] = compact(plan.get("caption") or plan.get("scene"), 80)
    return result


def _similar(left: str, right: str) -> float:
    left = re.sub(r"[\W_]+", "", left.lower())
    right = re.sub(r"[\W_]+", "", right.lower())
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def same_photo_topic(left: str, right: str) -> bool:
    """补足下划线主题键的同义状态后缀，不把任意单个公共词视作同题。"""
    if _similar(left, right) > .72:
        return True
    a = set(re.findall(r"[a-z]{3,}", left.lower()))
    b = set(re.findall(r"[a-z]{3,}", right.lower()))
    common = a & b
    return len(common) >= 2 and len(common) / max(1, min(len(a), len(b))) >= .75


def photo_scene_summary(photo: dict[str, Any], limit: int | None = None) -> str:
    """直接复用最终场景字段；仅避重材料可截短，聊天保留实际场景描述。"""
    for value in (photo.get("nltag"), photo.get("visual_summary"), photo.get("scene")):
        text = " ".join(str(value or "").split())
        if not text:
            continue
        if limit is not None and len(text) > limit:
            boundary = max(text.rfind(". ", 0, limit), text.rfind("。", 0, limit))
            if boundary < limit // 2:
                boundary = text.rfind(" ", 0, limit)
            text = text[:boundary if boundary >= limit // 2 else limit].rstrip(" ,，。") + "…"
        return text
    return ""


def record_photo_feedback(
    state: dict[str, Any], user_text: str, user_message_id: str,
    *, reply_to_message_id: int | None = None,
) -> dict[str, Any] | None:
    """关联真实输入与成功照片，保存证据；不自动认定建议被采纳或目标完成。"""
    text = compact(user_text, 240)
    if not text or not user_message_id or text.startswith("/"):
        return None
    recent = recent_photo_entries(state)
    if reply_to_message_id is not None:
        candidates = [p for p in reversed(recent) if p.get("message_id") == reply_to_message_id]
    else:
        # 增强输入含引用时由 Telegram 入口精确关联，不能用引用正文误造用户证据。
        if "【引用内容】" in text or text in {"好", "好的", "嗯", "嗯嗯", "晚安", "早安", "收到", "谢谢"}:
            return None
        candidates = []
        for p in reversed(recent):
            if time.time() - float(p.get("ts") or p.get("timestamp") or 0) > 6 * 3600:
                continue
            subject = str((p.get("photo_brief") or {}).get("main_subject") or "").strip()
            if len(subject) >= 2 and subject in text:
                candidates.append(p)
        if not candidates and recent and time.time() - float(recent[-1].get("ts") or recent[-1].get("timestamp") or 0) < 2 * 3600:
            if re.search(r"刚才那张|这张(?:图|照片)?|那张(?:图|照片)|(?:那个|这个)颜色|先别.*(?:这个|这件事)", text):
                candidates = [recent[-1]]
            elif re.fullmatch(r"(?:先|暂时)?别(?:做|画|弄|改|继续)(?:了)?[。！! ]*", text):
                history = session_schema.get_chat_history(state)
                if history and history[-1].get("role") == "assistant" and history[-1].get("content") == recent[-1].get("caption"):
                    candidates = [recent[-1]]
    if not candidates:
        return None
    photo = candidates[0]
    feedback = photo.setdefault("user_feedback", [])
    if any(f.get("user_message_id") == user_message_id for f in feedback):
        return None
    if feedback and feedback[-1].get("text") == text and time.time() - float(feedback[-1].get("ts") or 0) < 120:
        return None
    item = {"user_message_id": user_message_id, "text": text, "ts": time.time(),
            "reply_to_message_id": reply_to_message_id, "photo_message_id": photo.get("message_id")}
    feedback.append(item)
    photo["user_feedback"] = feedback[-3:]
    if re.search(r"(?:先|暂时)?别(?:做|画|弄|改|继续)|不要再|放弃|不做了", text):
        brief = photo.get("photo_brief") or {}
        if brief.get("topic_key") or brief.get("source_ref"):
            controls = state.setdefault("photo_topic_controls", [])
            controls.append({"topic_key": brief.get("topic_key", ""), "source_ref": brief.get("source_ref", ""),
                             "until": time.time() + 86400 if re.search(r"先|暂时|今天", text) else 0, "source": text})
            state["photo_topic_controls"] = controls[-16:]
    return item


def recent_photo_entries(state: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    result = []
    # 旧版本只有 sent_photos_history；合并读取，不能因压缩提示词丢掉旧曝光。
    legacy = [p for p in session_schema.get_sent_photos_history(state) if isinstance(p, dict)
              and p.get("source_kind") in {"scheduled_push", "followup_push", "manual_push"}]
    seen = set()
    for entry in session_schema.get_recent_push_topics(state) + legacy:
        if not isinstance(entry, dict):
            continue
        try:
            timestamp = float(entry.get("ts") or entry.get("timestamp") or 0)
            fresh = 0 <= now - timestamp <= 7 * 86400
        except (ValueError, TypeError):
            fresh = False
        key = ("message", entry["message_id"]) if entry.get("message_id") else (
            "content", int(timestamp) if fresh else 0, entry.get("caption"), entry.get("scene"),
            (entry.get("photo_brief") or {}).get("topic_key") or entry.get("topic"))
        if fresh and key not in seen:
            result.append(entry)
            seen.add(key)
    return sorted(result, key=lambda p: float(p.get("ts") or p.get("timestamp") or 0))[-32:]


def photo_repeat_reason(plan: dict[str, Any], state: dict[str, Any]) -> str:
    brief = normalize_photo_brief(plan)
    text = " ".join((str(plan.get("scene") or ""), str(plan.get("caption") or ""), json.dumps(brief, ensure_ascii=False)))
    for control in state.get("photo_topic_controls", []):
        if not isinstance(control, dict) or (control.get("until") and control["until"] <= time.time()):
            continue
        if (same_photo_topic(str(control.get("topic_key") or ""), brief["topic_key"])
                or (control.get("source_ref") and control["source_ref"] == brief["source_ref"])
                or (control.get("needle") and control["needle"] in text)):
            return "用户已结束或暂缓这个话题"
    recent = recent_photo_entries(state)[-8:]
    for old in recent:
        previous = old.get("photo_brief") or {}
        if not isinstance(previous, dict):
            previous = {}
        if brief["source_ref"] and brief["source_ref"] == previous.get("source_ref") and brief["source_version"] and brief["source_version"] == previous.get("source_version"):
            return "同一生活事件没有新的聊天或生活结果依据"
        if _similar(str(plan.get("caption") or ""), str(old.get("caption") or "")) > .86:
            return "配文与已发内容高度重复"
        if _similar(str(plan.get("scene") or ""), str(old.get("scene") or "")) > .9:
            return "画面与已发内容高度重复"
        same_topic = same_photo_topic(brief["topic_key"], str(previous.get("topic_key") or old.get("topic") or ""))
        evidenced_change = (brief["source_ref"] and brief["source_ref"] == previous.get("source_ref")
                            and brief["source_version"] and previous.get("source_version")
                            and brief["source_version"] != previous.get("source_version"))
        if same_topic and not evidenced_change:
            return "同一件事没有新的结果依据（同义改写不算变化）"
    if brief["sharing_motive"] == "waiting" and any(
        (old.get("photo_brief") or {}).get("sharing_motive") == "waiting" for old in recent
    ):
        return "换了道具仍然重复等待用户"
    for old in recent[-3:]:
        previous = old.get("photo_brief") or {}
        axes = ("subject_mode", "main_subject", "activity", "framing", "angle", "composition")
        comparable = [k for k in axes if brief.get(k) and previous.get(k)]
        if len(comparable) >= 5 and sum(_similar(brief[k], str(previous[k])) > .8 for k in comparable) >= 5:
            return "主体、动作与取景高度重复"
    return ""


def photo_history_context(state: dict[str, Any], *, include_visual: bool = True) -> str:
    records = []
    for entry in recent_photo_entries(state)[-8:]:
        brief = entry.get("photo_brief") or {}
        record = {"topic_key": compact(brief.get("topic_key") or entry.get("caption"), 60),
                  "shown": (photo_scene_summary(entry, limit=180) if include_visual else "") or compact(entry.get("caption"), 60)}
        caption = compact(entry.get("caption"), 60)
        if caption and caption not in record["shown"]:
            record["said"] = caption
        if entry.get("message_id"):
            record["photo_id"] = entry["message_id"]
        if brief.get("source_ref"):
            record["source_ref"] = brief["source_ref"]
        framing = "/".join(str(brief[k]) for k in ("subject_mode", "framing", "angle") if brief.get(k))
        if framing:
            record["frame"] = framing
        if entry.get("user_feedback"):
            record["user_feedback"] = [compact(f.get("text"), 100) for f in entry["user_feedback"][-2:]]
        records.append(record)
    controls = [c for c in state.get("photo_topic_controls", []) if not c.get("until") or c["until"] > time.time()]
    return "近期成功分享（事实连续性/避重；不是待续写素材，用户反馈不等于完成）:\n" + json.dumps(records, ensure_ascii=False, separators=(",", ":")) + "\n用户话题边界:\n" + json.dumps(controls[-12:], ensure_ascii=False, separators=(",", ":"))


def record_topic_control(state: dict[str, Any], user_text: str) -> None:
    """只识别用户明确的结束/暂缓，不把一般短答误判为许可或拒绝。"""
    text = user_text.strip()
    match = re.search(r"(?:别再(?:提|聊|说)|不要再(?:提|聊|说)|先别(?:提|聊|说)|暂时不(?:提|聊|说))\s*([^，。！？\n]{0,24})", text)
    if not match and not re.search(r"换个?话题|这事到此为止|这个不聊了|先不聊这个|今天先不聊", text):
        return
    recent = recent_photo_entries(state)
    latest = (recent[-1].get("photo_brief") or {}) if recent else {}
    needle = re.sub(r"[了吧呀啊]+$", "", match.group(1) if match else "").strip()
    if needle in {"这个", "这件事", "这事", "了"}:
        needle = ""
    topic_key = str(latest.get("topic_key") or "")
    if not needle and not topic_key:
        return
    paused = bool(re.search(r"先别|先不|暂时|今天", text))
    controls = state.setdefault("photo_topic_controls", [])
    controls.append({"topic_key": topic_key, "needle": needle, "until": time.time() + 86400 if paused else 0, "source": text[:120]})
    state["photo_topic_controls"] = controls[-16:]
