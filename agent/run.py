"""
Run the claims-review agent over every claim in the database and write logs/results.json.

    python -m agent.run
"""

from __future__ import annotations

import json
from pathlib import Path

from . import db
from .graph import build_graph, review_claim
from .tracing import TRACE_FILE, langgraph_config

RESULTS_FILE = Path(__file__).resolve().parent.parent / "logs" / "results.json"


def main() -> None:
    claims = db.list_claims()
    if not claims:
        raise SystemExit("No claims in the database. Run `docker compose up -d` then `python -m scripts.seed_claims_db`.")
    app, config = build_graph(), langgraph_config()

    results = []
    for claim in claims:
        print(f"\n=== {claim['claim_id']} ({claim['insurance_provider']}, {claim['claim_type']}) ===")
        final = review_claim(app, claim, config)
        print(json.dumps(final, indent=2))
        results.append(final)

    RESULTS_FILE.parent.mkdir(exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {len(results)} results to {RESULTS_FILE}\nLocal trace log: {TRACE_FILE}")


if __name__ == "__main__":
    main()
