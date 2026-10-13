"""Load the playbooks in skills/ (committed, original wording)."""

from consultant.config import ROOT

SKILLS = ROOT / "skills"
CASE_TYPES = ("profitability", "market_entry", "pricing", "growth", "ma_pe", "unconventional",
              "guesstimate")
CROSS_CUTTING = ("hypothesis_driven", "research_method", "deliverables", "strategy_concepts")


def load(name: str) -> str:
    return (SKILLS / f"{name}.md").read_text(encoding="utf-8")


def playbook(case_type: str) -> str:
    return load(case_type if case_type in CASE_TYPES else "unconventional")
