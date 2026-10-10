"""
Deterministic validation & finalization (docs/design_report.md §1.5). Nothing the model
says is executed as-is: every call is checked against the claim record, the overrides
already computed in code, and the retrieved evidence before becoming the final response.
"""

from __future__ import annotations

import re

from .llm import ALLOWED_ACTIONS, ALLOWED_DOCUMENTS, empty_usage

DISCLAIMER = (
    "This is a coverage determination based on your plan's policy document and the "
    "information on file. It is not medical advice."
)

ROUTED_TO_BY_DECISION = {
    "route_to_coding_review": "coding_review",
    "route_to_senior_reviewer": "senior_reviewer",
}

# Matches double-quoted spans of at least 4 chars, so a stray quote character
# doesn't get treated as an (empty or trivial) claimed policy citation.
_QUOTED_SPAN_RE = re.compile(r'"([^"]{4,})"')


def _normalize_for_match(text: str) -> str:
    """Collapse whitespace and casefold, so a verbatim quote survives PDF line-wrapping.
    Callers still do an exact substring check; there is no fuzzy matching."""
    return " ".join(text.split()).casefold()


def _strip_quote_edges(text: str) -> str:
    """Relax only a quote's boundary punctuation (a source's trailing ';' often comes
    back as '.', or a leading '-' bullet is dropped); the words must still match exactly."""
    return text.strip(" \t\n.,;:!?\"'-")


def grounding_problem(llm_output: dict, retrieved_chunks: list[dict], claim: dict | None = None) -> str | None:
    """Return a description of the first grounding violation, or None.

    - Every rationale entry has exactly one pointer: a retrieved `section` (with a
      verbatim `excerpt` from that section) or a `claim_field`.
    - Any double-quoted span in `summary` must appear verbatim in the retrieved policy
      text or in the claim's own free-text fields.

    This proves quotes are real, not that they support the assertion; that judgment
    stays with human/SME review."""
    valid_sections = {c["section"] for c in retrieved_chunks}
    section_text = {c["section"]: _normalize_for_match(c["text"]) for c in retrieved_chunks}
    all_policy_text = " ".join(section_text.values())
    claim_text_fields = ("provider_notes", "prior_denial_reason")
    all_claim_text = _normalize_for_match(
        " ".join(str((claim or {}).get(f) or "") for f in claim_text_fields)
    )
    all_quotable_text = all_policy_text + " " + all_claim_text
    rationale = llm_output.get("rationale") or []
    if not rationale:
        return "rationale array was empty -- every decision needs at least one grounded assertion."

    for i, item in enumerate(rationale):
        has_section = bool(item.get("section"))
        has_field = bool(item.get("claim_field"))
        if has_section and has_field:
            return f"rationale[{i}] has both 'section' and 'claim_field' -- use exactly one."
        if not has_section and not has_field:
            return f"rationale[{i}] has neither 'section' nor 'claim_field' -- every assertion needs exactly one."
        if has_section and item["section"] not in valid_sections:
            return (
                f"rationale[{i}] cites section {item['section']!r}, which is not among "
                f"the retrieved section ids {sorted(valid_sections)}."
            )
        if has_section:
            excerpt = (item.get("excerpt") or "").strip()
            if not excerpt:
                return f"rationale[{i}] cites section {item['section']!r} but has no 'excerpt' quoting it."
            if _normalize_for_match(_strip_quote_edges(excerpt)) not in section_text[item["section"]]:
                return (
                    f"rationale[{i}]'s excerpt does not appear verbatim in section {item['section']!r}'s "
                    "retrieved text -- quote it exactly, do not paraphrase or combine sentences."
                )

    for quoted in _QUOTED_SPAN_RE.findall(llm_output.get("summary") or ""):
        if _normalize_for_match(_strip_quote_edges(quoted)) not in all_quotable_text:
            return (
                f"summary quotes {quoted!r} in double quotes, but that text does not appear "
                "verbatim in the retrieved policy or in this claim's own notes -- quote exactly "
                "or drop the quotation marks."
            )

    if llm_output.get("action") not in ALLOWED_ACTIONS:
        return f"action {llm_output.get('action')!r} is not one of the allowed actions."
    return None


def _normalize_rationale(rationale_in: list[dict], retrieved_chunks: list[dict], insurer: str) -> tuple[list[dict], list[str]]:
    """Convert rationale entries to the output shape, dropping (and flagging) ungrounded ones."""
    section_titles = {c["section"]: c["title"] for c in retrieved_chunks}
    notes: list[str] = []
    out: list[dict] = []
    for item in rationale_in:
        assertion = item.get("assertion", "")
        section = item.get("section")
        field = item.get("claim_field")
        if section and section in section_titles:
            out.append(
                {
                    "assertion": assertion,
                    "source": "policy",
                    "insurer": insurer,
                    "section": section,
                    "title": section_titles[section],
                    "excerpt": item.get("excerpt", ""),
                }
            )
        elif field:
            out.append({"assertion": assertion, "source": "claim_data", "field": field})
        else:
            notes.append("dropped_ungrounded_or_hallucinated_assertion")
    return out, notes


def _response(
    claim: dict,
    decision: str,
    summary: str,
    rationale: list[dict],
    confidence: float | None,
    notes: list[str],
    *,
    documents: list[str] | None = None,
    payment: float | None = None,
    token_usage: dict | None = None,
) -> dict:
    """The single output schema every path (LLM, hard override, failure) returns."""
    return {
        "claim_id": claim["claim_id"],
        "decision": decision,
        "summary": summary,
        "rationale": rationale,
        "next_steps": {
            "documents_requested": documents,
            "routed_to": ROUTED_TO_BY_DECISION.get(decision),
            "payment_in_dollars": payment,
            "recommended_action": None,
        },
        "confidence": confidence,
        "token_usage": token_usage or empty_usage(),
        "disclaimer": DISCLAIMER,
        "validator_notes": notes,
    }


def _claim_field_rationale(assertion: str, fields: list[str]) -> list[dict]:
    return [{"assertion": assertion, "source": "claim_data", "field": f} for f in fields]


def validate_and_finalize(claim: dict, llm_output: dict, retrieved_chunks: list[dict]) -> dict:
    """Finalize a grounded LLM decision, correcting invalid arguments rather than executing them."""
    action = llm_output["action"]
    rationale, notes = _normalize_rationale(
        llm_output.get("rationale") or [], retrieved_chunks, claim["insurance_provider"]
    )

    payment = None
    if action == "approve_claim_payment":
        payment = llm_output.get("payment_in_dollars")
        if not payment or payment <= 0 or payment > claim["claimed_amount"]:
            notes.append("invalid_payment_amount_corrected_to_claimed_amount")
            payment = claim["claimed_amount"]

    documents = None
    if action == "request_missing_documentation":
        documents = [d for d in (llm_output.get("documents") or []) if d in ALLOWED_DOCUMENTS]
        if not documents:
            notes.append("empty_or_invalid_documents_list_corrected")
            documents = ["clinical_note"]

    return _response(
        claim, action, llm_output.get("summary", ""), rationale, llm_output.get("confidence"), notes,
        documents=documents, payment=payment, token_usage=llm_output.get("_token_usage"),
    )


def grounding_failure_response(claim: dict, problem: str, llm_output: dict) -> dict:
    """Fail closed when the model is still ungrounded after one correction: the proposed
    action is discarded, the failure and token usage are kept for audit."""
    return _response(
        claim, "route_to_senior_reviewer",
        "Grounding validation failed after one correction attempt; routed to a senior reviewer.",
        _claim_field_rationale(problem, ["claim_id"]), 0.0, ["grounding_retry_exhausted", problem],
        token_usage=llm_output.get("_token_usage"),
    )


def hard_override_response(
    claim: dict,
    decision: str,
    summary: str,
    fields: list[str],
    also_triggered: list[str] | None = None,
    documents: list[str] | None = None,
) -> dict:
    """Response for a rule-based decision that never reached the LLM (design_report.md §1.3).
    also_triggered records other override conditions that fired, so reviewers see all of them."""
    notes = ["hard_override_short_circuit"] + [f"also_triggered_{name}" for name in also_triggered or []]
    return _response(
        claim, decision, summary, _claim_field_rationale(summary, fields), 1.0, notes, documents=documents,
    )


def pipeline_error_response(claim: dict, exc: Exception) -> dict:
    """Response for a claim the pipeline itself failed on: escalate to a human (design_report.md §4.1)."""
    return _response(
        claim, "route_to_senior_reviewer", f"Agent pipeline error during automated review: {exc}",
        _claim_field_rationale(str(exc), ["claim_id"]), 0.0, ["agent_pipeline_error"],
    )
