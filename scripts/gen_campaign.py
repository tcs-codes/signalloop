"""Generate a SYNTHETIC campaign results CSV from contacts already in the DB (demo only).

The planted pattern and generator live in ``signalloop/services/demo_campaign.py``.

    python scripts/gen_campaign.py [--out fixtures/campaign_results.csv] [--seed 42]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signalloop.services.demo_campaign import (  # noqa: E402,F401  (re-exported for tests)
    DEFAULT_CSV, DEFAULT_SEED, FIELDS, generate, reply_probability,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args()
    n = generate(args.out, args.seed)
    print(f"wrote {n} synthetic rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
