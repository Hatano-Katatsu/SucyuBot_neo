from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


def _now() -> float:
    return time.time()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text or "")
    except Exception:
        return default


def hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(12)
    digest = hashlib.sha256((salt + "\n" + (password or "")).encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        alg, salt, digest = (encoded or "").split("$", 2)
    except ValueError:
        return False
    if alg != "sha256":
        return False
    return secrets.compare_digest(hash_password(password, salt=salt), f"sha256${salt}${digest}")


class AppStateStore:
    """SQLite 状态库：承载会话状态、上下文、checkpoint、dream、Web 凭据和模型设置。

    state.json 已弃用，所有运行态以这里为准。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        if os.environ.get("SUCYUBOT_TEST_FAST_SQLITE"):
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_schema(self):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    character_key TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    checkpointed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_scope "
                "ON chat_messages(session_id, character_key, id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    session_id TEXT NOT NULL,
                    character_key TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    source_until_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(session_id, character_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS diaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_key TEXT NOT NULL DEFAULT '',
                    diary_date TEXT NOT NULL,
                    content TEXT NOT NULL,
                    from_message_id INTEGER NOT NULL DEFAULT 0,
                    to_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, character_key, diary_date)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS life_plans (
                    session_id TEXT NOT NULL,
                    character_key TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(session_id, character_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_meta (
                    session_id TEXT NOT NULL,
                    character_key TEXT NOT NULL DEFAULT '',
                    last_dream_at REAL NOT NULL DEFAULT 0,
                    last_dream_message_id INTEGER NOT NULL DEFAULT 0,
                    last_checkpoint_at REAL NOT NULL DEFAULT 0,
                    last_checkpoint_message_id INTEGER NOT NULL DEFAULT 0,
                    character_history_summary TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(session_id, character_key)
                )
                """
            )
            # 迁移：旧库没有 character_history_summary 列时补上。
            meta_cols = {row["name"] for row in conn.execute("PRAGMA table_info(context_meta)")}
            if "character_history_summary" not in meta_cols:
                conn.execute("ALTER TABLE context_meta ADD COLUMN character_history_summary TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_credentials (
                    user_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL DEFAULT '',
                    access_token TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_profiles (
                    user_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, profile_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_model_settings (
                    user_id TEXT PRIMARY KEY,
                    chat_profile_id TEXT NOT NULL DEFAULT '',
                    fast_profile_id TEXT NOT NULL DEFAULT '',
                    vision_profile_id TEXT NOT NULL DEFAULT '',
                    chat_thinking INTEGER,
                    fast_thinking INTEGER,
                    updated_at REAL NOT NULL
                )
                """
            )
            settings_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_model_settings)")}
            if "vision_profile_id" not in settings_cols:
                conn.execute("ALTER TABLE user_model_settings ADD COLUMN vision_profile_id TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    profile_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL DEFAULT '',
                    tag TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_usage_scope "
                "ON llm_usage(created_at, profile_id, model, purpose, tag)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS city_catalogs (
                    catalog_key TEXT PRIMARY KEY,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def user_id_from_session(session_id: str) -> str:
        return str(session_id or "").removeprefix("telegram:")

    def append_messages(self, session_id: str, character_key: str, messages: list[dict[str, str]]) -> list[int]:
        if not session_id or not messages:
            return []
        user_id = self.user_id_from_session(session_id)
        now = _now()
        ids: list[int] = []
        with closing(self._connect()) as conn:
            for msg in messages:
                role = (msg.get("role") or "").strip()
                content = (msg.get("content") or "").strip()
                if not role or not content:
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO chat_messages(session_id, user_id, character_key, role, content, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, user_id, character_key or "", role, content, now),
                )
                ids.append(int(cur.lastrowid))
            conn.commit()
        return ids

    def update_latest_matching_message(
        self,
        session_id: str,
        character_key: str,
        role: str,
        old_content: str,
        new_content: str,
    ) -> bool:
        """更新或删除最近一条匹配内容的聊天消息。

        用于 Telegram 分段发送被新用户消息打断时，把已入库但尚未全部发出的 assistant
        内容裁剪到真实已发送部分；new_content 为空时删除该 assistant 行。
        """
        if not session_id or not role:
            return False
        old_content = str(old_content or "").strip()
        if not old_content:
            return False
        new_content = str(new_content or "").strip()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT id, content FROM chat_messages
                WHERE session_id = ? AND character_key = ? AND role = ? AND content = ?
                ORDER BY id DESC LIMIT 1
                """,
                (session_id, character_key or "", role, old_content),
            ).fetchone()
            if not row:
                return False
            msg_id = int(row["id"])
            if new_content:
                conn.execute("UPDATE chat_messages SET content = ? WHERE id = ?", (new_content, msg_id))
            else:
                conn.execute("DELETE FROM chat_messages WHERE id = ?", (msg_id,))
            conn.commit()
        return True

    def list_messages(
        self,
        session_id: str,
        character_key: str,
        *,
        after_id: int = 0,
        before_or_equal_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?", "character_key = ?", "id > ?"]
        params: list[Any] = [session_id, character_key or "", int(after_id or 0)]
        if before_or_equal_id is not None:
            clauses.append("id <= ?")
            params.append(int(before_or_equal_id))
        sql = f"SELECT * FROM chat_messages WHERE {' AND '.join(clauses)} ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def latest_message_id(self, session_id: str, character_key: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MAX(id) AS id FROM chat_messages WHERE session_id = ? AND character_key = ?",
                (session_id, character_key or ""),
            ).fetchone()
        return int(row["id"] or 0) if row else 0

    def get_checkpoint(self, session_id: str, character_key: str) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE session_id = ? AND character_key = ?",
                (session_id, character_key or ""),
            ).fetchone()
        if not row:
            return {"summary": "", "source_until_id": 0, "version": 0, "updated_at": 0}
        return dict(row)

    def upsert_checkpoint(self, session_id: str, character_key: str, summary: str, source_until_id: int):
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO checkpoints(session_id, character_key, summary, source_until_id, updated_at, version)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(session_id, character_key) DO UPDATE SET
                    summary = excluded.summary,
                    source_until_id = excluded.source_until_id,
                    updated_at = excluded.updated_at,
                    version = checkpoints.version + 1
                """,
                (session_id, character_key or "", summary, int(source_until_id or 0), now),
            )
            conn.execute(
                """
                INSERT INTO context_meta(session_id, character_key, last_checkpoint_at, last_checkpoint_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, character_key) DO UPDATE SET
                    last_checkpoint_at = excluded.last_checkpoint_at,
                    last_checkpoint_message_id = excluded.last_checkpoint_message_id
                """,
                (session_id, character_key or "", now, int(source_until_id or 0)),
            )
            conn.execute(
                "UPDATE chat_messages SET checkpointed = 1 WHERE session_id = ? AND character_key = ? AND id <= ?",
                (session_id, character_key or "", int(source_until_id or 0)),
            )
            conn.commit()

    def clear_checkpoint(self, session_id: str, character_key: str, *, source_until_id: int = 0):
        """清空 checkpoint 摘要，并把 checkpoint 边界推进到指定消息。

        /新场景 需要让旧摘要不再进入模型上下文，但旧聊天仍保留在 chat_messages，
        供 dream 之后继续整理。因此这里不删除消息，只建立新的 checkpoint 边界。
        """
        now = _now()
        until = int(source_until_id or 0)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO checkpoints(session_id, character_key, summary, source_until_id, updated_at, version)
                VALUES (?, ?, '', ?, ?, 1)
                ON CONFLICT(session_id, character_key) DO UPDATE SET
                    summary = '',
                    source_until_id = excluded.source_until_id,
                    updated_at = excluded.updated_at,
                    version = checkpoints.version + 1
                """,
                (session_id, character_key or "", until, now),
            )
            conn.execute(
                """
                INSERT INTO context_meta(session_id, character_key, last_checkpoint_at, last_checkpoint_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, character_key) DO UPDATE SET
                    last_checkpoint_at = excluded.last_checkpoint_at,
                    last_checkpoint_message_id = excluded.last_checkpoint_message_id
                """,
                (session_id, character_key or "", now, until),
            )
            if until > 0:
                conn.execute(
                    "UPDATE chat_messages SET checkpointed = 1 WHERE session_id = ? AND character_key = ? AND id <= ?",
                    (session_id, character_key or "", until),
                )
            conn.commit()

    def get_context_meta(self, session_id: str, character_key: str) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM context_meta WHERE session_id = ? AND character_key = ?",
                (session_id, character_key or ""),
            ).fetchone()
        if not row:
            return {
                "last_dream_at": 0,
                "last_dream_message_id": 0,
                "last_checkpoint_at": 0,
                "last_checkpoint_message_id": 0,
                "character_history_summary": "",
            }
        return dict(row)

    def mark_dream(self, session_id: str, character_key: str, to_message_id: int):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO context_meta(session_id, character_key, last_dream_at, last_dream_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, character_key) DO UPDATE SET
                    last_dream_at = excluded.last_dream_at,
                    last_dream_message_id = excluded.last_dream_message_id
                """,
                (session_id, character_key or "", _now(), int(to_message_id or 0)),
            )
            conn.commit()

    def upsert_character_history_summary(self, session_id: str, character_key: str, summary: str):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO context_meta(session_id, character_key, character_history_summary)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id, character_key) DO UPDATE SET
                    character_history_summary = excluded.character_history_summary
                """,
                (session_id, character_key or "", summary or ""),
            )
            conn.commit()

    def upsert_diary(
        self,
        session_id: str,
        character_key: str,
        diary_date: str,
        content: str,
        *,
        from_message_id: int = 0,
        to_message_id: int = 0,
    ):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO diaries(session_id, character_key, diary_date, content, from_message_id, to_message_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, character_key, diary_date) DO UPDATE SET
                    content = excluded.content,
                    from_message_id = excluded.from_message_id,
                    to_message_id = excluded.to_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, character_key or "", diary_date, content,
                    int(from_message_id or 0), int(to_message_id or 0), _now(),
                ),
            )
            conn.commit()

    def get_diary(self, session_id: str, character_key: str, diary_date: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM diaries WHERE session_id = ? AND character_key = ? AND diary_date = ?",
                (session_id, character_key or "", diary_date),
            ).fetchone()
        return dict(row) if row else None

    def recent_diaries(self, session_id: str, character_key: str, limit: int = 2) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM diaries
                WHERE session_id = ? AND character_key = ?
                ORDER BY diary_date DESC, updated_at DESC
                LIMIT ?
                """,
                (session_id, character_key or "", int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_diary(self, session_id: str, character_key: str, diary_date: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "DELETE FROM diaries WHERE session_id = ? AND character_key = ? AND diary_date = ?",
                (session_id, character_key or "", diary_date),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_life_plan(self, session_id: str, character_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM life_plans WHERE session_id = ? AND character_key = ?",
                (session_id, character_key or ""),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = _json_loads(data.get("payload") or "{}", {})
        return data

    def upsert_life_plan(self, session_id: str, character_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO life_plans(session_id, character_key, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, character_key) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (session_id, character_key or "", _json_dumps(payload or {}), now),
            )
            conn.commit()
        return {"session_id": session_id, "character_key": character_key or "", "payload": payload or {}, "updated_at": now}

    def delete_life_plan(self, session_id: str, character_key: str = "") -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "DELETE FROM life_plans WHERE session_id = ? AND character_key = ?",
                (session_id, character_key or ""),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_life_plans_for_session(self, session_id: str) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM life_plans WHERE session_id = ?", (session_id,))
            conn.commit()
            return int(cur.rowcount or 0)

    def set_web_password(self, user_id: str, password: str) -> dict[str, str]:
        token = self.get_or_create_web_token(user_id)
        encoded = hash_password(password)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO web_credentials(user_id, password_hash, access_token, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    access_token = CASE
                        WHEN web_credentials.access_token = '' THEN excluded.access_token
                        ELSE web_credentials.access_token
                    END,
                    updated_at = excluded.updated_at
                """,
                (str(user_id), encoded, token, _now()),
            )
            conn.commit()
        return {"user_id": str(user_id), "token": token}

    def get_or_create_web_token(self, user_id: str) -> str:
        user_id = str(user_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT access_token FROM web_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            token = (row["access_token"] if row else "") or ""
            if token:
                return token
            token = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO web_credentials(user_id, access_token, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET access_token = excluded.access_token, updated_at = excluded.updated_at
                """,
                (user_id, token, _now()),
            )
            conn.commit()
            return token

    def verify_user_password(self, user_id: str, password: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT password_hash FROM web_credentials WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        return bool(row and verify_password(password, row["password_hash"]))

    def user_for_token(self, token: str) -> str:
        if not token:
            return ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT user_id FROM web_credentials WHERE access_token = ?",
                (token,),
            ).fetchone()
        return str(row["user_id"]) if row else ""

    def upsert_model_profile(self, user_id: str, profile_id: str, data: dict[str, Any]):
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO model_profiles(user_id, profile_id, data, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, profile_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (str(user_id), profile_id, _json_dumps(data), _now()),
            )
            conn.commit()

    def list_model_profiles(self, user_id: str) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT profile_id, data FROM model_profiles WHERE user_id = ? ORDER BY profile_id",
                (str(user_id),),
            ).fetchall()
        return {row["profile_id"]: _json_loads(row["data"], {}) for row in rows}

    def get_user_model_settings(self, user_id: str) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM user_model_settings WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
        if not row:
            return {"chat_profile_id": "", "fast_profile_id": "", "vision_profile_id": ""}
        data = dict(row)
        return {
            "chat_profile_id": data.get("chat_profile_id") or "",
            "fast_profile_id": data.get("fast_profile_id") or "",
            "vision_profile_id": data.get("vision_profile_id") or "",
            "updated_at": data.get("updated_at") or 0,
        }

    def update_user_model_settings(self, user_id: str, **values: Any):
        current = self.get_user_model_settings(user_id)
        for key in ("chat_profile_id", "fast_profile_id", "vision_profile_id"):
            if key in values:
                current[key] = values.get(key) or ""
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO user_model_settings(
                    user_id, chat_profile_id, fast_profile_id, vision_profile_id,
                    chat_thinking, fast_thinking, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_profile_id = excluded.chat_profile_id,
                    fast_profile_id = excluded.fast_profile_id,
                    vision_profile_id = excluded.vision_profile_id,
                    chat_thinking = excluded.chat_thinking,
                    fast_thinking = excluded.fast_thinking,
                    updated_at = excluded.updated_at
                """,
                (
                    str(user_id),
                    current.get("chat_profile_id") or "",
                    current.get("fast_profile_id") or "",
                    current.get("vision_profile_id") or "",
                    None,
                    None,
                    _now(),
                ),
            )
            conn.commit()
        return self.get_user_model_settings(user_id)

    def record_llm_usage(
        self,
        *,
        profile_id: str = "",
        model: str = "",
        purpose: str = "",
        tag: str = "",
        session_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        total_tokens: int = 0,
    ):
        """记录一次 LLM 调用的 token 消耗。"""
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO llm_usage(
                    created_at, profile_id, model, purpose, tag, session_id,
                    prompt_tokens, completion_tokens, cached_tokens, total_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    str(profile_id or ""),
                    str(model or ""),
                    str(purpose or ""),
                    str(tag or ""),
                    str(session_id or ""),
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    int(cached_tokens or 0),
                    int(total_tokens or 0),
                ),
            )
            conn.commit()

    def aggregate_llm_usage(
        self,
        *,
        after: float | None = None,
        before: float | None = None,
        group_by: tuple[str, ...] = ("profile_id", "model", "purpose", "tag"),
    ) -> list[dict[str, Any]]:
        """按指定维度聚合 LLM 用量。"""
        cols = [c for c in group_by if c in {"profile_id", "model", "purpose", "tag", "session_id"}]
        if not cols:
            cols = ["profile_id"]
        select_cols = ", ".join(cols)
        clauses: list[str] = []
        params: list[Any] = []
        if after is not None:
            clauses.append("created_at >= ?")
            params.append(float(after))
        if before is not None:
            clauses.append("created_at < ?")
            params.append(float(before))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT {select_cols},
                   COUNT(*) AS requests,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(total_tokens) AS total_tokens,
                   MAX(created_at) AS last_used,
                   MIN(created_at) AS first_used
            FROM llm_usage
            {where}
            GROUP BY {select_cols}
            ORDER BY total_tokens DESC
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Session state (replaces state.json)
    # ------------------------------------------------------------------

    def save_session_state(self, session_id: str, data: dict[str, Any]):
        """保存单个会话的完整状态（JSON blob）。"""
        if not session_id:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO session_state(session_id, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (session_id, _json_dumps(data), _now()),
            )
            conn.commit()

    def load_session_state(self, session_id: str) -> dict[str, Any] | None:
        """读取单个会话状态，不存在返回 None。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return _json_loads(row["data"], {})

    def load_all_session_states(self) -> dict[str, dict[str, Any]]:
        """读取全部会话状态，返回 {session_id: state_dict}。"""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT session_id, data FROM session_state").fetchall()
        return {row["session_id"]: _json_loads(row["data"], {}) for row in rows}

    def delete_session_state(self, session_id: str):
        """删除单个会话状态。"""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM session_state WHERE session_id = ?", (session_id,))
            conn.commit()

    def has_session_states(self) -> bool:
        """判断 session_state 表是否有数据（用于迁移判断）。"""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM session_state").fetchone()
        return int(row["c"] or 0) > 0

    # ------------------------------------------------------------------
    # City catalogs (replaces state.json city_place_catalogs)
    # ------------------------------------------------------------------

    def save_city_catalog(self, catalog_key: str, data: dict[str, Any]):
        """保存城市地点目录。"""
        if not catalog_key:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO city_catalogs(catalog_key, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(catalog_key) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (catalog_key, _json_dumps(data), _now()),
            )
            conn.commit()

    def load_city_catalog(self, catalog_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data FROM city_catalogs WHERE catalog_key = ?",
                (catalog_key,),
            ).fetchone()
        if not row:
            return None
        return _json_loads(row["data"], {})

    def load_all_city_catalogs(self) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT catalog_key, data FROM city_catalogs").fetchall()
        return {row["catalog_key"]: _json_loads(row["data"], {}) for row in rows}
