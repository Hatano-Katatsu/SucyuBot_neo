from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path
from typing import Any


DEFAULT_LOG_PATH = Path("data/logs/llm_debug - 副本.json")
DEFAULT_ENTRY_TYPE = "chat:chat"
PROMPT_COMPONENT_KEYS = ("messages", "tools", "tool_choice")


@dataclass(frozen=True)
class EntryView:
    """一次 LLM 请求中用于比对的规范化视图。"""

    source_index: int
    session_id: str
    time: str
    ts: float
    messages: list[dict[str, Any]]
    message_hashes: list[str]
    prompt_payload: dict[str, Any]
    prompt_text: str
    ordered_prompt_text: str
    prompt_hash: str
    settings_text: str
    usage: dict[str, Any]


@dataclass(frozen=True)
class EqualBlock:
    """两次 prompt 中相同的消息块。"""

    old_start: int
    new_start: int
    size: int


@dataclass(frozen=True)
class PromptComparison:
    """相邻两次 prompt 的比对结果。"""

    old: EntryView
    new: EntryView
    prompt_changed: bool
    common_prefix_chars: int
    common_prefix_char_rate: float
    common_prefix_messages: int
    common_suffix_messages: int
    same_index_after_prefix: list[int]
    non_prefix_common_messages: int
    non_prefix_lcs_messages: int
    non_prefix_equal_blocks: list[EqualBlock]
    opcode_lines: list[str]
    settings_same: bool
    prompt_components_same: dict[str, bool]


def stable_json(value: Any) -> str:
    """用稳定 JSON 串作为语义相等比对基准，避免字典顺序造成误判。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def ordered_json(value: Any) -> str:
    """保留原始键序的 JSON 串，模拟真实请求体的线上字节序。

    前缀缓存命中取决于请求体的字面字节序列，因此公共前缀字符数
    必须基于保留插入顺序的序列化结果，不能用 sort_keys 重排。
    """

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def short_hash(value: Any) -> str:
    return sha256(stable_json(value).encode("utf-8")).hexdigest()[:12]


def common_prefix_chars(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def common_prefix_items(left: list[str], right: list[str]) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def common_suffix_items(left: list[str], right: list[str], prefix_size: int) -> int:
    suffix = 0
    while (
        len(left) - suffix > prefix_size
        and len(right) - suffix > prefix_size
        and left[-1 - suffix] == right[-1 - suffix]
    ):
        suffix += 1
    return suffix


def content_length(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    return len(stable_json(content))


def message_brief(message: dict[str, Any], index: int, *, snippet_chars: int = 0) -> str:
    role = str(message.get("role") or "")
    base = f"#{index} {role} len={content_length(message)} sha={short_hash(message)}"
    if snippet_chars <= 0:
        return base
    content = message.get("content")
    if not isinstance(content, str):
        content = stable_json(content)
    snippet = content.replace("\r", "\\r").replace("\n", "\\n")[:snippet_chars]
    return f"{base} text={snippet!r}"


def role_summary(messages: list[dict[str, Any]], start: int, size: int) -> str:
    roles = [str(item.get("role") or "") for item in messages[start:start + size]]
    if not roles:
        return ""
    if len(set(roles)) == 1:
        return f"{roles[0]} x{len(roles)}"
    if len(roles) <= 6:
        return ", ".join(roles)
    return ", ".join(roles[:6]) + f", ... x{len(roles)}"


def prompt_payload_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """提取会影响 prompt 形态的部分：消息、工具定义、工具选择。"""

    return {key: body[key] for key in PROMPT_COMPONENT_KEYS if key in body}


def settings_payload_from_body(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key not in PROMPT_COMPONENT_KEYS}


def usage_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    raw_usage = usage.get("raw") if isinstance(usage, dict) else None
    if isinstance(raw_usage, dict):
        return raw_usage
    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    response_usage = response.get("usage") if isinstance(response.get("usage"), dict) else None
    if isinstance(response_usage, dict):
        return response_usage
    return usage


def build_entry_view(source_index: int, entry: dict[str, Any]) -> EntryView:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    raw_messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    messages = [item for item in raw_messages if isinstance(item, dict)]
    message_hashes = [short_hash(message) for message in messages]
    prompt_payload = prompt_payload_from_body(body)
    prompt_text = stable_json(prompt_payload)
    ordered_prompt_text = ordered_json(prompt_payload)
    settings_text = stable_json(settings_payload_from_body(body))
    usage = usage_from_entry(entry)
    return EntryView(
        source_index=source_index,
        session_id=str(entry.get("session_id") or ""),
        time=str(entry.get("time") or ""),
        ts=float(entry.get("ts") or 0.0),
        messages=messages,
        message_hashes=message_hashes,
        prompt_payload=prompt_payload,
        prompt_text=prompt_text,
        ordered_prompt_text=ordered_prompt_text,
        prompt_hash=sha256(prompt_text.encode("utf-8")).hexdigest()[:16],
        settings_text=settings_text,
        usage=usage,
    )


def usage_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def provider_cache_tokens(usage: dict[str, Any]) -> int:
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}
    cached = usage_int(
        usage.get("prompt_cache_hit_tokens")
        or usage.get("prompt_cached_tokens")
        or usage.get("cached_tokens")
        or details.get("cached_tokens")
    )
    miss_tokens = usage_int(usage.get("prompt_cache_miss_tokens") or usage.get("cache_miss_tokens"))
    tokens = prompt_tokens(usage)
    if not cached and miss_tokens and tokens:
        cached = max(0, tokens - miss_tokens)
    return max(0, cached)


def prompt_tokens(usage: dict[str, Any]) -> int:
    if not isinstance(usage, dict):
        return 0
    return usage_int(usage.get("prompt_tokens"))


def cache_rate(usage: dict[str, Any]) -> float:
    tokens = prompt_tokens(usage)
    return provider_cache_tokens(usage) / tokens if tokens else 0.0


def non_prefix_equal_blocks(old_hashes: list[str], new_hashes: list[str], prefix_size: int) -> list[EqualBlock]:
    matcher = SequenceMatcher(a=old_hashes, b=new_hashes, autojunk=False)
    blocks: list[EqualBlock] = []
    for old_start, new_start, size in matcher.get_matching_blocks():
        if size <= 0:
            continue
        old_end = old_start + size
        if old_end <= prefix_size:
            continue
        trim = max(0, prefix_size - old_start)
        blocks.append(EqualBlock(old_start + trim, new_start + trim, size - trim))
    return blocks


def diff_opcode_lines(old: EntryView, new: EntryView, *, snippet_chars: int = 0) -> list[str]:
    matcher = SequenceMatcher(a=old.message_hashes, b=new.message_hashes, autojunk=False)
    lines: list[str] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        old_size = old_end - old_start
        new_size = new_end - new_start
        if tag == "equal":
            roles = role_summary(old.messages, old_start, old_size)
            lines.append(
                f"equal prev[{old_start}:{old_end}] -> curr[{new_start}:{new_end}] "
                f"messages={old_size} roles={roles}"
            )
            continue
        old_desc = ", ".join(
            message_brief(old.messages[index], index, snippet_chars=snippet_chars)
            for index in range(old_start, old_end)
        )
        new_desc = ", ".join(
            message_brief(new.messages[index], index, snippet_chars=snippet_chars)
            for index in range(new_start, new_end)
        )
        if not old_desc:
            old_desc = "-"
        if not new_desc:
            new_desc = "-"
        lines.append(
            f"{tag} prev[{old_start}:{old_end}]({old_size}) -> "
            f"curr[{new_start}:{new_end}]({new_size}); old={old_desc}; new={new_desc}"
        )
    return lines


def compare_entries(old: EntryView, new: EntryView, *, snippet_chars: int = 0) -> PromptComparison:
    # 前缀比对必须用保留键序的请求体文本，模拟线上字节序；
    # 语义相等判断（prompt_changed 等）仍用 sort_keys 的稳定文本。
    prefix_chars = common_prefix_chars(old.ordered_prompt_text, new.ordered_prompt_text)
    prefix_messages = common_prefix_items(old.message_hashes, new.message_hashes)
    suffix_messages = common_suffix_items(old.message_hashes, new.message_hashes, prefix_messages)
    old_tail = old.message_hashes[prefix_messages:]
    new_tail = new.message_hashes[prefix_messages:]
    common_counter = Counter(old_tail) & Counter(new_tail)
    blocks = non_prefix_equal_blocks(old.message_hashes, new.message_hashes, prefix_messages)
    same_index = [
        index
        for index in range(prefix_messages, min(len(old.message_hashes), len(new.message_hashes)))
        if old.message_hashes[index] == new.message_hashes[index]
    ]
    prompt_components_same = {}
    for key in PROMPT_COMPONENT_KEYS:
        prompt_components_same[key] = stable_json(old.prompt_payload.get(key)) == stable_json(new.prompt_payload.get(key))
    denominator = max(len(old.ordered_prompt_text), len(new.ordered_prompt_text), 1)
    return PromptComparison(
        old=old,
        new=new,
        prompt_changed=old.prompt_text != new.prompt_text,
        common_prefix_chars=prefix_chars,
        common_prefix_char_rate=prefix_chars / denominator,
        common_prefix_messages=prefix_messages,
        common_suffix_messages=suffix_messages,
        same_index_after_prefix=same_index,
        non_prefix_common_messages=sum(common_counter.values()),
        non_prefix_lcs_messages=sum(block.size for block in blocks),
        non_prefix_equal_blocks=blocks,
        opcode_lines=diff_opcode_lines(old, new, snippet_chars=snippet_chars),
        settings_same=old.settings_text == new.settings_text,
        prompt_components_same=prompt_components_same,
    )


def load_entries(path: Path, entry_type: str) -> list[EntryView]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    grouped = data.get("entries_by_type") if isinstance(data, dict) else {}
    raw_entries = grouped.get(entry_type) if isinstance(grouped, dict) else []
    if not isinstance(raw_entries, list):
        raise ValueError(f"{entry_type!r} is not a list in {path}")
    entries = [
        build_entry_view(index, entry)
        for index, entry in enumerate(raw_entries)
        if isinstance(entry, dict)
    ]
    return sorted(entries, key=lambda item: (item.session_id, item.ts, item.source_index))


def group_by_session(entries: list[EntryView]) -> dict[str, list[EntryView]]:
    grouped: dict[str, list[EntryView]] = {}
    for entry in entries:
        grouped.setdefault(entry.session_id, []).append(entry)
    return grouped


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def format_percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def build_report(
    *,
    path: Path,
    entry_type: str,
    entries: list[EntryView],
    grouped: dict[str, list[EntryView]],
    snippet_chars: int = 0,
) -> str:
    lines: list[str] = []
    lines.append("# LLM Chat Prompt Diff Report")
    lines.append("")
    lines.append(f"- Log: `{path}`")
    lines.append(f"- Entry type: `{entry_type}`")
    lines.append(f"- Entries: {len(entries)}")
    lines.append(f"- Sessions: {len(grouped)}")
    lines.append(
        "- Prefix tokens are provider-reported cache-hit tokens parsed from raw/provider usage aliases; "
        "local prefix counts are exact comparisons of normalized JSON prompt payloads."
    )
    lines.append("")

    for session_id, session_entries in grouped.items():
        comparisons = [
            compare_entries(session_entries[index - 1], session_entries[index], snippet_chars=snippet_chars)
            for index in range(1, len(session_entries))
        ]
        lines.append(f"## Session `{session_id or '<empty>'}`")
        lines.append("")
        lines.append("### Entries")
        lines.append("")
        lines.append("| seq | source | time | messages | prompt chars | prompt tokens | cached tokens | cache rate | prompt hash |")
        lines.append("| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |")
        for seq, entry in enumerate(session_entries):
            lines.append(
                f"| {seq} | {entry.source_index} | {entry.time} | {len(entry.messages)} | "
                f"{len(entry.prompt_text)} | {prompt_tokens(entry.usage)} | "
                f"{provider_cache_tokens(entry.usage)} | {format_percent(cache_rate(entry.usage))} | "
                f"`{entry.prompt_hash}` |"
            )
        lines.append("")
        if not comparisons:
            lines.append("Only one entry in this session; no pair comparison.")
            lines.append("")
            continue

        lines.append("### Pair Summary")
        lines.append("")
        lines.append(
            "| pair | changed | provider cached/prompt | local prefix messages | "
            "local prefix chars | non-prefix common | non-prefix LCS | suffix messages | settings same |"
        )
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        for comparison in comparisons:
            usage = comparison.new.usage
            provider_tokens = provider_cache_tokens(usage)
            tokens = prompt_tokens(usage)
            lines.append(
                f"| {comparison.old.source_index}->{comparison.new.source_index} | "
                f"{format_bool(comparison.prompt_changed)} | "
                f"{provider_tokens}/{tokens} ({format_percent(cache_rate(usage))}) | "
                f"{comparison.common_prefix_messages} | "
                f"{comparison.common_prefix_chars} ({format_percent(comparison.common_prefix_char_rate)}) | "
                f"{comparison.non_prefix_common_messages} | "
                f"{comparison.non_prefix_lcs_messages} | "
                f"{comparison.common_suffix_messages} | "
                f"{format_bool(comparison.settings_same)} |"
            )
        lines.append("")

        lines.append("### Pair Details")
        lines.append("")
        for comparison in comparisons:
            old = comparison.old
            new = comparison.new
            lines.append(f"#### `{old.source_index}` -> `{new.source_index}`")
            lines.append("")
            lines.append(f"- Prompt changed: {format_bool(comparison.prompt_changed)}")
            lines.append(
                f"- Provider cache hit: {provider_cache_tokens(new.usage)}/{prompt_tokens(new.usage)} "
                f"tokens ({format_percent(cache_rate(new.usage))})"
            )
            lines.append(
                f"- Local exact prefix: {comparison.common_prefix_messages} messages, "
                f"{comparison.common_prefix_chars} chars ({format_percent(comparison.common_prefix_char_rate)})"
            )
            lines.append(
                f"- Same after prefix: multiset={comparison.non_prefix_common_messages} messages, "
                f"ordered LCS={comparison.non_prefix_lcs_messages} messages, "
                f"same-index={len(comparison.same_index_after_prefix)} messages, "
                f"suffix={comparison.common_suffix_messages} messages"
            )
            component_status = ", ".join(
                f"{key}={format_bool(value)}"
                for key, value in comparison.prompt_components_same.items()
            )
            lines.append(f"- Prompt components same: {component_status}")
            lines.append(f"- Non-prompt request settings same: {format_bool(comparison.settings_same)}")
            if comparison.non_prefix_equal_blocks:
                lines.append("- Equal blocks outside prefix:")
                for block in comparison.non_prefix_equal_blocks:
                    roles = role_summary(old.messages, block.old_start, block.size)
                    lines.append(
                        f"  - prev[{block.old_start}:{block.old_start + block.size}] -> "
                        f"curr[{block.new_start}:{block.new_start + block.size}], "
                        f"messages={block.size}, roles={roles}"
                    )
            else:
                lines.append("- Equal blocks outside prefix: none")
            lines.append("- Message diff opcodes:")
            for opcode in comparison.opcode_lines:
                lines.append(f"  - {opcode}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare chat:chat prompts in llm_debug JSON by session and consecutive request."
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"llm_debug JSON path. Default: {DEFAULT_LOG_PATH}",
    )
    parser.add_argument(
        "--type",
        default=DEFAULT_ENTRY_TYPE,
        help=f"entry type under entries_by_type. Default: {DEFAULT_ENTRY_TYPE}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write Markdown report to this path instead of stdout",
    )
    parser.add_argument(
        "--snippet-chars",
        type=int,
        default=0,
        help="include content snippets for changed messages; default hides prompt text",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    entries = load_entries(args.log, args.type)
    grouped = group_by_session(entries)
    report = build_report(
        path=args.log,
        entry_type=args.type,
        entries=entries,
        grouped=grouped,
        snippet_chars=max(0, args.snippet_chars),
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        pair_count = sum(max(0, len(value) - 1) for value in grouped.values())
        print(f"wrote {args.output}")
        print(f"entries={len(entries)} sessions={len(grouped)} pairs={pair_count}")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
