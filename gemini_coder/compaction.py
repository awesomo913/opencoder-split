"""Session compaction - Python translation of the Claude Code Rust compaction engine.

When a conversation grows too large for context, this module:
1. Detects when compaction is needed (token count + message count thresholds)
2. Slices the session into removed + preserved messages
3. Merges any existing summary with a new summary of removed messages
4. Injects the exact Anthropic continuation prompt so the model resumes seamlessly

The continuation prompt, constants, and logic are a direct port of the Rust source.

Usage:
    from gemini_coder.compaction import (
        compact_session, should_compact, CompactionConfig, CompactionResult
    )

    config = CompactionConfig(preserve_recent_messages=4, max_estimated_tokens=10000)

    if should_compact(session_messages, config):
        result = compact_session(session_messages, config, summarizer=my_llm_summarize)
        new_messages = result.compacted_session
        print(f"Removed {result.removed_message_count} messages")
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Anthropic's exact continuation strings (from Rust constants) ──

COMPACT_CONTINUATION_PREAMBLE = (
    "This session is being continued from a previous conversation that "
    "ran out of context. The summary below covers the earlier portion "
    "of the conversation.\n\n"
)

COMPACT_RECENT_MESSAGES_NOTE = "Recent messages are preserved verbatim."

COMPACT_DIRECT_RESUME_INSTRUCTION = (
    "Continue the conversation from where it left off without asking "
    "the user any further questions. Resume directly \u2014 do not "
    "acknowledge the summary, do not recap what was happening, and "
    "do not preface with continuation text."
)

# Marker used to detect an existing compacted summary in message[0]
_SUMMARY_MARKER = "[COMPACTED SUMMARY]"
_SUMMARY_END_MARKER = "[/COMPACTED SUMMARY]"


# ── Config & Result dataclasses ───────────────────────────────────

@dataclass
class CompactionConfig:
    """Matches Anthropic's default thresholds for memory limits."""
    preserve_recent_messages: int = 4
    max_estimated_tokens: int = 10_000


@dataclass
class CompactionResult:
    """Output of a compaction operation."""
    summary: str = ""
    formatted_summary: str = ""
    compacted_session: list = field(default_factory=list)
    removed_message_count: int = 0


# ── Token estimation ──────────────────────────────────────────────

def estimate_message_tokens(message: dict) -> int:
    """Rough token estimation (1 token ~= 4 chars) to match the Rust engine.

    Works with message dicts like {"role": "user", "content": "..."}
    or any dict/string.
    """
    if isinstance(message, str):
        text = message
    elif isinstance(message, dict):
        text = str(message.get("content", ""))
    else:
        text = str(message)
    return (len(text) // 4) + 1


# ── Summary formatting ────────────────────────────────────────────

def format_compact_summary(summary: str) -> str:
    """Wrap a summary in markers so we can detect it later."""
    return f"{_SUMMARY_MARKER}\n{summary}\n{_SUMMARY_END_MARKER}"


def extract_existing_compacted_summary(message: dict) -> Optional[str]:
    """If message[0] is a system message containing a compacted summary, extract it.

    Returns the raw summary text (without markers), or None.
    """
    content = ""
    if isinstance(message, dict):
        content = str(message.get("content", ""))
    elif isinstance(message, str):
        content = message

    if _SUMMARY_MARKER not in content:
        return None

    start = content.find(_SUMMARY_MARKER)
    end = content.find(_SUMMARY_END_MARKER)
    if start == -1 or end == -1 or end <= start:
        return None

    inner_start = start + len(_SUMMARY_MARKER)
    return content[inner_start:end].strip()


# ── Summary generation ────────────────────────────────────────────

def summarize_messages(messages: list) -> str:
    """Generate a structural summary of removed messages.

    In a real deployment, you'd pass these to an LLM for compression.
    This default implementation creates a structured extraction that
    captures the key information without an LLM call.
    """
    if not messages:
        return "No prior messages."

    topics = []
    decisions = []
    code_refs = []

    for msg in messages:
        content = ""
        role = "unknown"
        if isinstance(msg, dict):
            content = str(msg.get("content", ""))
            role = msg.get("role", "unknown")
        elif isinstance(msg, str):
            content = msg

        # Extract key signals from the content
        lines = content.split("\n")
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            # Capture headings and task titles
            if line_stripped.startswith("#"):
                topics.append(line_stripped.lstrip("# ").strip())
            # Capture decisions/conclusions
            lower = line_stripped.lower()
            if any(kw in lower for kw in ["decided", "conclusion", "result:", "output:", "chosen"]):
                decisions.append(line_stripped[:120])
            # Capture file/code references
            if any(kw in lower for kw in [".py", ".js", ".ts", ".rs", "def ", "class ", "function "]):
                code_refs.append(line_stripped[:100])

    parts = []
    parts.append(f"Conversation scope: {len(messages)} messages compacted.")

    if topics:
        unique_topics = list(dict.fromkeys(topics))[:10]  # dedupe, cap at 10
        parts.append(f"Topics covered: {', '.join(unique_topics)}")

    if decisions:
        unique_decisions = list(dict.fromkeys(decisions))[:5]
        parts.append("Key decisions/outputs:")
        for d in unique_decisions:
            parts.append(f"  - {d}")

    if code_refs:
        unique_refs = list(dict.fromkeys(code_refs))[:8]
        parts.append("Code references:")
        for r in unique_refs:
            parts.append(f"  - {r}")

    parts.append("Timeline and sequence preserved in remaining messages.")
    return "\n".join(parts)


def merge_compact_summaries(existing: Optional[str], new_summary: str) -> str:
    """Merge an existing compacted summary with a new one.

    If there was already a summary from a previous compaction, prepend it
    so context accumulates across multiple compactions.
    """
    if existing:
        return (
            f"[Earlier context]\n{existing}\n\n"
            f"[More recent context]\n{new_summary}"
        )
    return new_summary


# ── Core compaction logic ─────────────────────────────────────────

def compacted_summary_prefix_len(messages: list) -> int:
    """How many messages at the start are existing compaction summaries.

    Returns 1 if messages[0] is a system message with a compacted summary,
    otherwise 0.
    """
    if not messages:
        return 0
    first = messages[0]
    if extract_existing_compacted_summary(first) is not None:
        return 1
    return 0


def should_compact(messages: list, config: CompactionConfig = CompactionConfig()) -> bool:
    """Check if a session needs compaction.

    Direct port of the Rust should_compact function:
    - Skip any existing compacted summary prefix
    - Check if remaining messages exceed both the count AND token thresholds
    """
    start = compacted_summary_prefix_len(messages)
    compactable = messages[start:]

    if len(compactable) <= config.preserve_recent_messages:
        return False

    total_tokens = sum(estimate_message_tokens(m) for m in compactable)
    return total_tokens >= config.max_estimated_tokens


def get_compact_continuation_message(
    summary: str,
    suppress_follow_up_questions: bool = True,
    recent_messages_preserved: bool = True,
) -> str:
    """Build the continuation message injected as message[0].

    Direct port of the Rust get_compact_continuation_message function.
    Uses Anthropic's exact preamble, note, and resume instruction.
    """
    base = f"{COMPACT_CONTINUATION_PREAMBLE}{format_compact_summary(summary)}"

    if recent_messages_preserved:
        base += f"\n\n{COMPACT_RECENT_MESSAGES_NOTE}"

    if suppress_follow_up_questions:
        base += f"\n{COMPACT_DIRECT_RESUME_INSTRUCTION}"

    return base


def compact_session(
    messages: list,
    config: CompactionConfig = CompactionConfig(),
    summarizer: Optional[Callable[[list], str]] = None,
) -> CompactionResult:
    """Compact a session's message list.

    Direct port of the Rust compact_session function:
    1. Check if compaction is needed — if not, return unchanged
    2. Detect any existing compacted summary in messages[0]
    3. Slice: removed messages vs. preserved recent messages
    4. Generate summary of removed messages (uses summarizer if provided)
    5. Merge with any existing summary
    6. Build continuation message with Anthropic's exact prompt
    7. Return new message list: [continuation_system_msg, ...preserved]

    Args:
        messages: The full session message list
        config: Compaction thresholds
        summarizer: Optional function(messages) -> str for LLM-powered summaries.
                    Falls back to structural extraction if not provided.
    """
    if not should_compact(messages, config):
        return CompactionResult(
            compacted_session=list(messages),
            removed_message_count=0,
        )

    # Detect existing summary
    existing_summary = extract_existing_compacted_summary(messages[0]) if messages else None
    compacted_prefix_len = 1 if existing_summary is not None else 0

    # Slice: what to remove vs. what to keep
    keep_from = max(0, len(messages) - config.preserve_recent_messages)
    removed = messages[compacted_prefix_len:keep_from]
    preserved = messages[keep_from:]

    # Generate summary of removed messages
    if summarizer is not None:
        try:
            new_summary = summarizer(removed)
        except Exception as e:
            logger.warning("Custom summarizer failed, using default: %s", e)
            new_summary = summarize_messages(removed)
    else:
        new_summary = summarize_messages(removed)

    # Merge with any existing summary from previous compaction
    merged_summary = merge_compact_summaries(existing_summary, new_summary)
    formatted = format_compact_summary(merged_summary)

    # Build the continuation message
    continuation = get_compact_continuation_message(
        merged_summary,
        suppress_follow_up_questions=True,
        recent_messages_preserved=len(preserved) > 0,
    )

    # Rebuild session: [system continuation, ...preserved messages]
    compacted = [{"role": "system", "content": continuation}]
    compacted.extend(preserved)

    logger.info(
        "[COMPACTION] Removed %d messages, preserved %d. "
        "Summary: %d chars, tokens before: ~%d, after: ~%d",
        len(removed),
        len(preserved),
        len(merged_summary),
        sum(estimate_message_tokens(m) for m in messages),
        sum(estimate_message_tokens(m) for m in compacted),
    )

    return CompactionResult(
        summary=merged_summary,
        formatted_summary=formatted,
        compacted_session=compacted,
        removed_message_count=len(removed),
    )
