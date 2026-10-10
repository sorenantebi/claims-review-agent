"""
Postgres claims store (docs/design_report.md Part 2). run.py and eval/harness.py read
claims through get_claim()/list_claims(); claims_data.json is seed data only.

A real deployment would consume a CDC stream from the payer's claims platform instead of
owning this table. Connection: DATABASE_URL, defaulting to docker-compose.yml's credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

DEFAULT_DATABASE_URL = "postgresql://claims:claims@localhost:5432/claims"
SEED_PATH = Path(__file__).resolve().parent / "claims_data.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    claim_id             TEXT PRIMARY KEY,
    member_status        TEXT NOT NULL,
    claim_type           TEXT NOT NULL,
    insurance_provider   TEXT NOT NULL,
    diagnosis_code       TEXT NOT NULL,
    procedure_code       TEXT NOT NULL,
    claimed_amount       NUMERIC NOT NULL,
    supporting_documents JSONB NOT NULL DEFAULT '[]'::jsonb,
    provider_notes       TEXT,
    prior_denial_reason  TEXT
);
"""

_COLUMNS = (
    "claim_id",
    "member_status",
    "claim_type",
    "insurance_provider",
    "diagnosis_code",
    "procedure_code",
    "claimed_amount",
    "supporting_documents",
    "provider_notes",
    "prior_denial_reason",
)


def database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_connection() -> psycopg.Connection:
    # One connection per call: the callers are short CLI runs, not a server.
    conn = psycopg.connect(database_url(), row_factory=dict_row)
    ensure_schema(conn)
    return conn


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def _row_to_claim(row: dict[str, Any]) -> dict[str, Any]:
    claim = dict(row)
    claim["claimed_amount"] = float(claim["claimed_amount"])
    return claim


def get_claim(claim_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM claims WHERE claim_id = %s", (claim_id,)
        ).fetchone()
        return _row_to_claim(row) if row else None
    finally:
        conn.close()


def list_claims() -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM claims ORDER BY claim_id").fetchall()
        return [_row_to_claim(r) for r in rows]
    finally:
        conn.close()


def upsert_claim(claim: dict[str, Any]) -> None:
    """Insert or update one claim (what a CDC event handler would call per message)."""
    conn = get_connection()
    try:
        payload = {k: claim.get(k) for k in _COLUMNS}
        payload["supporting_documents"] = json.dumps(claim.get("supporting_documents") or [])
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO claims ({", ".join(_COLUMNS)})
                VALUES ({", ".join(f"%({c})s" for c in _COLUMNS)})
                ON CONFLICT (claim_id) DO UPDATE SET
                    {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c != "claim_id")}
                """,
                payload,
            )
        conn.commit()
    finally:
        conn.close()


def seed_from_json(path: Path = SEED_PATH, *, overwrite: bool = False) -> int:
    """Load claims_data.json, skipping existing rows unless overwrite. Returns rows written."""
    claims = json.loads(path.read_text())
    conn = get_connection()
    try:
        existing = {r["claim_id"] for r in conn.execute("SELECT claim_id FROM claims").fetchall()}
    finally:
        conn.close()

    written = 0
    for claim in claims:
        if claim["claim_id"] in existing and not overwrite:
            continue
        upsert_claim(claim)
        written += 1
    return written
