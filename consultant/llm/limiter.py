"""Per-model sliding-window rate limiter: requests and tokens per minute and per day.

Usage: ev = limiter.acquire(est_total, est_output); ...call...; limiter.settle(ev, total, out).
Some Groq models also cap output tokens per minute (OTPM), checked at admission.
The day window is persisted so quotas survive restarts.
"""

import json
import threading
import time
from collections import deque
from pathlib import Path

MINUTE, DAY = 60.0, 86400.0


class QuotaExhausted(RuntimeError):
    """Daily quota would need a wait longer than max_wait. Checkpoint and stop, or fall back."""


class RequestTooLarge(ValueError):
    """A single request exceeds the per-minute token limit; it can never succeed. Trim context."""


def _wait(events, now, horizon, max_req, max_tok, tokens, field=1) -> float:
    # ponytail: O(n) scan over <=1 day of events (~1K per model); fine at free-tier volumes.
    recent = [e for e in events if e[0] > now - horizon]
    reqs, toks = len(recent), sum(e[field] for e in recent)

    def fits() -> bool:
        return (max_req is None or reqs + 1 <= max_req) and (
            max_tok is None or toks + tokens <= max_tok
        )

    if fits():
        return 0.0
    for e in recent:  # oldest first: when does enough of the window expire?
        reqs, toks = reqs - 1, toks - e[field]
        if fits():
            return e[0] + horizon - now
    return 0.0


class RateLimiter:
    def __init__(self, rpm=None, tpm=None, rpd=None, tpd=None, otpm=None, *,
                 state_path: Path | None = None, max_wait: float = 180.0, clock=time.time,
                 sleep=time.sleep):
        self.rpm, self.tpm, self.rpd, self.tpd, self.otpm = rpm, tpm, rpd, tpd, otpm
        self.state_path, self.max_wait = state_path, max_wait
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.events: deque[list] = deque()  # [timestamp, tokens, output_tokens], oldest first
        if state_path and state_path.exists():
            self.events.extend([*e, 0][:3] for e in json.loads(state_path.read_text()))

    def wait_time(self, tokens: int, out: int = 0) -> float:
        now = self.clock()
        while self.events and self.events[0][0] <= now - DAY:
            self.events.popleft()
        day = _wait(self.events, now, DAY, self.rpd, self.tpd, tokens)
        if day > self.max_wait:
            raise QuotaExhausted(f"daily quota exhausted; frees up in {day / 60:.0f} min")
        return max(day, _wait(self.events, now, MINUTE, self.rpm, self.tpm, tokens),
                   _wait(self.events, now, MINUTE, None, self.otpm, min(out, self.otpm or 0), 2))

    def acquire(self, tokens: int, out: int = 0) -> list:
        if self.tpm and tokens > self.tpm:
            raise RequestTooLarge(f"request needs ~{tokens} tokens, per-minute limit is {self.tpm}")
        while True:
            with self.lock:
                wait = self.wait_time(tokens, out)
                if wait <= 0:
                    ev = [self.clock(), tokens, out]
                    self.events.append(ev)
                    self._save()
                    return ev
            self.sleep(wait)

    def settle(self, ev: list, actual_tokens: int, actual_out: int | None = None) -> None:
        with self.lock:
            ev[1] = actual_tokens
            if actual_out is not None:
                ev[2] = actual_out
            self._save()

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(list(self.events)))
