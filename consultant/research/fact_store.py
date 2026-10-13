"""SQLite fact store. A fact cannot exist without a source URL and a verbatim quote (CHECK
constraints); estimates live in a separate assumptions table and are labelled as such.
Only verified facts are deliverable-eligible."""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  engagement_id TEXT NOT NULL,
  claim TEXT NOT NULL,
  metric TEXT NOT NULL DEFAULT '',
  value REAL,
  unit TEXT NOT NULL DEFAULT '',
  entity TEXT NOT NULL DEFAULT '',
  period TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL CHECK (length(source_url) > 0),
  source_title TEXT NOT NULL DEFAULT '',
  source_date TEXT,
  quote TEXT NOT NULL CHECK (length(quote) > 0),
  header TEXT NOT NULL DEFAULT '',  -- verbatim table header when the quote is a table row
  source_type TEXT NOT NULL,
  credibility INTEGER NOT NULL,
  retrieved_at TEXT NOT NULL,
  hypothesis_ids TEXT NOT NULL DEFAULT '[]',
  verified INTEGER,              -- NULL = not checked, 1 = quote found and supports claim, 0 = no
  verify_note TEXT
);
CREATE TABLE IF NOT EXISTS assumptions (
  id INTEGER PRIMARY KEY,
  engagement_id TEXT NOT NULL,
  name TEXT NOT NULL,
  value REAL NOT NULL,
  unit TEXT NOT NULL DEFAULT '',
  rationale TEXT NOT NULL,
  basis_fact_ids TEXT NOT NULL DEFAULT '[]',
  label TEXT NOT NULL DEFAULT 'estimate' CHECK (label = 'estimate'),
  created_at TEXT NOT NULL
);
"""


class Fact(BaseModel):
    id: int | None = None
    engagement_id: str
    claim: str
    metric: str = ""
    value: float | None = None
    unit: str = ""
    entity: str = ""
    period: str = ""
    source_url: str
    source_title: str = ""
    source_date: str | None = None
    quote: str
    header: str = ""
    source_type: str
    credibility: int
    retrieved_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    hypothesis_ids: list[str] = Field(default_factory=list)
    verified: bool | None = None
    verify_note: str | None = None


class Assumption(BaseModel):
    id: int | None = None
    engagement_id: str
    name: str
    value: float
    unit: str = ""
    rationale: str
    basis_fact_ids: list[int] = Field(default_factory=list)
    label: str = "estimate"
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class FactStore:
    def __init__(self, path: Path | str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _insert(self, table: str, row: dict) -> int:
        cols = ", ".join(row)
        with self.lock, self.db:
            cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES "
                                  f"({', '.join('?' * len(row))})", list(row.values()))
        return cur.lastrowid

    def add(self, fact: Fact) -> int:
        row = fact.model_dump(exclude={"id"})
        row["hypothesis_ids"] = json.dumps(row["hypothesis_ids"])
        return self._insert("facts", row)

    def add_assumption(self, a: Assumption) -> int:
        row = a.model_dump(exclude={"id"})
        row["basis_fact_ids"] = json.dumps(row["basis_fact_ids"])
        return self._insert("assumptions", row)

    @staticmethod
    def _fact(r: sqlite3.Row) -> Fact:
        d = dict(r)
        d["hypothesis_ids"] = json.loads(d["hypothesis_ids"])
        d["verified"] = None if d["verified"] is None else bool(d["verified"])
        return Fact(**d)

    def get(self, fact_id: int) -> Fact | None:
        r = self.db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return self._fact(r) if r else None

    def facts(self, engagement_id: str | None = None, verified_only: bool = False) -> list[Fact]:
        sql, args = "SELECT * FROM facts WHERE 1=1", []
        if engagement_id:
            sql, args = sql + " AND engagement_id = ?", [engagement_id]
        if verified_only:
            sql += " AND verified = 1"
        return [self._fact(r) for r in self.db.execute(sql + " ORDER BY id", args)]

    def eligible(self, engagement_id: str) -> list[Fact]:
        """Facts a deliverable may cite: sourced (schema-enforced) and verified."""
        return self.facts(engagement_id, verified_only=True)

    def set_verified(self, fact_id: int, ok: bool, note: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE facts SET verified = ?, verify_note = ? WHERE id = ?",
                            (int(ok), note, fact_id))

    def tag_hypothesis(self, fact_id: int, hypothesis_id: str) -> None:
        f = self.get(fact_id)
        if f and hypothesis_id not in f.hypothesis_ids:
            with self.lock, self.db:
                self.db.execute("UPDATE facts SET hypothesis_ids = ? WHERE id = ?",
                                (json.dumps([*f.hypothesis_ids, hypothesis_id]), fact_id))

    def assumptions(self, engagement_id: str) -> list[Assumption]:
        rows = self.db.execute("SELECT * FROM assumptions WHERE engagement_id = ? ORDER BY id",
                               (engagement_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["basis_fact_ids"] = json.loads(d["basis_fact_ids"])
            out.append(Assumption(**d))
        return out
