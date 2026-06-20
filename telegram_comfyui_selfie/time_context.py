from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def rough_time_period(hour: int) -> str:
    if 5 <= hour < 11:
        return "早晨"
    if 11 <= hour < 17:
        return "下午"
    if 17 <= hour < 21:
        return "傍晚"
    return "深夜"


def season_label(now: datetime, lat: float | None = None) -> str:
    month = now.month
    northern = lat is None or lat >= 0
    if northern:
        if month in (3, 4, 5):
            return "春季"
        if month in (6, 7, 8):
            return "夏季"
        if month in (9, 10, 11):
            return "秋季"
        return "冬季"
    if month in (3, 4, 5):
        return "秋季"
    if month in (6, 7, 8):
        return "冬季"
    if month in (9, 10, 11):
        return "春季"
    return "夏季"


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def parse_sun_time(value: Any, now: datetime) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})(?:\s*([AP]M))?$", text, flags=re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    marker = (match.group(3) or "").upper()
    if marker == "PM" and hour != 12:
        hour += 12
    elif marker == "AM" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _fallback_sun_times(now: datetime, season: str) -> tuple[datetime, datetime]:
    if season == "夏季":
        sunrise = now.replace(hour=5, minute=0, second=0, microsecond=0)
        sunset = now.replace(hour=19, minute=0, second=0, microsecond=0)
    elif season == "冬季":
        sunrise = now.replace(hour=7, minute=0, second=0, microsecond=0)
        sunset = now.replace(hour=17, minute=0, second=0, microsecond=0)
    else:
        sunrise = now.replace(hour=6, minute=0, second=0, microsecond=0)
        sunset = now.replace(hour=18, minute=0, second=0, microsecond=0)
    return sunrise, sunset


def build_time_context(now: datetime, weather: Any = None) -> dict[str, Any]:
    weather = weather if isinstance(weather, dict) else {}
    lat = _as_float(weather.get("lat"))
    season = season_label(now, lat)
    sunrise = parse_sun_time(weather.get("sunrise"), now)
    sunset = parse_sun_time(weather.get("sunset"), now)
    source = "weather"
    if sunrise is None or sunset is None:
        sunrise, sunset = _fallback_sun_times(now, season)
        source = "seasonal-fallback"

    minutes_from_sunrise = (now - sunrise).total_seconds() / 60
    minutes_to_sunset = (sunset - now).total_seconds() / 60
    minutes_after_sunset = (now - sunset).total_seconds() / 60

    if -45 <= minutes_from_sunrise < 0:
        period = "早晨"
        light_phase = "黎明"
        light_hint = "接近日出，适合微亮天色、冷蓝晨光，不宜写成强烈朝阳"
    elif 0 <= minutes_from_sunrise <= 75:
        period = "早晨"
        light_phase = "朝阳"
        light_hint = "日出后不久，适合朝阳、低角度金色晨光、清晨空气"
    elif minutes_from_sunrise < -45:
        period = "深夜"
        light_phase = "日出前"
        light_hint = "日出前，室外仍偏暗，避免写成朝阳或明亮清晨"
    elif 0 <= minutes_to_sunset <= 90:
        period = "傍晚"
        light_phase = "黄昏/落日"
        light_hint = "接近日落，适合落日、金色斜光、长阴影和黄昏氛围"
    elif 0 > minutes_to_sunset and minutes_after_sunset <= 60:
        period = "傍晚"
        light_phase = "暮色"
        light_hint = "日落后不久，适合暮色、余晖和城市灯光初亮，不宜写成正午阳光"
    elif minutes_to_sunset < 0:
        period = "深夜" if now.hour >= 23 or now.hour < 5 else "傍晚"
        light_phase = "入夜"
        light_hint = "太阳已落山，室外以夜色和人工光为主，避免朝阳/落日"
    else:
        if now.hour >= 11 and minutes_to_sunset > 90:
            period = "下午"
        else:
            period = rough_time_period(now.hour)
        light_phase = "日间自然光"
        light_hint = "太阳已升起且未接近日落，适合正常日间自然光"

    return {
        "period": period,
        "rough_period": rough_time_period(now.hour),
        "season": season,
        "sunrise": sunrise,
        "sunset": sunset,
        "sun_source": source,
        "light_phase": light_phase,
        "light_hint": light_hint,
    }


def format_time_context(ctx: dict[str, Any]) -> str:
    sunrise = ctx.get("sunrise")
    sunset = ctx.get("sunset")
    sun_text = ""
    if isinstance(sunrise, datetime) and isinstance(sunset, datetime):
        sun_text = f"，日出 {sunrise.strftime('%H:%M')}，日落 {sunset.strftime('%H:%M')}"
    return (
        f"{ctx.get('season', '季节未知')}{sun_text}；"
        f"当前光线: {ctx.get('light_phase', '未知')}，{ctx.get('light_hint', '')}"
    ).strip()


def format_light_guard(ctx: dict[str, Any]) -> str:
    phase = str(ctx.get("light_phase") or "")
    if phase in {"日间自然光", "朝阳", "黎明"}:
        return (
            "自然光硬规则: 当前不是黄昏/落日/暮色。"
            "不要把“晚上见”“稍后”“接下来傍晚”提前画成当前画面；"
            "不得写夕阳、落日、晚霞、黄昏、暮色、路灯刚亮、twilight、sunset、dusk、evening sky、streetlights turning on。"
            "如需表达等待晚上，只写动作、表情、手机消息或约定感，当前环境仍按现在的自然光。"
        )
    if phase == "黄昏/落日":
        return "自然光硬规则: 当前确实接近日落，可以写夕阳、晚霞、长阴影和金色斜光。"
    if phase in {"暮色", "入夜"}:
        return "自然光硬规则: 太阳已经落下，室外以暮色、夜色和人工光为主；不要写朝阳、正午阳光或仍在落日中的太阳。"
    return ""
