"""JSONL tracer, usage ledger and run budget."""

import json
import threading
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from consultant.config import ROOT


class BudgetExceeded(RuntimeError):
    """A run hit one of its caps. Callers checkpoint and stop cleanly."""


class Budget:
    """Generic counters with caps, e.g. {"tokens": 60000, "calls": 120, "searches": 20}."""

    def __init__(self, limits: dict[str, int] | None = None):
        self.limits = dict(limits or {})
        self.spent: dict[str, int] = defaultdict(int)
        self.lock = threading.Lock()

    @classmethod
    def from_profile(cls, prof: dict) -> "Budget":
        return cls({k.removeprefix("max_"): v for k, v in prof.items() if k.startswith("max_")})

    def check(self, kind: str, amount: int = 1) -> None:
        cap = self.limits.get(kind)
        if cap is not None and self.spent[kind] + amount > cap:
            raise BudgetExceeded(f"{kind} budget: {self.spent[kind]}+{amount} > {cap}")

    def charge(self, kind: str, amount: int = 1) -> None:
        with self.lock:
            self.spent[kind] += amount

    def remaining(self, kind: str) -> int | None:
        cap = self.limits.get(kind)
        return None if cap is None else cap - self.spent[kind]


class Tracer:
    def __init__(self, run_id: str | None = None, root: Path = ROOT / "traces"):
        self.run_id = run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self.lock = threading.Lock()

    def log(self, kind: str, **data) -> None:
        row = {"ts": time.time(), "kind": kind, **data}
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self.lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def events(self, kind: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x]
        return [r for r in rows if kind is None or r["kind"] == kind]

    def usage(self) -> dict[str, dict]:
        """Ledger per model: calls, cached calls, tokens, cost, seconds."""
        out: dict[str, dict] = defaultdict(lambda: defaultdict(float))
        for e in self.events("llm"):
            row = out[e["model"]]
            row["calls"] += 1
            row["cached"] += e.get("cached", False)
            for k in ("prompt_tokens", "completion_tokens", "cost_usd", "seconds"):
                row[k] += e.get(k) or 0
        return {m: dict(r) for m, r in out.items()}
