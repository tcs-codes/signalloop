"""Pydantic I/O models for LLM calls and API payloads."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- F8 traits
class Trait(BaseModel):
    dimension: str
    values: list[str]
    coverage: float = Field(ge=0, le=1)
    explanation: str


class PersonaHypothesis(BaseModel):
    titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)


class Traits(BaseModel):
    traits: list[Trait] = Field(default_factory=list)
    persona_hypothesis: PersonaHypothesis = Field(default_factory=PersonaHypothesis)
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- F1 ICP
# ">=" / "<=" are accepted here and rewritten to Crustdata's "=>" / "=<" by the validator.
FilterOp = Literal["=", "!=", "in", "not_in", ">", "<", "=>", "=<", ">=", "<=", "between", "(.)", "[.]"]


class FilterCond(BaseModel):
    field: str
    type: FilterOp
    value: Any


class FitWeights(BaseModel):
    industry: float = 0.3
    headcount: float = 0.25
    funding_stage: float = 0.25
    geo: float = 0.1
    growth: float = 0.1


class ICP(BaseModel):
    company_filters: list[FilterCond] = Field(default_factory=list)
    person_filters: list[FilterCond] = Field(default_factory=list)
    fit_weights: FitWeights = Field(default_factory=FitWeights)
    rationale: str = ""
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- F9 tuning
class FilterPatch(BaseModel):
    direction: Literal["loosen", "tighten"]
    action: Literal["add", "remove", "replace"]
    index: Optional[int] = None          # which company_filters entry (remove/replace)
    condition: Optional[FilterCond] = None  # new condition (add/replace)
    reason: str


class SimilarityReason(BaseModel):
    reason: str
