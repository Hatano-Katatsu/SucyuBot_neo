"""WebUI 酒馆导入：后台转换、可编辑预览与原子创建。"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import time
import uuid
from typing import Any

from aiohttp import web

from . import session_schema
from . import character_card
from .character_artifacts import avatar_file_path, avatar_public_marker
from .tavern_import import MAX_FILE_BYTES, IMPORT_VERSION, parse_tavern_file, conversion_batches, conversion_prompt, normalize_conversion, keep_valid_conversion
from .webui_common import json_error, json_ok, service_from, session_allowed
from .world_runtime import PLACE_TYPES


def _scope(request):
    sid = request.match_info["session_id"]
    if not session_allowed(request, sid):
        raise web.HTTPForbidden(text=json.dumps({"ok": False, "error": "无权访问此会话"}, ensure_ascii=False), content_type="application/json")
    return service_from(request), sid


def _public_draft(row):
    data = row["data"]
    return {"id": row["id"], "status": row["status"], "converted": data.get("converted"),
            "error": data.get("error", ""), "progress": data.get("progress", ""),
            "warnings": (data.get("parsed") or {}).get("warnings", []), "result": row["result"]}


def _merge_target(service, sid, character_id):
    state = service._get_session_state(sid)
    saved = session_schema.get_saved_characters(state)
    if character_id not in saved or saved[character_id].get("is_default"):
        return None
    card = copy.deepcopy(saved[character_id])
    if service._context_character_key(sid) == character_id:
        card.update(character_card.card_from_state(state))
    version = hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    fields = {k: v for k, v in card.items() if k not in {"world_snapshot", "import_source"}}
    return {"id": character_id, "version": version, "card": fields}


async def convert_import(service, sid: str, draft_id: str) -> None:
    row = service.app_store.get_import_draft(sid, draft_id)
    if not row:
        return
    data = row["data"]
    parsed = data["parsed"]
    try:
        batches = conversion_batches(parsed)
        if not batches:
            data["converted"] = normalize_conversion({}, parsed)
            data["converted"]["world"]["unresolved"] = data["converted"]["unresolved"]
            data["progress"] = "没有可启用的事实；原资料已保留供整理"
            service.app_store.update_import_draft(sid, draft_id, data, "ready")
            return
        merged: dict[str, Any] = {"character": {}, "world": {}, "entries": [], "unresolved": []}
        for index, batch in enumerate(batches):
            data["progress"] = f"正在整理 {index + 1}/{len(batches)} 批资料"
            if not service.app_store.update_import_draft(sid, draft_id, data, "pending"):
                return
            established = {"persona": str(merged["character"].get("persona") or "")[:1200], "world": {k: str(v)[:1600] for k, v in merged["world"].items() if k not in {"entries", "sources"}}}
            user = json.dumps({"kind": parsed["kind"], "name": parsed["name"], "place_types": {k: v["label"] for k, v in PLACE_TYPES.items()}, "established_context": established, "sources": batch}, ensure_ascii=False)
            failure = ""
            for attempt in range(2):
                raw = await service._call_life_plan_json(sid, conversion_prompt(), user + ("\n上次结构校验错误，请修正：" + failure if failure else ""), tag=f"tavern-import-{index}-{'repair' if attempt else 'convert'}")
                try:
                    batch_ids = {item["id"] for item in batch}
                    normalized = normalize_conversion(raw, {**parsed, "sources": [s for s in parsed["sources"] if s["id"] in batch_ids]})
                    break
                except ValueError as exc:
                    failure = str(exc)
            else:
                data.setdefault("rejected_conversion", []).append(raw)
                normalized = keep_valid_conversion(raw, {**parsed, "sources": [s for s in parsed["sources"] if s["id"] in batch_ids]})
            for key, value in normalized["character"].items():
                if key in {"persona", "appearance", "outfit"} and merged["character"].get(key) and value:
                    if value not in merged["character"][key]:
                        merged["character"][key] += "\n" + str(value)
                elif value:
                    merged["character"][key] = value
            world = normalized["world"]
            summary = merged["world"].get("summary", "")
            raw_world = raw.get("world") or {}
            merged["world"].update({k: v for k, v in world.items() if k not in {"entries", "sources"} and v and (raw_world.get(k) or k not in merged["world"])})
            if summary and world["summary"] and world["summary"] not in summary:
                merged["world"]["summary"] = summary + "\n" + world["summary"]
            merged["entries"].extend(world["entries"])
            merged["unresolved"].extend(normalized["unresolved"])
        # 按实体归并事实，来源合并；角色知识取保守交集，避免一次不确定被其他批次覆盖。
        entities = {}
        for entry in merged["entries"]:
            key = (entry["type"], entry["name"] or entry["id"])
            previous = entities.get(key)
            if previous:
                if entry["content"] not in previous["content"]:
                    previous["content"] += "\n" + entry["content"]
                previous["source_ids"] = sorted(set(previous["source_ids"] + entry["source_ids"]))
                previous["known"] = previous["known"] and entry["known"]
            else:
                entities[key] = entry
        merged["entries"] = list(entities.values())
        data["converted"] = normalize_conversion(merged, parsed)
        data["converted"]["unresolved"] = list(dict.fromkeys(data["converted"]["unresolved"]))
        data["converted"]["world"]["unresolved"] = data["converted"]["unresolved"]
        data["error"] = ""
        data["progress"] = "转换完成"
        service.app_store.update_import_draft(sid, draft_id, data, "ready")
    except asyncio.CancelledError:
        data["error"] = "转换中断，可重新转换"
        service.app_store.update_import_draft(sid, draft_id, data, "failed")
        raise
    except Exception as exc:
        data["error"] = str(exc)[:500]
        service.app_store.update_import_draft(sid, draft_id, data, "failed")


def _start_conversion(service, sid, draft_id):
    registry = service._tavern_import_tasks
    previous = registry.get(draft_id)
    if previous and not previous.done():
        return
    task = service._spawn_background(convert_import(service, sid, draft_id), name=f"tavern-import:{draft_id}", session_id=sid, scope="tavern-import", drain=False)
    registry[draft_id] = task
    task.add_done_callback(lambda done: registry.pop(draft_id, None) if registry.get(draft_id) is done else None)


def _conversion_cache_key(service, sid, parsed):
    # 密钥/端点仅参与本机摘要，不进入草稿、日志或浏览器。
    profiles = [service._resolved_llm_config(purpose, sid, disable_thinking=True) for purpose in ("chat", "image")]
    material = [IMPORT_VERSION, parsed["file_hash"], profiles]
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def api_import_upload(request):
    service, sid = _scope(request)
    try:
        raw = await request.read()
        if len(raw) > MAX_FILE_BYTES:
            return json_error("文件超过 12 MiB", 413)
        parsed = await asyncio.to_thread(parse_tavern_file, raw, request.query.get("filename", "导入文件"))
        cache_key = _conversion_cache_key(service, sid, parsed)
        cached = service.app_store.find_import_cache(sid, cache_key)
        if cached and cached["status"] in {"pending", "ready"}:
            if cached["status"] == "pending" and cached["id"] not in service._tavern_import_tasks:
                _start_conversion(service, sid, cached["id"])
            return json_ok({"draft": {"id": cached["id"], "status": cached["status"]}})
        draft_id = uuid.uuid4().hex
        data = {"parsed": parsed, "version": IMPORT_VERSION, "cache_key": cache_key,
                "avatar": base64.b64encode(raw).decode("ascii") if parsed["has_avatar"] else ""}
        service.app_store.create_import_draft(draft_id, sid, data)
        if cached and cached["data"].get("converted"):
            data["converted"] = copy.deepcopy(cached["data"]["converted"])
            service.app_store.update_import_draft(sid, draft_id, data, "ready")
            return json_ok({"draft": {"id": draft_id, "status": "ready"}})
        _start_conversion(service, sid, draft_id)
        return json_ok({"draft": {"id": draft_id, "status": "pending"}})
    except (ValueError, UnicodeError) as exc:
        return json_error(str(exc))


async def api_import_status(request):
    service, sid = _scope(request)
    row = service.app_store.get_import_draft(sid, request.match_info["draft_id"])
    if not row:
        return json_error("草稿不存在或已过期", 404)
    if row["status"] == "pending" and row["id"] not in service._tavern_import_tasks:
        row["data"]["error"] = "上次转换已中断，可重新转换"
        service.app_store.update_import_draft(sid, row["id"], row["data"], "failed")
        row["status"] = "failed"
    draft = _public_draft(row)
    if row["status"] == "ready":
        draft["merge_targets"] = [target for key in session_schema.get_saved_characters(service._get_session_state(sid))
                                  if (target := _merge_target(service, sid, key))]
    return json_ok({"draft": draft})


async def api_import_retry(request):
    service, sid = _scope(request)
    draft_id = request.match_info["draft_id"]
    row = service.app_store.get_import_draft(sid, draft_id)
    if not row or row["status"] == "committed":
        return json_error("草稿不可重新转换")
    if draft_id in service._tavern_import_tasks:
        return json_ok({"draft_id": draft_id})
    row["data"]["cache_key"] = _conversion_cache_key(service, sid, row["data"]["parsed"])
    row["data"].pop("converted", None)
    row["data"]["error"] = ""
    service.app_store.update_import_draft(sid, draft_id, row["data"], "pending")
    _start_conversion(service, sid, draft_id)
    return json_ok({"draft_id": draft_id})


async def api_import_commit(request):
    service, sid = _scope(request)
    draft_id = request.match_info["draft_id"]
    body = await request.json()
    async with service.character_operation_lock(sid):
        row = service.app_store.get_import_draft(sid, draft_id)
        if not row:
            return json_error("草稿不存在或已过期", 404)
        if row["status"] == "committed":
            return json_ok({"result": row["result"]})
        if row["status"] != "ready":
            return json_error("转换尚未成功", 409)
        converted = copy.deepcopy(row["data"]["converted"])
        card, world = converted["character"], converted["world"]
        merge_id = str(body.get("merge_character_id") or "")
        merge_target = _merge_target(service, sid, merge_id) if merge_id else None
        if merge_id and (not card or not merge_target or body.get("merge_version") != merge_target["version"]):
            return json_error("目标角色已变化，请刷新预览后再合并", 409)
        for key in ("bot_name", "persona", "appearance", "outfit"):
            if key in body and card:
                if len(str(body[key])) > 200000:
                    return json_error(f"{key} 超过 20 万字，请精简后创建")
                card[key] = str(body[key])
        for key in ("name", "summary", "photography", "kind", "city"):
            if "world_" + key in body:
                if len(str(body["world_" + key])) > 200000:
                    return json_error(f"world_{key} 超过 20 万字，请精简后创建")
                world[key] = str(body["world_" + key])
        if world["photography"] not in {"modern", "scene"}:
            return json_error("图片分享方式无效")
        if world.get("kind") not in {"real", "fictional"}:
            return json_error("世界类型无效")
        if world["kind"] == "fictional":
            world["city"] = ""
        state = copy.deepcopy(service._get_session_state(sid))
        saved = session_schema.get_saved_characters(state)
        target = str(body.get("character_id") or "")
        if not card and target and target not in saved:
            return json_error("目标角色不存在")
        world_id = uuid.uuid4().hex
        character_id = ""
        avatar_path = None
        if card:
            name = str(card.get("bot_name") or "导入角色").strip()[:100] or "导入角色"
            character_id = merge_id or name
            suffix = 2
            while not merge_id and (character_id in saved or character_id in {"__default__", service._default_character_payload()["id"]}):
                character_id = f"{name} {suffix}"
                suffix += 1
            card.update(character=character_id, world_id=world_id, world_snapshot=world,
                        import_source={"version": IMPORT_VERSION, "hash": row["data"]["parsed"]["file_hash"], "original": row["data"]["parsed"]["original"]})
            if row["data"].get("avatar") and not merge_id:
                avatar_path = avatar_file_path(service, sid, character_id)
                # 路径清洗可能使不同名称撞同一个文件；不覆盖已有头像。
                if avatar_path.exists():
                    character_id += " " + uuid.uuid4().hex[:8]
                    card["character"] = character_id
                    avatar_path = avatar_file_path(service, sid, character_id)
                avatar = base64.b64decode(row["data"]["avatar"])
                avatar_created = False
                def write_avatar():
                    nonlocal avatar_created
                    avatar_path.parent.mkdir(parents=True, exist_ok=True)
                    with avatar_path.open("xb") as stream:
                        avatar_created = True
                        stream.write(avatar)
                writing = asyncio.create_task(asyncio.to_thread(write_avatar))
                try:
                    await asyncio.shield(writing)
                except BaseException:
                    try:
                        await writing
                    finally:
                        if avatar_created:
                            await asyncio.to_thread(avatar_path.unlink, missing_ok=True)
                    raise
                card.update(avatar_path=avatar_public_marker(service, sid, character_id), avatar_updated_at=time.time())
            # 文件写入 await 后重新读取最新会话，再合并本次新建角色。
            state = copy.deepcopy(service._get_session_state(sid))
            saved = session_schema.get_saved_characters(state)
            if character_id in saved and not merge_id:
                if avatar_path:
                    await asyncio.to_thread(avatar_path.unlink, missing_ok=True)
                return json_error("角色在创建期间发生变化，请重试", 409)
            if merge_id:
                saved[character_id] = {**saved[character_id], **card}
                if service._context_character_key(sid) == character_id:
                    character_card.apply_card_to_state(state, card)
            else:
                saved[character_id] = card
        elif target:
            saved[target].update(world_id=world_id, world_snapshot=world)
            if service._context_character_key(sid) == target:
                session_schema.set_character_value(state, "custom_world_id", world_id)
                session_schema.set_character_value(state, "custom_world_snapshot", world)
        result = {"character_id": character_id or target, "world_id": world_id}
        try:
            result = service.app_store.commit_import_draft(sid, draft_id, service._user_id_for_session(sid), state, world_id, world, result)
        except Exception:
            if avatar_path:
                await asyncio.to_thread(avatar_path.unlink, missing_ok=True)
            raise
        service.sessions[sid] = state
        return json_ok({"result": result})


async def api_world_profiles(request):
    service, sid = _scope(request)
    return json_ok({"worlds": service.app_store.list_world_profiles(service._user_id_for_session(sid)), "place_types": {k: v["label"] for k, v in PLACE_TYPES.items()}})


async def api_world_profile_update(request):
    service, sid = _scope(request)
    body = await request.json()
    world_id = request.match_info["world_id"]
    owner = service._user_id_for_session(sid)
    row = service.app_store.get_world_profile(owner, world_id)
    if not row:
        return json_error("世界背景不存在", 404)
    world = row["data"]
    for key in ("name", "summary", "photography", "kind", "city"):
        if key in body:
            if len(str(body[key])) > 200000:
                return json_error(f"{key} 超过 20 万字")
            world[key] = str(body[key])
    if world.get("photography") not in {"modern", "scene"}:
        return json_error("图片分享方式无效")
    if world.get("kind") not in {"real", "fictional"}:
        return json_error("世界类型无效")
    if world["kind"] == "fictional":
        world["city"] = ""
    if "entries" in body:
        entries = body["entries"]
        if not isinstance(entries, list) or len(entries) > 2048:
            return json_error("条目数量无效")
        # 只允许编辑已有条目，不接受随意的来源引用和脚本字段。
        edits = {str(e.get("id")): e for e in entries if isinstance(e, dict)}
        for entry in world.get("entries", []):
            edit = edits.get(entry["id"], {})
            for key in ("name", "content"):
                if key in edit:
                    if len(str(edit[key])) > 200000:
                        return json_error("单条背景内容超过 20 万字")
                    entry[key] = str(edit[key])
            for key in ("known", "enabled"):
                if key in edit:
                    entry[key] = edit[key] is True
            if "place_key" in edit:
                if edit["place_key"] and edit["place_key"] not in PLACE_TYPES:
                    return json_error("地点用途无效")
                entry["place_key"] = edit["place_key"]
    try:
        ok = service.app_store.update_world_profile(owner, world_id, world, int(body.get("revision", 0)))
    except (ValueError, TypeError):
        return json_error("版本号无效")
    return json_ok() if ok else json_error("背景已被修改，请刷新后重试", 409)


async def api_world_profile_bind(request):
    service, sid = _scope(request)
    body = await request.json()
    world_id, character_id = str(body.get("world_id") or ""), str(body.get("character_id") or "")
    row = service.app_store.get_world_profile(service._user_id_for_session(sid), world_id) if world_id else None
    if world_id and not row:
        return json_error("世界背景不存在", 404)
    async with service.character_operation_lock(sid):
        state = service._get_session_state(sid)
        saved = session_schema.get_saved_characters(state)
        if character_id not in saved:
            return json_error("请先选择自定义角色")
        world = row["data"] if row else {}
        saved[character_id].update(world_id=world_id, world_snapshot=world)
        if service._context_character_key(sid) == character_id:
            session_schema.set_character_value(state, "custom_world_id", world_id)
            session_schema.set_character_value(state, "custom_world_snapshot", world)
        service._save_session_state(sid, state)
    return json_ok()
