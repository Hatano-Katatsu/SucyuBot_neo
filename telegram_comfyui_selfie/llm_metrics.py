"""LLM 缓存三态统计与不含正文、凭据的请求指纹。"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROMPT_LAYOUT_VERSION = "layered-v2"


def token_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
        if number < 0 or (isinstance(value, float) and number != value):
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def cache_usage(usage: Any, *, prompt_tokens: int | None = None) -> dict[str, Any]:
    """缺失、null 和非法数值为未知；显式零有独立语义，不被其他别名覆盖。"""
    raw = usage if isinstance(usage, dict) else {}
    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    prompt = token_count(raw.get("prompt_tokens") if prompt_tokens is None else prompt_tokens)
    result = {"cached_tokens": 0, "cache_reported": False, "cache_source": "missing", "cache_anomaly": ""}
    candidates = [(raw, k, k) for k in ("prompt_cache_hit_tokens", "prompt_cached_tokens", "cached_tokens")]
    candidates.append((details, "cached_tokens", "prompt_tokens_details.cached_tokens"))
    hits = [(obj[key], source) for obj, key, source in candidates if obj.get(key) is not None]
    if hits:
        value, source = hits[0]
        count = token_count(value)
        result["cache_source"] = source
        if count is None or (prompt is not None and count > prompt):
            result["cache_anomaly"] = "invalid_hit_tokens"
            return result
        result.update(cached_tokens=count, cache_reported=True)
        if any(token_count(v) != count for v, _ in hits[1:]):
            result["cache_anomaly"] = "conflicting_hit_fields"
    for key in ("prompt_cache_miss_tokens", "cache_miss_tokens"):
        if raw.get(key) is None:
            continue
        miss = token_count(raw[key])
        if miss is None or (prompt is not None and miss > prompt):
            result["cache_anomaly"] = "invalid_miss_tokens"
        elif prompt is not None:
            if hits:
                if result["cached_tokens"] + miss != prompt:
                    result["cache_anomaly"] = "conflicting_hit_miss"
            else:
                result.update(cached_tokens=prompt - miss, cache_reported=True, cache_source=f"derived:{key}")
        break
    return result


def usage_rates(row: dict[str, Any]) -> dict[str, Any]:
    """命中率只用已报告样本；覆盖率同时展示，避免把样本外推为全体。"""
    prompt = int(row.get("prompt_tokens") or 0)
    known = int(row.get("cache_reported_prompt_tokens") or 0)
    cached = int(row.get("cached_tokens") or 0)
    return {
        "cache_hit_rate": round(cached / known, 4) if known else None,
        "cache_coverage": round(known / prompt, 4) if prompt else None,
        "reported_cache_share": round(cached / prompt, 4) if prompt else None,
    }


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def endpoint_identity(url: str) -> str:
    """端点身份只保留协议、主机、端口和路径，不持久化 URL 凭据或查询参数。"""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def request_observation(body: dict[str, Any], url: str, headers: dict[str, str]) -> dict[str, Any]:
    """只对实际下发的消息/工具/设置取指纹，不把正文、图片或任意请求头复制到 INFO。"""
    messages = body.get("messages") or []
    route = headers.get("x-opencode-session")
    return {
        "layout_version": PROMPT_LAYOUT_VERSION,
        "endpoint": endpoint_identity(url),
        "settings_hash": fingerprint({k: body[k] for k in ("model", "thinking", "reasoning_effort", "enable_thinking", "max_tokens", "temperature", "top_p", "frequency_penalty", "presence_penalty", "tool_choice") if k in body}),
        "tools_hash": fingerprint(body.get("tools")),
        "prompt_hash": fingerprint(messages),
        "message_count": len(messages),
        "messages": [{"role": m.get("role"), "hash": fingerprint(m), "chars": len(m["content"]) if isinstance(m.get("content"), str) else None} for m in messages[:128]],
        "route_hash": fingerprint(route) if route else "",
        "user_agent": headers.get("User-Agent", ""),
    }
