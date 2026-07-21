from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .sqlite_migrations import SchemaMigrationResult, migrate_database


USER_PROFILE_KIND = "user_profile"
STABLE_KINDS = {USER_PROFILE_KIND, "profile", "preference", "relationship", "setting", "boundary", "visual"}
VALID_KINDS = STABLE_KINDS | {"event", "correction", "manual"}
KIND_ALIASES = {
    "资料": "profile",
    "用户资料": "profile",
    "用户画像": USER_PROFILE_KIND,
    "画像": USER_PROFILE_KIND,
    "userprofile": USER_PROFILE_KIND,
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
        self.schema_migration: SchemaMigrationResult = self._init_schema()
        self._mem_cache: dict[str, list[dict[str, Any]]] = {}

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        if os.environ.get("SUCYUBOT_TEST_FAST_SQLITE"):
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_schema(self) -> SchemaMigrationResult:
        return migrate_database(self.path, connection_factory=self._connect)

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
        summary = (summary or "").strip()[:600]
        if not session_id or not summary:
            return None
        character = (character or "").strip()
        kind = normalize_kind(kind)
        importance = clamp_importance(importance)
        tag_list = normalize_tags(tags)
        now = time.time()
        with closing(self._connect()) as conn:
            candidates = conn.execute(
                """
                SELECT id, summary, tags, importance FROM memories
                WHERE session_id = ? AND character = ? AND kind = ? AND status = 'active'
                """,
                (session_id, character, kind),
            ).fetchall()
            normalized_key = re.sub(r"\s+", "", summary).lower()
            existing = next(
                (row for row in candidates if re.sub(r"\s+", "", str(row["summary"] or "")).lower() == normalized_key),
                None,
            )
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
                self._evict_mem_cache(session_id, character)
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
                    summary,
                    json.dumps(tag_list, ensure_ascii=False),
                    importance,
                    (source or "")[:800],
                    now,
                    now,
                ),
            )
            conn.commit()
            self._evict_mem_cache(session_id, character)
            return int(cur.lastrowid)

    @staticmethod
    def _prepare_replacement_memories(
        candidates: Any,
        *,
        max_candidates: int | None = None,
    ) -> list[dict[str, Any]]:
        """严格校验全量重写候选，并转换成可直接写库的规范结构。"""
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("全量重写候选必须是非空数组")
        if max_candidates is not None and len(candidates) > max(0, int(max_candidates)):
            raise ValueError(f"全量重写候选超过上限：{len(candidates)} > {max_candidates}")

        prepared: list[dict[str, Any]] = []
        user_profile_count = 0
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise ValueError(f"第 {index + 1} 条全量重写候选不是对象")

            summary = candidate.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError(f"第 {index + 1} 条全量重写候选缺少有效 summary")

            raw_kind = candidate.get("kind", "event")
            if not isinstance(raw_kind, str):
                raise ValueError(f"第 {index + 1} 条全量重写候选 kind 无效")
            kind_key = raw_kind.strip().lower()
            kind = KIND_ALIASES.get(kind_key, kind_key)
            if kind not in VALID_KINDS or kind == "manual":
                raise ValueError(f"第 {index + 1} 条全量重写候选 kind 无效：{raw_kind!r}")

            raw_importance = candidate.get("importance", 3)
            if isinstance(raw_importance, bool) or not isinstance(raw_importance, int) or not 1 <= raw_importance <= 5:
                raise ValueError(f"第 {index + 1} 条全量重写候选 importance 必须是 1-5 的整数")

            raw_tags = candidate.get("tags", [])
            if not isinstance(raw_tags, list) or any(not isinstance(tag, str) for tag in raw_tags):
                raise ValueError(f"第 {index + 1} 条全量重写候选 tags 必须是字符串数组")

            if kind == USER_PROFILE_KIND:
                user_profile_count += 1
                if user_profile_count > 1:
                    raise ValueError("全量重写候选最多包含一条 user_profile")

            prepared.append({
                "kind": kind,
                "summary": summary.strip()[:600],
                "importance": raw_importance,
                "tags": normalize_tags(raw_tags),
            })
        return prepared

    def replace_non_manual_memories(
        self,
        session_id: str,
        character: str,
        included_ids: Any,
        candidates: Any,
        *,
        source: str = "dream-summarize",
        max_candidates: int | None = None,
    ) -> dict[str, Any]:
        """原子插入重写集合并停用本次实际提交给模型的旧记忆。"""
        if not str(session_id or "").strip():
            raise ValueError("session_id 不能为空")
        prepared = self._prepare_replacement_memories(candidates, max_candidates=max_candidates)

        if isinstance(included_ids, (str, bytes)):
            raise ValueError("included_ids 必须是记忆 ID 集合")
        try:
            raw_ids = list(included_ids)
        except TypeError as exc:
            raise ValueError("included_ids 必须是记忆 ID 集合") from exc
        normalized_ids: list[int] = []
        for value in raw_ids:
            if isinstance(value, bool):
                raise ValueError("included_ids 包含无效记忆 ID")
            try:
                memory_id = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("included_ids 包含无效记忆 ID") from exc
            if memory_id <= 0:
                raise ValueError("included_ids 包含无效记忆 ID")
            if memory_id not in normalized_ids:
                normalized_ids.append(memory_id)
        if not normalized_ids:
            raise ValueError("全量重写没有可替换的旧记忆")

        character = (character or "").strip()
        now = time.time()
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows: list[sqlite3.Row] = []
                # 为兼容 SQLite 较低的绑定变量上限，在同一事务内分块查询和更新。
                for offset in range(0, len(normalized_ids), 500):
                    id_chunk = normalized_ids[offset:offset + 500]
                    placeholders = ",".join("?" for _ in id_chunk)
                    rows.extend(conn.execute(
                        f"""
                        SELECT id, kind, status FROM memories
                        WHERE session_id = ? AND character = ? AND id IN ({placeholders})
                        """,
                        (session_id, character, *id_chunk),
                    ).fetchall())
                rows_by_id = {int(row["id"]): row for row in rows}
                missing = [memory_id for memory_id in normalized_ids if memory_id not in rows_by_id]
                if missing:
                    raise ValueError(f"待替换旧记忆不存在或不属于当前角色：{missing}")
                invalid_old = [
                    memory_id
                    for memory_id, row in rows_by_id.items()
                    if row["status"] != "active" or row["kind"] == "manual"
                ]
                if invalid_old:
                    raise ValueError(f"待替换旧记忆已失效或属于手动记忆：{invalid_old}")

                inserted_ids: list[int] = []
                for candidate in prepared:
                    cur = conn.execute(
                        """
                        INSERT INTO memories(
                            session_id, character, kind, summary, tags, importance,
                            source, status, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                        """,
                        (
                            session_id,
                            character,
                            candidate["kind"],
                            candidate["summary"],
                            json.dumps(candidate["tags"], ensure_ascii=False),
                            candidate["importance"],
                            str(source or "")[:800],
                            now,
                            now,
                        ),
                    )
                    inserted_ids.append(int(cur.lastrowid))

                deactivated = 0
                for offset in range(0, len(normalized_ids), 500):
                    id_chunk = normalized_ids[offset:offset + 500]
                    placeholders = ",".join("?" for _ in id_chunk)
                    cur = conn.execute(
                        f"""
                        UPDATE memories SET status = 'deleted', updated_at = ?
                        WHERE session_id = ? AND character = ? AND id IN ({placeholders})
                          AND status = 'active' AND kind <> 'manual'
                        """,
                        (now, session_id, character, *id_chunk),
                    )
                    deactivated += int(cur.rowcount or 0)
                if deactivated != len(normalized_ids):
                    raise RuntimeError("旧记忆集合在全量重写事务中发生变化")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "added": len(inserted_ids),
            "deactivated": len(normalized_ids),
            "inserted_ids": inserted_ids,
        }

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
                ORDER BY CASE WHEN kind = ? THEN 0 ELSE 1 END, importance DESC, updated_at DESC
                LIMIT ?
                """,
                (*params, USER_PROFILE_KIND, int(limit)),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def merge_user_profile_memories(self, session_id: str, *, character: str = "", source: str = "user-profile-merge") -> dict[str, Any]:
        """把同一角色下多条用户画像合并为唯一置顶记忆。"""
        character = (character or "").strip()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE session_id = ? AND character = ? AND kind = ? AND status = 'active'
                ORDER BY importance DESC, updated_at DESC, id DESC
                """,
                (session_id, character, USER_PROFILE_KIND),
            ).fetchall()
            profiles = [self._row_to_dict(row) for row in rows]
            if len(profiles) <= 1:
                return {
                    "changed": False,
                    "kept_id": int(profiles[0]["id"]) if profiles else None,
                    "merged": len(profiles),
                }

            keep = profiles[0]
            keep_id = int(keep["id"])
            seen_summary: set[str] = set()
            summary_parts: list[str] = []
            tag_parts: list[str] = []
            importance = 1
            source_parts: list[str] = []
            for item in profiles:
                summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()
                key = summary.lower()
                if summary and key not in seen_summary:
                    summary_parts.append(summary)
                    seen_summary.add(key)
                tag_parts.extend(str(tag) for tag in (item.get("tags") or []) if str(tag).strip())
                importance = max(importance, clamp_importance(item.get("importance")))
                item_source = str(item.get("source") or "").strip()
                if item_source:
                    source_parts.append(item_source)
            merged_summary = "；".join(summary_parts)[:600]
            merged_tags = normalize_tags(["用户画像", *tag_parts])
            merged_source = (source or "")[:120]
            if source_parts:
                merged_source = f"{merged_source}: " + " | ".join(source_parts)
            merged_source = merged_source[:800]
            now = time.time()
            conn.execute(
                """
                UPDATE memories
                SET summary = ?, tags = ?, importance = ?, source = ?, updated_at = ?
                WHERE id = ? AND session_id = ? AND character = ? AND kind = ? AND status = 'active'
                """,
                (
                    merged_summary,
                    json.dumps(merged_tags, ensure_ascii=False),
                    importance,
                    merged_source,
                    now,
                    keep_id,
                    session_id,
                    character,
                    USER_PROFILE_KIND,
                ),
            )
            conn.execute(
                """
                UPDATE memories
                SET status = 'deleted', updated_at = ?
                WHERE session_id = ? AND character = ? AND kind = ? AND status = 'active' AND id <> ?
                """,
                (now, session_id, character, USER_PROFILE_KIND, keep_id),
            )
            conn.commit()
        return {"changed": True, "kept_id": keep_id, "merged": len(profiles), "summary": merged_summary}

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

    def context_memories(self, session_id: str, *, character: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        cache_key = f"{session_id}:{character or ''}:{limit}"
        cached = self._mem_cache.get(cache_key)
        if cached is not None:
            return cached
        result = self.list_memories(session_id, character=character, limit=limit)
        self._mem_cache[cache_key] = result
        return result

    def _evict_mem_cache(self, session_id: str, character: str = "") -> None:
        prefix = f"{session_id}:{character or ''}:"
        for key in list(self._mem_cache.keys()):
            if key.startswith(prefix):
                self._mem_cache.pop(key, None)

    def delete_character_memories(self, session_id: str, character: str) -> int:
        """硬删除指定角色的全部长期记忆，用于删除角色时避免孤儿数据复活。"""
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "DELETE FROM memories WHERE session_id = ? AND character = ?",
                (session_id, (character or "").strip()),
            )
            conn.commit()
            self._evict_mem_cache(session_id, character)
            return int(cur.rowcount or 0)

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
            self._evict_mem_cache(session_id, character or "")
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
            self._evict_mem_cache(session_id, character or "")
            return cur.rowcount > 0

    def deactivate_non_manual_memory(self, session_id: str, memory_id: int, *, character: str | None = None) -> bool:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE memories SET status = 'deleted', updated_at = ? WHERE {scope} AND id = ? AND status = 'active' AND kind <> 'manual'",
                (time.time(), *params, int(memory_id)),
            )
            conn.commit()
            self._evict_mem_cache(session_id, character or "")
            return cur.rowcount > 0

    def deactivate_memory(self, session_id: str, memory_id: int, *, character: str | None = None) -> bool:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE memories SET status = 'deleted', updated_at = ? WHERE {scope} AND id = ? AND status = 'active'",
                (time.time(), *params, int(memory_id)),
            )
            conn.commit()
            self._evict_mem_cache(session_id, character or "")
            return cur.rowcount > 0

    def clear_session(self, session_id: str, *, character: str | None = None) -> int:
        scope, params = self._scope_clause(session_id, character)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE memories SET status = 'deleted', updated_at = ? WHERE {scope} AND status = 'active'",
                (time.time(), *params),
            )
            conn.commit()
            self._evict_mem_cache(session_id, character or "")
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
