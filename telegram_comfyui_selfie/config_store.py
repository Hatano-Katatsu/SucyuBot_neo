from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CONFIG_GROUPS: dict[str, list[str]] = {
    "telegram": [
        "telegram_bot_token", "allowed_chat_ids", "telegram_proxy_enabled", "telegram_proxy_url",
        "photo_caption_wait_seconds",
    ],
    "web": [
        "web_enabled", "web_host", "web_port", "web_public_host", "web_admin_username", "web_admin_password",
    ],
    "storage": [
        "long_memory_db_path", "user_log_enabled", "user_log_dir",
    ],
    "comfyui": [
        "comfyui_url", "comfyui_workflow_file", "image_backend", "animatool_workflow", "width", "height",
        "steps", "cfg", "sampler", "scheduler", "turbo_mode", "turbo_strength",
        "unet_model", "clip_model", "vae_model", "turbo_lora_model",
        "animatool_turbo_steps", "animatool_turbo_cfg", "animatool_filename_prefix",
        "comfyui_local_socket_port",
    ],
    "models": [
        "default_chat_model_profile", "default_fast_model_profile", "default_vision_model_profile", "global_model_profiles",
        "llm_temperature_scene", "llm_temperature_translate", "llm_temperature_classify",
        "chat_llm_temperature", "chat_llm_max_tokens", "chat_llm_top_p", "chat_llm_frequency_penalty", "chat_llm_presence_penalty",
        "image_llm_temperature_scene", "image_llm_temperature_translate", "image_llm_temperature_classify",
    ],
    "memory": [
        "long_memory_enabled", "long_memory_extract_enabled", "long_memory_context_limit",
        "context_window_message_limit", "checkpoint_keep_message_limit",
        "checkpoint_soft_limit_chars", "checkpoint_hard_limit_chars", "checkpoint_source_hard_limit_chars",
        "dream_source_hard_limit_chars", "dream_morning_hour", "dream_idle_hours",
        "short_context_reset_gap_hours",
    ],
    "role_defaults": [
        "positive_prefix", "default_hair", "default_eyes", "negative_prompt",
        "dynamic_appearance", "default_purity", "outfit_keywords", "accessory_keywords",
        "role_name", "bot_name", "bot_self_name",
        "scheduled_persona", "spatial_relationship", "allow_llm_change_appearance",
        "style_pool", "current_style", "selfie_frequency", "daily_selfie_limit",
        "location", "timezone_offset", "character_age_stage", "character_day_anchor",
        "user_gender", "chat_reply_length",
    ],
    "world": [
        "world_runtime_enabled", "world_city_places_enabled", "world_city_places_ttl_days",
        "world_user_place_ttl_hours", "world_character_place_ttl_hours",
        "world_character_place_strong_hours", "world_character_place_stale_rounds",
        "world_location_llm_extract", "world_holiday_dates", "world_workday_dates",
        "amap_api_key", "amap_poi_enabled", "amap_poi_per_type",
        "google_places_api_key", "google_places_enabled", "google_places_language",
        "push_continuity_hours", "image_min_gap_rounds", "scene_stale_minutes",
        "post_chat_push_enabled", "post_chat_push_delay_min_minutes",
        "post_chat_push_delay_max_minutes", "post_chat_push_daily_limit",
        "post_chat_push_cooldown_minutes",
    ],
    "search": [
        "tavily_api_key", "web_search_enabled", "web_search_daily_limit",
        "push_topic_search_daily_limit",
    ],
}


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return ""
    if raw in ("true", "True"):
        return True
    if raw in ("false", "False"):
        return False
    if raw in ("null", "None", "~"):
        return None
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
        try:
            return json.loads(raw)
        except Exception:
            return raw[1:-1]
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    if "\n" in text:
        return "|\n" + "\n".join(f"    {line}" for line in text.splitlines())
    return json.dumps(text, ensure_ascii=False)


def load_simple_yaml(path: str | Path) -> dict[str, Any]:
    """读取本项目生成的 YAML 配置。

    这是刻意很小的解析器：支持任意层级的嵌套字典和 literal block，
    足够覆盖本项目配置，避免为了配置文件格式引入新的运行时依赖。
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()

    def _peek_next_indent(start: int) -> int | None:
        for j in range(start, len(lines)):
            if lines[j].strip() and not lines[j].lstrip().startswith("#"):
                return len(lines[j]) - len(lines[j].lstrip())
        return None

    def _parse_block(start: int, base_indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        i = start
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                i += 1
                continue

            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if indent < base_indent:
                break
            if indent > base_indent:
                # 子行应该已经被父 key 递归消费，这里直接跳过避免死循环
                i += 1
                continue

            key, sep, raw = stripped.partition(":")
            if not sep:
                i += 1
                continue

            key = key.strip()
            raw = raw.strip()
            i += 1

            if raw == "|":
                # literal block：收集缩进大于 base_indent 的连续行
                block: list[str] = []
                content_indent: int | None = None
                while i < len(lines):
                    if not lines[i].strip() or lines[i].lstrip().startswith("#"):
                        i += 1
                        continue
                    child_indent = len(lines[i]) - len(lines[i].lstrip())
                    if child_indent <= base_indent:
                        break
                    if content_indent is None:
                        content_indent = child_indent
                    block.append(lines[i][content_indent:])
                    i += 1
                result[key] = "\n".join(block)
            elif raw == "":
                # 嵌套字典
                child_indent = _peek_next_indent(i)
                if child_indent is not None and child_indent > base_indent:
                    nested, i = _parse_block(i, child_indent)
                    result[key] = nested
                else:
                    result[key] = {}
            else:
                result[key] = _parse_scalar(raw)
        return result, i

    root, _ = _parse_block(0, 0)
    return root


def flatten_config(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                result[child_key] = child_value
        else:
            result[key] = value
    return result


def group_config(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    used: set[str] = set()
    grouped: dict[str, dict[str, Any]] = {}
    for group, keys in CONFIG_GROUPS.items():
        section = {key: values[key] for key in keys if key in values}
        if section:
            grouped[group] = section
            used.update(section)
    misc = {key: value for key, value in values.items() if key not in used}
    if misc:
        grouped["misc"] = misc
    return grouped


def dump_simple_yaml(values: dict[str, Any]) -> str:
    def _render_scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        text = str(value)
        if "\n" in text:
            return "|\n" + "\n".join(f"    {line}" for line in text.splitlines())
        return json.dumps(text, ensure_ascii=False)

    def _render_block(data: dict[str, Any], indent: int) -> list[str]:
        out: list[str] = []
        for key, value in data.items():
            if isinstance(value, dict):
                out.append(f"{' ' * indent}{key}:")
                out.extend(_render_block(value, indent + 2))
            elif isinstance(value, str) and "\n" in value:
                out.append(f"{' ' * indent}{key}: |")
                for line in value.splitlines():
                    out.append(f"{' ' * (indent + 2)}{line}")
            else:
                out.append(f"{' ' * indent}{key}: {_render_scalar(value)}")
        return out

    grouped = group_config(values)
    out: list[str] = [
        "# SucyuBot_neo runtime constants.",
        "# 会在运行中变化的用户设置、角色状态、上下文和记忆存放在 SQLite。",
    ]
    for group, section in grouped.items():
        out.append("")
        out.append(f"{group}:")
        out.extend(_render_block(section, 2))
    out.append("")
    return "\n".join(out)
