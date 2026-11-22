"""F7: seed-customer import (domains -> identify -> enrich -> seed accounts, suppressed)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from signalloop.models import Account
from signalloop.services import store, suppression
from signalloop.tools.crustdata import get_client, looks_like_domain, normalize_domain

MIN_CONFIDENCE = 0.5


@dataclass
class SeedImportResult:
    accounts: list[Account] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def parse_domains(text: str) -> list[str]:
    """Newline-, comma-, semicolon- or whitespace-separated list -> unique normalized domains."""
    out: list[str] = []
    for tok in re.split(r"[\s,;]+", text or ""):
        tok = tok.strip().strip('"').strip("'")
        if not tok or tok.lower() == "domain":
            continue
        d = normalize_domain(tok)
        if d and d not in out:
            out.append(d)
    return out


def parse_csv(source: str | Path | IO[str] | bytes) -> list[str]:
    """CSV with a ``domain`` column (case-insensitive header)."""
    if isinstance(source, bytes):
        source = io.StringIO(source.decode("utf-8-sig"))
    elif isinstance(source, (str, Path)) and Path(source).exists():
        source = Path(source).open(newline="", encoding="utf-8-sig")
    elif isinstance(source, str):
        source = io.StringIO(source)
    reader = csv.DictReader(source)
    col = next((h for h in (reader.fieldnames or []) if h and h.strip().lower() == "domain"), None)
    if col is None:
        raise ValueError("CSV needs a 'domain' column")
    return parse_domains("\n".join(row.get(col) or "" for row in reader))


def _best_match(entry: dict, domain: str) -> dict | None:
    matches = [m for m in entry.get("matches") or [] if (m.get("confidence_score") or 0) >= MIN_CONFIDENCE]
    if not matches:
        return None
    exact = [m for m in matches
             if normalize_domain(((m.get("company_data") or {}).get("basic_info") or {})
                                 .get("primary_domain") or "") == domain]
    return (exact or matches)[0]


def import_seeds(domains: list[str]) -> SeedImportResult:
    client = get_client()
    result = SeedImportResult()
    resolved: dict[str, str] = {}  # crustdata id -> input domain
    for raw in domains:
        d = normalize_domain(raw)
        if not d or not looks_like_domain(d):
            result.unresolved.append(raw)
            continue
        entries = client.company_identify(d)
        match = _best_match(entries[0], d) if entries else None
        cid = (match or {}).get("company_data", {}).get("crustdata_company_id")
        if cid is None:
            result.unresolved.append(d)
            continue
        resolved[str(cid)] = d

    if resolved:
        enriched = client.company_enrich(company_ids=list(resolved))
        seen: set[str] = set()
        for entry in enriched:
            m = (entry.get("matches") or [None])[0]
            data = (m or {}).get("company_data")
            if not data:
                continue
            cid = str(data.get("crustdata_company_id") or entry.get("matched_on"))
            data.setdefault("crustdata_company_id", cid)
            data.setdefault("basic_info", {}).setdefault("primary_domain", resolved.get(cid, ""))
            acc = store.upsert_account(data, is_seed=True)
            suppression.suppress_seed(acc)
            result.accounts.append(acc)
            seen.add(cid)
        result.unresolved.extend(d for cid, d in resolved.items() if cid not in seen)
    return result
