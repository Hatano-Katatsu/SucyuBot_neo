from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


STABLE_KINDS = {"profile", "preference", "relationship", "setting", "boundary", "visual"}
VALID_KINDS = STABLE_KINDS | {"event", "correction", "manual"}
KIND_ALIASES = {
    "资料": "profile",
    "用户资料": "profile",
    "偏好": "preference",
    "喜好": "preference",
    "关系": "relationship",
    "关系状态": "relationship",
    "设定": "setting",
    "世界设定": "setting",
    "边界": "boundary",
    "禁忌": "boundary",
    "外观": "visual",
    "视觉": "visual",
    "穿搭": "visual",
    "事件": "event",
    "纠正": "correction",
}


def normalize_kind(kind: str) -> str:
    kind = (kind or "").strip().lower()
    kind = KIND_ALIASES.get(kind, kind)
    return kind if kind in VALID_KINDS else "event"


def clamp_importance(value: Any) -> int:
    try:
        n = int(value)
    except Exception:
        n = 3
    return max(1, min(5, n))


def normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        raw = re.split(r"[,，\s]+", tags)
    elif isinstance(tags, list):
        raw = [str(item) for item in tags]
    else:
        raw = []
    result: list[str] = []
    seen = set()
    for item in raw:
        tag = item.strip()
        if not tag:
            continue
        key = tag.lower()
        if key not in seen:
            result.append(tag[:40])
            seen.add(key)
    return result[:12]


def tokenize_query(query: str) -> list[str]:
    query = (query or "").strip().lower()
    if not query:
        return []
    terms: list[str] = []
    terms.extend(re.findall(r"[a-z0-9_@.-]{2,}", query))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        if len(run) <= 12:
            terms.append(run)
        for size in (2, 3, 4):
            for i in range(0, max(0, len(run) - size + 1)):
                terms.append(run[i:i + size])
    seen = set()
    deduped = []
    for term in terms:
        if term not in seen:
            deduped.append(term)
            seen.add(term)
    return deduped[:40]


class LongTermMemoryStore:
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
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character TEXT NOT NULL DEFAULT '',
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
            # 迁移：旧库没有 character 列时补上。既有记忆归入默认角色（空串）。
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(memories)")}
            if "character" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN character TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_session_char_status ON memories(session_id, character, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(session_id, character, kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(session_id, character, updated_at)")
            conn.commit()

    def add_memory(
        self,
        session_id: str,
        kind: str,
        summary: str,
        *,
        character: str = "",
        importance: Any = 3,
        tags: Any = None,
        source: str = "",
    ) -> int | None:
        summary = (summary or "").strip()
        if not session_id or not summary:
            return None
        character = (character or "").strip()
        kind = normalize_kind(kind)
        importance = clamp_importance(importance)
        tag_list = normalize_tags(tags)
        now = time.time()
        with closing(self._connect()) as conn:
            existing = conn.execute(
                """
                SELECT id, tags, importance FROM memories
                WHERE session_id = ? AND character = ? AND kind = ? AND status = 'active' AND lower(summary) = lower(?)
                LIMIT 1
                """,
                (session_id, character, kind, summary),
            ).fetchone()
            if existing:
                merged_tags = normalize_tags(json.loads(existing["tags"] or "[]") + tag_list)
                conn.execute(
                    """
                    UPDATE memories
                    SET tags = ?, importance = ?, source = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(merged_tags, ensure_ascii=False),
                        max(int(existing["importance"]), importance),
                        source or "",
                        now,
                        int(existing["id"]),
                    ),
                )
                conn.commit()
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO memories(session_id, character, kind, summary, tags, importance, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    session_id,
                    character,
                    kind,
                    summary[:600],
                    json.dumps(tag_list, ensure_ascii=False),
                    importance,
                    (source or "")[:800],
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    @staticmethod
    def _scope_clause(session_id: str, character: str | None) -> tuple[str, list[Any]]:
        clause = "session_id = ?"
        params: list[Any] = [session_id]
        if character is not None:
            clause += " AND character = ?"
            params.append(character)
        return clause, params

    def list_memories(self, session_id: str, *, character: str | None = None, limit: int = 20, include_inactive: bool = False) -> list[dict[str, Any]]:
        scope, params = self._scope_clause(session_id, character)
        status_sql = "" if include_inactive else "AND status = 'active'"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memories
                WHERE {scope} {status_sql}
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def search_memories(self, session_id: str, query: str, *, character: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memories
                WHERE {scope} AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 300
                """,
                tuple(params),
            ).fetchall()
        memories = [self._row_to_dict(row) for row in rows]
        terms = tokenize_query(query)
        if not terms:
            return sorted(memories, key=self._base_score, reverse=True)[:limit]
        scored = [(self._score_memory(memory, terms), memory) for memory in memories]
        scored = [(score, memory) for score, memory in scored if score > 0]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [memory for _, memory in scored[:limit]]

    def context_memories(self, session_id: str, query: str, *, character: str | None = None, limit: int = 8, stable_limit: int = 4) -> list[dict[str, Any]]:
        return self.list_memories(session_id, character=character, limit=limit)

    def mark_used(self, ids: list[int]):
        return


    def update_memory(self, session_id: str, memory_id: int, *, character: str | None = None, summary: str | None = None, kind: str | None = None, importance: Any = None, tags: Any = None, source: str | None = None) -> bool:
        scope, params = self._scope_clause(session_id, character)
        fields = []
        values: list[Any] = []
        if summary is not None:
            fields.append("summary = ?")
            values.append(str(summary or "").strip()[:600])
        if kind is not None:
            fields.append("kind = ?")
            values.append(normalize_kind(kind))
        if importance is not None:
            fields.append("importance = ?")
            values.append(clamp_importance(importance))
        if tags is not None:
            fields.append("tags = ?")
            values.append(json.dumps(normalize_tags(tags), ensure_ascii=False))
        if source is not None:
            fields.append("source = ?")
            values.append(str(source or "")[:800])
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.append(time.time())
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT kind FROM memories WHERE {scope} AND id = ? AND status = 'active'",
                (*params, int(memory_id)),
            ).fetchone()
            if not row or row["kind"] == "manual":
                return False
            cur = conn.execute(
                f"UPDATE memories SET {', '.join(fields)} WHERE {scope} AND id = ? AND status = 'active'",
                (*values, *params, int(memory_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def edit_memory(self, session_id: str, memory_id: int, *, character: str | None = None, summary: str | None = None, kind: str | None = None, importance: Any = None, tags: Any = None, source: str | None = None) -> bool:
        """用户显式编辑记忆时使用；允许修改 manual 记忆。"""
        scope, params = self._scope_clause(session_id, character)
        fields = []
        values: list[Any] = []
        if summary is not None:
            fields.append("summary = ?")
            values.append(str(summary or "").strip()[:600])
        if kind is not None:
            fields.append("kind = ?")
            values.append(normalize_kind(kind))
        if importance is not None:
            fields.append("importance = ?")
            values.append(clamp_importance(importance))
        if tags is not None:
            fields.append("tags = ?")
            values.append(json.dumps(normalize_tags(tags), ensure_ascii=False))
        if source is not None:
            fields.append("source = ?")
            values.append(str(source or "")[:800])
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.append(time.time())
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE memories SET {', '.join(fields)} WHERE {scope} AND id = ? AND status = 'active'",
                (*values, *params, int(memory_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def deactivate_non_manual_memory(self, session_id: str, memory_id: int, *, character: str | None = None) -> bool:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE memories SET status = 'deleted', updated_at = ? WHERE {scope} AND id = ? AND status = 'active' AND kind <> 'manual'",
                (time.time(), *params, int(memory_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def deactivate_memory(self, session_id: str, memory_id: int, *, character: str | None = None) -> bool:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE memories SET status = 'deleted', updated_at = ? WHERE {scope} AND id = ? AND status = 'active'",
                (time.time(), *params, int(memory_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def clear_session(self, session_id: str, *, character: str | None = None) -> int:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE memories SET status = 'deleted', updated_at = ? WHERE {scope} AND status = 'active'",
                (time.time(), *params),
            )
            conn.commit()
            return int(cur.rowcount)

    def count_active(self, session_id: str, *, character: str | None = None) -> int:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM memories WHERE {scope} AND status = 'active'",
                tuple(params),
            ).fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.get("tags") or "[]")
        except Exception:
            item["tags"] = []
        return item

    @staticmethod
    def _base_score(memory: dict[str, Any]) -> float:
        score = float(memory.get("importance") or 3) * 2
        if memory.get("kind") in STABLE_KINDS:
            score += 2
        # hit_count 是观测指标，不参与检索排序；否则每次注入记忆都会改变下次排序，
        # 造成相同数据下的上下文漂移，并削弱前缀缓存可复现性。
        return score

    def _score_memory(self, memory: dict[str, Any], terms: list[str]) -> float:
        hay_summary = str(memory.get("summary") or "").lower()
        hay_tags = " ".join(str(tag) for tag in memory.get("tags") or []).lower()
        score = self._base_score(memory)
        matched = 0
        for term in terms:
            if term in hay_summary:
                score += 2.0 if len(term) >= 3 else 1.0
                matched += 1
            if term in hay_tags:
                score += 1.5
                matched += 1
        if matched == 0:
            return 0
        return score


def format_memory_lines(memories: list[dict[str, Any]], *, with_ids: bool = True) -> str:
    lines = []
    for memory in memories:
        tags = memory.get("tags") or []
        tag_text = f" #{' #'.join(tags[:4])}" if tags else ""
        prefix = f"{memory['id']}. " if with_ids else "- "
        lines.append(f"{prefix}[{memory.get('kind', 'event')}/重要度{memory.get('importance', 3)}] {memory.get('summary', '')}{tag_text}")
    return "\n".join(lines)
