from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .webui_common import (
    character_value,
    human_ago,
    json_error,
    json_ok,
    require_admin,
    service_from,
    session_allowed,
)


def log_chunk_items(service, base_path: Path, active_path: Path | None = None) -> list[dict[str, Any]]:
    paths = service._log_all_paths(base_path) if hasattr(service, "_log_all_paths") else ([base_path] if base_path.exists() else [])
    active_name = active_path.name if active_path is not None else ""
    items = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        is_current = path == base_path
        items.append({
            "name": path.name,
            "label": ("当前块 " if is_current else "历史块 ") + path.name,
            "current": is_current,
            "active": path.name == active_name,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "mtime_ago": human_ago(time.time() - stat.st_mtime),
        })
    return items


USER_LOG_LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(?P<tag>\S+)(?:\s+(?P<message>.*))?$"
)


def error_log_paths(service) -> list[Path]:
    """只读取专用错误日志 errors.log 及其历史分块。"""
    if hasattr(service, "_error_log_all_paths"):
        return service._error_log_all_paths()
    path = service._user_log_dir() / "errors.log"
    return [path] if path.exists() else []


def parse_error_log_line(path: Path, line: str, line_no: int, mtime: float) -> dict[str, Any] | None:
    match = USER_LOG_LINE_RE.match(line)
    timestamp = match.group("time") if match else ""
    tag = match.group("tag") if match else ""
    message = (match.group("message") if match else line) or ""
    if tag != "ERROR" and "ERROR" not in line and "error" not in line.lower():
        return None
    session_id = ""
    session_match = re.match(r"session=([^\s]+)\s*(.*)$", message)
    if session_match:
        session_id = session_match.group(1)
        message = session_match.group(2).strip()
    payload = None
    marker = ""
    for candidate in ("LLM_FULL_LOG", "MEMORY_OP_FAILED"):
        if candidate in message:
            marker = candidate
            raw_json = message.split(candidate, 1)[1].strip()
            try:
                payload = json.loads(raw_json)
            except Exception:
                payload = None
            break
    item: dict[str, Any] = {
        "file": path.name,
        "line_no": line_no,
        "line": line,
        "time": timestamp,
        "tag": tag or "",
        "message": message,
        "session_id": session_id,
        "kind": marker,
        "mtime": mtime,
    }
    if payload is not None:
        item["payload"] = payload
        if isinstance(payload, dict):
            item["error"] = payload.get("error") or ""
            item["request"] = payload.get("request")
            item["response"] = payload.get("response")
    return item


async def api_logs(request: web.Request):
    service = service_from(request)
    log_dir = service._user_log_dir()
    items = []
    if log_dir.exists():
        for path in log_dir.glob("telegram_*.log"):
            # 历史分块形如 telegram_123.20260630_153000.log；列表只展示当前块。
            if "." in path.stem[len("telegram_"):]:
                continue
            chat_id = path.stem[len("telegram_"):]
            try:
                stat = path.stat()
            except OSError:
                continue
            sid = service.session_id_for_chat(chat_id)
            if not session_allowed(request, sid):
                continue
            state = service.sessions.get(sid, {})
            items.append({
                "chat_id": chat_id,
                "session_id": sid,
                "character": character_value(state, "custom_character", "") or "",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_ago": human_ago(time.time() - stat.st_mtime),
            })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return json_ok({"logs": items, "enabled": service._user_log_enabled(), "dir": str(log_dir)})


async def api_log_detail(request: web.Request):
    service = service_from(request)
    chat_id = request.match_info["chat_id"]
    sid = service.session_id_for_chat(chat_id)
    if not session_allowed(request, sid):
        return json_error("无权访问此日志", status=403)
    base_path = service._user_log_path(sid)
    if hasattr(service, "_resolve_log_chunk_path"):
        path = service._resolve_log_chunk_path(base_path, request.query.get("chunk") or "")
    else:
        path = service._user_log_latest_path(sid) if hasattr(service, "_user_log_latest_path") else base_path
    if not path.exists():
        return json_error("日志不存在", status=404)
    try:
        tail = max(1, min(5000, int(request.query.get("tail", "500"))))
    except ValueError:
        tail = 500
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return json_ok({
        "chat_id": chat_id,
        "chunk": path.name,
        "chunk_size": path.stat().st_size,
        "chunks": log_chunk_items(service, base_path, path),
        "total_lines": len(lines),
        "shown_lines": min(tail, len(lines)),
        "content": "\n".join(lines[-tail:]),
    })


async def api_log_clear(request: web.Request):
    service = service_from(request)
    chat_id = request.match_info["chat_id"]
    sid = service.session_id_for_chat(chat_id)
    if not session_allowed(request, sid):
        return json_error("无权清除此日志", status=403)
    try:
        paths = service._user_log_all_paths(sid) if hasattr(service, "_user_log_all_paths") else [service._user_log_path(sid)]
        for path in paths:
            if path.exists():
                path.unlink()
    except OSError as exc:
        return json_error(str(exc), status=500)
    return json_ok()


async def api_llm_debug_log(request: web.Request):
    require_admin(request)
    service = service_from(request)
    base_path = service._llm_debug_log_path()
    path = service._resolve_log_chunk_path(base_path, request.query.get("chunk") or "") if hasattr(service, "_resolve_log_chunk_path") else base_path
    if not path.exists():
        return json_ok({"content": {}, "updated_at": None, "chunks": []})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return json_ok({
            "content": data.get("entries_by_type", {}),
            "updated_at": data.get("updated_at"),
            "chunk": path.name,
            "chunks": log_chunk_items(service, base_path, path),
        })
    except Exception as exc:
        return json_error(str(exc), status=500)


async def api_system_error_log(request: web.Request):
    require_admin(request)
    service = service_from(request)
    try:
        limit = max(1, min(1000, int(request.query.get("limit", "300"))))
    except ValueError:
        limit = 300
    error_lines: list[dict[str, Any]] = []
    for path in error_log_paths(service):
        try:
            stat = path.stat()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines, start=1):
                item = parse_error_log_line(path, line, index, stat.st_mtime)
                if item:
                    error_lines.append(item)
        except Exception:
            continue
    error_lines.sort(key=lambda x: (x.get("time") or "", x.get("mtime") or 0, x.get("line_no") or 0), reverse=True)
    return json_ok({"errors": error_lines[:limit], "total": len(error_lines)})
