"""离线统计聊天上下文质量指标（改动前后各跑一次做对比）。

数据来源全部是现有日志，不需要人工打标：
  - data/logs/telegram_*.log 的 CACHE / USAGE / BOT / CHECKPOINT 行
  - data/logs/llm_debug.jsonl 与 data/logs/chunks/llm_debug.*.jsonl 里 tag=chat 的请求

指标：
  1. 逐字历史量：每条 CACHE 行的 hist 分布（中位数、hist<=2 占比）
  2. 背景/对话字数比：chat 请求里 system 总字符 ÷ user+assistant 历史字符
  3. 历史层中段 system 消息：每请求条数与平均字符
  4. 相邻 BOT 回复 4-gram Jaccard 重复率
  5. 四段结构模板率：（）「」（）「」 严格结构占比
  6. 回复长度分布
  7. push-prep / 普通 checkpoint 次数

用法：py -3 scripts/context_quality_metrics.py [--logs data/logs] [--since 2026-08-01]
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import statistics
from collections import Counter
from pathlib import Path

CACHE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) [\d:]+ CACHE prefix .*hist=(\d+)")
BOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) [\d:]+ BOT (.+)$")
PUSH_PREP_RE = re.compile(r" CHECKPOINT push-prep ")
NORMAL_CKPT_RE = re.compile(r" CHECKPOINT until=")
TEMPLATE_RE = re.compile(r"^（[^）]*）\s*「[^」]*」\s*（[^）]*）\s*「[^」]*」$")


def _ngrams(text: str, n: int = 4) -> set[str]:
    text = re.sub(r"\s+", "", text)
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def scan_telegram_logs(log_dir: Path, since: str) -> dict:
    hist: list[int] = []
    replies: list[str] = []
    push_prep = 0
    normal_ckpt = 0
    for path in sorted(log_dir.glob("telegram_*.log")):
        if "TEST" in path.name:
            continue
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = CACHE_RE.match(line)
                if m and m.group(1) >= since:
                    hist.append(int(m.group(2)))
                    continue
                m = BOT_RE.match(line)
                if m and m.group(1) >= since:
                    text = m.group(2).replace(" ⏎ ", "\n").strip()
                    if text.startswith(("未知命令", "ComfyUI 自拍服务", "WebUI 访问方式", "回复生成失败")):
                        continue
                    replies.append(text)
                    continue
                if PUSH_PREP_RE.search(line):
                    push_prep += 1
                elif NORMAL_CKPT_RE.search(line):
                    normal_ckpt += 1
    jaccards = [
        _jaccard(_ngrams(a), _ngrams(b)) for a, b in zip(replies, replies[1:])
    ]
    template_hits = sum(1 for r in replies if TEMPLATE_RE.match(r.replace("\n", "")))
    lengths = [len(re.sub(r"\s+", "", r)) for r in replies]
    return {
        "cache_rows": len(hist),
        "hist_median": statistics.median(hist) if hist else 0,
        "hist_le2_ratio": (sum(1 for h in hist if h <= 2) / len(hist)) if hist else 0,
        "replies": len(replies),
        "adjacent_4gram_jaccard_mean": statistics.mean(jaccards) if jaccards else 0,
        "template_ratio": (template_hits / len(replies)) if replies else 0,
        "reply_len_p10_p50_p90": (
            [statistics.quantiles(lengths, n=10)[0], statistics.median(lengths), statistics.quantiles(lengths, n=10)[-1]]
            if len(lengths) >= 10 else lengths
        ),
        "push_prep_checkpoints": push_prep,
        "normal_checkpoints": normal_ckpt,
    }


def scan_chat_requests(log_dir: Path, since: str) -> dict:
    files = sorted(
        glob.glob(str(log_dir / "chunks" / "llm_debug.*.jsonl")) + [str(log_dir / "llm_debug.jsonl")],
        key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
    )
    ratios: list[float] = []
    mid_system_counts: list[int] = []
    mid_system_chars: list[int] = []
    prompt_chars: list[int] = []
    for path in files:
        if not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict) or entry.get("tag") != "chat":
                    continue
                if str(entry.get("time") or "")[:10] < since:
                    continue
                messages = (((entry.get("request") or {}).get("body") or {}).get("messages")) or []
                if not messages:
                    continue
                first_dialog = next((i for i, m in enumerate(messages) if m.get("role") in ("user", "assistant")), len(messages))
                sys_chars = 0
                dialog_chars = 0
                mid_sys = []
                total = 0
                for i, m in enumerate(messages):
                    content = m.get("content")
                    if not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False)
                    total += len(content)
                    if m.get("role") == "system":
                        sys_chars += len(content)
                        if first_dialog < i < len(messages) - 2:
                            mid_sys.append(len(content))
                    elif i < len(messages) - 1:
                        dialog_chars += len(content)
                prompt_chars.append(total)
                ratios.append(sys_chars / dialog_chars if dialog_chars else float("inf"))
                mid_system_counts.append(len(mid_sys))
                mid_system_chars.extend(mid_sys)
    finite = [r for r in ratios if r != float("inf")]
    return {
        "chat_requests": len(ratios),
        "prompt_chars_median": statistics.median(prompt_chars) if prompt_chars else 0,
        "background_to_dialog_ratio_median": statistics.median(finite) if finite else 0,
        "mid_history_system_per_request": statistics.mean(mid_system_counts) if mid_system_counts else 0,
        "mid_history_system_avg_chars": statistics.mean(mid_system_chars) if mid_system_chars else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", default="data/logs")
    parser.add_argument("--since", default="2000-01-01")
    args = parser.parse_args()
    log_dir = Path(args.logs)
    report = {
        "telegram_logs": scan_telegram_logs(log_dir, args.since),
        "chat_requests": scan_chat_requests(log_dir, args.since),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
