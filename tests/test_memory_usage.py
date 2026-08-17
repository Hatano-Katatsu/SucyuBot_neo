from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import unittest
from contextlib import closing

from tests.support import ServiceFixtureMixin


class MemoryImportanceAnchorTestCase(ServiceFixtureMixin, unittest.TestCase):
    """重要性打分锚点回归：提取/整理 prompt 必须带 1-5 分锚点，整理口径与提取一致。"""

    def test_extract_prompt_contains_importance_anchors(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            captured = {}

            async def fake_call_llm(system, user, **kw):
                captured["system"] = system
                return json.dumps({"memories": []}, ensure_ascii=False)

            svc._call_llm = fake_call_llm
            svc.has_llm_config = lambda purpose, session_id="": True

            await svc._extract_long_term_memories(sid, "今天午饭吃了拉面", "好吃吗？", character="角色A")

            system = captured.get("system") or ""
            self.assertIn("importance 打分锚点", system)
            self.assertIn("转瞬即逝的日常细节", system)
            self.assertIn("边界、纠正、重大事件", system)

        asyncio.run(run())

    def test_incremental_organize_prompt_has_anchors_and_unused_days(self):
        async def run():
            svc = self.make_service()
            sid = "telegram:1"
            key = "角色A"
            never_used = svc.memory.add_memory(sid, "preference", "喜欢吃草莓", character=key, importance=3)
            used = svc.memory.add_memory(sid, "event", "上周一起去看了电影", character=key, importance=4)
            svc.memory.touch_memories(sid, [used], character=key)
            captured = {}

            async def fake_call_llm(system, user, **kw):
                captured["system"] = system
                captured["user"] = user
                return json.dumps({"ops": []}, ensure_ascii=False)

            svc._call_llm = fake_call_llm
            svc.has_llm_config = lambda purpose, session_id="": True

            editable = svc.memory.list_memories(sid, character=key, limit=10)
            result = await svc._incremental_organize_memories(sid, key, editable, diaries=[])

            self.assertEqual(result.get("status"), "no_op")
            system = captured.get("system") or ""
            self.assertIn("Importance scoring anchors", system)
            self.assertIn("deletion candidates", system)
            user = captured.get("user") or ""
            # 从未使用的记忆标注 never；被 touch 过的记忆给出天数
            self.assertIn(f"{never_used}. [preference/importance=3/unused_days=never/", user)
            self.assertIn(f"{used}. [event/importance=4/unused_days=0/", user)

        asyncio.run(run())


class MemoryOrganizeSkipTestCase(ServiceFixtureMixin, unittest.TestCase):
    """事件驱动整理回归：上次整理后零写入跳过，首次运行与新写入不跳过。"""

    def _prepare(self):
        svc = self.make_service()
        sid = "telegram:1"
        key = "角色A"
        svc.has_llm_config = lambda purpose, session_id="": True
        calls = []

        async def fake_call_llm(system, user, **kw):
            calls.append(kw.get("tag"))
            return json.dumps({"ops": []}, ensure_ascii=False)

        svc._call_llm = fake_call_llm
        return svc, sid, key, calls

    def test_first_run_organizes_and_records_watermark(self):
        async def run():
            svc, sid, key, calls = self._prepare()
            svc.memory.add_memory(sid, "event", "首次整理的记忆", character=key)

            result = await svc._organize_memories_after_dream(sid, key)

            self.assertEqual(result.get("status"), "no_op")
            self.assertTrue(calls, "首次运行（无水位记录）必须正常整理")
            meta = svc.app_store.get_context_meta(sid, key)
            self.assertGreater(float(meta.get("last_memory_organize_watermark") or 0), 0)

        asyncio.run(run())

    def test_no_changes_after_organize_skips_llm(self):
        async def run():
            svc, sid, key, calls = self._prepare()
            svc.memory.add_memory(sid, "event", "已整理过的记忆", character=key)

            first = await svc._organize_memories_after_dream(sid, key)
            self.assertEqual(first.get("status"), "no_op")
            self.assertTrue(calls)
            calls.clear()

            second = await svc._organize_memories_after_dream(sid, key)

            self.assertEqual(second.get("status"), "skipped")
            self.assertEqual(second.get("reason"), "no_memory_changes")
            self.assertFalse(calls, "零写入时不得再调 LLM 整理")

        asyncio.run(run())

    def test_new_write_after_organize_reruns(self):
        async def run():
            svc, sid, key, calls = self._prepare()
            svc.memory.add_memory(sid, "event", "第一条记忆", character=key)

            first = await svc._organize_memories_after_dream(sid, key)
            self.assertEqual(first.get("status"), "no_op")
            calls.clear()

            svc.memory.add_memory(sid, "preference", "整理后新写入的记忆", character=key)
            second = await svc._organize_memories_after_dream(sid, key)

            self.assertEqual(second.get("status"), "no_op")
            self.assertTrue(calls, "整理后有新写入时必须重新整理")

        asyncio.run(run())


class MemoryUsageStoreTestCase(ServiceFixtureMixin, unittest.TestCase):
    """last_used_at 回归：touch 只写使用时间戳、不失效读缓存；排序按最近被想起。"""

    def test_touch_memories_updates_last_used_at_without_evicting_cache(self):
        svc = self.make_service()
        sid = "telegram:1"
        mid = svc.memory.add_memory(sid, "event", "被想起的记忆", character="角色A")
        cached = svc.memory.context_memories(sid, character="角色A", limit=8)
        self.assertIsNone(cached[0]["last_used_at"])
        before_updated = cached[0]["updated_at"]

        updated = svc.memory.touch_memories(sid, [mid], character="角色A")

        self.assertEqual(updated, 1)
        row = svc.memory.list_memories(sid, character="角色A", limit=8)[0]
        self.assertIsNotNone(row["last_used_at"])
        self.assertGreater(row["last_used_at"], 0)
        # touch 不改内容时间戳，避免干扰整理水位判断
        self.assertEqual(row["updated_at"], before_updated)
        # 内容未变，读缓存不失效：context_memories 仍返回同一缓存对象
        self.assertIs(svc.memory.context_memories(sid, character="角色A", limit=8), cached)

    def test_list_memories_prefers_recently_used_on_importance_tie(self):
        svc = self.make_service()
        sid = "telegram:1"
        old_used = svc.memory.add_memory(sid, "preference", "较早更新但刚被想起", character="角色A", importance=3)
        recent = svc.memory.add_memory(sid, "preference", "较新更新但久未使用", character="角色A", importance=3)
        now = time.time()
        # 固定时间戳：第一条 updated_at 更旧但 last_used_at 更新
        with closing(sqlite3.connect(svc.memory.path)) as conn:
            conn.execute(
                "UPDATE memories SET updated_at = ?, last_used_at = ? WHERE id = ?",
                (now - 86400, now - 60, old_used),
            )
            conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?",
                (now - 3600, recent),
            )
            conn.commit()

        ordered = svc.memory.list_memories(sid, character="角色A", limit=8)

        self.assertEqual([m["id"] for m in ordered], [old_used, recent])

    def test_long_term_memory_context_touches_injected_ids(self):
        svc = self.make_service()
        sid = "telegram:1"
        state = svc._get_session_state(sid)
        state["custom_character"] = "角色A"
        svc._save_session_state(sid, state)
        svc.memory.add_memory(sid, "preference", "喜欢草莓", character="角色A")

        text = svc._long_term_memory_context(sid)

        self.assertIn("喜欢草莓", text)
        first_used = svc.memory.list_memories(sid, character="角色A", limit=8)[0]["last_used_at"]
        self.assertIsNotNone(first_used)

        # 第二次注入命中读缓存，但注入层仍应刷新 last_used_at
        time.sleep(0.02)
        svc._long_term_memory_context(sid)
        second_used = svc.memory.list_memories(sid, character="角色A", limit=8)[0]["last_used_at"]
        self.assertGreater(second_used, first_used)


if __name__ == "__main__":
    unittest.main()
