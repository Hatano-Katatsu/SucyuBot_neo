from __future__ import annotations

from typing import Any


# 同一个设置字段既兼容旧布尔开关，也可直接携带 OpenAI-compatible reasoning_effort。
THINKING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled", "enable", "开启", "开", "启用"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled", "disable", "关闭", "关", "停用"}


def normalize_thinking_setting(value: Any) -> bool | str | None:
    """规范化 thinking 设置：None、旧布尔值，或 reasoning effort 字符串。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
        raise ValueError("thinking 数值只支持 0 或 1")

    text = str(value).strip().lower()
    if not text:
        return None
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    if text in THINKING_EFFORTS:
        return text
    choices = ", ".join(THINKING_EFFORTS)
    raise ValueError(f"thinking 只支持空值、true/false 或 effort：{choices}")


def normalize_profile_thinking_effort(value: Any) -> str:
    """规范化模型 profile 的默认 reasoning effort；空值表示沿用旧思考开关。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in THINKING_EFFORTS:
        return text
    choices = ", ".join(THINKING_EFFORTS)
    raise ValueError(f"模型默认 effort 只支持空值或：{choices}")


def resolve_thinking_setting(value: Any, *, fallback: bool) -> tuple[bool, str]:
    """把单字段设置拆成运行时开关与可选 effort；none effort 仍需原样下发。"""
    normalized = normalize_thinking_setting(value)
    if normalized is None:
        return bool(fallback), ""
    if isinstance(normalized, bool):
        return normalized, ""
    return normalized != "none", normalized
