"""
Render agent/policies_source/*.txt to agent/policies/*.pdf for the synthetic insurers.

One source line per PDF line in a monospace font, so pypdf extraction round-trips
line-for-line and the section-heading chunker needs no special handling. Only needed
after editing a source file; the committed PDFs are the runtime input.

    python -m scripts.generate_policy_pdfs
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

SOURCE_DIR = Path(__file__).resolve().parent.parent / "agent" / "policies_source"
OUT_DIR = Path(__file__).resolve().parent.parent / "agent" / "policies"

FONT = "Courier"
FONT_SIZE = 9
LEADING = 12  # points between line baselines
MARGIN = 54  # 0.75in
PAGE_WIDTH, PAGE_HEIGHT = letter


def render_pdf(text_path: Path, pdf_path: Path) -> None:
    lines = text_path.read_text().splitlines()
    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    c.setFont(FONT, FONT_SIZE)

    y = PAGE_HEIGHT - MARGIN
    for line in lines:
        if y < MARGIN:
            c.showPage()
            c.setFont(FONT, FONT_SIZE)
            y = PAGE_HEIGHT - MARGIN
        # drawString with an empty string still advances the cursor for a
        # blank source line, preserving paragraph breaks.
        c.drawString(MARGIN, y, line)
        y -= LEADING

    c.save()


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    txt_files = sorted(SOURCE_DIR.glob("*.txt"))
    if not txt_files:
        raise SystemExit(f"No source .txt files found under {SOURCE_DIR}")

    for txt_path in txt_files:
        pdf_path = OUT_DIR / f"{txt_path.stem}.pdf"
        render_pdf(txt_path, pdf_path)
        print(f"wrote {pdf_path.relative_to(OUT_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
