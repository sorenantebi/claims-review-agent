"""
Tracing (docs/design_report.md §1.2): every graph node runs in an OpenTelemetry span.

- Always on: spans are appended to logs/trace.jsonl, no account needed.
- Optional: with LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY set, the graph run also gets a
  Langfuse CallbackHandler (SDK v4) so it shows up on the Langfuse dashboard.
"""

from __future__ import annotations

import functools
import json
import os
import pathlib

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
TRACE_FILE = LOG_DIR / "trace.jsonl"


class JsonlFileExporter(SpanExporter):
    """One JSON line per finished span."""

    def export(self, spans):
        with open(TRACE_FILE, "a") as f:
            for span in spans:
                ctx = span.get_span_context()
                f.write(
                    json.dumps(
                        {
                            "name": span.name,
                            "trace_id": format(ctx.trace_id, "032x"),
                            "span_id": format(ctx.span_id, "016x"),
                            "duration_ms": round((span.end_time - span.start_time) / 1e6, 2),
                            "attributes": dict(span.attributes or {}),
                            "status": span.status.status_code.name,
                        }
                    )
                    + "\n"
                )
        return SpanExportResult.SUCCESS

    def shutdown(self):  # pragma: no cover - required by SpanExporter interface
        pass


_provider = TracerProvider(resource=Resource.create({"service.name": "claims-review-agent"}))
_provider.add_span_processor(SimpleSpanProcessor(JsonlFileExporter()))
trace.set_tracer_provider(_provider)
_tracer = trace.get_tracer("claims-review-agent")


def traced(span_name: str):
    """Wrap a graph node in a span tagged with claim_id and any error."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(state, *args, **kwargs):
            with _tracer.start_as_current_span(span_name) as span:
                claim_id = (state or {}).get("claim", {}).get("claim_id")
                if claim_id:
                    span.set_attribute("claim_id", claim_id)
                try:
                    return fn(state, *args, **kwargs)
                except Exception as exc:
                    span.set_attribute("error", str(exc))
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                    raise

        return wrapper

    return decorator


def _langfuse_status() -> tuple[bool, str]:
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return False, "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set -- local OTel trace log only."
    try:
        from langfuse import get_client

        if get_client().auth_check():
            return True, "Langfuse authenticated -- this run will also appear on your Langfuse dashboard."
        return False, "Langfuse keys present but auth_check() failed -- local OTel trace log only."
    except Exception as exc:  # tracing must never stop a run
        return False, f"Langfuse unavailable ({exc}) -- local OTel trace log only."


def langgraph_config() -> dict:
    """The `config=` for graph.invoke(): adds the Langfuse handler when it authenticates."""
    enabled, message = _langfuse_status()
    print(f"[tracing] {message}", flush=True)
    if not enabled:
        return {}
    from langfuse.langchain import CallbackHandler

    return {"callbacks": [CallbackHandler()]}
