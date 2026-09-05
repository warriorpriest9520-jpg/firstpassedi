"""
evaluation/test_cases.py
─────────────────────────
Demonstration test scenarios for FirstPass EDI.

Each function is a self-contained scenario that exercises a distinct
system behaviour. Run all five with:

    python evaluation/test_cases.py

Design rationale:
  These are "glass-box" integration demos — they wire the guardrails and
  evaluation layer together with minimal mocking so a reader can see the
  full call chain in one place. They are not unit tests (no assertions);
  they print structured output so they work as a live walkthrough in class.
"""

from __future__ import annotations

import json
import sys
import os
from datetime import date, timedelta

# Allow running from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from guardrails.schema_validator import SchemaValidator
from guardrails.confidence_gate import ConfidenceGate, GateableResult
from guardrails.staleness_check import StalenessCheck
from evaluation.metrics import EvaluationMetrics


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    print(f"\n{'═' * 62}")
    print(f"  TEST: {title}")
    print('═' * 62)


# ---------------------------------------------------------------------------
# Sample X12 856 strings used across test cases
# ---------------------------------------------------------------------------

# A valid 856 ASN — passes structural validation
# ISA06 (sender ID) and ISA08 (receiver ID) must each be exactly 15 characters,
# padded with trailing spaces. Position 105 (0-indexed) must be the segment
# terminator '~'. Padding errors shift that position and break all parsing.
_VALID_856 = (
    "ISA*00*          *00*          *ZZ*ACMESUPPLIER   *ZZ*RETAILERA      "
    "*260901*0734*^*00401*000000001*0*P*>~"
    "GS*SH*ACMESUPPLIER*RETAILERA*20260901*0734*1*X*004010~"
    "ST*856*0001~"
    "BSN*00*ASN20260901001*20260901*073400*0001~"
    "HL*1**S~"
    "DTM*011*20260901~"
    "DTM*063*20260902~"
    "REF*BM*PO-20260901-001~"
    "HL*2*1*O~"
    "REF*CO*ORD-88512~"
    "HL*3*2*P~"
    "TD1*CTN25*1****G*48*LB~"
    "TD5*B*2*UPSG~"
    "SE*12*0001~"
    "GE*1*1~"
    "IEA*1*000000001~"
)

# 856 missing DTM*063 (ship-by date) — RetailerA requires it
_MISSING_DTM063_856 = (
    "ISA*00*          *00*          *ZZ*ACMESUPPLIER   *ZZ*RETAILERA      "
    "*260901*0734*^*00401*000000002*0*P*>~"
    "GS*SH*ACMESUPPLIER*RETAILERA*20260901*0734*2*X*004010~"
    "ST*856*0002~"
    "BSN*00*ASN20260901002*20260901*073400*0001~"
    "HL*1**S~"
    "DTM*011*20260901~"
    # DTM*063 intentionally omitted
    "REF*BM*PO-20260901-002~"
    "HL*2*1*O~"
    "REF*CO*ORD-88513~"
    "HL*3*2*P~"
    "TD1*CTN25*1****G*52*LB~"
    "TD5*B*2*UPSG~"
    "SE*11*0002~"
    "GE*1*2~"
    "IEA*1*000000002~"
)

# Structurally broken: segment terminator collides with element separator
# Intentionally short: ISA header terminates at '*P*' with no '>' or '~'.
# With correct 15-char padding, len=104 < 106 → "ISA header too short" error.
_BROKEN_856 = (
    "ISA*00*          *00*          *ZZ*ACMESUPPLIER   *ZZ*RETAILERB      "
    "*260901*0734*^*00401*000000003*0*P*"  # terminator is missing — ISA too short
)

# Ambiguous 856: wrong REF qualifier — could be data entry or spec mismatch
_AMBIGUOUS_856 = (
    "ISA*00*          *00*          *ZZ*ACMESUPPLIER   *ZZ*RETAILERA      "
    "*260901*0734*^*00401*000000004*0*P*>~"
    "GS*SH*ACMESUPPLIER*RETAILERA*20260901*0734*4*X*004010~"
    "ST*856*0004~"
    "BSN*00*ASN20260901004*20260901*073400*0001~"
    "HL*1**S~"
    "DTM*011*20260901~"
    "DTM*063*20260902~"
    "REF*ZZ*UNKNOWN-QUALIFIER-VALUE~"   # ZZ is ambiguous — not BM or PO
    "HL*2*1*O~"
    "REF*CO*ORD-88514~"
    "HL*3*2*P~"
    "TD1*CTN25*1****G*35*LB~"
    "TD5*B*2*UPSG~"
    "SE*12*0004~"
    "GE*1*4~"
    "IEA*1*000000004~"
)


# ---------------------------------------------------------------------------
# Test Case 1: Happy path — valid 856 passes all checks
# ---------------------------------------------------------------------------

def test_case_1_happy_path() -> None:
    """
    Scenario: A fully compliant 856 from AcmeSupplier to RetailerA.
    Expected: Structural validation passes, confidence gate approves submission.
    """
    _section("1 — Happy Path: Valid 856 (all checks pass)")

    validator = SchemaValidator()
    result = validator.validate(_VALID_856)

    print(f"  Structural validation : {'PASSED ✓' if result.passed else 'FAILED ✗'}")
    print(f"  Segments parsed       : {result.segment_count}")
    print(f"  Errors                : {result.error_count}")

    # Simulate agent verdict: high confidence, document is compliant
    gate_input = GateableResult(
        confidence=0.97,
        status="pass",
        document_id="ASN20260901001",
        partner="RetailerA",
    )
    gate = ConfidenceGate()
    print(f"  Agent confidence      : {gate_input.confidence:.0%}")
    print(f"  Should submit         : {'YES ✓' if gate.should_submit(gate_input) else 'NO'}")
    print(f"  Should escalate       : {'YES' if gate.should_escalate(gate_input) else 'NO ✓'}")
    print("\n  ➜  Document submitted to EDI network automatically.")


# ---------------------------------------------------------------------------
# Test Case 2: Missing DTM*063 — partner-specific compliance failure
# ---------------------------------------------------------------------------

def test_case_2_missing_dtm063() -> None:
    """
    Scenario: 856 structurally valid but missing DTM*063 (ship-by date).
    RetailerA's routing guide requires this segment; without it, a chargeback
    for 'missing ASN date' will be issued.
    Expected: Structural validation passes, but agent flags compliance failure.
    """
    _section("2 — Partner-Specific Failure: Missing DTM*063")

    validator = SchemaValidator()
    result = validator.validate(_MISSING_DTM063_856)

    print(f"  Structural validation : {'PASSED ✓' if result.passed else 'FAILED ✗'}")
    print(f"  (Structure is valid — missing DTM*063 is a compliance rule, not X12 syntax)")

    # Agent detects DTM*063 missing against RetailerA spec; high confidence
    gate_input = GateableResult(
        confidence=0.91,
        status="fail",
        document_id="ASN20260901002",
        partner="RetailerA",
        error_list=[
            {
                "severity": "error",
                "segment": "DTM",
                "description": "Required DTM*063 (Ship-By Date) is absent.",
                "recommended_fix": (
                    "Add DTM*063*YYYYMMDD after DTM*011 at shipment HL level. "
                    "RetailerA routing guide v2025-Q4 section 4.3 requires this."
                ),
            }
        ],
        hypothesis_tree=[
            {"hypothesis": "DTM*063 omitted by sender", "confidence": 0.91},
            {"hypothesis": "Sender used DTM*011 intending to cover ship date", "confidence": 0.09},
        ],
    )

    gate = ConfidenceGate()
    print(f"\n  Agent verdict         : {gate_input.status.upper()}")
    print(f"  Agent confidence      : {gate_input.confidence:.0%}")
    print(f"  Should submit         : {'YES' if gate.should_submit(gate_input) else 'NO ✗'}")
    print(f"  Should escalate       : {'YES' if gate.should_escalate(gate_input) else 'NO ✓ (agent confident in diagnosis)'}")
    print(f"\n  Errors found:")
    for err in gate_input.error_list:
        print(f"    [{err['severity'].upper()}] {err['segment']}: {err['description']}")
        print(f"    Fix: {err['recommended_fix']}")
    print("\n  ➜  Document BLOCKED. Fix returned to AcmeSupplier for correction.")


# ---------------------------------------------------------------------------
# Test Case 3: Structurally invalid — caught by guardrail before agent runs
# ---------------------------------------------------------------------------

def test_case_3_structural_invalid() -> None:
    """
    Scenario: 856 with a malformed ISA header (too short, terminator missing).
    Expected: SchemaValidator rejects the document; agent reasoning loop never runs.
    """
    _section("3 — Structural Guardrail: Malformed ISA (rejected pre-reasoning)")

    validator = SchemaValidator()
    result = validator.validate(_BROKEN_856)

    print(f"  Structural validation : {'PASSED' if result.passed else 'FAILED ✗ (as expected)'}")
    print(f"  Errors found          : {result.error_count}")
    for err in result.errors:
        print(f"    [{err.severity.upper()}] {err.segment} (pos {err.position}): {err.description}")
        print(f"    Fix: {err.recommended_fix}")

    print("\n  ➜  Document REJECTED at guardrail layer. Agent loop NOT invoked.")
    print("     Sender notified: reformat ISA header and resubmit.")


# ---------------------------------------------------------------------------
# Test Case 4: Ambiguous error — Tree of Thought diagnostic triggered
# ---------------------------------------------------------------------------

def test_case_4_ambiguous_tot_diagnostic() -> None:
    """
    Scenario: 856 contains REF*ZZ which is structurally valid but semantically
    ambiguous. The agent generates a hypothesis tree (Tree of Thought) and
    confidence is below the diagnostic threshold, triggering escalation.
    """
    _section("4 — Ambiguous Error: ToT Diagnostic + Escalation")

    validator = SchemaValidator()
    result = validator.validate(_AMBIGUOUS_856)
    print(f"  Structural validation : {'PASSED ✓' if result.passed else 'FAILED'}")

    # Agent produces a split hypothesis tree — not confident enough
    gate_input = GateableResult(
        confidence=0.58,    # Below 0.70 diagnostic threshold → ESCALATE
        status="fail",
        document_id="ASN20260901004",
        partner="RetailerA",
        error_list=[
            {
                "severity": "warning",
                "segment": "REF",
                "description": "REF qualifier 'ZZ' is non-standard. RetailerA expects 'BM' (bill of lading) or 'PO' (purchase order).",
                "recommended_fix": "Confirm with RetailerA whether ZZ is an approved trading partner-specific code.",
            }
        ],
        hypothesis_tree=[
            {
                "hypothesis": "Sender used ZZ as a custom trading-partner qualifier per an informal agreement",
                "confidence": 0.38,
                "evidence": "No prior chargebacks on REF*ZZ for this partner",
            },
            {
                "hypothesis": "ZZ is a data-entry error; sender intended BM",
                "confidence": 0.34,
                "evidence": "BM is the most common REF qualifier in 856s from AcmeSupplier",
            },
            {
                "hypothesis": "ZZ reflects a routing guide version mismatch",
                "confidence": 0.28,
                "evidence": "Routing guide last updated 97 days ago — potentially stale",
            },
        ],
    )

    gate = ConfidenceGate()
    should_submit  = gate.should_submit(gate_input)
    should_escalate = gate.should_escalate(gate_input)

    print(f"\n  Agent confidence      : {gate_input.confidence:.0%}  (threshold 70%)")
    print(f"  Should submit         : {'YES' if should_submit else 'NO ✗'}")
    print(f"  Should escalate       : {'YES ← triggered' if should_escalate else 'NO'}")

    if should_escalate:
        report = gate.get_escalation_report(gate_input)
        print(f"\n  Escalation report:")
        print(f"    Reason: {report['reason']}")
        print(f"    Hypothesis tree ({len(report['hypothesis_tree'])} branches):")
        for h in report["hypothesis_tree"]:
            print(f"      {h['confidence']:.0%}  {h['hypothesis']}")
            print(f"           Evidence: {h['evidence']}")

    print("\n  ➜  Escalated to compliance analyst with full hypothesis tree.")


# ---------------------------------------------------------------------------
# Test Case 5: New partner — no history, staleness warning triggers escalation
# ---------------------------------------------------------------------------

def test_case_5_new_partner_no_history() -> None:
    """
    Scenario: 856 from AcmeSupplier to RetailerB — a brand-new trading
    relationship. No historical chargebacks exist; the routing guide was
    just received and dated today. Agent escalates because it cannot
    leverage any prior pattern data.
    """
    _section("5 — New Partner: No History + Spec Freshness Check")

    # Routing guide effective date is today — fresh, no staleness warning
    checker = StalenessCheck()
    freshness = checker.check_spec_freshness(
        partner_name="RetailerB",
        spec_date=date.today(),
        days_threshold=90,
    )
    print(f"  Spec freshness check  : {freshness.warning_message}")

    # But what if it was old?
    old_date = date.today() - timedelta(days=110)
    stale_result = checker.check_spec_freshness(
        partner_name="RetailerB",
        spec_date=old_date,
        days_threshold=90,
    )
    print(f"  (Simulated old spec)  : {stale_result.warning_message}")
    if stale_result.recommended_action:
        print(f"  Recommended action    : {stale_result.recommended_action}")

    # Agent has no historical embeddings for RetailerB → escalates
    gate_input = GateableResult(
        confidence=0.52,    # Below 0.70 diagnostic threshold
        status="ambiguous",
        document_id="ASN20260901005",
        partner="RetailerB",
        extra_context={
            "reason": "New trading partner — zero historical chargeback records",
            "historical_records": 0,
            "note": "Cannot apply prior pattern matching; escalating for manual review.",
        },
        hypothesis_tree=[],   # No hypothesis tree — agent produced none
    )

    gate = ConfidenceGate()
    should_escalate = gate.should_escalate(gate_input)

    print(f"\n  Agent confidence      : {gate_input.confidence:.0%}")
    print(f"  Historical records    : {gate_input.extra_context['historical_records']}")
    print(f"  Should escalate       : {'YES ← triggered' if should_escalate else 'NO'}")

    if should_escalate:
        report = gate.get_escalation_report(gate_input)
        print(f"  Escalation reason     : {report['reason']}")
        print(f"  Action required       : {report['action_required']}")

    print("\n  ➜  Routed to senior compliance analyst for first-time partner review.")
    print("     Recommend: establish baseline spec and run 3 shadow validations")
    print("     before enabling auto-submit for RetailerB.")


# ---------------------------------------------------------------------------
# Entry point — run all test cases
# ---------------------------------------------------------------------------

def run_all() -> None:
    """Run every test case sequentially with a metrics summary at the end."""
    print("\n" + "█" * 62)
    print("  FirstPass EDI — Demonstration Test Suite")
    print("  Capstone Project: X12 856 Compliance Validation")
    print("█" * 62)

    test_case_1_happy_path()
    test_case_2_missing_dtm063()
    test_case_3_structural_invalid()
    test_case_4_ambiguous_tot_diagnostic()
    test_case_5_new_partner_no_history()

    # ── Synthetic metrics summary ──────────────────────────────────────────
    # Five synthetic records mapping to the five test cases above.
    print(f"\n{'═' * 62}")
    print("  METRICS SUMMARY (synthetic records for these 5 scenarios)")
    print('═' * 62)

    records = [
        # Case 1: valid doc, compliant, no chargeback, no FP, resolved, fast
        {
            "document_id": "ASN20260901001", "partner": "RetailerA",
            "ground_truth": "compliant", "agent_verdict": "pass",
            "chargeback_avoided": True, "false_positive": False,
            "escalated": False, "retrieval_ranks": [1],
            "diagnostic_resolved": True, "latency_seconds": 2.1,
        },
        # Case 2: non-compliant doc caught, chargeback avoided, agent confident
        {
            "document_id": "ASN20260901002", "partner": "RetailerA",
            "ground_truth": "non_compliant", "agent_verdict": "fail",
            "chargeback_avoided": True, "false_positive": False,
            "escalated": False, "retrieval_ranks": [2],
            "diagnostic_resolved": True, "latency_seconds": 3.4,
        },
        # Case 3: structural rejection, non-compliant, caught at guardrail
        {
            "document_id": "ASN20260901003", "partner": "RetailerB",
            "ground_truth": "non_compliant", "agent_verdict": "fail",
            "chargeback_avoided": True, "false_positive": False,
            "escalated": False, "retrieval_ranks": [1],
            "diagnostic_resolved": True, "latency_seconds": 0.3,
        },
        # Case 4: ambiguous, escalated, agent uncertain — not resolved autonomously
        {
            "document_id": "ASN20260901004", "partner": "RetailerA",
            "ground_truth": "non_compliant", "agent_verdict": "escalated",
            "chargeback_avoided": True, "false_positive": False,
            "escalated": True, "retrieval_ranks": [3, 5],
            "diagnostic_resolved": False, "latency_seconds": 6.8,
        },
        # Case 5: new partner, escalated — not resolved autonomously
        {
            "document_id": "ASN20260901005", "partner": "RetailerB",
            "ground_truth": "compliant", "agent_verdict": "escalated",
            "chargeback_avoided": True, "false_positive": False,
            "escalated": True, "retrieval_ranks": [0],
            "diagnostic_resolved": False, "latency_seconds": 4.1,
        },
    ]

    em = EvaluationMetrics()
    report = em.compute(records)
    em.generate_report(report)


if __name__ == "__main__":
    run_all()
