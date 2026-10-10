"""
The one LLM step in the graph (docs/design_report.md §1.4). Everything objective
is decided in code (graph.py); the model only judges whether the clinical picture meets
the insurer's coverage language, returned as one structured, grounded tool call.

Runs on the Claude Agent SDK using your Claude Code login (or CLAUDE_CODE_OAUTH_TOKEN /
ANTHROPIC_API_KEY, see .env.example).

The six actions are one tool, submit_claim_decision, with `action` constrained by a JSON
Schema enum; every built-in tool is stripped (`tools=[]`) so the model can do nothing else.
Each `rationale` entry carries its own evidence pointer, so grounding is checked
per assertion (validator.grounding_problem).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

ALLOWED_ACTIONS = [
    "approve_pre_authorization",
    "approve_claim_payment",
    "deny_claim",
    "request_missing_documentation",
    "route_to_senior_reviewer",
    "route_to_coding_review",
]

ALLOWED_DOCUMENTS = [
    "clinical_note",
    "op_note",
    "discharge_summary",
    "appeal_letter",
    "claim_form",
    "prior_treatment_history",
    "imaging_report",
]

SYSTEM_PROMPT = (Path(__file__).resolve().parent / "prompt.md").read_text()

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "total_tokens",
    "cost_usd",
)


def empty_usage() -> dict[str, float]:
    return {**dict.fromkeys(USAGE_KEYS, 0), "cost_usd": 0.0}


def add_usage(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in USAGE_KEYS}


DECISION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ALLOWED_ACTIONS},
        "payment_in_dollars": {
            "type": "number",
            "description": "Required only when action == approve_claim_payment.",
        },
        "documents": {
            "type": "array",
            "items": {"type": "string", "enum": ALLOWED_DOCUMENTS},
            "description": "Required only when action == request_missing_documentation.",
        },
        "rationale": {
            "type": "array",
            "minItems": 1,
            "description": (
                "One short factual statement per entry; grammar and complete sentences "
                "are not required. Do not collapse the rationale into a paragraph."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "assertion": {"type": "string", "description": "One factual claim, in plain language."},
                    "section": {
                        "type": "string",
                        "description": "Policy section id grounding this assertion. Omit if using claim_field instead.",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": (
                            "Required whenever 'section' is used. An exact, verbatim quote (a phrase or "
                            "sentence, not a paraphrase) copied directly from that section's provided text. "
                            "Checked by exact substring match against the real retrieved text, so quote "
                            "precisely -- do not summarize, combine, or lightly reword it."
                        ),
                    },
                    "claim_field": {
                        "type": "string",
                        "description": "Claim-record field grounding this assertion. Omit if using section instead.",
                    },
                },
                "required": ["assertion"],
            },
        },
        "summary": {
            "type": "string",
            "description": (
                "One concise line stating the decision and decisive facts; fragments are acceptable. "
                'When the decision turns on specific policy language or claim text, quote it directly in '
                'double quotes (e.g. \'denied: policy states "must not be primarily for member convenience"\'), '
                "copied verbatim from that source. Any quoted span is checked against the real retrieved "
                "policy text and this claim's own notes, so quote exactly rather than paraphrasing inside "
                "quotation marks."
            ),
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["action", "rationale", "summary", "confidence"],
}


@tool(
    "submit_claim_decision",
    "Submit the grounded claims-review decision for this claim.",
    DECISION_INPUT_SCHEMA,
)
async def _submit_claim_decision(args: dict[str, Any]) -> dict[str, Any]:
    # No side effects: _decide_async reads the structured args from the ToolUseBlock.
    return {"content": [{"type": "text", "text": "Decision recorded."}]}


_decision_server = create_sdk_mcp_server(name="claims", version="1.0.0", tools=[_submit_claim_decision])

_OPTIONS = ClaudeAgentOptions(
    system_prompt=SYSTEM_PROMPT,
    model=MODEL,
    tools=[],  # strip every built-in tool -- the model can ONLY call our one tool
    mcp_servers={"claims": _decision_server},
    allowed_tools=["mcp__claims__submit_claim_decision"],
    max_turns=15,  # headroom for tool-search + the actual call + occasional extra reasoning turns
)


def _build_prompt(claim: dict, chunks: list[dict], correction_note: str | None) -> str:
    evidence = "\n\n".join(f"(§{c['section']}) {c['title']}\n{c['text']}" for c in chunks)
    prompt = (
        "Claim record:\n"
        f"{json.dumps(claim, indent=2)}\n\n"
        f"Full policy document ({claim['insurance_provider']} only), by section:\n"
        f"{evidence}\n\n"
        "Decide the next action and call submit_claim_decision."
    )
    if correction_note:
        prompt += f"\n\nNote: your previous attempt had a problem -- fix it: {correction_note}"
    return prompt


async def _decide_async(claim: dict, chunks: list[dict], correction_note: str | None) -> dict:
    prompt = _build_prompt(claim, chunks, correction_note)
    decision: dict[str, Any] | None = None
    token_usage = empty_usage()

    async for message in query(prompt=prompt, options=_OPTIONS):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name.endswith("submit_claim_decision"):
                    decision = block.input
        elif isinstance(message, ResultMessage) and message.subtype != "success" and decision is None:
            raise RuntimeError(f"Agent SDK query did not complete successfully: {message}")
        elif isinstance(message, ResultMessage):
            for usage in (message.model_usage or {}).values():
                token_usage["input_tokens"] += usage.get("inputTokens", 0)
                token_usage["output_tokens"] += usage.get("outputTokens", 0)
                token_usage["cache_read_input_tokens"] += usage.get("cacheReadInputTokens", 0)
                token_usage["cache_creation_input_tokens"] += usage.get("cacheCreationInputTokens", 0)
                token_usage["cost_usd"] += usage.get("costUSD", 0.0)
            token_usage["total_tokens"] = token_usage["input_tokens"] + token_usage["output_tokens"]

    if decision is None:
        raise RuntimeError("Model never called submit_claim_decision.")
    decision["_token_usage"] = token_usage
    return decision


def decide(claim: dict, chunks: list[dict], correction_note: str | None = None) -> dict:
    return asyncio.run(_decide_async(claim, chunks, correction_note))
