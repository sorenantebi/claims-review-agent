"""
Policy retrieval (docs/design_report.md §2.1).

- One policy per insurer, isolated by filename: a claim for "Aetna" can only load
  aetna's policy. Unknown insurers raise rather than fall back to a default.
- Ingestion is offline: scripts/extract_policy_documents.py turns policies/*.pdf into
  policies_extracted/*.txt. This serving path only reads that text, and refuses an
  extraction older than its PDF.
- Text is chunked at numbered section headings ("5.1 MRI Coverage") so every chunk is a
  whole, citable unit.
- No ranking: each policy is 1-2 pages, so the model gets the insurer's whole document.
  Reintroduce top-k (BM25/embeddings) once a single policy no longer fits the prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

POLICY_DIR = Path(__file__).resolve().parent / "policies"
EXTRACTED_DIR = Path(__file__).resolve().parent / "policies_extracted"

INSURER_TO_SLUG = {
    "united healthcare": "united_healthcare",
    "aetna": "aetna",
    "blue cross blue shield": "blue_cross_blue_shield",
}

_SECTION_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+(.+)$")


@dataclass
class Chunk:
    section: str
    title: str
    text: str


def insurer_slug(insurance_provider: str) -> str:
    key = insurance_provider.strip().lower()
    if key not in INSURER_TO_SLUG:
        raise ValueError(
            f"No policy document configured for insurer {insurance_provider!r}. "
            f"Known insurers: {list(INSURER_TO_SLUG)}"
        )
    return INSURER_TO_SLUG[key]


def _check_extraction_current(slug: str) -> Path:
    """Fail closed if ingestion hasn't run since the PDF last changed; otherwise a policy
    update would silently never take effect."""
    pdf_path = POLICY_DIR / f"{slug}.pdf"
    txt_path = EXTRACTED_DIR / f"{slug}.txt"

    if not pdf_path.exists():
        raise FileNotFoundError(f"No policy PDF found for {slug!r} at {pdf_path}.")
    if not txt_path.exists():
        raise FileNotFoundError(
            f"No extracted text for {slug!r} at {txt_path}. Run "
            "`python -m scripts.extract_policy_documents` first."
        )
    if pdf_path.stat().st_mtime > txt_path.stat().st_mtime:
        raise RuntimeError(
            f"{pdf_path.name} was modified after its extraction ({txt_path.name}) was "
            "last generated -- re-run `python -m scripts.extract_policy_documents` "
            "before serving this policy again."
        )
    return txt_path


@cache
def load_policy_chunks(slug: str) -> list[Chunk]:
    txt_path = _check_extraction_current(slug)
    lines = txt_path.read_text().splitlines()

    chunks: list[Chunk] = []
    current_section, current_title, buf = None, None, []

    def flush():
        if current_section is not None and buf:
            chunks.append(Chunk(current_section, current_title, "\n".join(buf).strip()))

    for line in lines:
        m = _SECTION_HEADING_RE.match(line.strip())
        if m:
            flush()
            current_section, current_title = m.group(1), m.group(2)
            buf = [line]
        elif current_section is not None:
            buf.append(line)
    flush()
    return chunks


def retrieve(insurance_provider: str) -> list[dict]:
    """Every section of this insurer's policy, tagged with its section id for citation checks."""
    slug = insurer_slug(insurance_provider)
    return [{"insurer_slug": slug, **vars(c)} for c in load_policy_chunks(slug)]
