from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .http_limits import read_limited_json, read_limited_text, response_limit
from .model_security import PublicOnlyResolver, validate_public_model_base_url
from .model_thinking import resolve_thinking_setting


logger = logging.getLogger(__name__)


def _normalize_openai_api_base(value: Any) -> str:
    """把 OpenAI-compatible Base URL 或完整 chat/completions URL 归一为 API Base。"""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    suffix = "/chat/completions"
    if path.lower().endswith(suffix):
        path = path[:-len(suffix)].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)).rstrip("/")


def _openai_chat_completions_url(value: Any) -> str:
    base = _normalize_openai_api_base(value)
    if not base:
        return ""
    parsed = urlsplit(base)
    path = f"{parsed.path.rstrip('/')}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _openai_models_url(value: Any) -> str:
    """从 Base URL 或完整 chat/completions URL 推导标准 models 目录地址。"""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.path.rstrip("/").lower().endswith("/models"):
        return raw
    base = _normalize_openai_api_base(raw)
    parsed = urlsplit(base)
    path = f"{parsed.path.rstrip('/')}/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


# 思考文本泄漏清洗：只处理显式的思考标签 <thinking>/<reasoning>/<analysis>。
# 不做关键词启发式判断（如 "we need to"），避免误判正常输出。
_THINKING_TAG_RE = re.compile(
    r"^\s*<(?P<tag>thinking|reasoning|analysis)>.*?</(?P=tag)>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)


def _strip_llm_thinking_prefixes(text: str) -> str:
    """反复剥离开头由思考标签包裹的块，直到没有更多思考文本或文本过短。"""
    stripped = str(text or "").strip()
    for _ in range(8):
        if not stripped or len(stripped) < 40:
            break
        new = _THINKING_TAG_RE.sub("", stripped, count=1).strip()
        if len(new) >= len(stripped):
            break
        stripped = new
    return stripped


def _looks_like_llm_thinking(text: str) -> bool:
    """判断 LLM 输出是否包含思考标签包裹的块（可能是整段思考或混入思考）。

    只按显式思考标签判断，不依赖关键词启发式。
    """
    return bool(re.search(
        r"<(?:thinking|reasoning|analysis)>.*?</(?:thinking|reasoning|analysis)>",
        str(text or ""),
        flags=re.DOTALL | re.IGNORECASE,
    ))


# 日志脱敏：vision 请求会把图片编码成 base64 data_url 放进 messages，
# 落盘到 llm_debug.jsonl / 用户 ERROR 日志时会污染日志并放大体积。
# _redact_base64 在序列化前递归扫描，把图片相关内容整体丢弃。
_BASE64_DATA_URL_RE = re.compile(r"data:[^;,]+;base64,[A-Za-z0-9+/=]+")
_BARE_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{256,}={0,2}")

_SIMPLE_LLM_CACHE_ANCHORS: dict[str, str] = {
    "roleplay-image-plan": (
        "Stable prefix for roleplay-image-plan v1.\n"
        "Task: convert roleplay context into one image plan and return strict JSON only.\n"
        "Output contract: scene, view, aspect_ratio, caption, new_appearance_tags, clothing_off, "
        "character_location, user_location, is_intimate, partner_in_frame, device_in_frame.\n"
        "Stable rules: plan exactly one frozen moment, never a collage or sequence; keep stable "
        "appearance out of scene; use new_appearance_tags only for one-shot clothing/accessory/hair "
        "changes; use clothing_off for removed garments/accessories or explicit nudity; preserve hard "
        "spatial/body constraints; obey the user's explicit camera/composition request for free image "
        "commands; avoid phones, camera UI, chat UI, mirrors, and devices unless the requested view "
        "explicitly requires mirror/device visibility; choose only 2:3 or 3:2 aspect ratio.\n"
        "Dynamic persona, weather, world state, memories, continuity, and the current request appear "
        "after this stable prefix and override only where they provide concrete facts."
    ),
    "translate": (
        "Stable prefix for image tag translation v1.\n"
        "Task: translate the provided scene into concise English image-generation tags while preserving "
        "subject ownership, action direction, camera/view constraints, visible weather/light, and safety "
        "guards. Return prompt text only, not explanations."
    ),
    "image-judge": (
        "Stable prefix for image-judge v1. Decide whether the current roleplay moment benefits from one image. "
        "Return strict JSON only with send, intent, mood, and view. Do not invent a new scene."
    ),
}


class LLMRuntimeMixin:
    """模型 profile 解析、OpenAI-compatible 调用及其用量与调试记录。"""

    @staticmethod
    def _llm_profile_model_name(profile: dict[str, Any], thinking: bool) -> tuple[str, str, str]:
        """按 ref/app.py 的 profile 结构解析思考/非思考模型。"""
        if thinking and profile.get("model_think"):
            return profile.get("model_think") or "", profile.get("base_url") or "", profile.get("api_key") or ""
        if not thinking and profile.get("model_no_think"):
            return (
                profile.get("model_no_think") or "",
                profile.get("base_url_no_think") or profile.get("base_url") or "",
                profile.get("api_key_no_think") or profile.get("api_key") or "",
            )
        return profile.get("model") or profile.get("model_no_think") or profile.get("model_think") or "", profile.get("base_url") or "", profile.get("api_key") or ""

    def _user_id_for_session(self, session_id: str = "") -> str:
        return str(session_id or "").removeprefix("telegram:")

    def _global_model_profiles(self) -> dict[str, dict[str, Any]]:
        profiles = self.config.get("global_model_profiles") or {}
        return profiles if isinstance(profiles, dict) else {}

    @staticmethod
    def _extract_model_catalog_ids(payload: Any) -> list[str]:
        """兼容 OpenAI data 数组以及常见 models 数组响应。"""
        if isinstance(payload, dict):
            items = payload.get("data")
            if not isinstance(items, list):
                items = payload.get("models")
        else:
            items = payload
        if not isinstance(items, list):
            return []
        model_ids: set[str] = set()
        for item in items[:5000]:
            if isinstance(item, str):
                model_id = item.strip()
            elif isinstance(item, dict):
                model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
            else:
                model_id = ""
            if model_id and len(model_id) <= 512:
                model_ids.add(model_id)
        return sorted(model_ids, key=str.casefold)

    async def _fetch_global_model_catalog_source(
        self,
        session: aiohttp.ClientSession,
        source: dict[str, Any],
    ) -> list[str]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {source['api_key']}"}
        async with session.get(
            source["models_url"],
            headers=headers,
            allow_redirects=False,
        ) as resp:
            if resp.status != 200:
                detail = await read_limited_text(
                    resp,
                    response_limit(self.config, "error_text"),
                    label="模型目录错误响应",
                )
                raise RuntimeError(f"HTTP {resp.status}: {detail[:300]}")
            payload = await read_limited_json(
                resp,
                response_limit(self.config, "llm_json"),
                label="模型目录响应",
            )
        return self._extract_model_catalog_ids(payload)

    def global_model_catalog(self) -> list[dict[str, Any]]:
        """返回启动期发现的全局模型目录副本，禁止暴露任何密钥。"""
        return [
            {
                **dict(item),
                "source_profile_ids": list(item.get("source_profile_ids") or []),
            }
            for item in (getattr(self, "_global_model_catalog", None) or [])
        ]

    async def load_global_model_catalog_once(self) -> list[dict[str, Any]]:
        """服务启动时从已配置的全局 OpenAI-compatible 端点拉取一次模型目录。"""
        if getattr(self, "_global_model_catalog_loaded", False):
            return self.global_model_catalog()
        self._global_model_catalog_loaded = True
        self._global_model_catalog = []

        sources: dict[tuple[str, str], dict[str, Any]] = {}
        for profile_id, raw_profile in self._global_model_profiles().items():
            profile = raw_profile if isinstance(raw_profile, dict) else {}
            branches = (
                (profile.get("base_url"), profile.get("api_key")),
                (
                    profile.get("base_url_no_think"),
                    profile.get("api_key_no_think") or profile.get("api_key"),
                ),
            )
            for raw_base, api_key in branches:
                api_base = _normalize_openai_api_base(raw_base)
                if not api_base or not api_key:
                    continue
                models_url = _openai_models_url(raw_base)
                source_key = (models_url, str(api_key))
                source = sources.setdefault(source_key, {
                    "models_url": models_url,
                    "api_base": api_base,
                    "api_key": str(api_key),
                    "source_profile_ids": [],
                })
                if str(profile_id) not in source["source_profile_ids"]:
                    source["source_profile_ids"].append(str(profile_id))

        if not sources:
            return []

        source_list = list(sources.values())
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as session:
                results = await asyncio.gather(
                    *(self._fetch_global_model_catalog_source(session, source) for source in source_list),
                    return_exceptions=True,
                )
        except Exception as exc:
            logger.warning("启动期模型目录加载失败：%s", exc)
            return []

        catalog_sources: dict[tuple[str, str], set[str]] = {}
        for source, result in zip(source_list, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                logger.warning(
                    "无法从全局模型端点 %s 拉取模型列表（profiles=%s）：%s",
                    source["models_url"],
                    ",".join(source["source_profile_ids"]),
                    result,
                )
                continue
            for model_id in result:
                key = (model_id, source["api_base"])
                catalog_sources.setdefault(key, set()).update(source["source_profile_ids"])

        self._global_model_catalog = [
            {
                "id": model_id,
                "api_base": api_base,
                "source_profile_id": sorted(profile_ids)[0],
                "source_profile_ids": sorted(profile_ids),
            }
            for (model_id, api_base), profile_ids in sorted(
                catalog_sources.items(),
                key=lambda item: (item[0][0].casefold(), item[0][1]),
            )
        ]
        logger.info("启动期模型目录加载完成：%s 个可用模型", len(self._global_model_catalog))
        return self.global_model_catalog()

    def _resolve_llm_profile_entry_details(
        self,
        purpose: str,
        session_id: str = "",
    ) -> tuple[str, dict[str, Any], bool, str, str]:
        """解析当前会话实际使用的 LLM profile。

        chat 使用 chat_profile_id，image/fast 使用 fast_profile_id，vision 使用 vision_profile_id。
        vision 没有显式配置时保持为空，用于关闭图片理解链路。
        thinking 优先级：用户显式设置 > 全局默认 > profile disable_thinking；设置可为布尔或 effort。
        """
        user_id = self._user_id_for_session(session_id)
        settings = self.app_store.get_user_model_settings(user_id) if user_id else {}
        user_profiles = self.app_store.list_model_profiles(user_id) if user_id else {}
        global_profiles = self._global_model_profiles()
        if purpose == "chat":
            profile_id = settings.get("chat_profile_id") or self.config.get("default_chat_model_profile") or ""
        elif purpose == "vision":
            profile_id = settings.get("vision_profile_id") or self.config.get("default_vision_model_profile") or ""
        else:
            profile_id = settings.get("fast_profile_id") or self.config.get("default_fast_model_profile") or ""
        profile = user_profiles.get(profile_id) or {}
        profile_scope = "user" if profile else ""
        if not profile:
            profile = global_profiles.get(profile_id) or {}
            profile_scope = "global" if profile else ""
        if purpose == "vision" and not profile:
            return str(profile_id or ""), {}, False, "", ""
        if not profile and global_profiles:
            profile_id, profile = next(iter(global_profiles.items()))
            profile_scope = "global"
        thinking, thinking_effort = self._profile_thinking_resolution(purpose, profile, settings)
        return str(profile_id or ""), dict(profile or {}), thinking, thinking_effort, profile_scope

    def _resolve_llm_profile_entry(
        self,
        purpose: str,
        session_id: str = "",
    ) -> tuple[str, dict[str, Any], bool, str]:
        """兼容既有四元组接口；effort 由详细解析接口供请求层使用。"""
        profile_id, profile, thinking, _, profile_scope = self._resolve_llm_profile_entry_details(
            purpose,
            session_id,
        )
        return profile_id, profile, thinking, profile_scope

    def _profile_thinking_resolution(
        self,
        purpose: str,
        profile: dict[str, Any],
        settings: dict[str, Any],
    ) -> tuple[bool, str]:
        """解析 thinking 开关和 reasoning effort，保留旧开/关配置语义。"""
        if purpose == "chat":
            key = "chat_thinking"
            global_key = "chat_thinking_enabled"
        elif purpose == "vision":
            key = "vision_thinking"
            global_key = "vision_thinking_enabled"
        else:
            key = "fast_thinking"
            global_key = "fast_thinking_enabled"

        disable = profile.get("disable_thinking", self._get_llm_value(purpose, "disable_thinking", False))
        if isinstance(disable, str):
            disable = disable.strip().lower() in ("true", "1", "yes", "on")
        fallback = not bool(disable)

        user_value = settings.get(key)
        if user_value is not None:
            return resolve_thinking_setting(user_value, fallback=fallback)
        global_value = self.config.get(global_key)
        if isinstance(global_value, str):
            global_value = global_value.strip()
        if global_value not in (None, ""):
            return resolve_thinking_setting(global_value, fallback=fallback)
        return fallback, ""

    def _profile_thinking(self, purpose: str, profile: dict[str, Any], settings: dict[str, Any]) -> bool:
        """解析当前 purpose 的 thinking 开关。

        优先级（从高到低）：
        1. 用户显式设置（chat_thinking/fast_thinking/vision_thinking 三态）——前端/角色卡页面可改；
        2. 全局配置默认（chat_thinking_enabled/fast_thinking_enabled/vision_thinking_enabled）——配置文件可改；
        3. profile 的 disable_thinking。
        """
        return self._profile_thinking_resolution(purpose, profile, settings)[0]

    def _resolve_llm_profile(self, purpose: str, session_id: str = "") -> tuple[str, dict[str, Any], bool]:
        """兼容旧调用者的三元组 profile 解析接口。"""
        profile_id, profile, thinking, _ = self._resolve_llm_profile_entry(purpose, session_id)
        return profile_id, profile, thinking

    def _resolved_llm_config(self, purpose: str, session_id: str = "", disable_thinking: bool | None = None) -> dict[str, Any]:
        profile_id, profile, thinking, thinking_effort, profile_scope = self._resolve_llm_profile_entry_details(
            purpose,
            session_id,
        )
        if disable_thinking is not None:
            # 调用方显式要求关思考（如 checkpoint/dream 等结构化任务），覆盖用户设置。
            thinking = not bool(disable_thinking)
            thinking_effort = ""
        model, api_base, api_key = self._llm_profile_model_name(profile, thinking)
        if purpose != "vision" and not api_base:
            if profile_scope == "user":
                api_base = "https://api.deepseek.com/v1"
            else:
                api_base = self._get_llm_value(purpose, "api_base", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1"
        if purpose != "vision" and not api_key and profile_scope != "user":
            api_key = self._get_llm_value(purpose, "api_key", "") or ""
        if purpose != "vision" and not model:
            model = self._get_llm_value(purpose, "model", "deepseek-chat") or "deepseek-chat"
        return {
            "profile_id": profile_id,
            "profile_scope": profile_scope,
            "profile": profile,
            "thinking": thinking,
            "thinking_effort": thinking_effort,
            "api_base": _normalize_openai_api_base(api_base),
            "api_key": api_key,
            "model": model,
            "max_tokens": profile.get("max_tokens") or self._get_llm_value(purpose, "max_tokens", "4096") or "4096",
            "timeout": profile.get("timeout") or 120,
            "thinking_control": profile.get("thinking_control", "model_name"),
        }

    def _record_llm_usage_from_response(
        self,
        data: dict[str, Any],
        resolved: dict[str, Any],
        *,
        tag: str = "",
        purpose: str = "",
        session_id: str = "",
    ):
        """从 LLM 返回的 usage 字段提取 token 消耗并写入数据库。"""
        usage = data.get("usage") or {} if isinstance(data, dict) else {}
        if not usage:
            return
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cached_tokens = self._cached_tokens_from_usage(usage, prompt_tokens=prompt_tokens)
        self.app_store.record_llm_usage(
            profile_id=str(resolved.get("profile_id") or ""),
            model=str(resolved.get("model") or ""),
            purpose=str(purpose or ""),
            tag=str(tag or ""),
            session_id=str(session_id or ""),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens or prompt_tokens + completion_tokens,
        )

    @staticmethod
    def _cached_tokens_from_usage(usage: dict[str, Any] | None, *, prompt_tokens: int = 0) -> int:
        """兼容不同 OpenAI-compatible provider 的缓存命中字段。"""
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cached_tokens = int(
            usage.get("prompt_cache_hit_tokens")
            or usage.get("prompt_cached_tokens")
            or usage.get("cached_tokens")
            or details.get("cached_tokens")
            or 0
        )
        miss_tokens = int(usage.get("prompt_cache_miss_tokens") or usage.get("cache_miss_tokens") or 0)
        if not cached_tokens and miss_tokens and prompt_tokens:
            cached_tokens = max(0, int(prompt_tokens or 0) - miss_tokens)
        return max(0, cached_tokens)

    @staticmethod
    def _redact_base64(value: Any) -> Any:
        """递归遍历可序列化结构，把图片相关内容整体丢弃，避免 base64 字节流进入日志。

        处理策略（按优先级）：
        1. OpenAI 多模态消息中的 image_url 元素
           ``{"type": "image_url", "image_url": {...}}`` → 整体替换为 "<image omitted>"；
        2. data URL（``data:<mime>;base64,...``）→ 替换为 "<image omitted>"；
        3. 裸 base64 串（>=256 字符，排除短 hex/hash）→ 替换为 "<base64 omitted>"。
        """
        if isinstance(value, dict):
            if value.get("type") == "image_url" and isinstance(value.get("image_url"), (dict, str)):
                return "<image omitted>"
            return {k: LLMRuntimeMixin._redact_base64(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [LLMRuntimeMixin._redact_base64(v) for v in value]
        if isinstance(value, str):
            redacted = _BASE64_DATA_URL_RE.sub("<image omitted>", value)
            redacted = _BARE_BASE64_RE.sub("<base64 omitted>", redacted)
            return redacted
        return value

    @staticmethod
    def _json_safe(value: Any) -> Any:
        """把调试数据压成可 JSON 序列化结构，避免日志写入影响主请求。

        会递归脱敏 base64 内容（data URL 与裸 base64 串），防止图片字节流进入日志。
        """
        try:
            cleaned = LLMRuntimeMixin._redact_base64(value)
            return json.loads(json.dumps(cleaned, ensure_ascii=False, default=str))
        except Exception:
            return str(value)

    @staticmethod
    def _llm_usage_debug_summary(data: dict[str, Any] | None) -> dict[str, Any]:
        usage = (data or {}).get("usage") if isinstance(data, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cached_tokens = LLMRuntimeMixin._cached_tokens_from_usage(usage, prompt_tokens=prompt_tokens)
        miss_tokens = int(usage.get("prompt_cache_miss_tokens") or usage.get("cache_miss_tokens") or 0)
        return {
            "raw": dict(usage),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "cache_miss_tokens": miss_tokens,
            "cache_hit_rate": round(cached_tokens / prompt_tokens, 4) if prompt_tokens else 0,
        }

    @staticmethod
    def _llm_finish_reason(data: dict[str, Any] | None) -> str:
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice = choices[0] if isinstance(choices[0], dict) else {}
        return str(choice.get("finish_reason") or "")

    def _llm_debug_log_path(self) -> Path:
        return self._user_log_dir() / "llm_debug.jsonl"

    def _llm_debug_legacy_log_path(self) -> Path:
        return self._user_log_dir() / "llm_debug.json"

    def _migrate_legacy_llm_debug_log(self) -> None:
        """把旧版分组 JSON 日志一次性迁为按行 JSON，并保留原文件备份。"""
        path = self._llm_debug_log_path()
        legacy = self._llm_debug_legacy_log_path()
        if path.exists() or not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            grouped = data.get("entries_by_type") if isinstance(data, dict) else {}
            entries = []
            if isinstance(grouped, dict):
                for values in grouped.values():
                    if isinstance(values, list):
                        entries.extend(item for item in values if isinstance(item, dict))
            entries.sort(key=lambda item: (float(item.get("ts") or 0), str(item.get("time") or "")))
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                for entry in entries:
                    handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            temp.replace(path)

            backup = legacy.with_name("llm_debug.legacy.json")
            index = 1
            while backup.exists():
                backup = legacy.with_name(f"llm_debug.legacy.{index}.json")
                index += 1
            legacy.replace(backup)
        except Exception as exc:
            logger.debug("migrate legacy llm debug log failed: %s", exc)

    def _flush_llm_debug(self, *, force: bool = False) -> None:
        pending = getattr(self, "_llm_debug_buffer", [])
        if not pending:
            return
        threshold = int(getattr(self, "_llm_debug_flush_threshold", 10) or 10)
        if not force and len(pending) < threshold:
            return
        batch = list(pending)
        try:
            path = self._llm_debug_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._migrate_legacy_llm_debug_log()
            self._rotate_log_file_if_needed(path)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                for entry in batch:
                    handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            del pending[:len(batch)]
        except Exception as exc:
            logger.debug("flush llm debug failed: %s", exc)

    def _record_llm_debug(
        self,
        *,
        purpose: str,
        tag: str,
        session_id: str,
        resolved: dict[str, Any],
        request_url: str,
        request_body: dict[str, Any],
        response: Any,
        status: int | None = None,
        error: str = "",
    ) -> None:
        """按 JSONL 追加完整 LLM 请求/返回，读取端按游标分页。"""
        key = f"{purpose or 'unknown'}:{tag or 'untagged'}"
        now = time.time()
        usage_summary = self._llm_usage_debug_summary(response if isinstance(response, dict) else None)
        entry = {
            "ts": now,
            "time": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "type": key,
            "purpose": purpose or "",
            "tag": tag or "",
            "session_id": session_id or "",
            "profile_id": str(resolved.get("profile_id") or ""),
            "model": str(resolved.get("model") or ""),
            "thinking": bool(resolved.get("thinking")),
            "status": status,
            "finish_reason": self._llm_finish_reason(response if isinstance(response, dict) else None),
            "completion_tokens": usage_summary.get("completion_tokens", 0),
            "max_tokens": (request_body or {}).get("max_tokens"),
            "request": {
                "url": request_url,
                "body": self._json_safe(request_body),
            },
            "response": self._json_safe(response),
            "usage": usage_summary,
        }
        if error:
            entry["error"] = error
        self._llm_debug_buffer.append(entry)
        self._flush_llm_debug(force=False)

    def _record_llm_error_log(
        self,
        *,
        session_id: str,
        purpose: str,
        tag: str,
        request_url: str = "",
        request_body: dict[str, Any] | None = None,
        response: Any = None,
        status: int | None = None,
        error: str = "",
    ) -> None:
        """把失败时的完整 LLM 请求/返回写入用户 ERROR 日志，避免只看到兜底文案。"""
        if not session_id:
            return
        response_data = response if isinstance(response, dict) else None
        usage_summary = self._llm_usage_debug_summary(response_data)
        payload = {
            "purpose": purpose or "",
            "tag": tag or "",
            "status": status,
            "error": error or "",
            "finish_reason": self._llm_finish_reason(response_data),
            "completion_tokens": usage_summary.get("completion_tokens", 0),
            "request": {
                "url": request_url or "",
                "body": self._json_safe(request_body or {}),
            },
            "response": self._json_safe(response),
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            text = str(payload)
        self._ulog(session_id, "ERROR", f"LLM_FULL_LOG {text}")

    def _get_llm_value(self, purpose: str, name: str, default=None):
        prefix = "chat_llm" if purpose == "chat" else "image_llm"
        value = self.config.get(f"{prefix}_{name}")
        if value not in ("", None):
            return value
        legacy_map = {
            "api_base": "llm_api_base",
            "api_key": "llm_api_key",
            "model": "llm_model",
            "max_tokens": "llm_max_tokens",
            "disable_thinking": "llm_disable_thinking",
            "temperature": "llm_temperature_scene",
            "temperature_scene": "llm_temperature_scene",
            "temperature_translate": "llm_temperature_translate",
            "temperature_classify": "llm_temperature_classify",
        }
        legacy_key = legacy_map.get(name)
        if legacy_key:
            legacy_value = self.config.get(legacy_key)
            if legacy_value not in ("", None):
                return legacy_value
        return default

    def _llm_sampling_params_enabled(self) -> bool:
        """是否下发温度、top_p 与重复/存在惩罚等可选采样参数。"""
        value = self.config.get("llm_sampling_params_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on", "开启", "启用")
        return bool(value)

    def has_llm_config(self, purpose: str, session_id: str = "") -> bool:
        resolved = self._resolved_llm_config(purpose, session_id)
        if purpose == "vision":
            return bool(resolved.get("api_key") and resolved.get("api_base") and resolved.get("model"))
        return bool(resolved.get("api_key"))

    @staticmethod
    def _resolved_models_match(first: dict[str, Any], second: dict[str, Any]) -> bool:
        if not (
            first.get("api_key")
            and first.get("api_base")
            and first.get("model")
            and second.get("api_key")
            and second.get("api_base")
            and second.get("model")
        ):
            return False
        return (
            str(first.get("api_base") or "").strip(),
            str(first.get("model") or "").strip(),
        ) == (
            str(second.get("api_base") or "").strip(),
            str(second.get("model") or "").strip(),
        )

    def _chat_uses_native_multimodal(self, session_id: str = "") -> bool:
        """chat 与 vision 指向同一实际模型时，让 chat 直接读取用户图片。"""
        chat = self._resolved_llm_config("chat", session_id)
        vision = self._resolved_llm_config("vision", session_id)
        return self._resolved_models_match(chat, vision)

    @staticmethod
    def _strip_image_content_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """移除多模态图片 part，供不支持图片的辅助模型安全复用消息。"""
        cleaned_messages: list[dict[str, Any]] = []
        for message in messages:
            cleaned = dict(message)
            content = cleaned.get("content")
            if isinstance(content, list):
                kept_parts: list[Any] = []
                removed = False
                for part in content:
                    is_image = isinstance(part, dict) and (
                        str(part.get("type") or "").strip().lower() in {"image", "image_url", "input_image"}
                        or "image_url" in part
                    )
                    if is_image:
                        removed = True
                        continue
                    kept_parts.append(part)
                if removed and not kept_parts:
                    kept_parts.append({"type": "text", "text": "[图片内容已从辅助模型上下文移除]"})
                cleaned["content"] = kept_parts
            elif isinstance(content, dict):
                is_image = (
                    str(content.get("type") or "").strip().lower() in {"image", "image_url", "input_image"}
                    or "image_url" in content
                )
                if is_image:
                    cleaned["content"] = "[图片内容已从辅助模型上下文移除]"
            cleaned_messages.append(cleaned)
        return cleaned_messages

    async def _call_llm_messages(
        self,
        messages: list[dict[str, Any]],
        tools=None,
        tool_choice=None,
        tag: str = "",
        temp: float | None = None,
        purpose: str = "image",
        disable_thinking: bool | None = None,
        session_id: str = "",
        sampling: bool = False,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolved_llm_config(purpose, session_id, disable_thinking=disable_thinking)
        api_base = resolved["api_base"]
        api_key = resolved["api_key"]
        if not api_key:
            label = "chat model" if purpose == "chat" else ("vision model" if purpose == "vision" else "fast model")
            raise RuntimeError(f"{label} API Key is not configured")
        max_tokens_value = max_tokens if max_tokens is not None else (resolved.get("max_tokens") or "4096")
        try:
            max_tokens_int = max(1, int(max_tokens_value))
        except (TypeError, ValueError):
            max_tokens_int = 4096
        body = {
            "model": resolved["model"],
            "max_tokens": max_tokens_int,
        }
        sampling_params_enabled = self._llm_sampling_params_enabled()
        if sampling_params_enabled:
            body["temperature"] = (
                float(self._get_llm_value(purpose, "temperature", "0.95"))
                if temp is None
                else temp
            )
        # 采样参数（top_p / 重复惩罚）：仅真实聊天回复链路显式开启。
        # 聊天默认带 top_p（核采样砍掉低概率胡话尾巴）+ frequency_penalty（抗车轱辘复读），
        # 摆脱「温度调高说胡话 / 调低复读」的两难；checkpoint/dream/memory 等结构化低温任务不带。
        if sampling_params_enabled and sampling:
            for _sample_key in ("top_p", "frequency_penalty", "presence_penalty"):
                _sample_raw = self._get_llm_value(purpose, _sample_key, "")
                if _sample_raw in ("", None):
                    continue
                try:
                    body[_sample_key] = float(_sample_raw)
                except (TypeError, ValueError):
                    logger.warning("忽略非法采样参数 %s=%r", _sample_key, _sample_raw)
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        request_messages = messages
        if purpose not in {"chat", "vision"}:
            vision_resolved = self._resolved_llm_config("vision", session_id)
            if not self._resolved_models_match(resolved, vision_resolved):
                request_messages = self._strip_image_content_from_messages(messages)
        body["messages"] = request_messages
        thinking = bool(resolved.get("thinking"))
        thinking_effort = str(resolved.get("thinking_effort") or "").strip().lower()
        control = str(resolved.get("thinking_control") or "model_name")
        if thinking_effort:
            # effort 与旧开关共用设置字段；选中 effort 时使用标准 OpenAI-compatible 参数。
            body["reasoning_effort"] = thinking_effort
        elif control == "param_always":
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        elif control == "param":
            # 双向显式控制：用户/配置显式开启或关闭思考都要落实到请求体，
            # 否则 param 模式在 thinking=True 时不发参数，模型默认行为可能与设置不一致。
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        elif control == "enable_thinking":
            body["enable_thinking"] = bool(thinking)
        request_url = _openai_chat_completions_url(api_base)
        private_profile = resolved.get("profile_scope") == "user"
        if private_profile:
            try:
                validate_public_model_base_url(api_base)
            except ValueError as exc:
                raise RuntimeError(f"用户模型 Base URL 不安全: {exc}") from exc
        last_error = None
        for attempt in range(2):
            connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver()) if private_profile else None
            async with aiohttp.ClientSession(
                trust_env=not private_profile,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=float(resolved.get("timeout") or 120)),
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as s:
                async with s.post(
                    request_url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept-Encoding": "gzip, deflate"},
                    json=body,
                    allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        text = await read_limited_text(
                            resp,
                            response_limit(self.config, "error_text"),
                            label="LLM 错误响应",
                        )
                        self._record_llm_debug(
                            purpose=purpose,
                            tag=tag,
                            session_id=session_id,
                            resolved=resolved,
                            request_url=request_url,
                            request_body=body,
                            response={"status": resp.status, "text": text},
                            status=resp.status,
                            error=f"LLM request failed: {resp.status}",
                        )
                        self._record_llm_error_log(
                            session_id=session_id,
                            purpose=purpose,
                            tag=tag,
                            request_url=request_url,
                            request_body=body,
                            response={"status": resp.status, "text": text},
                            status=resp.status,
                            error=f"LLM request failed: {resp.status}",
                        )
                        last_error = RuntimeError(f"LLM request failed: {resp.status} {text}")
                        if resp.status == 500 and attempt == 0:
                            logger.warning("LLM request failed with 500, retrying in 1 second...")
                            await asyncio.sleep(1)
                            continue
                        raise last_error
                    data = await read_limited_json(
                        resp,
                        response_limit(self.config, "llm_json"),
                        label="LLM JSON 响应",
                    )
                    break
        else:
            raise last_error
        # 记录 token 消耗（不阻塞主链路，解析失败仅记录日志）。
        try:
            self._record_llm_usage_from_response(data, resolved, tag=tag, purpose=purpose, session_id=session_id)
        except Exception as exc:
            logger.debug("record llm usage failed: %s", exc)
        self._record_llm_debug(
            purpose=purpose,
            tag=tag,
            session_id=session_id,
            resolved=resolved,
            request_url=request_url,
            request_body=body,
            response=data,
            status=200,
        )
        return data

    async def _call_llm(self, system: str, user: str, temp: float = 0.3, tag: str = "", purpose: str = "image", disable_thinking: bool | None = None, session_id: str = "", max_tokens: int | None = None) -> str:
        anchor = _SIMPLE_LLM_CACHE_ANCHORS.get(tag or "")
        messages = []
        if anchor:
            messages.append({"role": "system", "content": anchor})
        messages.extend([{"role": "system", "content": system}, {"role": "user", "content": user}])
        data = await self._call_llm_messages(messages, tag=tag, temp=temp, purpose=purpose, disable_thinking=disable_thinking, session_id=session_id, max_tokens=max_tokens)
        msg = data.get("choices", [{}])[0].get("message", {})
        text = (msg.get("content") or "").strip()
        text = _strip_llm_thinking_prefixes(text)
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
        if not text:
            finish_reason = data.get("choices", [{}])[0].get("finish_reason") or ""
            reasoning_len = len(str(msg.get("reasoning_content") or ""))
            self._record_llm_error_log(
                session_id=session_id,
                purpose=purpose,
                tag=tag,
                request_body={"messages": messages},
                response=data,
                status=200,
                error=f"LLM returned empty content (finish_reason={finish_reason}, reasoning_len={reasoning_len})",
            )
            if reasoning_len:
                # 思考内容不能当作结果返回：思考模型在 max_tokens 内只输出了 reasoning（通常 finish_reason=length），
                # 静默兜底会把思考文本当成正式结果流入下游（如 translate → AnimaFlow tags）。
                raise RuntimeError("LLM 返回空内容（模型仅输出思考、无正式回答）")
            raise RuntimeError("LLM 返回空内容")
        return text
