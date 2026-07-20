from __future__ import annotations

import re
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
