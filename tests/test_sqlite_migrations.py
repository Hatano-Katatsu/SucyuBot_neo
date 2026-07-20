from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from telegram_comfyui_selfie.app_store import AppStateStore
from telegram_comfyui_selfie.memory import LongTermMemoryStore
from telegram_comfyui_selfie.sqlite_migrations import (
    LATEST_SCHEMA_VERSION,
    SchemaMigrationError,
    migrate_database,
)
from tests.support import make_project_temp_dir


def _user_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0] if row else 0)


def _columns(path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


class SQLiteMigrationTestCase(unittest.TestCase):
    def test_fresh_database_reaches_latest_version_without_backup(self):
        root = make_project_temp_dir("sqlite_migration_fresh")
        path = root / "memory.sqlite3"

        memory = LongTermMemoryStore(path)
        app_store = AppStateStore(path)

        self.assertEqual(memory.schema_migration.previous_version, 0)
        self.assertEqual(
            memory.schema_migration.applied_versions,
            tuple(range(1, LATEST_SCHEMA_VERSION + 1)),
        )
        self.assertIsNone(memory.schema_migration.backup_path)
        self.assertEqual(app_store.schema_migration.applied_versions, ())
        self.assertEqual(_user_version(path), LATEST_SCHEMA_VERSION)
        self.assertFalse(list(root.glob("*.schema-migration-*-backup-*.sqlite3")))

    def test_unversioned_legacy_database_is_upgraded_and_preserved(self):
        root = make_project_temp_dir("sqlite_migration_legacy")
        path = root / "memory.sqlite3"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """
                CREATE TABLE memories (
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
                """
            )
            conn.execute(
                """
                INSERT INTO memories(
                    session_id, kind, summary, tags, importance, source,
                    status, created_at, updated_at, hit_count
                ) VALUES ('telegram:old', 'event', '旧记忆', '[]', 3, '', 'active', 1, 1, 0)
                """
            )
            conn.commit()

        memory = LongTermMemoryStore(path)

        self.assertEqual(memory.schema_migration.previous_version, 0)
        self.assertEqual(_user_version(path), LATEST_SCHEMA_VERSION)
        self.assertIn("character", _columns(path, "memories"))
        self.assertIn("payload", _columns(path, "telegram_update_inbox"))
        self.assertIn("value", _columns(path, "app_metadata"))
        records = memory.list_memories("telegram:old", character="")
        self.assertEqual([item["summary"] for item in records], ["旧记忆"])
        backup = memory.schema_migration.backup_path
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertEqual(_user_version(backup), 0)
        self.assertNotIn("character", _columns(backup, "memories"))

        # 同库的 AppStateStore 能直接使用统一后的 schema，重复启动不再产生备份。
        before = set(root.glob("*.schema-migration-*-backup-*.sqlite3"))
        app_store = AppStateStore(path)
        app_store.save_session_state("telegram:old", {"custom_character": "旧角色"})
        self.assertEqual(app_store.load_session_state("telegram:old")["custom_character"], "旧角色")
        self.assertEqual(before, set(root.glob("*.schema-migration-*-backup-*.sqlite3")))

    def test_each_historical_user_version_upgrades_in_order(self):
        for historical_version in range(1, LATEST_SCHEMA_VERSION):
            with self.subTest(historical_version=historical_version):
                root = make_project_temp_dir(f"sqlite_migration_v{historical_version}")
                path = root / "memory.sqlite3"
                migrate_database(path, target_version=historical_version)
                with closing(sqlite3.connect(path)) as conn:
                    memory_columns = _columns(path, "memories")
                    if "character" in memory_columns:
                        conn.execute(
                            """
                            INSERT INTO memories(
                                session_id, character, kind, summary, tags, importance,
                                source, status, created_at, updated_at, hit_count
                            ) VALUES (?, '', 'event', '版本记忆', '[]', 3, '', 'active', 1, 1, 0)
                            """,
                            (f"telegram:v{historical_version}",),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO memories(
                                session_id, kind, summary, tags, importance,
                                source, status, created_at, updated_at, hit_count
                            ) VALUES (?, 'event', '版本记忆', '[]', 3, '', 'active', 1, 1, 0)
                            """,
                            (f"telegram:v{historical_version}",),
                        )
                    conn.execute(
                        """
                        INSERT INTO context_meta(
                            session_id, character_key, last_dream_at, last_dream_message_id,
                            last_checkpoint_at, last_checkpoint_message_id
                        ) VALUES (?, '', 0, 0, 0, 0)
                        """,
                        (f"telegram:v{historical_version}",),
                    )
                    conn.execute(
                        """
                        INSERT INTO user_model_settings(
                            user_id, chat_profile_id, fast_profile_id,
                            chat_thinking, fast_thinking, updated_at
                        ) VALUES (?, 'chat', 'fast', NULL, NULL, 1)
                        """,
                        (f"v{historical_version}",),
                    )
                    conn.commit()

                app_store = AppStateStore(path)

                self.assertEqual(app_store.schema_migration.previous_version, historical_version)
                self.assertEqual(
                    app_store.schema_migration.applied_versions,
                    tuple(range(historical_version + 1, LATEST_SCHEMA_VERSION + 1)),
                )
                self.assertEqual(_user_version(path), LATEST_SCHEMA_VERSION)
                self.assertIn("character", _columns(path, "memories"))
                self.assertIn("character_history_summary", _columns(path, "context_meta"))
                self.assertIn("vision_profile_id", _columns(path, "user_model_settings"))
                self.assertIn("payload", _columns(path, "telegram_update_inbox"))
                settings = app_store.get_user_model_settings(f"v{historical_version}")
                self.assertEqual(settings["chat_profile_id"], "chat")
                self.assertEqual(settings["vision_profile_id"], "")

                backup = app_store.schema_migration.backup_path
                self.assertIsNotNone(backup)
                self.assertEqual(_user_version(backup), historical_version)
                if historical_version < 2:
                    self.assertNotIn("character", _columns(backup, "memories"))
                if historical_version < 3:
                    self.assertNotIn("character_history_summary", _columns(backup, "context_meta"))
                if historical_version < 4:
                    self.assertNotIn("vision_profile_id", _columns(backup, "user_model_settings"))
                if historical_version < 5:
                    self.assertEqual(_columns(backup, "telegram_update_inbox"), set())

    def test_invalid_legacy_schema_rolls_back_and_keeps_recoverable_backup(self):
        root = make_project_temp_dir("sqlite_migration_rollback")
        path = root / "memory.sqlite3"
        with closing(sqlite3.connect(path)) as conn:
            # 同名表缺少已知历史版本的必需列，最终校验必须让整条升级链回滚。
            conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, marker TEXT NOT NULL)")
            conn.execute("INSERT INTO memories(id, marker) VALUES (1, 'keep-me')")
            conn.commit()

        with self.assertLogs("telegram_comfyui_selfie.sqlite_migrations", level="ERROR"):
            with self.assertRaises(SchemaMigrationError):
                LongTermMemoryStore(path)

        self.assertEqual(_user_version(path), 0)
        self.assertEqual(_columns(path, "memories"), {"id", "marker"})
        self.assertEqual(_columns(path, "context_meta"), set())
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT marker FROM memories WHERE id = 1").fetchone()[0], "keep-me")

        backups = list(root.glob(
            f"*.schema-migration-v0-to-v{LATEST_SCHEMA_VERSION}-backup-*.sqlite3"
        ))
        self.assertEqual(len(backups), 1)
        backup = backups[0]
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT marker FROM memories WHERE id = 1").fetchone()[0], "keep-me")


if __name__ == "__main__":
    unittest.main()
