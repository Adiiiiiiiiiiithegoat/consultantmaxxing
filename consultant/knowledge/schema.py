"""Structured case and industry-chunk models."""

from typing import Literal

from pydantic import BaseModel, Field

CaseType = Literal["profitability", "market_entry", "pricing", "growth", "ma_pe",
                   "unconventional", "guesstimate"]


class QA(BaseModel):
    question: str = Field(description="what the candidate asked")
    answer: str = Field(description="what the interviewer replied, with every fact/number kept")


class DataReveal(BaseModel):
    trigger: str = Field(description="the kind of question or analysis step that made the "
                                     "interviewer hand over this data")
    data: str = Field(description="the exact data given: all numbers, units, tables as text")


class TurnLabel(BaseModel):
    turn: int = Field(description="interviewer turn number")
    kind: Literal["scoping", "data", "none"]
    trigger: str = Field(description="short phrase: what the candidate asked/proposed")


class CaseAnnotation(BaseModel):
    """What the LLM adds to a speaker-labelled transcript."""
    industry: str = Field(description="client's industry, a few words")
    numerical: bool = Field(description="true only if solving the case requires calculations")
    labels: list[TurnLabel] = Field(description="one label per interviewer turn after [0]")
    structure: str = Field(description="the candidate's framework as an indented outline")
    key_insight: str = Field(description="the root cause or crux the case turns on")
    recommendation: str = Field(description="the final recommendation; empty if none")
    interviewer_feedback: str = Field(description="feedback/takeaways; empty if none")


class Case(BaseModel):
    id: str
    title: str
    firm: str
    case_type: CaseType
    difficulty: Literal["easy", "moderate", "challenging"]
    source_pages: list[int]
    industry: str
    numerical: bool
    problem_statement: str
    clarifying_qa: list[QA]  # question = short trigger phrase; answer = interviewer verbatim
    structure: str
    data_reveals: list[DataReveal]  # data = interviewer verbatim
    key_insight: str
    recommendation: str
    interviewer_feedback: str
    # interviewer turns labelled 'none' (acks, steers): the simulator may still give them
    other_replies: list[DataReveal] = Field(default_factory=list)
    transcript: list[dict[str, str]] = Field(default_factory=list)  # the real interview


class IndustryChunk(BaseModel):
    industry: str
    aspect: Literal["value_chain", "kpis", "revenue_drivers", "cost_drivers", "trends",
                    "market_structure", "other"]
    text: str = Field(description="the content of this aspect, rewritten as compact notes")
    source_page: int = 0


class IndustryChunks(BaseModel):
    chunks: list[IndustryChunk]
