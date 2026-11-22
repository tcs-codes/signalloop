"""F26: segment lift analysis.

For every dimension, every pair of the 3 dimensions whose reply rates vary most, and the
triple of those 3, compute n, responders, rate, baseline, lift = rate / baseline. Segments with n < MIN_N_SHOW
are hidden; n < LOW_CONFIDENCE_N is flagged. An LLM narrative covers the top 3 positive and
top 2 negative segments and may only use numbers from the table (validated).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import pandas as pd
from sqlmodel import delete, select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.llm import call_llm
from signalloop.models import Campaign, Insight
from signalloop.schemas import InsightNarrative
from signalloop.services import campaigns, evidence

MIN_N_SHOW = 10
LOW_CONFIDENCE_N = 30
PAIR_DIMS = 3
SEP = " × "


@dataclass
class AnalysisResult:
    campaign_id: int
    baseline: float
    total: int
    insights: list[Insight] = field(default_factory=list)
    narrative: InsightNarrative | None = None

    def top(self, k: int = 5, positive: bool = True) -> list[Insight]:
        rows = [i for i in self.insights if (i.lift > 1 if positive else i.lift < 1)]
        return sorted(rows, key=lambda i: i.lift, reverse=positive)[:k]


def load_frame(campaign_id: int) -> pd.DataFrame:
    with session_scope() as s:
        c = s.get(Campaign, campaign_id)
    path = (c.analysis_json or {}).get("frame_path") if c else None
    if path and Path(path).exists():
        return pd.read_parquet(path)
    return campaigns.build_frame(campaign_id)


def segment_table(df: pd.DataFrame, dims: list[str]) -> pd.DataFrame:
    baseline = df["responded"].mean() if len(df) else 0.0
    g = df.groupby(dims, dropna=False)["responded"].agg(n="size", responders="sum").reset_index()
    g["rate"] = g["responders"] / g["n"]
    g["baseline_rate"] = baseline
    g["lift"] = g["rate"] / baseline if baseline else 0.0
    g["dimension"] = SEP.join(dims)
    g["segment_value"] = g[dims].astype(str).agg(SEP.join, axis=1)
    return g[["dimension", "segment_value", "n", "responders", "rate", "baseline_rate", "lift"]]


def dimension_spread(df: pd.DataFrame, dim: str) -> float:
    t = segment_table(df, [dim])
    t = t[t["n"] >= MIN_N_SHOW]
    if len(t) < 2:
        return 0.0
    mean = (t["rate"] * t["n"]).sum() / t["n"].sum()
    return float(((t["rate"] - mean) ** 2 * t["n"]).sum() / t["n"].sum())


def compute(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    dims = [d for d in campaigns.CORE_DIMS if df[d].nunique() > 1]
    tables = [segment_table(df, [d]) for d in dims]
    ranked = sorted(dims, key=lambda d: dimension_spread(df, d), reverse=True)[:PAIR_DIMS]
    # pairs of the top dims, plus their triple: a pattern such as "Series A x 20-50 staff x VP+"
    # is diluted in every pair (each pair mixes in the third dimension's weak values)
    tables += [segment_table(df, list(p)) for k in (2, 3) for p in combinations(ranked, k)]
    out = pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()
    if len(out):
        out = out[out["n"] >= MIN_N_SHOW].copy()
        out["low_confidence"] = out["n"] < LOW_CONFIDENCE_N
        out = out[~out["segment_value"].str.contains("unknown")]
    return out, ranked


def _fmt_row(i: Insight) -> str:
    return (f"- {i.dimension} = {i.segment_value}: n={i.n}, replies={i.responders}, "
            f"rate={i.rate * 100:.1f}%, lift={i.lift:.1f}x" + (" (low confidence)" if i.low_confidence else ""))


def _number_validator(pool: list[float]):
    def check(n: InsightNarrative) -> None:
        text = " ".join([n.headline, n.narrative, *n.positives, *n.negatives])
        bad = [x for x in evidence.numbers_in(text) if not evidence.number_supported(x, pool)]
        if bad:
            raise ValueError(f"these numbers are not in the results table: {sorted(set(bad))}. "
                             "Use only rates, lifts, n, replies and the baseline as given.")
    return check


def analyze(campaign_id: int, narrate: bool = True) -> AnalysisResult:
    df = load_frame(campaign_id)
    if df.empty:
        raise ValueError(f"campaign {campaign_id} has no outcomes; import and enrich it first")
    table, pair_dims = compute(df)
    baseline = float(df["responded"].mean())
    with session_scope() as s:
        s.exec(delete(Insight).where(Insight.campaign_id == campaign_id))
        rows = []
        for r in table.itertuples(index=False):
            ins = Insight(campaign_id=campaign_id, dimension=r.dimension, segment_value=str(r.segment_value),
                          n=int(r.n), responders=int(r.responders), rate=round(float(r.rate), 4),
                          baseline_rate=round(float(r.baseline_rate), 4), lift=round(float(r.lift), 3),
                          low_confidence=bool(r.low_confidence))
            s.add(ins)
            rows.append(ins)
        s.flush()
    res = AnalysisResult(campaign_id, baseline, len(df), rows)

    if narrate and rows:
        pos, neg = res.top(3, True), res.top(2, False)
        pool = [baseline, baseline * 100, len(df)]
        for i in pos + neg:
            pool += [i.n, i.responders, i.rate, i.rate * 100, i.lift, round(i.lift, 1), round(i.rate * 100, 1)]
            pool += evidence.numbers_in(i.segment_value.replace("-", " "))   # e.g. the "20-50" band
        res.narrative = call_llm(
            "segment_insights",
            {"baseline_pct": f"{baseline * 100:.1f}", "total": len(df),
             "positives": "\n".join(_fmt_row(i) for i in pos) or "(none)",
             "negatives": "\n".join(_fmt_row(i) for i in neg) or "(none)"},
            InsightNarrative, model=get_settings().MODEL_STRONG, agent="campaign_analyst",
            validator=_number_validator(pool))
        top = pos[0] if pos else None
        with session_scope() as s:
            if top is not None:
                s.get(Insight, top.id).narrative = res.narrative.headline
            c = s.get(Campaign, campaign_id)
            c.analysis_json = {**(c.analysis_json or {}), "baseline": baseline, "total": len(df),
                               "pair_dims": pair_dims, "narrative": res.narrative.model_dump(),
                               "top_positive_ids": [i.id for i in pos], "top_negative_ids": [i.id for i in neg]}
    return res


def insights_for(campaign_id: int) -> list[Insight]:
    with session_scope() as s:
        return list(s.exec(select(Insight).where(Insight.campaign_id == campaign_id)
                           .order_by(Insight.lift.desc())).all())
