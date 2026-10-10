"""
Offline evaluation (docs/evaluation.md): runs the compiled graph against
eval/golden_set.json and computes deterministic metrics. Needs the seeded database.

    python -m eval.harness                              # full scorecard
    python -m eval.harness --claim CLM1006              # one claim
    python -m eval.harness --repeat 3 --claim CLM1006   # consistency check (§2.3)
"""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter, defaultdict

from agent import db
from agent.graph import build_graph, review_claim
from agent.tracing import langgraph_config
from agent.validator import _normalize_for_match as _norm

GOLDEN_SET_PATH = pathlib.Path(__file__).resolve().parent / "golden_set.json"
RESULTS_PATH = pathlib.Path(__file__).resolve().parent / "results.json"


def load_golden_set() -> list[dict]:
    return json.loads(GOLDEN_SET_PATH.read_text())


def run_claim(app, claim_id: str, config: dict) -> dict:
    claim = db.get_claim(claim_id)
    if claim is None:
        raise SystemExit(f"{claim_id} not in the claims database. Run `python -m scripts.seed_claims_db`.")
    return review_claim(app, claim, config)


def cited_sections_and_fields(final: dict) -> tuple[set[str], set[str]]:
    sections = {r["section"] for r in final.get("rationale", []) if r.get("source") == "policy"}
    fields = {r["field"] for r in final.get("rationale", []) if r.get("source") == "claim_data"}
    return sections, fields


def grounding_recall(final: dict, required: dict) -> float:
    req_sections = set(required.get("sections", []))
    req_fields = set(required.get("claim_fields", []))
    total_required = len(req_sections) + len(req_fields)
    if total_required == 0:
        return 1.0
    got_sections, got_fields = cited_sections_and_fields(final)
    hit = len(req_sections & got_sections) + len(req_fields & got_fields)
    return hit / total_required


def snippet_fidelity(final: dict, required: dict) -> float | None:
    """Share of curated snippets ({section: operative sentence}) that overlap the model's
    excerpt for that section. Stricter than grounding_recall, which only checks the
    section id. None when the row has no curated snippets, so it isn't scored as 0."""
    snippets: dict = required.get("snippets") or {}
    if not snippets:
        return None
    by_section = {
        r["section"]: _norm(r.get("excerpt", ""))
        for r in final.get("rationale", [])
        if r.get("source") == "policy"
    }
    hits = 0
    for section, golden in snippets.items():
        got = by_section.get(section, "")
        golden_norm = _norm(golden)
        if got and (golden_norm in got or got in golden_norm):
            hits += 1
    return hits / len(snippets)


def score_one(gold: dict, final: dict) -> dict:
    predicted = final["decision"]
    acceptable = {gold["gold_action"], *gold.get("acceptable_alternate_actions", [])}
    exact_match = predicted == gold["gold_action"]
    match_with_alternate = predicted in acceptable

    arg_correct = True
    if match_with_alternate and predicted == "approve_claim_payment" and "gold_payment" in gold:
        arg_correct = final["next_steps"]["payment_in_dollars"] == gold["gold_payment"]
    elif match_with_alternate and predicted == "request_missing_documentation" and "gold_documents" in gold:
        arg_correct = set(final["next_steps"]["documents_requested"] or []) == set(gold["gold_documents"])

    # Currently unreachable (ungrounded output is escalated before _normalize_rationale);
    # kept as a tripwire. grounding_exhausted is the meaningful signal.
    hallucinated = "dropped_ungrounded_or_hallucinated_assertion" in final.get("validator_notes", [])
    grounding_exhausted = "grounding_retry_exhausted" in final.get("validator_notes", [])
    usage = final.get("token_usage", {})

    return {
        "claim_id": gold["claim_id"],
        "difficulty": gold["difficulty"],
        "path": gold["path"],
        "predicted": predicted,
        "gold": gold["gold_action"],
        "exact_match": exact_match,
        "match_with_alternate": match_with_alternate,
        "arg_correct": arg_correct,
        "grounding_recall": round(grounding_recall(final, gold["required_evidence"]), 3),
        "snippet_fidelity": snippet_fidelity(final, gold["required_evidence"]),
        "hallucinated_or_ungrounded": hallucinated,
        "grounding_exhausted": grounding_exhausted,
        "confidence": final.get("confidence"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": usage.get("cost_usd", 0.0),
    }


def print_scorecard(rows: list[dict]) -> None:
    print(f"\n{'claim':8s} {'diff':5s} {'path':8s} {'predicted':28s} {'gold':28s} {'exact':6s} {'alt':4s} {'arg':4s} {'recall':6s} {'halluc':6s} {'tokens':8s}")
    for r in rows:
        print(
            f"{r['claim_id']:8s} {r['difficulty']:5s} {r['path']:8s} {r['predicted']:28s} {r['gold']:28s} "
            f"{str(r['exact_match']):6s} {str(r['match_with_alternate']):4s} {str(r['arg_correct']):4s} "
            f"{r['grounding_recall']:<6} {str(r['hallucinated_or_ungrounded']):6s} {r['total_tokens']:<8}"
        )

    n = len(rows)
    exact_acc = sum(r["exact_match"] for r in rows) / n
    alt_acc = sum(r["match_with_alternate"] for r in rows) / n
    arg_acc = sum(r["arg_correct"] for r in rows if r["match_with_alternate"]) / max(
        1, sum(r["match_with_alternate"] for r in rows)
    )
    avg_recall = sum(r["grounding_recall"] for r in rows) / n
    halluc_rate = sum(r["hallucinated_or_ungrounded"] for r in rows) / n
    exhausted_rate = sum(r["grounding_exhausted"] for r in rows) / n
    llm_rows = [r for r in rows if r["path"] == "llm"]
    total_tokens = sum(r["total_tokens"] for r in rows)
    total_cost = sum(r["cost_usd"] for r in rows)
    avg_llm_tokens = sum(r["total_tokens"] for r in llm_rows) / max(1, len(llm_rows))
    curated = [r["snippet_fidelity"] for r in rows if r["snippet_fidelity"] is not None]
    snippet_str = f"{(sum(curated) / len(curated)):.0%} (n={len(curated)} curated)" if curated else "n/a (none curated yet)"

    print(f"\nOverall (n={n}): exact_match={exact_acc:.0%}  with_alternates={alt_acc:.0%}  "
          f"arg_correct|matched={arg_acc:.0%}  avg_grounding_recall={avg_recall:.0%}  hallucination_rate={halluc_rate:.0%}  "
          f"grounding_exhausted_rate={exhausted_rate:.0%}  avg_snippet_fidelity={snippet_str}")
    print(f"Token usage: total={total_tokens:,}  avg_llm_claim={avg_llm_tokens:,.0f}  cost_usd={total_cost:.4f}")

    for key in ("difficulty", "path"):
        print(f"\nBy {key}:")
        buckets = defaultdict(list)
        for r in rows:
            buckets[r[key]].append(r)
        for k, group in sorted(buckets.items()):
            gn = len(group)
            gacc = sum(r["match_with_alternate"] for r in group) / gn
            grec = sum(r["grounding_recall"] for r in group) / gn
            print(f"  {k:10s} n={gn:2d}  accuracy(w/ alternates)={gacc:.0%}  avg_grounding_recall={grec:.0%}")


def run_repeat(app, claim_id: str, n: int, config: dict) -> None:
    decisions = []
    for i in range(n):
        final = run_claim(app, claim_id, config)
        decisions.append(final["decision"])
        print(f"  run {i + 1}/{n}: {final['decision']} (confidence={final.get('confidence')})")
    counts = Counter(decisions)
    agreement = counts.most_common(1)[0][1] / n
    print(f"\n{claim_id} consistency over {n} runs: {dict(counts)}  |  agreement rate={agreement:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim", help="Run just this claim_id instead of the full golden set")
    parser.add_argument("--repeat", type=int, help="Run the selected claim N times to measure consistency")
    args = parser.parse_args()

    golden = load_golden_set()
    if args.claim:
        golden = [g for g in golden if g["claim_id"] == args.claim]
        if not golden:
            raise SystemExit(f"{args.claim} not found in golden_set.json")

    app, config = build_graph(), langgraph_config()

    if args.repeat:
        if not args.claim:
            raise SystemExit("--repeat requires --claim")
        run_repeat(app, args.claim, args.repeat, config)
        return

    rows = []
    for gold in golden:
        print(f"[eval] processing {gold['claim_id']}...", flush=True)
        final = run_claim(app, gold["claim_id"], config)
        row = score_one(gold, final)
        rows.append(row)
        print(
            f"[eval] {gold['claim_id']}: {row['predicted']} "
            f"(match={row['match_with_alternate']}, path={row['path']})",
            flush=True,
        )

    print_scorecard(rows)
    RESULTS_PATH.write_text(json.dumps(rows, indent=2))
    print(f"\nSaved: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
