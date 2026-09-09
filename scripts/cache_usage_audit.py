"""从真实 DEBUG 离线核查缓存三态与人设去重；默认只读，--apply 才回填唯一匹配的旧用量。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_comfyui_selfie.app_store import AppStateStore
from telegram_comfyui_selfie.llm_metrics import cache_usage, endpoint_identity, fingerprint, token_count, usage_rates
from telegram_comfyui_selfie.prompt_layout import build_image_planner_messages


def audit(database: Path, paths: list[Path], *, apply: bool = False) -> dict:
    """使用原始 usage；无正文输出、无外部请求、重复日志不会重复统计或回填。"""
    backup_path = None
    if apply:
        # 已升级的库也必须备份；使用 SQLite backup 纳入 WAL 中已提交的数据。
        backup_path = database.with_name(f"{database.name}.cache-backfill-{time.time_ns()}.bak")
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("回填前备份完整性检查失败")
        AppStateStore(database)
    uri = database.resolve().as_uri() + ("?mode=rw" if apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)")}
    new_schema = "cache_reported" in columns
    report = {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0, "cache_reported_prompt_tokens": 0, "cache_unknown_requests": 0,
              "unique_matches": 0, "ambiguous_matches": 0, "unmatched": 0, "eligible_backfills": 0, "applied": 0,
              "duplicate_log_entries": 0, "invalid_log_lines": 0, "planner_requests": 0, "shortened_requests": 0,
              "planner_chars_before": 0, "planner_chars_after": 0}
    seen = set()
    try:
        for path in paths:
            with path.open(encoding="utf-8-sig") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        report["invalid_log_lines"] += 1
                        continue
                    if not isinstance(entry, dict):
                        report["invalid_log_lines"] += 1
                        continue
                    identity = fingerprint(entry)
                    if identity in seen:
                        report["duplicate_log_entries"] += 1
                        continue
                    seen.add(identity)
                    response = entry.get("response")
                    summary = entry.get("usage")
                    raw = (response.get("usage") if isinstance(response, dict) else None) or (summary.get("raw") if isinstance(summary, dict) else None)
                    if entry.get("status") != 200 or not isinstance(raw, dict):
                        continue
                    prompt = token_count(raw.get("prompt_tokens"))
                    completion = token_count(raw.get("completion_tokens", 0))
                    if not prompt or completion is None:
                        report["invalid_log_lines"] += 1
                        continue
                    cache = cache_usage(raw)
                    report["requests"] += 1
                    report["prompt_tokens"] += prompt
                    report["cached_tokens"] += cache["cached_tokens"]
                    if cache["cache_reported"]:
                        report["cache_reported_prompt_tokens"] += prompt
                    else:
                        report["cache_unknown_requests"] += 1
                    request = entry.get("request") or {}
                    messages = (request.get("body") or {}).get("messages") or []
                    if entry.get("tag") == "roleplay-image-plan":
                        report["planner_requests"] += 1
                        before = sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))
                        after = before
                        for i, message in enumerate(messages):
                            content = message.get("content") or ""
                            if message.get("role") != "system" or not isinstance(content, str) or not content.startswith("Scene boundary:"):
                                continue
                            boundary = content.find("\n\n你是")
                            if i <= 1 or boundary < 0:
                                break
                            # 只重放本次实际改动的组装函数，保留其余历史和请求原文。
                            persona = str(messages[1].get("content") or "").split("\n\n你当前扮演", 1)[0]
                            rebuilt = build_image_planner_messages(content[:boundary], content[boundary:], "", context=messages[:i], persona=persona)
                            rebuilt = rebuilt[:-1] + messages[i + 1:]
                            after = sum(len(m.get("content") or "") for m in rebuilt if isinstance(m.get("content"), str))
                            break
                        report["planner_chars_before"] += before
                        report["planner_chars_after"] += after
                        report["shortened_requests"] += int(after < before)
                    stamp = float(entry.get("ts") or 0)
                    rows = conn.execute(
                        "SELECT * FROM llm_usage WHERE created_at BETWEEN ? AND ? AND model=? AND profile_id=? AND tag=? AND session_id=? AND prompt_tokens=? AND completion_tokens=?",
                        (stamp - 10, stamp + 10, entry.get("model", ""), entry.get("profile_id", ""), entry.get("tag", ""), entry.get("session_id", ""), prompt, completion),
                    ).fetchall()
                    if len(rows) != 1:
                        report["ambiguous_matches" if rows else "unmatched"] += 1
                        continue
                    report["unique_matches"] += 1
                    row = rows[0]
                    if new_schema and (row["cache_source"] not in ("", "legacy_positive") or row["request_meta"] != "{}"):
                        continue
                    report["eligible_backfills"] += 1
                    if apply:
                        cursor = conn.execute(
                            "UPDATE llm_usage SET cached_tokens=?,cache_reported=?,cache_source=?,cache_anomaly=?,endpoint=? WHERE id=? AND cache_source IN ('','legacy_positive') AND request_meta='{}'",
                            (cache["cached_tokens"], int(cache["cache_reported"]), cache["cache_source"], cache["cache_anomaly"], endpoint_identity(request.get("url", "")), row["id"]),
                        )
                        report["applied"] += cursor.rowcount
        if apply:
            conn.commit()
    finally:
        conn.close()
    report.update(usage_rates(report))
    report["backup_path"] = str(backup_path) if backup_path else None
    report["planner_chars_saved"] = report["planner_chars_before"] - report["planner_chars_after"]
    report["note"] = "字符节省来自真实日志上运行当前去重与任务上下文组装函数，不是 token 或费用估算；样本命中率不代表缺失样本。"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--logs", type=Path, nargs="+", required=True)
    parser.add_argument("--apply", action="store_true", help="迁移并回填唯一匹配的旧记录，默认只读")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.database, args.logs, apply=args.apply)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
