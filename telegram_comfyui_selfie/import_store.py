"""导入草稿和世界背景的有作用域 SQLite 存储。"""
from __future__ import annotations

import json
import time
from contextlib import closing
from typing import Any


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ImportStoreMixin:
    def find_import_cache(self, session_id: str, cache_key: str) -> dict[str, Any] | None:
        """只复用同一会话、未过期且未编辑的转换底稿。"""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT draft_id,data,status FROM import_drafts WHERE session_id=? AND expires_at>? AND status IN ('pending','ready','committed') ORDER BY updated_at DESC LIMIT 50", (session_id, time.time())).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            if data.get("cache_key") == cache_key:
                return {"id": row["draft_id"], "data": data, "status": row["status"]}
        return None

    def get_world_profile(self, user_id: str, world_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM world_profiles WHERE world_id=? AND user_id=?", (world_id, user_id)).fetchone()
        if not row:
            return None
        return {"id": row["world_id"], "revision": row["revision"], "data": json.loads(row["data"])}

    def list_world_profiles(self, user_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM world_profiles WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [{"id": r["world_id"], "revision": r["revision"], "data": json.loads(r["data"])} for r in rows]

    def update_world_profile(self, user_id: str, world_id: str, data: dict[str, Any], revision: int) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute("UPDATE world_profiles SET data=?,revision=revision+1,updated_at=? WHERE world_id=? AND user_id=? AND revision=?",
                                  (_dump(data), time.time(), world_id, user_id, revision))
            conn.commit()
        return cursor.rowcount == 1

    def create_import_draft(self, draft_id: str, session_id: str, data: dict[str, Any]) -> None:
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM import_drafts WHERE expires_at<?", (now,))
            conn.execute("INSERT INTO import_drafts(draft_id,session_id,status,data,expires_at,updated_at) VALUES (?,?,'pending',?,?,?)",
                         (draft_id, session_id, _dump(data), now + 86400, now))
            conn.commit()

    def get_import_draft(self, session_id: str, draft_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM import_drafts WHERE draft_id=? AND session_id=? AND expires_at>?", (draft_id, session_id, time.time())).fetchone()
        if not row:
            return None
        return {"id": row["draft_id"], "status": row["status"], "data": json.loads(row["data"]), "result": json.loads(row["result"]), "updated_at": row["updated_at"]}

    def update_import_draft(self, session_id: str, draft_id: str, data: dict[str, Any], status: str) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute("UPDATE import_drafts SET data=?,status=?,updated_at=? WHERE draft_id=? AND session_id=? AND status!='committed' AND expires_at>?",
                                  (_dump(data), status, time.time(), draft_id, session_id, time.time()))
            conn.commit()
        return cursor.rowcount == 1

    def commit_import_draft(self, session_id: str, draft_id: str, user_id: str, state: dict[str, Any], world_id: str,
                            world: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """角色、世界和成功标记在同一事务提交；重复请求返回原结果。"""
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,result FROM import_drafts WHERE draft_id=? AND session_id=? AND expires_at>?", (draft_id, session_id, now)).fetchone()
            if not row:
                raise ValueError("导入草稿不存在或已过期")
            if row["status"] == "committed":
                return json.loads(row["result"])
            if row["status"] != "ready":
                raise ValueError("草稿尚未转换成功")
            if world_id:
                conn.execute("INSERT INTO world_profiles(world_id,user_id,data,updated_at) VALUES (?,?,?,?)", (world_id, user_id, _dump(world), now))
            conn.execute("INSERT INTO session_state(session_id,data,updated_at) VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                         (session_id, _dump(state), now))
            conn.execute("UPDATE import_drafts SET status='committed',result=?,updated_at=? WHERE draft_id=?", (_dump(result), now, draft_id))
            conn.commit()
        return result
