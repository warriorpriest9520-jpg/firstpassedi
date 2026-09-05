"""
guardrails/confidence_gate.py
──────────────────────────────
Confidence thresholding for AI-generated validation and diagnostic results.

Design rationale:
  The agent assigns a confidence score (0.0–1.0) to every decision it makes.
  Rather than blindly routing all outputs, the gate enforces two thresholds:

    • VALIDATION threshold (0.85): If the agent is less than 85% confident
      that a document is compliant, it goes to human review — not submission.
      Better to delay one shipment than eat a $500 chargeback.

    • DIAGNOSTIC threshold (0.70): If the root-cause hypothesis has < 70%
      confidence, escalate with the full hypothesis tree so the compliance
      analyst can see the agent's reasoning and decide.

  These thresholds are parameterizable for fine-tuning after evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Thresholds (tunable)
# ---------------------------------------------------------------------------

VALIDATION_SUBMIT_THRESHOLD: float = 0.85
DIAGNOSTIC_ESCALATE_THRESHOLD: float = 0.70


# ---------------------------------------------------------------------------
# Data types accepted by the gate
# ---------------------------------------------------------------------------

@dataclass
class GateableResult:
    """
    Minimal interface expected by ConfidenceGate.

    Any result object passed to the gate should have at least these fields.
    In practice, ValidationResult and DiagnosticResult from the agent both
    satisfy this contract.
    """
    confidence: float               # 0.0–1.0 agent confidence score
    status: str                     # "pass" | "fail" | "ambiguous"
    document_id: str = ""
    partner: str = ""
    hypothesis_tree: list[dict[str, Any]] = field(default_factory=list)
    error_list: list[dict[str, Any]] = field(default_factory=list)
    extra_context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class ConfidenceGate:
    """
    Routes validation and diagnostic results based on agent confidence.

    Usage::

        gate = ConfidenceGate()
        if gate.should_submit(result):
            submit_to_edi_network(result)
        elif gate.should_escalate(result):
            report = gate.get_escalation_report(result)
            send_to_compliance_team(report)
        else:
            route_to_human_review(result)
    """

    def __init__(
        self,
        validation_threshold: float = VALIDATION_SUBMIT_THRESHOLD,
        diagnostic_threshold: float = DIAGNOSTIC_ESCALATE_THRESHOLD,
    ) -> None:
        """
        Args:
            validation_threshold: Minimum confidence to auto-submit a
                validated document (default 0.85).
            diagnostic_threshold: Minimum confidence on a root-cause
                hypothesis before escalation is suppressed (default 0.70).
        """
        self.validation_threshold = validation_threshold
        self.diagnostic_threshold = diagnostic_threshold

    # ── Public API ─────────────────────────────────────────────────────────

    def should_submit(self, result: GateableResult) -> bool:
        """
        Return True when the document is safe to auto-submit to the EDI
        network without human review.

        Conditions:
          - status is "pass"  AND
          - confidence >= validation_threshold
        """
        return result.status == "pass" and result.confidence >= self.validation_threshold

    def should_escalate(self, result: GateableResult) -> bool:
        """
        Return True when the diagnostic hypothesis is uncertain enough that
        a human compliance analyst must review the full reasoning tree.

        Conditions:
          - status is "fail" or "ambiguous"  AND
          - confidence < diagnostic_threshold  (agent is not sure why it failed)
          - OR no hypothesis tree was produced (agent gave up)
        """
        if result.status == "pass":
            return False  # passing docs don't need escalation

        low_confidence = result.confidence < self.diagnostic_threshold
        no_hypothesis  = len(result.hypothesis_tree) == 0

        return low_confidence or no_hypothesis

    def get_escalation_report(self, result: GateableResult) -> dict[str, Any]:
        """
        Build a structured escalation report for the compliance team.

        The report contains the full hypothesis tree so analysts can see
        exactly what the agent considered and why it was uncertain.

        Returns:
            A dict suitable for JSON serialization and email/ticket attachment.
        """
        reason = self._escalation_reason(result)

        return {
            "escalation_type": "compliance_review_required",
            "document_id": result.document_id,
            "partner": result.partner,
            "agent_confidence": result.confidence,
            "status": result.status,
            "reason": reason,
            "hypothesis_tree": result.hypothesis_tree,
            "error_list": result.error_list,
            "action_required": (
                "Please review the hypothesis tree and determine the correct "
                "disposition: fix-and-resubmit, waiver, or reject."
            ),
            "extra_context": result.extra_context,
        }

    # ── Internal helpers ────────────────────────────────────────────────────

    def _escalation_reason(self, result: GateableResult) -> str:
        """Produce a human-readable explanation of why escalation was triggered."""
        if len(result.hypothesis_tree) == 0:
            return (
                "Agent produced no diagnostic hypothesis. "
                "Manual investigation required."
            )
        if result.confidence < self.diagnostic_threshold:
            return (
                f"Agent confidence ({result.confidence:.0%}) is below the "
                f"diagnostic threshold ({self.diagnostic_threshold:.0%}). "
                "Hypothesis tree included for analyst review."
            )
        # status == "fail" but confidence acceptable — partner-specific rule
        return (
            f"Document failed compliance validation "
            f"(confidence {result.confidence:.0%}). "
            "See error_list for details."
        )
