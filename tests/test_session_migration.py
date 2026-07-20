from __future__ import annotations

import json
import unittest

from telegram_comfyui_selfie import TelegramComfyUIService
from tests.support import ServiceFixtureMixin, make_project_temp_dir


class SessionStateMigrationTestCase(ServiceFixtureMixin, unittest.TestCase):
    """state.json -> SQLite 迁移测试。"""

    def test_state_json_migrates_to_sqlite_on_first_load(self):
        import json as _json

        tmp = make_project_temp_dir("state_json")
        config_path = tmp / "config.yml"
        config_path.write_text("telegram:\n  telegram_bot_token: \"t\"\n", encoding="utf-8")
        state_path = tmp / "state.json"

        # 写一个旧版 state.json
        old_state = {
            "sessions": {
                "telegram:42": {
                    "custom_character": "迁移测试角色",
                    "custom_bot_name": "迁移测试",
                    "purity": 7,
                    "sent_photos_history": [],
                    "chat_history": [],
                },
            },
            "city_place_catalogs": {
                "shanghai": {
                    "city": "上海",
                    "updated_at": 1000,
                    "places": {"home": ["家"]},
                    "source": "test",
                },
            },
        }
        state_path.write_text(_json.dumps(old_state, ensure_ascii=False), encoding="utf-8")

        svc = TelegramComfyUIService(config_path, state_path)

        # 迁移后应从 SQLite 读取
        self.assertEqual(svc.sessions["telegram:42"]["custom_character"], "迁移测试角色")
        self.assertEqual(svc.sessions["telegram:42"]["purity"], 7)
        self.assertEqual(svc.city_place_catalogs["shanghai"]["city"], "上海")

        # SQLite 应有数据
        self.assertTrue(svc.app_store.has_session_states())
        sqlite_state = svc.app_store.load_session_state("telegram:42")
        self.assertEqual(sqlite_state["custom_character"], "迁移测试角色")
        self.assertEqual(sqlite_state["character"]["custom_bot_name"], "迁移测试")
        self.assertIn("context", sqlite_state)
        self.assertTrue(list(tmp.glob("state.state-json-migration-backup-*.json")))

        # city_catalogs 也应在 SQLite
        sqlite_catalog = svc.app_store.load_city_catalog("shanghai")
        self.assertEqual(sqlite_catalog["city"], "上海")

    def test_legacy_sqlite_state_boxes_migrate_on_restart_with_backup(self):
        tmp = make_project_temp_dir("sqlite_box_migration")
        config_path = tmp / "config.json"
        state_path = tmp / "state.json"
        config_path.write_text(json.dumps({"telegram_bot_token": "TEST"}, ensure_ascii=False), encoding="utf-8")

        svc = TelegramComfyUIService(config_path, state_path)
        sid = "telegram:box"
        svc.app_store.save_session_state(sid, {
            "custom_bot_name": "旧角色",
            "custom_positive_prefix": "1girl, red eyes",
            "purity": 5,
            "dynamic_appearance": "blue dress",
            "chat_history": [{"role": "user", "content": "旧消息"}],
            "user_place": "home",
            "saved_characters": {"旧角色": {"character": "旧角色"}},
        })
        self.assertFalse(list(tmp.glob("memory.box-migration-backup-*.sqlite3")))

        restarted = TelegramComfyUIService(config_path, state_path)
        loaded = restarted.app_store.load_session_state(sid)
        self.assertEqual(loaded["character"]["custom_bot_name"], "旧角色")
        self.assertEqual(loaded["character"]["custom_positive_prefix"], "1girl, red eyes")
        self.assertEqual(loaded["clothing"]["dynamic_appearance"], "blue dress")
        self.assertEqual(loaded["context"]["chat_history"][0]["content"], "旧消息")
        self.assertEqual(loaded["place"]["user_place"], "home")
        self.assertEqual(loaded["session"]["saved_characters"]["旧角色"]["character"], "旧角色")
        backups = list(tmp.glob("memory.box-migration-backup-*.sqlite3"))
        self.assertEqual(len(backups), 1)

        TelegramComfyUIService(config_path, state_path)
        self.assertEqual(len(list(tmp.glob("memory.box-migration-backup-*.sqlite3"))), 1)

    def test_save_and_load_session_state_via_sqlite(self):
        svc = self.make_service()
        sid = "telegram:99"
        state = svc._get_session_state(sid)
        state["custom_character"] = "SQLite测试"
        state["purity"] = 3
        svc._save_session_state(sid, state)

        # 重新加载
        svc2 = TelegramComfyUIService(svc.config_path, svc.state_path)
        loaded = svc2._get_session_state(sid)
        self.assertEqual(loaded["custom_character"], "SQLite测试")
        self.assertEqual(loaded["purity"], 3)

    def test_delete_session_removes_from_sqlite(self):
        svc = self.make_service()
        sid = "telegram:77"
        state = svc._get_session_state(sid)
        state["custom_character"] = "待删除"
        svc._save_session_state(sid, state)
        self.assertIsNotNone(svc.app_store.load_session_state(sid))

        svc.sessions.pop(sid, None)
        svc.app_store.delete_session_state(sid)
        self.assertIsNone(svc.app_store.load_session_state(sid))

    def test_city_catalog_persists_to_sqlite(self):
        svc = self.make_service()
        svc._store_city_catalog("beijing", "北京", {"home": ["家"], "park": ["朝阳公园"]}, "test")
        # 内存中有
        self.assertEqual(svc.city_place_catalogs["beijing"]["city"], "北京")
        # SQLite 中也有
        catalog = svc.app_store.load_city_catalog("beijing")
        self.assertEqual(catalog["city"], "北京")
        self.assertEqual(catalog["places"]["park"], ["朝阳公园"])

        # 重新加载
        svc2 = TelegramComfyUIService(svc.config_path, svc.state_path)
        self.assertEqual(svc2.city_place_catalogs["beijing"]["city"], "北京")
