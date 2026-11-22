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


# ---------------------------------------------------------------- F15 brief
class EvidenceRef(BaseModel):
    field: str          # path into the evidence JSON, e.g. company.funding.last_round_amount_usd
    value: str          # the value as quoted from that path


class BriefBullet(BaseModel):
    text: str
    refs: list[int] = Field(default_factory=list)   # indices into Brief.evidence_refs


class Brief(BaseModel):
    headline: str
    bullets: list[BriefBullet]
    recommended_angle: Literal["funding", "hiring", "growth", "leader_change"]
    evidence_refs: list[EvidenceRef]
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- F16 drafts
class EmailDraft(BaseModel):
    subject: str
    body: str
    missing: list[str] = Field(default_factory=list)


class LinkedInDraft(BaseModel):
    note: str
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- F17 critic
ClaimType = Literal["prospect_fact", "product_claim", "other"]
Verdict = Literal["supported", "unsupported", "contradicted"]


class ExtractedClaim(BaseModel):
    claim: str
    type: ClaimType


class ClaimList(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)


class ClaimVerdict(BaseModel):
    claim: str
    verdict: Verdict
    evidence: Optional[str] = None     # evidence path or quote that supports/contradicts it
    reason: str = ""


class VerdictList(BaseModel):
    verdicts: list[ClaimVerdict] = Field(default_factory=list)


class CriticResult(BaseModel):
    passed: bool
    claims: list[dict] = Field(default_factory=list)     # claim, type, verdict, evidence, reason, source
    violations: list[str] = Field(default_factory=list)
    rewrites: int = 0
    needs_human: bool = False
