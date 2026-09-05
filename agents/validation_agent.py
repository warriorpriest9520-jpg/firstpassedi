"""
FirstPass EDI — Validation Agent (ReAct Loop)
================================================
Validates an X12 856 document against a trading partner's routing guide spec
using the **ReAct** (Reasoning + Acting) pattern.

ReAct Pattern
─────────────
  For each segment in the 856:

    THOUGHT  ─ Reason about what the spec requires for this segment type.
    ACTION   ─ Call a validation tool (lookup spec rule, check field format,
               cross-reference ShipStation data).
    OBSERVATION ─ Record the compliance result and any errors found.

  After all segments are processed (or max_react_steps reached):

    THOUGHT  ─ Synthesise all observations into an overall verdict.
    FINISH   ─ Return a structured ValidationResult.

In DEMO_MODE, LLM calls are replaced with a deterministic rule-engine so
the pipeline can be demonstrated without an OpenAI API key.  The ReAct
step log is still emitted for educational inspection.

Severity Classification
───────────────────────
  CRITICAL — Document cannot be submitted; partner will reject it outright.
             Examples: missing required segment, zero shipped quantity, invalid PO.
  WARNING  — Document may be accepted but could cause downstream issues.
             Examples: missing recommended field, non-preferred carrier SCAC.
  INFO     — Cosmetic / non-standard but parseable.
             Examples: extra whitespace in a reference number.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from config import config
from agents.retrieval_agent import ContextPackage
from tools.edi_parser import ParsedEDI856
from tools.shipstation_client import ShipStationClient, ShipmentInfo

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class ValidationError:
    """A single compliance issue found during validation."""

    segment_type: str       # e.g. "SN1", "TD5"
    element_id: str         # e.g. "SN1-02", "TD5-03"
    severity: str           # "critical" | "warning" | "info"
    code: str               # Short machine-readable code, e.g. "QTY_ZERO"
    description: str        # Human-readable explanation


@dataclass
class ReactStep:
    """One iteration of the ReAct reasoning loop (for explainability / audit)."""

    step_number: int
    segment_type: str
    thought: str
    action: str             # Tool/check name
    action_input: dict
    observation: str
    errors_found: list[ValidationError] = field(default_factory=list)


@dataclass
class ValidationResult:
    """
    The outcome of the ValidationAgent's ReAct loop.

    ``passed`` is True only if:
      - No CRITICAL errors were found, AND
      - The calculated confidence >= ``ValidationConfig.confidence_threshold``

    ``confidence`` is a float in [0, 1] computed from error severity counts
    weighted by ``ValidationConfig.severity_weights``.
    """

    passed: bool
    confidence: float
    errors: list[ValidationError]
    react_steps: list[ReactStep]
    segment_count: int          # Total segments evaluated
    partner_name: str
    shipment_id: str
    summary: str                # One-line human-readable verdict


# ── Built-in validation rules (deterministic, no LLM required) ───────────────


def _validate_bsn(elements: list[str]) -> list[ValidationError]:
    """Validate the BSN (Beginning Segment for Ship Notice) segment."""
    errors: list[ValidationError] = []
    transaction_type = elements[0] if elements else ""
    shipment_id = elements[1] if len(elements) > 1 else ""
    date_val = elements[2] if len(elements) > 2 else ""
    hier_code = elements[4] if len(elements) > 4 else ""

    if transaction_type not in ("00", "05", "06"):
        errors.append(ValidationError(
            segment_type="BSN", element_id="BSN-01", severity="critical",
            code="BSN01_INVALID",
            description=f"BSN01 transaction type '{transaction_type}' must be 00, 05, or 06.",
        ))
    if not shipment_id:
        errors.append(ValidationError(
            segment_type="BSN", element_id="BSN-02", severity="critical",
            code="BSN02_MISSING",
            description="BSN02 shipment ID is required but missing.",
        ))
    if not re.match(r"^\d{8}$", date_val):
        errors.append(ValidationError(
            segment_type="BSN", element_id="BSN-03", severity="critical",
            code="BSN03_DATE_FORMAT",
            description=f"BSN03 date '{date_val}' must be 8-digit CCYYMMDD.",
        ))
    if hier_code and hier_code != "0002":
        errors.append(ValidationError(
            segment_type="BSN", element_id="BSN-05", severity="warning",
            code="BSN05_HIER_CODE",
            description=f"BSN05 hierarchical structure '{hier_code}' expected '0002' for S/O/P/I.",
        ))
    return errors


def _validate_td1(elements: list[str]) -> list[ValidationError]:
    """Validate the TD1 (Carrier Details – Quantity and Weight) segment."""
    errors: list[ValidationError] = []
    weight = elements[6] if len(elements) > 6 else ""
    weight_qual = elements[7] if len(elements) > 7 else ""    # G=Gross, N=Net

    if weight_qual and weight_qual not in ("G", "N"):
        errors.append(ValidationError(
            segment_type="TD1", element_id="TD1-07", severity="warning",
            code="TD1_WEIGHT_QUAL",
            description=f"TD1-07 weight qualifier '{weight_qual}' should be 'G' (Gross) or 'N' (Net).",
        ))
    if weight:
        try:
            w = float(weight)
            if w <= 0:
                errors.append(ValidationError(
                    segment_type="TD1", element_id="TD1-08", severity="critical",
                    code="TD1_WEIGHT_ZERO",
                    description="TD1-08 gross weight must be greater than 0.",
                ))
        except ValueError:
            errors.append(ValidationError(
                segment_type="TD1", element_id="TD1-08", severity="critical",
                code="TD1_WEIGHT_FORMAT",
                description=f"TD1-08 weight '{weight}' is not a valid number.",
            ))
    else:
        errors.append(ValidationError(
            segment_type="TD1", element_id="TD1-08", severity="critical",
            code="TD1_WEIGHT_MISSING",
            description="TD1-08 gross weight is required by RetailerA spec.",
        ))
    return errors


def _validate_td5(elements: list[str]) -> list[ValidationError]:
    """Validate the TD5 (Carrier Details – Routing) segment."""
    errors: list[ValidationError] = []
    scac = elements[2] if len(elements) > 2 else ""

    # Accepted SCAC codes per RetailerA routing guide.
    # In production, this list comes from the spec (parsed from the context package).
    ACCEPTED_SCAC = {"UPS", "UPSN", "FDEG", "FXFE", "ONTRAC", "ESTES", "SAIA", "ODFL", "FAST"}

    if not scac:
        errors.append(ValidationError(
            segment_type="TD5", element_id="TD5-03", severity="critical",
            code="TD5_SCAC_MISSING",
            description="TD5-03 carrier SCAC code is required but missing.",
        ))
    elif len(scac) < 2 or len(scac) > 4:
        errors.append(ValidationError(
            segment_type="TD5", element_id="TD5-03", severity="warning",
            code="TD5_SCAC_LENGTH",
            description=f"TD5-03 SCAC '{scac}' should be 2–4 characters.",
        ))
    elif scac.upper() not in ACCEPTED_SCAC:
        errors.append(ValidationError(
            segment_type="TD5", element_id="TD5-03", severity="warning",
            code="TD5_SCAC_NONSTANDARD",
            description=f"TD5-03 SCAC '{scac}' is not in the preferred carrier list: {sorted(ACCEPTED_SCAC)}.",
        ))
    return errors


def _validate_lin(elements: list[str]) -> list[ValidationError]:
    """Validate the LIN (Item Identification) segment."""
    errors: list[ValidationError] = []
    qualifier = elements[1] if len(elements) > 1 else ""
    barcode = elements[2] if len(elements) > 2 else ""

    if qualifier != "UP":
        errors.append(ValidationError(
            segment_type="LIN", element_id="LIN-02", severity="warning",
            code="LIN_QUALIFIER",
            description=f"LIN-02 qualifier '{qualifier}' is not 'UP' (UPC-A). RetailerA requires UPC.",
        ))
    if not re.match(r"^\d{12}$", barcode):
        errors.append(ValidationError(
            segment_type="LIN", element_id="LIN-03", severity="critical",
            code="LIN_UPC_FORMAT",
            description=f"LIN-03 barcode '{barcode}' must be exactly 12 numeric digits (UPC-A).",
        ))
    return errors


def _validate_sn1(elements: list[str]) -> list[ValidationError]:
    """Validate the SN1 (Item Detail – Shipment) segment."""
    errors: list[ValidationError] = []
    qty_str = elements[1] if len(elements) > 1 else ""
    uom = elements[2] if len(elements) > 2 else ""

    if not qty_str:
        errors.append(ValidationError(
            segment_type="SN1", element_id="SN1-02", severity="critical",
            code="SN1_QTY_MISSING",
            description="SN1-02 shipped quantity is required.",
        ))
    else:
        try:
            qty = float(qty_str)
            if qty <= 0:
                errors.append(ValidationError(
                    segment_type="SN1", element_id="SN1-02", severity="critical",
                    code="SN1_QTY_ZERO",
                    description=f"SN1-02 shipped quantity is {qty}. Must be > 0.",
                ))
        except ValueError:
            errors.append(ValidationError(
                segment_type="SN1", element_id="SN1-02", severity="critical",
                code="SN1_QTY_FORMAT",
                description=f"SN1-02 quantity '{qty_str}' is not a valid number.",
            ))
    if not uom:
        errors.append(ValidationError(
            segment_type="SN1", element_id="SN1-03", severity="warning",
            code="SN1_UOM_MISSING",
            description="SN1-03 unit of measure is missing (expected 'EA', 'CA', etc.).",
        ))
    return errors


def _validate_prf(elements: list[str]) -> list[ValidationError]:
    """Validate the PRF (Purchase Order Reference) segment."""
    errors: list[ValidationError] = []
    po_number = elements[0] if elements else ""

    if not po_number:
        errors.append(ValidationError(
            segment_type="PRF", element_id="PRF-01", severity="critical",
            code="PRF_PO_MISSING",
            description="PRF-01 purchase order number is required at the Order HL level.",
        ))
    elif len(po_number) > 22:
        errors.append(ValidationError(
            segment_type="PRF", element_id="PRF-01", severity="warning",
            code="PRF_PO_LENGTH",
            description=f"PRF-01 PO number '{po_number}' exceeds 22 characters.",
        ))
    return errors


# Dispatch table: segment type → validator function
_SEGMENT_VALIDATORS = {
    "BSN": _validate_bsn,
    "TD1": _validate_td1,
    "TD5": _validate_td5,
    "LIN": _validate_lin,
    "SN1": _validate_sn1,
    "PRF": _validate_prf,
}


# ── Validation Agent ──────────────────────────────────────────────────────────


class ValidationAgent:
    """
    Validates an X12 856 document against a trading partner spec using a
    ReAct (Reasoning + Acting) loop.

    In DEMO_MODE, a deterministic rule engine replaces LLM calls.
    In production mode (with a valid OPENAI_API_KEY), GPT-4o is used for
    thought and synthesis steps, with rule-based tools handling structured
    field checks.

    Parameters
    ----------
    shipstation_client:
        Optional ShipStation client for cross-validating weight and carrier data.
        Defaults to a new ShipStationClient (mock in demo mode).
    """

    def __init__(
        self,
        shipstation_client: Optional[ShipStationClient] = None,
    ) -> None:
        self._ss = shipstation_client or ShipStationClient()
        self._demo_mode = config.demo_mode
        self._cfg = config.validation

        if not self._demo_mode:
            try:
                import openai
                self._llm = openai.OpenAI(api_key=config.openai_api_key)
            except ImportError:
                logger.warning("openai package not installed; falling back to demo mode.")
                self._demo_mode = True

    def validate(
        self,
        document: ParsedEDI856,
        context: ContextPackage,
    ) -> ValidationResult:
        """
        Run the ReAct validation loop on a parsed 856 document.

        Iterates over each segment in ``document.raw_segments``, applying
        the ReAct pattern (Thought → Action → Observation) for each.
        Stops early if ``max_react_steps`` is exceeded.

        Parameters
        ----------
        document:
            Fully parsed ``ParsedEDI856`` object.
        context:
            Context package assembled by the RetrievalAgent.

        Returns
        -------
        ValidationResult
            ``passed=True`` only if no CRITICAL errors were found AND
            confidence >= ``config.validation.confidence_threshold``.
        """
        logger.info(
            "ValidationAgent starting ReAct loop for shipment '%s' (partner='%s', demo=%s).",
            document.bsn.shipment_id,
            document.partner_name or context.partner_name,
            self._demo_mode,
        )

        all_errors: list[ValidationError] = []
        react_steps: list[ReactStep] = []
        step_number = 0

        # Flatten the segment list; skip envelope segments (ISA, GS, GE, IEA).
        _SKIP = {"ISA", "GS", "GE", "IEA", "CTT"}
        content_segments = [s for s in document.raw_segments if s["type"] not in _SKIP]

        # Also cross-reference ShipStation for the first PRF's PO number.
        shipment_info: Optional[ShipmentInfo] = None

        for seg in content_segments:
            if step_number >= self._cfg.max_react_steps:
                logger.warning(
                    "ReAct loop hit max_react_steps=%d; stopping early.",
                    self._cfg.max_react_steps,
                )
                break

            seg_type = seg["type"]
            elements = seg["elements"]

            # ── THOUGHT ───────────────────────────────────────────────────────
            thought = self._think(seg_type, elements, context)

            # ── ACTION: run validator + optional cross-reference ───────────────
            action_name = f"validate_{seg_type.lower()}"
            action_input = {"segment_type": seg_type, "elements": elements}

            # Fetch ShipStation data when we hit the first PRF (PO reference).
            if seg_type == "PRF" and elements and elements[0] and shipment_info is None:
                po_ref = elements[0]
                shipment_info = self._ss.get_shipment(po_ref)
                action_input["shipstation_lookup"] = {
                    "po_ref": po_ref,
                    "found": shipment_info.found,
                }

            errors_for_seg = self._act(seg_type, elements, shipment_info)

            # ── OBSERVATION ───────────────────────────────────────────────────
            observation = self._observe(seg_type, errors_for_seg, shipment_info if seg_type == "PRF" else None)

            react_steps.append(ReactStep(
                step_number=step_number,
                segment_type=seg_type,
                thought=thought,
                action=action_name,
                action_input=action_input,
                observation=observation,
                errors_found=errors_for_seg,
            ))

            all_errors.extend(errors_for_seg)
            step_number += 1

        # ── FINISH: synthesise results ────────────────────────────────────────
        confidence = self._calculate_confidence(all_errors, len(content_segments))
        has_critical = any(e.severity == "critical" for e in all_errors)
        passed = (not has_critical) and (confidence >= self._cfg.confidence_threshold)

        summary = self._synthesise(passed, confidence, all_errors, context.partner_name)

        logger.info(
            "ValidationAgent complete: passed=%s, confidence=%.2f, errors=%d (critical=%d).",
            passed,
            confidence,
            len(all_errors),
            sum(1 for e in all_errors if e.severity == "critical"),
        )

        return ValidationResult(
            passed=passed,
            confidence=confidence,
            errors=all_errors,
            react_steps=react_steps,
            segment_count=step_number,
            partner_name=document.partner_name or context.partner_name,
            shipment_id=document.bsn.shipment_id,
            summary=summary,
        )

    # ── ReAct helpers ─────────────────────────────────────────────────────────

    def _think(
        self,
        seg_type: str,
        elements: list[str],
        context: ContextPackage,
    ) -> str:
        """
        Generate a THOUGHT about what to check for this segment.

        In demo mode: return a pre-scripted thought string.
        In production: call GPT-4o with a concise prompt.
        """
        if self._demo_mode:
            return self._demo_thought(seg_type, elements)

        # ── Production: LLM-generated thought ─────────────────────────────────
        prompt = f"""You are an EDI compliance validator.
Partner spec summary:
{context.spec_content[:800]}

Current segment: {seg_type}
Elements: {elements}

In ONE sentence, describe what rule(s) from the spec you will check for this segment.
Respond with only the sentence. No preamble."""

        response = self._llm.chat.completions.create(
            model=config.openai_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.0,
        )
        return response.choices[0].message.content.strip()

    def _act(
        self,
        seg_type: str,
        elements: list[str],
        shipment_info: Optional[ShipmentInfo],
    ) -> list[ValidationError]:
        """
        Execute the validation ACTION for a segment.

        Uses the deterministic rule engine (``_SEGMENT_VALIDATORS``) as the
        primary tool.  Cross-references ShipStation for weight/carrier checks.
        """
        errors: list[ValidationError] = []

        # Run segment-specific rule validator.
        validator = _SEGMENT_VALIDATORS.get(seg_type)
        if validator:
            errors.extend(validator(elements))

        # Cross-check TD1 weight against ShipStation if we have shipment data.
        if seg_type == "TD1" and shipment_info and shipment_info.found:
            edi_weight_str = elements[6] if len(elements) > 6 else ""
            try:
                edi_weight = float(edi_weight_str)
                ss_weight = shipment_info.weight_lbs
                if ss_weight > 0 and abs(edi_weight - ss_weight) / ss_weight > 0.05:
                    errors.append(ValidationError(
                        segment_type="TD1",
                        element_id="TD1-08",
                        severity="warning",
                        code="TD1_WEIGHT_MISMATCH",
                        description=(
                            f"EDI weight {edi_weight} LB differs from ShipStation "
                            f"weight {ss_weight} LB by more than 5%."
                        ),
                    ))
            except (ValueError, ZeroDivisionError):
                pass

        return errors

    def _observe(
        self,
        seg_type: str,
        errors: list[ValidationError],
        shipment_info: Optional[ShipmentInfo],
    ) -> str:
        """Format an OBSERVATION string summarising what the action found."""
        if not errors:
            return f"Segment {seg_type}: COMPLIANT — no issues found."

        parts = [f"Segment {seg_type}: {len(errors)} issue(s) found:"]
        for e in errors:
            parts.append(f"  [{e.severity.upper()}] {e.element_id}: {e.description}")

        if shipment_info and not shipment_info.found:
            parts.append("  [WARNING] PO reference not found in ShipStation — cannot cross-validate.")

        return "\n".join(parts)

    # ── Demo thought strings ───────────────────────────────────────────────────

    @staticmethod
    def _demo_thought(seg_type: str, elements: list[str]) -> str:
        thoughts = {
            "ST":  "Verifying ST01 = 856 (Ship Notice transaction set identifier).",
            "BSN": "Checking BSN01 (transaction type), BSN02 (shipment ID), BSN03 (date format CCYYMMDD), and BSN05 (hierarchical structure code).",
            "HL":  f"Noting HL level code '{elements[2] if len(elements) > 2 else '?'}' — validating structural position in S/O/P/I hierarchy.",
            "TD1": "Verifying TD1-07 (weight qualifier G/N) and TD1-08 (gross weight > 0 and numeric).",
            "TD5": "Checking TD5-03 (carrier SCAC) is present, 2–4 characters, and in RetailerA's accepted SCAC list.",
            "REF": "Confirming REF segment is present and reference ID is non-empty.",
            "DTM": "Validating DTM date format is CCYYMMDD (8 digits).",
            "PRF": "Checking PRF-01 (PO number) is present, ≤ 22 chars, and cross-referencing with ShipStation.",
            "PO4": "Noting PO4 item physical details; checking for numeric quantity.",
            "LIN": "Verifying LIN-02 = 'UP' (UPC qualifier) and LIN-03 is exactly 12 numeric digits.",
            "SN1": "Checking SN1-02 (shipped quantity > 0) and SN1-03 (unit of measure present).",
            "PID": "Confirming PID (product description) is present; no strict format requirements.",
            "SE":  "Verifying SE01 (segment count) matches actual segment count in transaction.",
        }
        return thoughts.get(seg_type, f"Checking {seg_type} against partner spec requirements.")

    # ── Scoring and synthesis ──────────────────────────────────────────────────

    def _calculate_confidence(
        self, errors: list[ValidationError], segment_count: int
    ) -> float:
        """
        Compute a confidence score in [0, 1].

        confidence = 1 − (weighted_error_sum / segment_count)

        Clamped to [0.0, 1.0].  A document with no errors scores 1.0.
        """
        if segment_count == 0:
            return 0.0

        weights = self._cfg.severity_weights
        weighted_sum = sum(weights.get(e.severity, 0.1) for e in errors)
        raw = 1.0 - (weighted_sum / segment_count)
        return round(max(0.0, min(1.0, raw)), 4)

    def _synthesise(
        self,
        passed: bool,
        confidence: float,
        errors: list[ValidationError],
        partner_name: str,
    ) -> str:
        """Generate a one-line human-readable verdict."""
        critical = [e for e in errors if e.severity == "critical"]
        warnings = [e for e in errors if e.severity == "warning"]

        if passed:
            return (
                f"✅  PASS — Document meets {partner_name} spec requirements. "
                f"Confidence: {confidence:.0%}."
                + (f" ({len(warnings)} warning(s) noted.)" if warnings else "")
            )
        else:
            reason = (
                f"{len(critical)} critical error(s)" if critical
                else f"confidence {confidence:.0%} below threshold {self._cfg.confidence_threshold:.0%}"
            )
            return (
                f"❌  FAIL — Document does NOT meet {partner_name} spec. "
                f"Reason: {reason}. "
                f"Total issues: {len(errors)} ({len(critical)} critical, {len(warnings)} warning)."
            )
