from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


logger = logging.getLogger(__name__)

ConnectionFactory = Callable[[], sqlite3.Connection]
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


@dataclass(frozen=True)
class SchemaMigrationResult:
    """一次数据库 schema 检查或升级的结果。"""

    previous_version: int
    current_version: int
    applied_versions: tuple[int, ...] = ()
    backup_path: Path | None = None


class SchemaMigrationError(RuntimeError):
    """数据库版本或结构不符合可安全迁移的约束。"""


def _execute(conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        conn.execute(statement)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")')}


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if column in _table_columns(conn, table):
        return
    escaped = table.replace('"', '""')
    conn.execute(f'ALTER TABLE "{escaped}" ADD COLUMN {definition}')


def _migration_v1_base_schema(conn: sqlite3.Connection) -> None:
    """建立最初的合并状态库结构，保留当时尚未分角色的记忆表。"""

    _execute(
        conn,
        (
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                importance INTEGER NOT NULL DEFAULT 3,
                source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_used_at REAL,
                hit_count INTEGER NOT NULL DEFAULT 0
            )
            """,
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
            """,
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_scope "
            "ON chat_messages(session_id, character_key, id)",
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
            """,
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
            """,
            """
            CREATE TABLE IF NOT EXISTS life_plans (
                session_id TEXT NOT NULL,
                character_key TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(session_id, character_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS context_meta (
                session_id TEXT NOT NULL,
                character_key TEXT NOT NULL DEFAULT '',
                last_dream_at REAL NOT NULL DEFAULT 0,
                last_dream_message_id INTEGER NOT NULL DEFAULT 0,
                last_checkpoint_at REAL NOT NULL DEFAULT 0,
                last_checkpoint_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, character_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS web_credentials (
                user_id TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL DEFAULT '',
                access_token TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS model_profiles (
                user_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, profile_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_model_settings (
                user_id TEXT PRIMARY KEY,
                chat_profile_id TEXT NOT NULL DEFAULT '',
                fast_profile_id TEXT NOT NULL DEFAULT '',
                chat_thinking INTEGER,
                fast_thinking INTEGER,
                updated_at REAL NOT NULL
            )
            """,
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
            """,
            "CREATE INDEX IF NOT EXISTS idx_llm_usage_scope "
            "ON llm_usage(created_at, profile_id, model, purpose, tag)",
            """
            CREATE TABLE IF NOT EXISTS session_state (
                session_id TEXT PRIMARY KEY,
                data TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS city_catalogs (
                catalog_key TEXT PRIMARY KEY,
                data TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            )
            """,
        ),
    )


def _migration_v2_character_memories(conn: sqlite3.Connection) -> None:
    """长期记忆增加角色隔离，并补齐对应查询索引。"""

    _add_column_if_missing(
        conn,
        "memories",
        "character",
        "character TEXT NOT NULL DEFAULT ''",
    )
    _execute(
        conn,
        (
            "CREATE INDEX IF NOT EXISTS idx_memories_session_char_status "
            "ON memories(session_id, character, status)",
            "CREATE INDEX IF NOT EXISTS idx_memories_kind "
            "ON memories(session_id, character, kind)",
            "CREATE INDEX IF NOT EXISTS idx_memories_updated "
            "ON memories(session_id, character, updated_at)",
        ),
    )


def _migration_v3_character_history(conn: sqlite3.Connection) -> None:
    """上下文元数据增加角色历史摘要。"""

    _add_column_if_missing(
        conn,
        "context_meta",
        "character_history_summary",
        "character_history_summary TEXT NOT NULL DEFAULT ''",
    )


def _migration_v4_vision_profile(conn: sqlite3.Connection) -> None:
    """用户模型设置增加视觉模型 profile。"""

    _add_column_if_missing(
        conn,
        "user_model_settings",
        "vision_profile_id",
        "vision_profile_id TEXT NOT NULL DEFAULT ''",
    )


SCHEMA_MIGRATIONS: tuple[Migration, ...] = (
    (1, "base_schema", _migration_v1_base_schema),
    (2, "character_memories", _migration_v2_character_memories),
    (3, "character_history", _migration_v3_character_history),
    (4, "vision_profile", _migration_v4_vision_profile),
)
LATEST_SCHEMA_VERSION = SCHEMA_MIGRATIONS[-1][0]


_BASE_EXPECTED_COLUMNS: dict[str, set[str]] = {
    "memories": {
        "id", "session_id", "kind", "summary", "tags", "importance", "source",
        "status", "created_at", "updated_at", "last_used_at", "hit_count",
    },
    "chat_messages": {
        "id", "session_id", "user_id", "character_key", "role", "content",
        "created_at", "checkpointed",
    },
    "checkpoints": {
        "session_id", "character_key", "summary", "source_until_id", "updated_at", "version",
    },
    "diaries": {
        "id", "session_id", "character_key", "diary_date", "content",
        "from_message_id", "to_message_id", "updated_at",
    },
    "life_plans": {"session_id", "character_key", "payload", "updated_at"},
    "context_meta": {
        "session_id", "character_key", "last_dream_at", "last_dream_message_id",
        "last_checkpoint_at", "last_checkpoint_message_id",
    },
    "web_credentials": {"user_id", "password_hash", "access_token", "updated_at"},
    "model_profiles": {"user_id", "profile_id", "data", "updated_at"},
    "user_model_settings": {
        "user_id", "chat_profile_id", "fast_profile_id", "chat_thinking",
        "fast_thinking", "updated_at",
    },
    "llm_usage": {
        "id", "created_at", "profile_id", "model", "purpose", "tag", "session_id",
        "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens",
    },
    "session_state": {"session_id", "data", "updated_at"},
    "city_catalogs": {"catalog_key", "data", "updated_at"},
}


def _validate_schema(conn: sqlite3.Connection, version: int) -> None:
    missing: list[str] = []
    for table, expected in _BASE_EXPECTED_COLUMNS.items():
        columns = _table_columns(conn, table)
        absent = expected - columns
        if not columns:
            missing.append(f"table:{table}")
        elif absent:
            missing.append(f"{table}:[{','.join(sorted(absent))}]")

    if version >= 2 and "character" not in _table_columns(conn, "memories"):
        missing.append("memories:[character]")
    if version >= 3 and "character_history_summary" not in _table_columns(conn, "context_meta"):
        missing.append("context_meta:[character_history_summary]")
    if version >= 4 and "vision_profile_id" not in _table_columns(conn, "user_model_settings"):
        missing.append("user_model_settings:[vision_profile_id]")
    if missing:
        raise SchemaMigrationError("SQLite schema 校验失败：" + "; ".join(missing))


def _read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0] if row else 0)


def _has_existing_schema(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
          AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def _next_backup_path(path: Path, from_version: int, to_version: int) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = path.suffix or ".sqlite3"
    base = f"{path.stem}.schema-migration-v{from_version}-to-v{to_version}-backup-{stamp}"
    candidate = path.with_name(f"{base}{suffix}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{base}-{counter}{suffix}")
        counter += 1
    return candidate


def _backup_database(
    conn: sqlite3.Connection,
    path: Path,
    from_version: int,
    to_version: int,
) -> Path:
    backup_path = _next_backup_path(path, from_version, to_version)
    try:
        with closing(sqlite3.connect(backup_path)) as destination:
            conn.backup(destination)
            check = destination.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise SchemaMigrationError("SQLite migration 备份完整性检查失败")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def migrate_database(
    path: str | Path,
    *,
    connection_factory: ConnectionFactory | None = None,
    target_version: int | None = None,
) -> SchemaMigrationResult:
    """按顺序在单个事务内升级共享 SQLite 状态库，并在升级前做在线备份。"""

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.exists() and db_path.stat().st_size > 0
    factory = connection_factory or (lambda: sqlite3.connect(db_path, timeout=10))
    target = LATEST_SCHEMA_VERSION if target_version is None else int(target_version)
    known_versions = {version for version, _name, _migration in SCHEMA_MIGRATIONS}
    if target < 1 or target not in known_versions:
        raise SchemaMigrationError(f"未知 SQLite 目标版本：{target}")

    with closing(factory()) as conn:
        previous = _read_user_version(conn)
        if previous > LATEST_SCHEMA_VERSION:
            raise SchemaMigrationError(
                f"数据库版本 {previous} 高于程序支持的 {LATEST_SCHEMA_VERSION}，拒绝降级"
            )
        if previous > target:
            raise SchemaMigrationError(f"数据库版本 {previous} 高于目标版本 {target}，拒绝降级")
        if previous == target:
            _validate_schema(conn, target)
            return SchemaMigrationResult(previous, previous)

        backup_path = None
        if existed and (previous > 0 or _has_existing_schema(conn)):
            backup_path = _backup_database(conn, db_path, previous, target)

        applied: list[int] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            locked_version = _read_user_version(conn)
            if locked_version > target:
                raise SchemaMigrationError(
                    f"加锁后数据库版本 {locked_version} 高于目标版本 {target}，拒绝降级"
                )
            for version, name, migration in SCHEMA_MIGRATIONS:
                if locked_version < version <= target:
                    migration(conn)
                    conn.execute(f"PRAGMA user_version = {version}")
                    applied.append(version)
                    logger.debug("已应用 SQLite migration v%s (%s)", version, name)
            _validate_schema(conn, target)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.exception(
                "SQLite migration v%s -> v%s 失败，事务已回滚；备份=%s",
                previous,
                target,
                backup_path or "无（新库）",
            )
            if isinstance(exc, SchemaMigrationError):
                raise
            raise SchemaMigrationError(
                f"SQLite migration v{previous} -> v{target} 失败，事务已回滚"
            ) from exc

    if backup_path:
        logger.info(
            "SQLite schema 已从 v%s 升级到 v%s，升级前备份：%s",
            previous,
            target,
            backup_path,
        )
    return SchemaMigrationResult(previous, target, tuple(applied), backup_path)
