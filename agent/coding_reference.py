"""
Diagnosis <-> procedure consistency table, standing in for a licensed ICD-10/CPT crosswalk
(docs/design_report.md, Assumptions). Procedures without an entry are not gated (e.g. OFFICE-VISIT).
Kept small and explicit so a mismatch like CLM1003 (routine exam billed against an
appendectomy) can be verified by reading this file.
"""

from __future__ import annotations

# procedure_code -> list of acceptable diagnosis_code prefixes
CONSISTENT_DIAGNOSIS_PREFIXES: dict[str, list[str]] = {
    "MRI-LS-SPINE": ["M54", "M51"],
    "APPENDECTOMY": ["K35", "K36", "K37"],
    "BIOLOGIC-INJECTION": ["J45"],
    "ORAL-ANTIBIOTIC": ["L03", "L08", "A49"],
    "PSYCH-THERAPY": ["F32", "F33", "F41", "F43"],
    "HIP-REPLACEMENT": ["S72", "M16"],
    "LIPID-PANEL": ["E78", "Z13"],
    # OFFICE-VISIT intentionally omitted: too generic to gate on procedure code alone.
}


def is_consistent(diagnosis_code: str, procedure_code: str) -> bool:
    """Return False only when we have an explicit rule for this procedure AND the
    diagnosis prefix doesn't match it. Unknown procedures are assumed consistent
    (permissive default) rather than silently failing closed."""
    allowed_prefixes = CONSISTENT_DIAGNOSIS_PREFIXES.get(procedure_code)
    if not allowed_prefixes:
        return True
    return any(diagnosis_code.startswith(p) for p in allowed_prefixes)
