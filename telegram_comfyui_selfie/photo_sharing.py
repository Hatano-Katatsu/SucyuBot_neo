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


def recent_photo_entries(state: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    result = []
    for entry in session_schema.get_recent_push_topics(state):
        if not isinstance(entry, dict):
            continue
        try:
            fresh = now - float(entry.get("ts") or 0) <= 7 * 86400
        except (ValueError, TypeError):
            fresh = False
        if fresh:
            result.append(entry)
    return result[-32:]


def photo_repeat_reason(plan: dict[str, Any], state: dict[str, Any]) -> str:
    brief = normalize_photo_brief(plan)
    text = " ".join((str(plan.get("scene") or ""), str(plan.get("caption") or ""), json.dumps(brief, ensure_ascii=False)))
    for control in state.get("photo_topic_controls", []):
        if not isinstance(control, dict) or (control.get("until") and control["until"] <= time.time()):
            continue
        if control.get("topic_key") == brief["topic_key"] or (control.get("needle") and control["needle"] in text):
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
        same_topic = _similar(brief["topic_key"], str(previous.get("topic_key") or old.get("topic") or "")) > .78
        same_delta = _similar(brief["actual_change"], str(previous.get("actual_change") or "")) > .8
        if same_topic and (not brief["actual_change"] or same_delta):
            return "同一件事没有新的结果"
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


def photo_history_context(state: dict[str, Any]) -> str:
    records = []
    for entry in recent_photo_entries(state)[-8:]:
        brief = entry.get("photo_brief")
        records.append(brief if isinstance(brief, dict) else {"topic": compact(entry.get("caption"), 60)})
    controls = [c for c in state.get("photo_topic_controls", []) if not c.get("until") or c["until"] > time.time()]
    return "近期成功分享的取景（仅避重，不是待续写素材）:\n" + json.dumps(records, ensure_ascii=False) + "\n用户话题边界:\n" + json.dumps(controls[-12:], ensure_ascii=False)


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
