"""
guardrails/schema_validator.py
──────────────────────────────
Structural validation of X12 856 (Advance Ship Notice) documents.

Design rationale:
  Runs BEFORE the AI reasoning loop. Any document that fails structural
  validation is rejected immediately — no tokens wasted on garbage input.
  This mirrors the "parse before reason" pattern used in production EDI
  translators and prevents prompt-injection via malformed segments.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class StructuralError:
    """A single structural problem found in the X12 document."""
    severity: str           # "error" | "warning"
    segment: str            # Segment identifier, e.g. "ISA", "GS"
    position: int           # Zero-based segment index in the stream
    description: str        # Human-readable explanation
    recommended_fix: str    # Actionable guidance for the sender


@dataclass
class ValidationResult:
    """
    Outcome of structural validation.

    Attributes:
        passed          – True only when zero errors are present.
        errors          – All structural errors found (may include warnings).
        segment_count   – How many segments were parsed.
        envelope_isa    – Parsed ISA control header fields (or None).
    """
    passed: bool
    errors: list[StructuralError] = field(default_factory=list)
    segment_count: int = 0
    envelope_isa: dict[str, str] | None = None

    @property
    def error_count(self) -> int:
        return sum(1 for e in self.errors if e.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for e in self.errors if e.severity == "warning")


# ---------------------------------------------------------------------------
# Expected element counts for each segment (minimum required elements).
# These are intentionally conservative — just enough to catch obviously
# malformed documents without becoming a full X12 spec parser.
# ---------------------------------------------------------------------------

_MIN_ELEMENT_COUNTS: dict[str, int] = {
    "ISA": 16,   # Always exactly 16 elements in the interchange header
    "GS":  8,    # Functional group header
    "ST":  2,    # Transaction set header
    "BSN": 4,    # Beginning segment for ship notice
    "HL":  3,    # Hierarchical level (shipment/order/item)
    "DTM": 2,    # Date/time reference
    "REF": 2,    # Reference identification
    "TD1": 1,    # Carrier details (quantity & weight)
    "TD5": 1,    # Carrier details (routing)
    "SE":  2,    # Transaction set trailer
    "GE":  2,    # Functional group trailer
    "IEA": 2,    # Interchange control trailer
}

# Envelope segments that MUST appear, in expected order
_REQUIRED_ENVELOPE_SEGMENTS = ["ISA", "GS", "ST", "SE", "GE", "IEA"]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class SchemaValidator:
    """
    Validates X12 structural integrity of an 856 document.

    Usage::

        validator = SchemaValidator()
        result = validator.validate(raw_edi_text)
        if not result.passed:
            for err in result.errors:
                print(err.description)
    """

    def validate(self, raw_edi: str) -> ValidationResult:
        """
        Parse and structurally validate a raw X12 EDI string.

        Returns a ValidationResult. The result's ``passed`` flag is True
        only when no errors (severity="error") are present.
        """
        errors: list[StructuralError] = []

        # ── Step 1: Detect envelope characters from ISA header ──────────────
        # The ISA segment is fixed-width and always starts the interchange.
        # Position 3 is the element separator; position 105 is the segment
        # terminator (both are single characters in real X12 files).
        if not raw_edi.startswith("ISA"):
            errors.append(StructuralError(
                severity="error",
                segment="ISA",
                position=0,
                description="Document does not begin with ISA segment.",
                recommended_fix="Ensure ISA is the first segment in the file.",
            ))
            return ValidationResult(passed=False, errors=errors)

        if len(raw_edi) < 106:
            errors.append(StructuralError(
                severity="error",
                segment="ISA",
                position=0,
                description="ISA header too short to extract envelope characters.",
                recommended_fix="ISA must be exactly 106 characters including the segment terminator.",
            ))
            return ValidationResult(passed=False, errors=errors)

        element_sep: str = raw_edi[3]      # e.g. "*"
        segment_term: str = raw_edi[105]   # e.g. "~"

        # Sanity-check: separators must differ and not be alphanumeric
        if element_sep == segment_term:
            errors.append(StructuralError(
                severity="error",
                segment="ISA",
                position=0,
                description=f"Element separator and segment terminator are identical ('{element_sep}').",
                recommended_fix="Use distinct characters, e.g. element_sep='*' and segment_term='~'.",
            ))

        if element_sep.isalnum() or segment_term.isalnum():
            errors.append(StructuralError(
                severity="error",
                segment="ISA",
                position=0,
                description="Separator or terminator is alphanumeric, which is illegal in X12.",
                recommended_fix="Use non-alphanumeric characters such as '*' and '~'.",
            ))

        if errors:
            # Can't split reliably — stop early
            return ValidationResult(passed=False, errors=errors)

        # ── Step 2: Split into segments ──────────────────────────────────────
        # Strip trailing whitespace/newlines, then split on terminator.
        raw_segments = [
            s.strip()
            for s in raw_edi.replace("\n", "").replace("\r", "").split(segment_term)
            if s.strip()
        ]

        segment_count = len(raw_segments)

        # ── Step 3: Check required envelope segments present ─────────────────
        found_segment_ids = [seg.split(element_sep)[0] for seg in raw_segments]

        for required in _REQUIRED_ENVELOPE_SEGMENTS:
            if required not in found_segment_ids:
                errors.append(StructuralError(
                    severity="error",
                    segment=required,
                    position=-1,
                    description=f"Required envelope segment '{required}' is missing.",
                    recommended_fix=f"Add the {required} segment in the correct position.",
                ))

        # ── Step 4: Check 856-specific required segment: BSN ─────────────────
        if "BSN" not in found_segment_ids:
            errors.append(StructuralError(
                severity="error",
                segment="BSN",
                position=-1,
                description="Transaction type 856 requires a BSN (Beginning Segment for Ship Notice).",
                recommended_fix="Add BSN segment immediately after the ST segment.",
            ))

        # ── Step 5: Per-segment element count checks ─────────────────────────
        for idx, raw_seg in enumerate(raw_segments):
            elements = raw_seg.split(element_sep)
            seg_id = elements[0]
            actual_count = len(elements) - 1  # exclude the segment ID itself

            if seg_id in _MIN_ELEMENT_COUNTS:
                min_required = _MIN_ELEMENT_COUNTS[seg_id]
                if actual_count < min_required:
                    errors.append(StructuralError(
                        severity="error",
                        segment=seg_id,
                        position=idx,
                        description=(
                            f"{seg_id} has {actual_count} element(s); "
                            f"minimum required is {min_required}."
                        ),
                        recommended_fix=(
                            f"Ensure {seg_id} contains at least {min_required} "
                            f"elements separated by '{element_sep}'."
                        ),
                    ))

        # ── Step 6: Parse ISA fields for metadata ────────────────────────────
        isa_fields = raw_segments[0].split(element_sep)
        envelope_isa: dict[str, str] | None = None
        if len(isa_fields) >= 17:
            envelope_isa = {
                "authorization_info_qualifier": isa_fields[1],
                "authorization_info":           isa_fields[2],
                "security_info_qualifier":      isa_fields[3],
                "security_info":                isa_fields[4],
                "sender_id_qualifier":          isa_fields[5],
                "sender_id":                    isa_fields[6].strip(),
                "receiver_id_qualifier":        isa_fields[7],
                "receiver_id":                  isa_fields[8].strip(),
                "interchange_date":             isa_fields[9],
                "interchange_time":             isa_fields[10],
                "control_number":               isa_fields[13],
            }

        passed = all(e.severity != "error" for e in errors)
        return ValidationResult(
            passed=passed,
            errors=errors,
            segment_count=segment_count,
            envelope_isa=envelope_isa,
        )
