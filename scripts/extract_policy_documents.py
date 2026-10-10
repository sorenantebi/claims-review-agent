"""
Offline ingestion: extract every agent/policies/*.pdf to agent/policies_extracted/*.txt.
Retrieval reads only the extracted text and refuses one older than its PDF, so re-run
this whenever a policy PDF changes.

    python -m scripts.extract_policy_documents
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

POLICY_DIR = Path(__file__).resolve().parent.parent / "agent" / "policies"
EXTRACTED_DIR = Path(__file__).resolve().parent.parent / "agent" / "policies_extracted"


def extract_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def main() -> None:
    EXTRACTED_DIR.mkdir(exist_ok=True)
    pdf_files = sorted(POLICY_DIR.glob("*.pdf"))
    if not pdf_files:
        raise SystemExit(f"No policy PDFs found under {POLICY_DIR}")

    for pdf_path in pdf_files:
        text = extract_text(pdf_path)
        out_path = EXTRACTED_DIR / f"{pdf_path.stem}.txt"
        out_path.write_text(text)
        print(f"extracted {pdf_path.relative_to(POLICY_DIR.parent.parent)} "
              f"-> {out_path.relative_to(POLICY_DIR.parent.parent)} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
