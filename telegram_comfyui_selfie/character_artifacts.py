from __future__ import annotations

import re
import hashlib
from pathlib import Path


def safe_avatar_part(value: str) -> str:
    """把会话/角色标识转成不会越出头像目录的文件名片段。"""
    text = str(value or "").strip().replace("..", "_")
    return re.sub(r"[\s/\\:*?\"<>|]+", "_", text).strip("._") or "unknown"


def avatar_file_path(service, session_id: str, character_id: str) -> Path:
    session_part = safe_avatar_part(session_id)
    character_part = safe_avatar_part(character_id)
    return service.state_path.parent / "avatars" / session_part / f"{character_part}.png"


def avatar_session_dir(service, session_id: str) -> Path:
    return service.state_path.parent / "avatars" / safe_avatar_part(session_id)


def wardrobe_preview_dir(service, session_id: str, character_key: str | None = None) -> Path:
    """用完整标识哈希隔离预览，避免清洗文件名造成角色/会话碰撞。"""
    root = service.state_path.parent / "wardrobe_previews" / hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root if character_key is None else root / hashlib.sha256(character_key.encode("utf-8")).hexdigest()


def avatar_public_marker(service, session_id: str, character_id: str) -> str:
    try:
        rel = avatar_file_path(service, session_id, character_id).relative_to(
            service.state_path.parent
        )
        return rel.as_posix()
    except Exception:
        return (
            f"avatars/{safe_avatar_part(session_id)}/"
            f"{safe_avatar_part(character_id)}.png"
        )
