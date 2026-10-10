"""
LangGraph workflow (docs/design_report.md §1.3).

Every objective rule is a hard, pre-LLM gate; only claims that pass all of them
reach retrieval and the model:

- inactive member      -> deny_claim (always wins: no coverage relationship exists)
- exactly one of {high_amount, mismatch, missing_docs} -> that condition's own action
- two or more of them  -> route_to_senior_reviewer (no condition outranks another)

check_overrides() evaluates every condition independently, so all fired conditions
are recorded in the final response's validator_notes, not just the one that decided
the action (see CLM1003/CLM1004/CLM3001-CLM3004 in eval/golden_set.json).
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from . import llm, retrieval, validator
from .coding_reference import is_consistent
from .tracing import traced

ESSENTIAL_DOCS = {
    "preapproval": ["clinical_note"],
    # Every insurer's Appeals section (§8) requires a written appeal request.
    "appeal": ["appeal_letter"],
}
HIGH_AMOUNT_THRESHOLD = 5000

# Display order for overrides_triggered, not a priority order.
CONDITION_ORDER = ["inactive", "high_amount", "mismatch", "missing_docs"]

CONDITION_FIELDS = {
    "high_amount": ["claimed_amount"],
    "mismatch": ["diagnosis_code", "procedure_code"],
    "missing_docs": ["supporting_documents"],
}


class ClaimState(TypedDict, total=False):
    claim: dict[str, Any]
    override: str | None  # a CONDITION_ORDER name, "multiple", or None
    overrides_triggered: list[str]
    retrieved_chunks: list[dict]
    llm_output: dict
    grounding_error: str | None
    final: dict


def missing_documents(claim: dict) -> list[str]:
    required = ESSENTIAL_DOCS.get(claim["claim_type"], [])
    return [d for d in required if d not in claim.get("supporting_documents", [])]


def _also_triggered(state: ClaimState, resolved_by: str) -> list[str]:
    return [name for name in state.get("overrides_triggered", []) if name != resolved_by]


@traced("classify_claim")
def classify_claim(state: ClaimState) -> ClaimState:
    # Placeholder for field normalization in a real system; kept as its own span
    # so the trace matches the design report's graph.
    return state


@traced("check_overrides")
def check_overrides(state: ClaimState) -> ClaimState:
    claim = state["claim"]
    fired = {
        "inactive": claim["member_status"] == "inactive",
        "mismatch": not is_consistent(claim["diagnosis_code"], claim["procedure_code"]),
        "missing_docs": bool(missing_documents(claim)),
        "high_amount": claim["claimed_amount"] > HIGH_AMOUNT_THRESHOLD,
    }
    triggered = [name for name in CONDITION_ORDER if fired[name]]
    others = [name for name in triggered if name != "inactive"]

    if fired["inactive"]:
        override = "inactive"
    elif len(others) >= 2:
        override = "multiple"
    else:
        override = others[0] if others else None

    return {**state, "override": override, "overrides_triggered": triggered}


@traced("deny_inactive")
def deny_inactive(state: ClaimState) -> ClaimState:
    final = validator.hard_override_response(
        state["claim"], "deny_claim", "Plan inactive at time of service.", ["member_status"],
        also_triggered=_also_triggered(state, "inactive"),
    )
    return {**state, "final": final}


@traced("coding_review")
def coding_review(state: ClaimState) -> ClaimState:
    claim = state["claim"]
    summary = (
        f"Diagnosis code {claim['diagnosis_code']} is not clinically consistent with "
        f"procedure code {claim['procedure_code']} per standard coding guidelines. "
        "Flagged for medical coding review; not approved."
    )
    final = validator.hard_override_response(
        claim, "route_to_coding_review", summary, CONDITION_FIELDS["mismatch"],
        also_triggered=_also_triggered(state, "mismatch"),
    )
    return {**state, "final": final}


@traced("high_amount_review")
def high_amount_review(state: ClaimState) -> ClaimState:
    claim = state["claim"]
    summary = (
        f"Claimed amount ${claim['claimed_amount']:,} exceeds the ${HIGH_AMOUNT_THRESHOLD:,} "
        "auto-review threshold. Routed directly to a senior medical reviewer without an "
        "automated coverage recommendation."
    )
    final = validator.hard_override_response(
        claim, "route_to_senior_reviewer", summary, CONDITION_FIELDS["high_amount"],
        also_triggered=_also_triggered(state, "high_amount"),
    )
    return {**state, "final": final}


@traced("multi_override_review")
def multi_override_review(state: ClaimState) -> ClaimState:
    triggered = state["overrides_triggered"]
    summary = (
        f"Multiple review conditions triggered simultaneously: {', '.join(triggered)}. "
        "No single condition takes priority over another here; escalated directly to "
        "a senior medical reviewer rather than resolving to one condition's specific action."
    )
    fields = [f for name in triggered for f in CONDITION_FIELDS[name]]
    final = validator.hard_override_response(state["claim"], "route_to_senior_reviewer", summary, fields)
    return {**state, "final": final}


@traced("request_docs")
def request_docs(state: ClaimState) -> ClaimState:
    claim = state["claim"]
    missing = missing_documents(claim)
    summary = (
        f"Required documentation for a {claim['claim_type']} claim is incomplete: "
        f"missing {missing}. Claim marked pending additional information."
    )
    final = validator.hard_override_response(
        claim, "request_missing_documentation", summary, CONDITION_FIELDS["missing_docs"],
        also_triggered=_also_triggered(state, "missing_docs"), documents=missing,
    )
    return {**state, "final": final}


@traced("retrieve_policy_evidence")
def retrieve_policy_evidence(state: ClaimState) -> ClaimState:
    return {**state, "retrieved_chunks": retrieval.retrieve(state["claim"]["insurance_provider"])}


@traced("llm_decision")
def llm_decision(state: ClaimState) -> ClaimState:
    claim, chunks = state["claim"], state["retrieved_chunks"]
    output = llm.decide(claim, chunks)

    problem = validator.grounding_problem(output, chunks, claim)
    if problem:
        # One retry with the exact problem (design_report.md §1.5), rather than silently dropping bad citations.
        retry = llm.decide(claim, chunks, correction_note=problem)
        retry["_token_usage"] = llm.add_usage(output["_token_usage"], retry["_token_usage"])
        output = retry

    return {
        **state,
        "llm_output": output,
        "grounding_error": validator.grounding_problem(output, chunks, claim),
    }


@traced("validate_and_finalize")
def validate_and_finalize(state: ClaimState) -> ClaimState:
    if state.get("grounding_error"):
        final = validator.grounding_failure_response(state["claim"], state["grounding_error"], state["llm_output"])
    else:
        final = validator.validate_and_finalize(state["claim"], state["llm_output"], state["retrieved_chunks"])
    return {**state, "final": final}


OVERRIDE_NODES = {
    "inactive": deny_inactive,
    "mismatch": coding_review,
    "missing_docs": request_docs,
    "high_amount": high_amount_review,
    "multiple": multi_override_review,
}


def build_graph():
    graph = StateGraph(ClaimState)
    for node in (classify_claim, check_overrides, retrieve_policy_evidence, llm_decision,
                 validate_and_finalize, *OVERRIDE_NODES.values()):
        graph.add_node(node.__name__, node)

    graph.set_entry_point("classify_claim")
    graph.add_edge("classify_claim", "check_overrides")
    graph.add_conditional_edges(
        "check_overrides",
        lambda state: state["override"] or "continue",
        {**{k: v.__name__ for k, v in OVERRIDE_NODES.items()}, "continue": "retrieve_policy_evidence"},
    )
    graph.add_edge("retrieve_policy_evidence", "llm_decision")
    graph.add_edge("llm_decision", "validate_and_finalize")
    for terminal in ("validate_and_finalize", *(n.__name__ for n in OVERRIDE_NODES.values())):
        graph.add_edge(terminal, END)
    return graph.compile()


def review_claim(app, claim: dict, config: dict) -> dict:
    """Run one claim through the graph. Any pipeline error (SDK failure, turn limit,
    unknown insurer) escalates to a human instead of crashing the batch (design_report.md §4.1)."""
    try:
        return app.invoke({"claim": claim}, config=config)["final"]
    except Exception as exc:
        return validator.pipeline_error_response(claim, exc)
