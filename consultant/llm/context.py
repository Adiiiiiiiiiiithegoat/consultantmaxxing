"""Context management: estimate tokens and trim messages to fit a per-request ceiling.

On Groq free tier a single request (prompt + max_tokens) must fit under 8K TPM, so agents
pass summaries and fact ids, and anything that still overflows is trimmed here.
"""

import json

CHARS_PER_TOKEN = 3  # conservative; real tokenizers average ~4 for English prose


def estimate_tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return len(text) // CHARS_PER_TOKEN + 1


def clip(text: str, max_tokens: int) -> str:
    """Keep head and tail of text, cutting the middle."""
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    half = max(limit // 2 - 40, 0)
    return f"{text[:half]}\n[... {len(text) - 2 * half} chars omitted ...]\n{text[-half:]}"


def fit(messages: list[dict], max_tokens: int) -> list[dict]:
    """Drop the oldest middle turns, then clip the longest message, until the list fits.

    Keeps the leading system message and the final message intact where possible.
    """
    msgs = [dict(m) for m in messages]
    head = 1 if msgs and msgs[0].get("role") == "system" else 0
    dropped = 0
    while estimate_tokens(msgs) > max_tokens and len(msgs) - head > 1:
        msgs.pop(head)
        # never leave a tool result without the assistant turn that called it
        while len(msgs) - head > 1 and msgs[head].get("role") == "tool":
            msgs.pop(head)
            dropped += 1
        dropped += 1
    if dropped:
        note = {"role": "user", "content": f"[{dropped} earlier messages omitted to fit context]"}
        msgs.insert(head, note)
    while estimate_tokens(msgs) > max_tokens:
        longest = max(msgs, key=lambda m: len(m.get("content") or ""))
        content = longest.get("content") or ""
        overflow = estimate_tokens(msgs) - max_tokens
        new_budget = max(estimate_tokens(content) - overflow - 50, 50)
        if new_budget >= estimate_tokens(content) - 1:
            break  # nothing left to clip
        longest["content"] = clip(content, new_budget)
    return msgs
