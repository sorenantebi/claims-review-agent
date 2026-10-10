# Claims Review Agent — Reference Implementation

A local reference implementation of the claims workflow, grounding and validation logic
in `docs/design_report.md` Part 1 (evaluation plan in `docs/evaluation.md`). The production
architecture in Part 2 (claims-platform integration, BAA-covered inference, shadow mode) is
designed, not implemented. Run against sample claim data and three **synthetic**
per-insurer policy documents (only one real example policy PDF was provided; see
`agent/policies_source/`).

## Stack

- **Orchestration**: LangGraph (`agent/graph.py`) — the exact node/edge graph drawn in
  the design report, deterministic override gates first, LLM reasoning only for the
  genuinely interpretive step.
- **Claims storage**: Postgres (`agent/db.py`, `docker-compose.yml`) — a real database,
  not a flat file. `agent/claims_data.json` is seed data only (`scripts/seed_claims_db.py`
  loads it in once); `run.py` and `eval/harness.py` both query the `claims` table.
- **Policy documents**: real PDFs (`agent/policies/*.pdf`) — the two synthetic insurers
  were generated from plain-text sources (`scripts/generate_policy_pdfs.py`,
  `agent/policies_source/*.txt`) specifically so `pypdf` extraction round-trips cleanly,
  modeling a real PDF-based policy corpus rather than pre-extracted text. **Ingestion
  and serving are separate steps**: `scripts/extract_policy_documents.py` extracts each
  PDF to plain text once, offline (`agent/policies_extracted/*.txt`); `agent/retrieval.py`
  — the per-claim, low-latency path — only ever reads that extracted text and never
  parses a PDF itself, refusing to serve a stale extraction if the source PDF changed
  since the last extraction run.
- **LLM**: [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
  (`agent/llm.py`) — authenticated against your **Claude Code subscription**, not a
  separate console.anthropic.com API key. One custom tool, `submit_claim_decision`,
  with every built-in tool stripped out so the model can do nothing else. Token usage
  and cost are captured from the SDK's result metadata on every call.
- **Retrieval**: full-document, insurer-isolated (`agent/retrieval.py`) — each policy
  PDF is 1-2 pages, so the model gets the whole matched insurer's document (still
  chunked into sections for citation ids), not a ranked top-k subset. No BM25/embeddings
  needed at this corpus size; see the module docstring for when that'd change.
- **Tracing**: OpenTelemetry, always on, local JSON-lines log
  (`logs/trace.jsonl`) — plus optional Langfuse if you add keys (`agent/tracing.py`).
- **Validation**: deterministic argument/citation/override checks in
  `agent/validator.py` — nothing the model says is executed as-is.

## Setup

```bash
cd claims-review-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d                    # starts Postgres for claims storage
python -m scripts.seed_claims_db        # loads agent/claims_data.json into it, once
python -m scripts.extract_policy_documents  # extracts agent/policies/*.pdf -> agent/policies_extracted/*.txt
```

**Auth**: if you're already logged into `claude` on this machine, you need to do
nothing else — the Agent SDK reuses that login automatically (verified working). On a
different machine or in CI with no interactive login, run `claude setup-token` once and
put the printed value in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.

**Database**: `DATABASE_URL` in `.env.example` matches `docker-compose.yml`'s default
credentials, so the setup above just works. Point it at any Postgres instance if you'd
rather not use the bundled one. Re-run `scripts/seed_claims_db.py --overwrite` any time
`agent/claims_data.json` changes and you want the DB to reflect it.

**Policy ingestion**: re-run `scripts/extract_policy_documents.py` any time a policy PDF
changes — `agent/retrieval.py` checks the extraction's timestamp against its source PDF
and refuses to serve a stale one, so a forgotten re-extraction fails loudly instead of
silently serving an out-of-date policy.

**Langfuse** (optional): add `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` to `.env` to
also see runs on your Langfuse dashboard — confirmed working: each claim shows up there
as its own "LangGraph" trace. Leave blank and you still get full tracing via the local
`logs/trace.jsonl`. **Note**: `langfuse.langchain.CallbackHandler` needs `langchain`
installed too (already in `requirements.txt`), even though the graph itself only uses
LangGraph.

## Run

```bash
python -m agent.run
```

Processes every claim in the Postgres `claims` table (19 claims — the original 10
from the source spec's numbering, `CLM1001`–`CLM1006`, `CLM1008`–`CLM1009`,
`CLM2001`–`CLM2002` (itself skipping `CLM1007`), plus `CLM3001`–`CLM3009`, added in one
batch to close specific evaluation-coverage gaps — see `docs/evaluation.md` §1.1 for why
each one exists), prints each decision, and writes `logs/results.json` +
`logs/trace.jsonl` (append-only — delete it for a clean log).

Evaluate against the golden set (`eval/golden_set.json`) instead of just running once:

```bash
python -m eval.harness                            # full scorecard
python -m eval.harness --claim CLM1006             # one claim
python -m eval.harness --repeat 3 --claim CLM1006  # consistency check
```

See `docs/evaluation.md` for the full evaluation plan this harness implements a slice of.

## What a real run produced (and what it's worth noticing)

The deterministic routing was exact on every claim, every run — inactive-member,
code-mismatch, missing-essential-doc, and >$5,000 overrides all fire correctly and
short-circuit before any LLM call (all four are hard, pre-LLM gates — see
`docs/design_report.md` §1.3). That part is code, not a model, so it's 100% reproducible.

The LLM reasoning step surfaced findings worth reading as **evaluation findings**, not
implementation bugs — exactly what `docs/evaluation.md` exists to catch, and why several of
these claims are permanent rows in `eval/golden_set.json`:

1. **CLM1001** — the model treats "prior treatment history" in policy §3.2 as requiring
   its own document, not as satisfied by a mention in `provider_notes`, and requests it
   as missing. A *defensible but stricter* reading, and a genuine ambiguity: does a
   narrative mention satisfy a named documentation requirement? This is the gold label
   in `eval/golden_set.json`, included specifically because there's no clean right
   answer — it tests consistency, not a single correct output.
2. **CLM1005** — a real regression was found and fixed here: an earlier retrieval
   setup returned a ranked subset of the policy document, and the model misapplied a
   Prior-Authorization-only documentation rule (§3.2) to this non-PA standard claim.
   Fixed by returning the full policy document instead (`agent/retrieval.py`) — kept in
   the golden set as a permanent regression guard.
3. **CLM1006** (the harder appeal case) has landed on `approve_claim_payment` on one run
   and `route_to_senior_reviewer` on a re-run — expected LLM variance on a genuinely
   ambiguous case, and exactly why `eval/golden_set.json` marks
   `route_to_senior_reviewer` as an acceptable alternate action and why
   `eval/harness.py --repeat` exists (`docs/evaluation.md` §2.3).

None of this needed to be staged — it's what actually running this turns up, and it's a
stronger validation of the evaluation plan than a clean run would have been: the plan's
metrics (grounding recall, hallucination rate, consistency-under-repeat) are aimed
exactly at findings like these.

## Layout

```
claims-review-agent/
  agent/
    graph.py                  # LangGraph workflow: rule gates, retrieval, LLM, validation
    llm.py                    # Claude Agent SDK call + submit_claim_decision tool
    prompt.md                 # system prompt for the LLM step
    validator.py              # deterministic grounding/argument checks, single output schema
    retrieval.py              # per-insurer section chunking, reads policies_extracted/ only
    coding_reference.py       # diagnosis/procedure consistency table
    db.py                     # Postgres claims store
    tracing.py                # OpenTelemetry (+ optional Langfuse) tracing
    run.py                    # entry point
    claims_data.json          # seed data only, loaded into Postgres
    policies/                 # policy PDFs (2 synthetic, generated from policies_source/)
    policies_source/          # plain-text sources for the synthetic PDFs
    policies_extracted/       # offline-extracted text, what retrieval.py reads
  scripts/
    seed_claims_db.py           # claims_data.json -> Postgres
    generate_policy_pdfs.py     # policies_source/*.txt -> policies/*.pdf
    extract_policy_documents.py # policies/*.pdf -> policies_extracted/*.txt
  eval/
    golden_set.json           # gold actions + required evidence
    harness.py                # offline evaluation harness
  docs/
    design_report.md / .pdf   # architecture design report
    evaluation.md             # evaluation plan
    slides.html               # design summary deck
  logs/                       # run output, created on first run (gitignored)
  docker-compose.yml          # local Postgres
  requirements.txt
  .env.example
```
