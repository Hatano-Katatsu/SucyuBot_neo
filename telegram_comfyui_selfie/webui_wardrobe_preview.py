from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import uuid
from urllib.parse import quote

from aiohttp import web

from . import generation, session_schema
from .character_artifacts import wardrobe_preview_dir
from .webui_characters import character_operation_lock, required_character_key_from_request
from .webui_common import json_error, json_ok, service_from, session_allowed


PREVIEW_SCENE = (
    "Single character standing in a private bedroom dressing area, full body from head to shoes, "
    "relaxed arms at sides, facing viewer, plain ivory backdrop, soft even indoor lighting, "
    "show the complete current outfit clearly, no text, no camera, no phone, no collage"
)


def preview_snapshot(service, sid):
    """渲染副本独立持有状态和展示缓存，不向 live 会话或状态库提交衣物变更。"""
    preview = copy.copy(service)
    preview.config = copy.deepcopy(service.config)
    preview.config.update(width=832, height=1248, batch_size=1)
    preview.sessions = {sid: copy.deepcopy(service._get_session_state(sid))}
    preview._last_prompt_slots_by_session = {}
    preview._last_generated_nltag_by_session = {}
    preview._save_session_state = lambda *args, **kwargs: None
    generation.build_prompt(preview, PREVIEW_SCENE, session_id=sid, view="third")
    slots = preview._last_prompt_slots
    # 时间/天气不参与搭配身份；外貌、衣物状态、画风和后端改变时使用新的缓存。
    signature = {name: getattr(slots, name) for name in (
        "quality", "count", "identity", "base_appearance", "effective_appearance",
        "style_artist", "style_general", "safety", "one_shot_appearance",
    )}
    signature["nudity"] = session_schema.get_nudity(preview.sessions[sid])
    signature["version"] = 1
    signature["render"] = {key: preview.config.get(key) for key in (
        "animaflow_enabled", "animaflow_workflow", "animaflow_cfg", "animaflow_steps",
        "negative_prompt", "sampler", "scheduler", "turbo_mode", "turbo_strength",
        "steps", "cfg", "unet_model", "clip_model", "vae_model", "turbo_lora_model",
        "comfyui_workflow_file",
    )}
    key = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return preview, key


def _descriptor(service, sid, character_key, key):
    path = wardrobe_preview_dir(service, sid, character_key) / f"{key}.image"
    cached = path.is_file()
    url = None
    if cached:
        url = (f"/api/sessions/{quote(sid, safe='')}/wardrobe-preview/{key}"
               f"?character_key={quote(character_key or '__default__', safe='')}&v={path.stat().st_mtime_ns}")
    return {"key": key, "cached": cached, "image_url": url}


def _image_content_type(data):
    """只接受生图后端支持的栅格格式，不将错误页或 SVG 当成预览。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data):
        return "image/webp"
    raise ValueError("生图后端未返回支持的 PNG/JPEG/WebP 图片")


def _save_preview(path, data):
    """原样保存生图结果，原子替换；刷新失败时保留旧图。"""
    _image_content_type(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def api_wardrobe_preview(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_key = required_character_key_from_request(request)
    if character_key is None:
        return json_error("请选择角色")
    async with character_operation_lock(service, sid):
        if character_key != service._context_character_key(sid):
            return json_error("请先将该角色设为当前角色", status=409)
        preview, key = preview_snapshot(service, sid)
        descriptor = await asyncio.to_thread(_descriptor, service, sid, character_key, key)
        if request.method == "GET":
            return json_ok({"preview": descriptor})
        if request.query.get("key") != key:
            return json_error("当前搭配已变化，请刷新后重新生成", status=409)
        if descriptor["cached"] and request.query.get("refresh") != "1":
            return json_ok({"preview": descriptor})
        try:
            # 共用 GPU 队列与 HTTP 连接，只让隔离副本接收规划器的展示缓存。
            async with service._gen_lock:
                service._generating = True
                try:
                    service._ensure_comfy_session()
                    preview.comfy_session = service.comfy_session
                    ok, images, error = await generation.do_generate_locked(
                        preview, PREVIEW_SCENE, session_id=sid, orientation="2:3", view="third",
                    )
                finally:
                    service._generating = False
            if not ok or not images:
                return json_error(f"搭配预览生成失败：{error or '未返回图片'}", status=502)
            path = wardrobe_preview_dir(service, sid, character_key) / f"{key}.image"
            # shield 并排空写盘，避免角色删除与尚未结束的后台文件写入交错。
            writer = asyncio.create_task(asyncio.to_thread(_save_preview, path, images[0]))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                await writer
                raise
            return json_ok({"preview": await asyncio.to_thread(_descriptor, service, sid, character_key, key)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return json_error(f"搭配预览失败：{exc}", status=502)


async def api_wardrobe_preview_image(request: web.Request):
    service = service_from(request)
    sid = request.match_info["session_id"]
    if not session_allowed(request, sid):
        return json_error("无权访问此会话", status=403)
    character_key = required_character_key_from_request(request)
    key = request.match_info["preview_key"]
    if character_key is None or not re.fullmatch(r"[0-9a-f]{64}", key):
        return json_error("预览不存在", status=404)
    path = wardrobe_preview_dir(service, sid, character_key) / f"{key}.image"
    if not await asyncio.to_thread(path.is_file):
        return json_error("预览不存在", status=404)
    try:
        mime = await asyncio.to_thread(lambda: _image_content_type(path.read_bytes()))
    except (OSError, ValueError):
        return json_error("预览文件不可用，请重新生成", status=404)
    return web.FileResponse(path, headers={"Content-Type": mime, "Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"})
