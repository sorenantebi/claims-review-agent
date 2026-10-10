"""
Seed the Postgres claims table from agent/claims_data.json (needs `docker compose up -d`).

    python -m scripts.seed_claims_db              # insert missing rows only
    python -m scripts.seed_claims_db --overwrite  # upsert every row
"""

from __future__ import annotations

import argparse

from agent import db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true", help="Upsert every row, not just missing ones")
    args = parser.parse_args()

    written = db.seed_from_json(overwrite=args.overwrite)
    total = len(db.list_claims())
    print(f"Wrote {written} row(s). Table now has {total} claim(s) total.")


if __name__ == "__main__":
    main()
