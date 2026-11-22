"""F17: fact-check critic.

Pass A (MODEL_FAST): extract factual claims from the draft.
Pass B (code + MODEL_FAST): every number in a claim must appear in the evidence (code), and
the model judges each claim against the evidence. The stricter verdict wins, so an invented
number is caught even if the model misses it. A draft passes only if every prospect/product
claim is ``supported``.
"""
from __future__ import annotations

import json

from signalloop.config import get_settings
from signalloop.llm import call_llm
from signalloop.schemas import ClaimList, CriticResult, VerdictList
from signalloop.services import evidence

SEVERITY = {"supported": 0, "unsupported": 1, "contradicted": 2}
CHECKED_TYPES = {"prospect_fact", "product_claim"}
MONEY_MIN = 100_000


def _funding_amounts(doc: dict) -> list[tuple[str, float]]:
    out = []
    for path in ("company.funding.last_round_amount_usd", "company.funding.total_investment_usd"):
        v = evidence.resolve(doc, path)
        if isinstance(v, (int, float)) and v:
            out.append((path, float(v)))
    return out


def code_check(claim: str, doc: dict, pool: list[float]) -> tuple[str, str, str | None]:
    """Deterministic number check -> (verdict, reason, evidence path)."""
    for n in evidence.numbers_in(claim):
        if evidence.number_supported(n, pool):
            continue
        if n >= MONEY_MIN:
            amounts = _funding_amounts(doc)
            if amounts:
                path, v = amounts[0]
                return "contradicted", f"{n:,.0f} does not match the evidence ({v:,.0f})", path
        return "unsupported", f"the number {n:g} does not appear in the evidence", None
    return "supported", "", None


def _verdict_validator(n: int):
    def check(v: VerdictList) -> None:
        if len(v.verdicts) != n:
            raise ValueError(f"return exactly {n} verdicts (one per claim), got {len(v.verdicts)}")
    return check


def review(draft: str, doc: dict) -> CriticResult:
    fast = get_settings().MODEL_FAST
    extracted = call_llm("critic_extract", {"draft": draft}, ClaimList, model=fast, agent="critic")
    for c in extracted.claims:  # one line per claim keeps the numbered list unambiguous
        c.claim = " ".join(c.claim.split())
    checked = [c for c in extracted.claims if c.type in CHECKED_TYPES]
    claims_out: list[dict] = [{"claim": c.claim, "type": c.type, "verdict": "skipped", "source": "n/a"}
                              for c in extracted.claims if c.type not in CHECKED_TYPES]
    if not checked:
        return CriticResult(passed=True, claims=claims_out)

    verdicts = call_llm("critic_check",
                        {"evidence": doc,
                         "claims": "\n".join(f"{i + 1}. {c.claim}" for i, c in enumerate(checked))},
                        VerdictList, model=fast, agent="critic",
                        validator=_verdict_validator(len(checked))).verdicts
    pool = evidence.evidence_numbers(doc)
    violations: list[str] = []
    for c, v in zip(checked, verdicts):
        code_v, code_reason, code_path = code_check(c.claim, doc, pool)
        if SEVERITY[code_v] > SEVERITY[v.verdict]:
            verdict, reason, ev, source = code_v, code_reason, code_path, "code"
        else:
            verdict, reason, ev, source = v.verdict, v.reason, v.evidence, "llm"
        claims_out.append({"claim": c.claim, "type": c.type, "verdict": verdict,
                           "evidence": ev, "reason": reason, "source": source})
        if verdict != "supported":
            violations.append(f"'{c.claim}' is {verdict}: {reason}"
                              + (f" (evidence: {ev})" if ev else ""))
    return CriticResult(passed=not violations, claims=claims_out, violations=violations)


def summarize(result: CriticResult | dict) -> str:
    r = result if isinstance(result, CriticResult) else CriticResult.model_validate(result)
    if r.passed:
        return f"passed ({r.rewrites} rewrite{'s' if r.rewrites != 1 else ''})"
    return f"needs human: {len(r.violations)} issue(s) after {r.rewrites} rewrites"


def dumps(result: CriticResult) -> str:
    return json.dumps(result.model_dump(), indent=2)
