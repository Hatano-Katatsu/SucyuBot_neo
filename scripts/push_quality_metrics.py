"""只读统计缓存、成功照片类型和推送结果；输出不含会话标识或聊天正文。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path


def evaluate(database: Path, since: float, until: float | None = None) -> dict:
    until = time.time() if until is None else until
    if since >= until:
        raise ValueError("since must be before until")
    usage = defaultdict(list)
    outcomes, subjects, reasons = Counter(), Counter(), Counter()
    feedback, direct, photos, seen = 0, 0, 0, set()
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        for row in conn.execute("SELECT tag,prompt_tokens,cached_tokens,cache_reported,request_meta FROM llm_usage WHERE created_at>=? AND created_at<?", (since, until)):
            usage[row["tag"]].append(dict(row))
        for row in conn.execute("SELECT session_id,data FROM session_state"):
            state = json.loads(row["data"])
            diagnostics = (state.get("session") or {}).get("push_diagnostics") or state.get("push_diagnostics") or []
            for event in diagnostics:
                if since <= float(event.get("ts") or 0) < until:
                    outcomes[event.get("outcome") or "unknown"] += 1
                    if event.get("reason"):
                        reasons[event["reason"]] += 1
            contexts = [state] + list((state.get("character_contexts") or {}).values())
            for context in contexts:
                records = (context.get("context") or {}).get("recent_push_topics") or context.get("recent_push_topics") or []
                for photo in records:
                    key = (row["session_id"], photo.get("message_id"), photo.get("ts"))
                    if key in seen:
                        continue
                    seen.add(key)
                    if since <= float(photo.get("ts") or 0) < until:
                        photos += 1
                        subjects[(photo.get("photo_brief") or {}).get("subject_mode") or "unknown"] += 1
                    for item in photo.get("user_feedback") or []:
                        if since <= float(item.get("ts") or 0) < until:
                            feedback += 1
                            direct += int(item.get("reply_to_message_id") is not None)
    cache = {}
    for tag, rows in sorted(usage.items()):
        total = sum(r["prompt_tokens"] for r in rows)
        known = [r for r in rows if r["cache_reported"]]
        covered = sum(r["prompt_tokens"] for r in known)
        cached = sum(r["cached_tokens"] for r in known)
        times = [json.loads(r["request_meta"] or "{}").get("duration_ms") for r in rows]
        times = [t for t in times if isinstance(t, (int, float))]
        cache[tag] = {"requests": len(rows), "input_tokens": total,
                      "cache_hit_pct": round(100 * cached / covered, 2) if covered else None,
                      "cache_coverage_pct": round(100 * covered / total, 2) if total else None,
                      "input_median": statistics.median(r["prompt_tokens"] for r in rows),
                      "duration_median_ms": statistics.median(times) if times else None}
    return {"since": since, "until": until, "diagnostic_outcomes": dict(outcomes),
            "diagnostic_reasons": dict(reasons), "retained_successful_photos": photos,
            "subject_modes": dict(subjects), "related_user_messages": feedback,
            "explicit_photo_replies": direct, "usage_by_tag": cache,
            "note": "照片与反馈受每角色32条/7天、每张最近3条反馈保留范围限制；诊断每会话最多256条。没有记录不代表没有发生，不是全量回复率。"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/memory.sqlite3"))
    parser.add_argument("--since", required=True, help="带时区的 ISO 时间，例如 2026-09-18T00:00:00+08:00")
    parser.add_argument("--until", help="带时区的 ISO 时间，默认当前时间")
    args = parser.parse_args()

    def parse_time(value: str) -> float:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parser.error("时间必须包含时区，避免跨时区对比偏移")
        return parsed.timestamp()

    result = evaluate(args.db, parse_time(args.since), parse_time(args.until) if args.until else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
