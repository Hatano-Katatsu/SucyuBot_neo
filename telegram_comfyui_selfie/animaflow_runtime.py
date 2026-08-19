from __future__ import annotations

import asyncio
import copy
import logging
import math
import re
import time
from typing import Any

import aiohttp

from .http_limits import read_limited_json, response_limit


logger = logging.getLogger(__name__)

ANIMAFLOW_CATALOG_PATH = "/anima/workflows"
ANIMAFLOW_SCHEMA_PATH = "/anima/schema"
ANIMAFLOW_KNOWLEDGE_PATH = "/anima/knowledge"
ANIMAFLOW_GENERATE_PATH = "/anima/generate"
PREFERRED_ANIMAFLOW_WORKFLOW = "anima29_turbo"
LEGACY_TURBO_V1_WORKFLOW = "turbo_v1"
ANIMAFLOW_NEGATIVE_FIELDS = ("neg", "negative", "negative_prompt")
ANIMAFLOW_NLTAG_FIELDS = ("nltag", "nl_tag", "nl_tags", "tags")

# `/anima/workflows` 不可用时只保留改造前的 turbo_v1 兼容入口。这里是 Bot 的
# HTTP 协议兜底，不包含或导入任何 ComfyUI 插件实现。
LEGACY_TURBO_V1_META: dict[str, Any] = {
    "name": LEGACY_TURBO_V1_WORKFLOW,
    "description": "兼容回退：使用原 turbo_v1 固定接口",
    "deprecated": False,
    "legacy_fallback": True,
    "defaults": {"cfg": 1.0, "steps": 12},
    "endpoints": {
        "generate": "/anima/generate_turbo_v1",
        "schema": "/anima/schema_turbo_v1",
        "knowledge": "/anima/knowledge_new_models",
    },
}
LEGACY_TURBO_V1_SCHEMA: dict[str, Any] = {
    "name": "generate_anima_turbo_v1",
    "description": "Anima Turbo v1.0 兼容请求格式。",
    "parameters": {
        "type": "object",
        "properties": {
            "quality_meta_year_safe": {
                "type": "string",
                "description": "masterpiece, best quality 与 safe/sensitive/nsfw/explicit 安全等级。",
                "default": "masterpiece, best quality, safe",
            },
            "count": {"type": "string", "default": "1girl"},
            "character": {"type": "string", "default": ""},
            "series": {"type": "string", "default": ""},
            "appearance": {"type": "string", "default": ""},
            "artist": {"type": "string", "default": ""},
            "tags": {"type": "string", "description": "英文自然语言场景描述。"},
            "neg": {"type": "string", "default": ""},
            "cfg": {"type": "number", "default": 1.0, "minimum": 0.7, "maximum": 1.0},
            "steps": {"type": "integer", "default": 10, "minimum": 8, "maximum": 12},
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "3:2", "1:1", "2:3", "9:16"],
                "default": "1:1",
            },
            "width": {"type": "integer", "minimum": 64},
            "height": {"type": "integer", "minimum": 64},
            "batch_size": {"type": "integer", "default": 1, "minimum": 1},
            "seed": {"type": "integer", "minimum": 0},
            "positive": {"type": "string", "default": ""},
            "filename_prefix": {"type": "string", "default": "AnimaFlow_turbo_v1_"},
        },
        "required": ["quality_meta_year_safe", "count", "tags"],
        "additionalProperties": False,
    },
}

_CATALOG_TTL = 300.0
_RESOURCE_TTL = 300.0
_catalog_cache: dict[str, tuple[dict[str, Any], float]] = {}
_resource_cache: dict[str, tuple[dict[str, Any], float]] = {}


class AnimaFlowError(RuntimeError):
    """AnimaFlow 发现或资源接口不可用。"""


def animaflow_enabled(config: dict[str, Any]) -> bool:
    """读取新开关，并兼容旧版 image_backend=animatool/animaflow。"""
    if "animaflow_enabled" in config:
        return bool(config.get("animaflow_enabled"))
    backend = str(config.get("image_backend", "") or "").strip().lower()
    return backend in {"animaflow", "animatool"}


def configured_animaflow_workflow(config: dict[str, Any]) -> str:
    """读取工作流配置；旧键只用于无损迁移。"""
    return str(
        config.get("animaflow_workflow")
        or config.get("animatool_workflow")
        or PREFERRED_ANIMAFLOW_WORKFLOW
    ).strip()


def _base_url(service: Any) -> str:
    value = getattr(service, "comfyui_url", "")
    if callable(value):
        value = value()
    if not value:
        value = service.config.get("comfyui_url", "http://127.0.0.1:8188")
    return str(value).rstrip("/")


def _ensure_comfy_session(service: Any) -> None:
    # 延迟导入避免 generation -> animaflow_runtime 的模块级循环。
    from .generation import ensure_comfy_session

    ensure_comfy_session(service)


async def _get_json(service: Any, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
    _ensure_comfy_session(service)
    url = f"{_base_url(service)}{path}"
    try:
        async with service.comfy_session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await read_limited_json(
                resp,
                response_limit(service.config, "comfy_json"),
                label=f"AnimaFlow {path} 响应",
            )
            if resp.status != 200:
                raise AnimaFlowError(f"AnimaFlow {path} HTTP {resp.status}: {data}")
    except AnimaFlowError:
        raise
    except Exception as exc:
        raise AnimaFlowError(f"无法访问 AnimaFlow {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnimaFlowError(f"AnimaFlow {path} 返回的不是 JSON 对象")
    return data


def _endpoint_path(value: Any, fallback: str) -> str:
    """从目录中的人类可读 endpoint 文本提取受限的 /anima/* 路径。"""
    match = re.search(r"(/anima/[A-Za-z0-9_.\-/]+)", str(value or ""))
    path = match.group(1) if match else fallback
    if not path.startswith("/anima/") or ".." in path:
        return fallback
    return path


def normalize_animaflow_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化 `/anima/workflows` 响应。"""
    raw_workflows = payload.get("workflows")
    if not isinstance(raw_workflows, dict) or not raw_workflows:
        raise AnimaFlowError("/anima/workflows 未返回可用工作流")
    workflows: dict[str, dict[str, Any]] = {}
    for raw_name, raw_info in raw_workflows.items():
        name = str(raw_name or "").strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            continue
        info = raw_info if isinstance(raw_info, dict) else {}
        endpoints = info.get("endpoints") if isinstance(info.get("endpoints"), dict) else {}
        workflows[name] = {
            "name": name,
            "description": str(info.get("description") or "").strip(),
            "deprecated": bool(info.get("deprecated", False)),
            "defaults": dict(info.get("defaults")) if isinstance(info.get("defaults"), dict) else {},
            "endpoints": {
                "generate": _endpoint_path(endpoints.get("generate"), ANIMAFLOW_GENERATE_PATH),
                "schema": _endpoint_path(endpoints.get("schema"), ANIMAFLOW_SCHEMA_PATH),
                "knowledge": _endpoint_path(endpoints.get("knowledge"), ANIMAFLOW_KNOWLEDGE_PATH),
            },
        }
        for key in ("cfg", "steps", "default_cfg", "default_steps"):
            if key in info:
                workflows[name][key] = info[key]
    if not workflows:
        raise AnimaFlowError("/anima/workflows 中没有合法工作流")
    default = str(payload.get("default") or "").strip()
    return {"default": default if default in workflows else "", "workflows": workflows}


def legacy_turbo_v1_catalog(reason: str = "") -> dict[str, Any]:
    """构造改造前 turbo_v1 固定接口的单工作流兼容目录。"""
    return {
        "default": LEGACY_TURBO_V1_WORKFLOW,
        "workflows": {LEGACY_TURBO_V1_WORKFLOW: copy.deepcopy(LEGACY_TURBO_V1_META)},
        "legacy_fallback": True,
        "fallback_reason": str(reason or "AnimaFlow 工作流发现不可用"),
    }


async def fetch_animaflow_catalog(
    service: Any,
    *,
    force: bool = False,
    ttl: float = _CATALOG_TTL,
) -> dict[str, Any]:
    """从 ComfyUI 发现 AnimaFlow 工作流并短时缓存。"""
    url = _base_url(service)
    now = time.monotonic()
    cached = _catalog_cache.get(url)
    if not force and cached and now - cached[1] < ttl:
        return cached[0]
    try:
        catalog = normalize_animaflow_catalog(await _get_json(service, ANIMAFLOW_CATALOG_PATH))
    except AnimaFlowError as exc:
        logger.warning("AnimaFlow 工作流发现失败，回退 turbo_v1: %s", exc)
        catalog = legacy_turbo_v1_catalog(str(exc))
    _catalog_cache[url] = (catalog, now)
    if force:
        prefix = f"{url}|"
        for key in tuple(_resource_cache):
            if key.startswith(prefix):
                _resource_cache.pop(key, None)
    return catalog


def select_animaflow_workflow(catalog: dict[str, Any], requested: str = "") -> str:
    """选择非弃用工作流：显式选择 > anima29_turbo > 任意 turbo > 服务端默认。"""
    workflows = catalog.get("workflows") if isinstance(catalog, dict) else {}
    if not isinstance(workflows, dict) or not workflows:
        raise AnimaFlowError("没有可选择的 AnimaFlow 工作流")
    active = [name for name, info in workflows.items() if not bool((info or {}).get("deprecated"))]
    candidates = active or list(workflows)
    requested = str(requested or "").strip()
    if requested in candidates:
        return requested
    if PREFERRED_ANIMAFLOW_WORKFLOW in candidates:
        return PREFERRED_ANIMAFLOW_WORKFLOW
    turbo = sorted(name for name in candidates if "turbo" in name.lower())
    if turbo:
        return turbo[0]
    default = str(catalog.get("default") or "")
    if default in candidates:
        return default
    return sorted(candidates)[0]


async def resolve_animaflow_workflow(
    service: Any,
    workflow: str = "",
    *,
    force: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """发现并解析当前工作流，返回 (名称, 元信息, 目录)。"""
    catalog = await fetch_animaflow_catalog(service, force=force)
    requested = workflow or configured_animaflow_workflow(service.config)
    selected = select_animaflow_workflow(catalog, requested)
    meta = catalog["workflows"][selected]
    service._animaflow_resolved_workflow = selected
    service._animaflow_catalog = catalog
    service._animaflow_fallback_reason = str(catalog.get("fallback_reason") or "")
    return selected, meta, catalog


def resolve_legacy_turbo_v1_fallback(
    service: Any,
    reason: str = "",
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """立即切换到旧 turbo_v1 HTTP 兼容协议。"""
    catalog = legacy_turbo_v1_catalog(reason)
    meta = catalog["workflows"][LEGACY_TURBO_V1_WORKFLOW]
    service._animaflow_resolved_workflow = LEGACY_TURBO_V1_WORKFLOW
    service._animaflow_catalog = catalog
    service._animaflow_fallback_reason = str(catalog.get("fallback_reason") or "")
    return LEGACY_TURBO_V1_WORKFLOW, meta, catalog


async def _fetch_resource(
    service: Any,
    workflow: str,
    kind: str,
    *,
    meta: dict[str, Any] | None = None,
    force: bool = False,
    ttl: float = _RESOURCE_TTL,
) -> dict[str, Any]:
    if meta is None:
        workflow, meta, _ = await resolve_animaflow_workflow(service, workflow, force=force)
    path = str((meta.get("endpoints") or {}).get(kind) or "")
    fallback = ANIMAFLOW_SCHEMA_PATH if kind == "schema" else ANIMAFLOW_KNOWLEDGE_PATH
    path = _endpoint_path(path, fallback)
    cache_key = f"{_base_url(service)}|{workflow}|{kind}|{path}"
    now = time.monotonic()
    cached = _resource_cache.get(cache_key)
    if not force and cached and now - cached[1] < ttl:
        return cached[0]
    try:
        data = await _get_json(service, path, params={"workflow": workflow})
    except AnimaFlowError as exc:
        if not bool(meta.get("legacy_fallback")):
            raise
        logger.warning("turbo_v1 兼容 %s 接口不可用，使用内置协议兜底: %s", kind, exc)
        data = copy.deepcopy(LEGACY_TURBO_V1_SCHEMA) if kind == "schema" else {}
    _resource_cache[cache_key] = (data, now)
    return data


async def load_animaflow_workflow_resources(
    service: Any,
    workflow: str = "",
    *,
    force: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """发现并加载资源；任一发现资源失败时整体回退旧 turbo_v1 协议。"""
    selected, meta, catalog = await resolve_animaflow_workflow(service, workflow, force=force)
    try:
        schema, knowledge = await asyncio.gather(
            fetch_animaflow_schema(service, selected, meta=meta, force=force),
            fetch_animaflow_knowledge(service, selected, meta=meta, force=force),
        )
    except AnimaFlowError as exc:
        if bool(meta.get("legacy_fallback")):
            raise
        logger.warning("AnimaFlow %s 资源检测失败，回退 turbo_v1: %s", selected, exc)
        selected, meta, catalog = resolve_legacy_turbo_v1_fallback(service, str(exc))
        schema, knowledge = await asyncio.gather(
            fetch_animaflow_schema(service, selected, meta=meta, force=force),
            fetch_animaflow_knowledge(service, selected, meta=meta, force=force),
        )
    return selected, meta, catalog, schema, knowledge


async def fetch_animaflow_schema(
    service: Any,
    workflow: str = "",
    *,
    meta: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """按目录端点获取选中工作流的实时 schema。"""
    return await _fetch_resource(service, workflow, "schema", meta=meta, force=force)


async def fetch_animaflow_knowledge(
    service: Any,
    workflow: str = "",
    *,
    meta: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """按目录端点获取选中工作流的实时 knowledge。"""
    return await _fetch_resource(service, workflow, "knowledge", meta=meta, force=force)


def _finite_number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number_from_text(text: str, key: str) -> float | None:
    """解析接口说明里的显式默认值，不把普通推荐区间误当默认值。"""
    source = str(text or "")
    escaped = re.escape(key)
    patterns = (
        rf"(?i)\b{escaped}\b\s*[=:：]\s*([0-9]+(?:\.[0-9]+)?)",
        rf"(?i)\b{escaped}\b[^,，。;；\n)]{{0,48}}?(?:默认|缺省|default(?:\s+is)?|recommended\s+default)\s*(?:为|值|[=:：])?\s*([0-9]+(?:\.[0-9]+)?)",
        rf"(?i)(?:默认|缺省|default)\s*(?:的)?\s*\b{escaped}\b\s*(?:为|值|[=:：])?\s*([0-9]+(?:\.[0-9]+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            return _finite_number(match.group(1))
    return None


def _schema_properties(schema: dict[str, Any]) -> dict[str, Any]:
    params = schema.get("parameters") if isinstance(schema, dict) else {}
    properties = params.get("properties") if isinstance(params, dict) else {}
    return properties if isinstance(properties, dict) else {}


def _resource_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(_resource_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_resource_text(item) for item in value)
    return str(value or "")


def animaflow_workflow_defaults(
    meta: dict[str, Any],
    schema: dict[str, Any],
    knowledge: dict[str, Any],
) -> dict[str, float | int | None]:
    """从目录、schema 与 knowledge 动态提取工作流默认 cfg/steps。"""
    properties = _schema_properties(schema)
    defaults = meta.get("defaults") if isinstance(meta.get("defaults"), dict) else {}
    description = str(meta.get("description") or "")
    knowledge_text = _resource_text(knowledge)
    result: dict[str, float | int | None] = {"cfg": None, "steps": None}
    for key in ("cfg", "steps"):
        prop = properties.get(key) if isinstance(properties.get(key), dict) else {}
        candidates = (
            defaults.get(key),
            meta.get(f"default_{key}"),
            prop.get("default"),
        )
        value = next((number for number in (_finite_number(item) for item in candidates) if number is not None), None)
        if value is None:
            for text in (description, str(prop.get("description") or ""), knowledge_text):
                value = _number_from_text(text, key)
                if value is not None:
                    break
        result[key] = int(value) if key == "steps" and value is not None else value
    return result


def _bounds_from_schema(schema: dict[str, Any], key: str) -> dict[str, float | int | None]:
    prop = _schema_properties(schema).get(key)
    if not isinstance(prop, dict):
        return {"minimum": None, "maximum": None}
    minimum = _finite_number(prop.get("minimum"))
    maximum = _finite_number(prop.get("maximum"))
    if key == "steps":
        minimum = int(minimum) if minimum is not None else None
        maximum = int(maximum) if maximum is not None else None
    return {"minimum": minimum, "maximum": maximum}


def cfg_is_one(value: Any) -> bool:
    number = _finite_number(value)
    return number is not None and math.isclose(number, 1.0, rel_tol=0.0, abs_tol=1e-9)


def animaflow_effective_cfg(config: dict[str, Any], default: Any = None) -> float | None:
    value = config.get("animaflow_cfg")
    if value in (None, ""):
        value = config.get("animatool_turbo_cfg", default)
    number = _finite_number(value)
    fallback = _finite_number(default)
    return number if number is not None else fallback


def animaflow_effective_steps(config: dict[str, Any], default: Any = None) -> int | None:
    value = config.get("animaflow_steps")
    if value in (None, ""):
        value = config.get("animatool_turbo_steps", default)
    number = _finite_number(value)
    fallback = _finite_number(default)
    resolved = number if number is not None else fallback
    return int(resolved) if resolved is not None else None


def _append_uncensored_hint(value: Any) -> str:
    text = str(value or "").strip().rstrip(" ,.")
    lowered = text.lower()
    additions = [tag for tag in ("no mosaic", "uncensored") if tag not in lowered]
    if not additions:
        return str(value or "").strip()
    suffix = ", ".join(additions)
    return f"{text}, {suffix}" if text else suffix


def apply_animaflow_cfg_policy(
    payload: dict[str, Any],
    *,
    cfg: Any,
    safety: str,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """cfg=1 时彻底移除负面字段；NSFW/explicit 只在正面补无码提示。"""
    result = dict(payload or {})
    if not cfg_is_one(cfg):
        return result
    for field in ANIMAFLOW_NEGATIVE_FIELDS:
        result.pop(field, None)
    if str(safety or "").strip().lower() not in {"nsfw", "explicit"}:
        return result
    properties = _schema_properties(schema or {})
    target = next((field for field in ANIMAFLOW_NLTAG_FIELDS if str(result.get(field) or "").strip()), "")
    if not target:
        target = next((field for field in ANIMAFLOW_NLTAG_FIELDS if field in properties), "")
    if not target and ("positive" in properties or "positive" in result):
        target = "positive"
    if target:
        result[target] = _append_uncensored_hint(result.get(target))
    return result


async def inspect_animaflow_workflow(
    service: Any,
    workflow: str = "",
    *,
    force: bool = False,
) -> dict[str, Any]:
    """管理员面板所需的目录、schema 默认值和参数范围。"""
    selected, meta, catalog, schema, knowledge = await load_animaflow_workflow_resources(
        service,
        workflow,
        force=force,
    )
    defaults = animaflow_workflow_defaults(meta, schema, knowledge)
    cfg_default = defaults.get("cfg")
    workflows = [
        {
            "name": name,
            "description": str(info.get("description") or ""),
            "deprecated": bool(info.get("deprecated")),
        }
        for name, info in catalog["workflows"].items()
    ]
    return {
        "catalog_default": catalog.get("default") or "",
        "selected": selected,
        "description": str(meta.get("description") or ""),
        "workflows": workflows,
        "defaults": defaults,
        "cfg_bounds": _bounds_from_schema(schema, "cfg"),
        "steps_bounds": _bounds_from_schema(schema, "steps"),
        "supports_negative": any(field in _schema_properties(schema) for field in ANIMAFLOW_NEGATIVE_FIELDS)
        and not cfg_is_one(cfg_default),
        "legacy_fallback": bool(catalog.get("legacy_fallback")),
        "fallback_reason": str(catalog.get("fallback_reason") or ""),
    }


def clear_animaflow_caches() -> None:
    """测试与配置切换使用的缓存失效入口。"""
    _catalog_cache.clear()
    _resource_cache.clear()
