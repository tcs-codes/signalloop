"""F8: trait extraction with explanations.

Step 1 (pandas, deterministic): value counts and coverage per dimension over the seeds.
Step 2 (LLM): picks/explains traits from those numbers. A validator rejects any trait whose
coverage does not match the computed number, is below 60%, or whose explanation does not
quote the count; the code-computed coverage always wins.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd
from sqlmodel import select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.llm import call_llm
from signalloop.models import Account
from signalloop.schemas import Traits

MIN_COVERAGE = 0.6
MAX_TRAIT_VALUES = 3
DIMENSIONS = ["industry", "headcount_band", "funding_stage", "hq_country",
              "headcount_growth_band", "technologies"]
MULTI_VALUED = {"technologies"}
SENIORITY_VALUES = ["CXO", "Owner / Partner", "Vice President", "Director", "Manager", "Senior"]


def product_text() -> str:
    p: Path = get_settings().fixtures_dir / "product.md"
    return p.read_text(encoding="utf-8") if p.exists() else "(no product description provided)"


def seed_accounts() -> list[Account]:
    with session_scope() as s:
        return list(s.exec(select(Account).where(Account.is_seed == True)).all())  # noqa: E712


def seed_frame(accounts: Iterable[Account]) -> pd.DataFrame:
    rows = [(a.firmographics_json or {}).get("normalized", {}) for a in accounts]
    df = pd.DataFrame(rows)
    for d in DIMENSIONS:
        if d not in df.columns:
            df[d] = None
    return df


def coverage_for(df: pd.DataFrame, dimension: str, values: list[str]) -> tuple[int, float]:
    n = len(df)
    if n == 0:
        return 0, 0.0
    vals = {str(v).lower() for v in values}
    if dimension in MULTI_VALUED:
        hit = df[dimension].apply(lambda xs: isinstance(xs, list) and
                                  vals <= {str(x).lower() for x in xs})
    else:
        hit = df[dimension].apply(lambda x: x is not None and not pd.isna(x) and str(x).lower() in vals)
    count = int(hit.sum())
    return count, round(count / n, 4)


def compute_stats(accounts: list[Account]) -> dict:
    df = seed_frame(accounts)
    n = len(df)
    dims: dict[str, dict] = {}
    for d in DIMENSIONS:
        if d in MULTI_VALUED:
            exploded = df[d].apply(lambda xs: xs if isinstance(xs, list) else []).explode().dropna()
            counts = exploded.value_counts()
            missing = int(df[d].apply(lambda xs: not xs).sum())
            common = [v for v, c in counts.items() if n and c / n >= MIN_COVERAGE][:MAX_TRAIT_VALUES]
            cand_values = common
        else:
            series = df[d].dropna()
            counts = series.astype(str).value_counts()
            missing = n - len(series)
            cand_values, cum = [], 0
            for v, c in counts.items():
                if len(cand_values) >= MAX_TRAIT_VALUES:
                    break
                cand_values.append(v)
                cum += c
                if n and cum / n >= MIN_COVERAGE:
                    break
        count, cov = coverage_for(df, d, cand_values) if cand_values else (0, 0.0)
        dims[d] = {
            "value_counts": {str(k): int(v) for k, v in counts.items()},
            "missing": missing,
            "candidate": {"values": [str(v) for v in cand_values], "count": count, "coverage": cov},
        }
    return {"n": n, "dimensions": dims}


def _validator(stats: dict, df: pd.DataFrame):
    n = stats["n"]

    def check(t: Traits) -> None:
        errors = []
        for tr in t.traits:
            if tr.dimension not in stats["dimensions"]:
                errors.append(f"unknown dimension '{tr.dimension}'")
                continue
            count, cov = coverage_for(df, tr.dimension, tr.values)
            if abs(cov - tr.coverage) > 0.01:
                errors.append(f"{tr.dimension}: coverage {tr.coverage} but values {tr.values} "
                              f"cover {count} of {n} = {cov}")
            if cov < MIN_COVERAGE:
                errors.append(f"{tr.dimension}: coverage {cov} is below {MIN_COVERAGE}; drop it")
            if f"{count} of {n}" not in tr.explanation and f"{round(cov * 100)}%" not in tr.explanation:
                errors.append(f"{tr.dimension}: explanation must quote '{count} of {n}'")
        if errors:
            raise ValueError("; ".join(errors))

    return check


def extract_traits(accounts: list[Account] | None = None) -> tuple[Traits, dict]:
    accounts = accounts if accounts is not None else seed_accounts()
    if not accounts:
        raise ValueError("no seed accounts; import seeds first")
    stats = compute_stats(accounts)
    df = seed_frame(accounts)
    traits = call_llm(
        "extract_traits",
        {"stats": stats["dimensions"], "n": stats["n"], "product": product_text(),
         "seniority_values": ", ".join(SENIORITY_VALUES)},
        Traits, model=get_settings().MODEL_STRONG, agent="icp_strategist",
        validator=_validator(stats, df),
    )
    for tr in traits.traits:  # code-computed numbers are authoritative
        tr.coverage = coverage_for(df, tr.dimension, tr.values)[1]
    return traits, stats
