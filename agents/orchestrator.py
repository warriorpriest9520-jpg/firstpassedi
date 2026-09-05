"""
FirstPass EDI — Orchestrator Agent
=====================================
The Orchestrator is the top-level workflow manager for the FirstPass EDI
pipeline.  It receives an inbound X12 856 document, routes it through the
agent pipeline, and decides what to do with the result.

Pipeline
────────

  ┌───────────────────────────────────────────────────────────────────────┐
  │  inbound 856 string                                                   │
  └──────────────────────────┬────────────────────────────────────────────┘
                             │
                     EDIParser.parse()
                             │
                   ┌─────────▼──────────┐
                   │  ParsedEDI856      │
                   └─────────┬──────────┘
                             │
                RetrievalAgent.query(partner, "856")
                             │
                   ┌─────────▼──────────┐
                   │  ContextPackage    │
                   └─────────┬──────────┘
                             │
                ValidationAgent.validate(doc, context)
                             │
              ┌──────────────▼──────────────┐
              │      ValidationResult        │
              └──────────────┬──────────────┘
                             │
              ┌──────────────┼─────────────────────────┐
              │ PASSED       │                    FAILED │
              │              │                          │
  OrderfulClient        DiagnosticAgent             (≥3rd cycle?)
  .submit_856()         .diagnose()                  ESCALATE
              │              │
      SubmissionResult  DiagnosticResult
              │              │
           DONE      confidence ≥ 0.7?
                             │
                   YES ──────┼────── NO
                   │                  │
              apply fixes          ESCALATE
              revalidate
              (max 2 cycles)

State
─────
  WorkflowState is a dataclass tracking current phase, cycle count, and
  accumulated results.  A new WorkflowState is created per document.

Escalation triggers
───────────────────
  - DiagnosticAgent confidence < config.orchestrator.escalation_confidence_threshold
  - Revalidation cycle count >= config.orchestrator.max_revalidation_cycles
  - Orderful submission rejected (non-retryable)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import config
from agents.retrieval_agent import ContextPackage, RetrievalAgent
from agents.validation_agent import ValidationAgent, ValidationResult
from agents.diagnostic_agent import DiagnosticAgent, DiagnosticResult
from tools.edi_parser import EDIParser, ParsedEDI856
from tools.orderful_client import OrderfulClient, SubmissionResult
from tools.vector_store import VectorStore

logger = logging.getLogger(__name__)


# ── Trading partner registry ───────────────────────────────────────────────────
# Maps ISA sender/receiver ID pairs to human-readable partner names.
# In production, this would be loaded from a database or config file.

_PARTNER_REGISTRY: dict[str, str] = {
    "RETAILERA01": "RetailerA",
    "RETAILERB01": "RetailerB",
    "RETAILERC01": "RetailerC",
}


def _resolve_partner(parsed: ParsedEDI856) -> str:
    """
    Determine trading partner name from the ISA receiver ID.

    Falls back to the raw receiver_id if the ID is not in the registry.
    """
    receiver_id = parsed.interchange.receiver_id.strip().upper()
    return _PARTNER_REGISTRY.get(receiver_id, receiver_id or "UnknownPartner")


# ── Workflow state ─────────────────────────────────────────────────────────────


@dataclass
class WorkflowState:
    """
    Mutable state object tracking pipeline progress for one document.

    Created fresh per document.  Passed by reference through the pipeline
    so each stage can append to ``events`` and update counters.
    """

    document_id: str
    partner_name: str = ""
    phase: str = "INIT"             # INIT → RETRIEVE → VALIDATE → DIAGNOSE → SUBMIT | ESCALATE
    revalidation_cycles: int = 0
    started_at: datetime = field(default_factory=datetime.utcnow)

    # Accumulated results (populated as pipeline progresses)
    parsed_doc: Optional[ParsedEDI856] = None
    context: Optional[ContextPackage] = None
    validation_result: Optional[ValidationResult] = None
    diagnostic_result: Optional[DiagnosticResult] = None
    submission_result: Optional[SubmissionResult] = None
    escalation_reason: str = ""
    events: list[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        """Append a timestamped event to the event log."""
        ts = datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]
        entry = f"[{ts}] {msg}"
        self.events.append(entry)
        logger.info("[%s] %s", self.document_id[:12], msg)


@dataclass
class WorkflowResult:
    """
    Final output returned to the caller of ``OrchestratorAgent.process()``.

    Status values
    ─────────────
      SUBMITTED   — Document passed validation and was accepted by Orderful.
      ESCALATED   — Pipeline could not autonomously resolve errors; human needed.
      ERROR       — Unrecoverable pipeline error (parse failure, etc.).
    """

    document_id: str
    partner_name: str
    status: str                         # "SUBMITTED" | "ESCALATED" | "ERROR"
    revalidation_cycles: int
    duration_seconds: float
    events: list[str]
    validation_result: Optional[ValidationResult] = None
    diagnostic_result: Optional[DiagnosticResult] = None
    submission_result: Optional[SubmissionResult] = None
    escalation_reason: str = ""
    context_warnings: list[str] = field(default_factory=list)


# ── Orchestrator ──────────────────────────────────────────────────────────────


class OrchestratorAgent:
    """
    Top-level pipeline coordinator for FirstPass EDI.

    Instantiate once and call ``process()`` for each inbound 856 document.

    Parameters
    ----------
    All parameters are optional.  Defaults instantiate each sub-agent using
    global ``config`` settings.  Pass pre-built instances for testing or to
    share a single VectorStore across agents.
    """

    def __init__(
        self,
        parser: Optional[EDIParser] = None,
        vector_store: Optional[VectorStore] = None,
        retrieval_agent: Optional[RetrievalAgent] = None,
        validation_agent: Optional[ValidationAgent] = None,
        diagnostic_agent: Optional[DiagnosticAgent] = None,
        orderful_client: Optional[OrderfulClient] = None,
    ) -> None:
        # Shared VectorStore so all agents use the same in-process ChromaDB connection.
        shared_store = vector_store or VectorStore()

        self._parser = parser or EDIParser()
        self._retrieval = retrieval_agent or RetrievalAgent(vector_store=shared_store)
        self._validation = validation_agent or ValidationAgent()
        self._diagnostic = diagnostic_agent or DiagnosticAgent(vector_store=shared_store)
        self._orderful = orderful_client or OrderfulClient()
        self._cfg = config.orchestrator

    def process(self, raw_edi: str, document_id: Optional[str] = None) -> WorkflowResult:
        """
        Process one X12 856 document through the full FirstPass EDI pipeline.

        Parameters
        ----------
        raw_edi:
            The full raw X12 856 string (ISA through IEA).
        document_id:
            Optional caller-provided ID for tracing.  Auto-generated if omitted.

        Returns
        -------
        WorkflowResult
            Populated with final status, all intermediate results, and event log.
        """
        import uuid
        doc_id = document_id or f"DOC-{uuid.uuid4().hex[:8].upper()}"
        state = WorkflowState(document_id=doc_id)
        state.log("Pipeline started.")

        try:
            # ── Phase 1: Parse ─────────────────────────────────────────────────
            state.phase = "PARSE"
            state.log("Phase 1: Parsing X12 856 document …")
            parsed = self._parse(raw_edi, state)
            if parsed is None:
                return self._error_result(state, "Parse failed — document is malformed.")

            state.parsed_doc = parsed
            state.partner_name = _resolve_partner(parsed)
            parsed.partner_name = state.partner_name
            state.log(
                f"Parsed OK: shipment_id='{parsed.bsn.shipment_id}', "
                f"partner='{state.partner_name}', "
                f"segments={len(parsed.raw_segments)}."
            )

            # ── Phase 2: Retrieve context ──────────────────────────────────────
            state.phase = "RETRIEVE"
            state.log("Phase 2: Retrieving partner spec and history from vector store …")
            context = self._retrieve(state)
            state.context = context

            if context.warnings:
                for w in context.warnings:
                    state.log(f"⚠️  Retrieval warning: {w}")

            state.log(
                f"Retrieved spec '{context.spec_version}' for '{context.partner_name}' "
                f"({len(context.ranked_chunks)} chunks). Stale: {context.is_stale}."
            )

            # ── Phase 3: Validate ──────────────────────────────────────────────
            state.phase = "VALIDATE"
            state.log("Phase 3: Running ValidationAgent ReAct loop …")
            val_result = self._validate(parsed, context, state)
            state.validation_result = val_result
            state.log(
                f"Validation: {'PASS' if val_result.passed else 'FAIL'} | "
                f"confidence={val_result.confidence:.0%} | "
                f"errors={len(val_result.errors)}"
            )

            # ── Branch: validate passed → submit ───────────────────────────────
            if val_result.passed:
                return self._submit(raw_edi, state)

            # ── Phase 4: Diagnose → fix → revalidate loop ─────────────────────
            while True:
                state.phase = "DIAGNOSE"
                state.log(
                    f"Phase 4: DiagnosticAgent ToT beam search "
                    f"(cycle {state.revalidation_cycles + 1}/{self._cfg.max_revalidation_cycles}) …"
                )
                diag_result = self._diagnose(val_result, context, state)
                state.diagnostic_result = diag_result
                state.log(diag_result.summary)

                # Escalate if diagnostic confidence is too low.
                if diag_result.should_escalate:
                    return self._escalate(
                        state,
                        reason=(
                            f"DiagnosticAgent confidence {diag_result.confidence:.0%} "
                            f"< threshold {self._cfg.escalation_confidence_threshold:.0%}. "
                            "Human review required."
                        ),
                    )

                # Apply recommended fix (logged action, not a real code change).
                state.revalidation_cycles += 1
                self._apply_fix_recommendations(diag_result, state)

                # Cap revalidation cycles.
                if state.revalidation_cycles >= self._cfg.max_revalidation_cycles:
                    return self._escalate(
                        state,
                        reason=(
                            f"Maximum revalidation cycles ({self._cfg.max_revalidation_cycles}) "
                            "reached without passing validation. Escalating for manual fix."
                        ),
                    )

                # Re-validate.
                state.phase = "REVALIDATE"
                state.log(
                    f"Re-running validation (cycle {state.revalidation_cycles}) …"
                )
                val_result = self._validate(parsed, context, state)
                state.validation_result = val_result
                state.log(
                    f"Re-validation: {'PASS' if val_result.passed else 'FAIL'} | "
                    f"confidence={val_result.confidence:.0%}"
                )

                if val_result.passed:
                    return self._submit(raw_edi, state)
                # else: loop back to diagnose with updated val_result

        except Exception as exc:
            logger.exception("Unhandled exception in pipeline for doc '%s'.", doc_id)
            return self._error_result(state, f"Pipeline error: {exc}")

    # ── Pipeline stage helpers ────────────────────────────────────────────────

    def _parse(self, raw_edi: str, state: WorkflowState) -> Optional[ParsedEDI856]:
        """Attempt to parse the raw EDI string; return None on failure."""
        try:
            return self._parser.parse(raw_edi)
        except Exception as exc:
            state.log(f"Parse error: {exc}")
            return None

    def _retrieve(self, state: WorkflowState) -> ContextPackage:
        """Retrieve partner spec and history from the vector store."""
        return self._retrieval.query(
            partner_name=state.partner_name,
            doc_type="856",
        )

    def _validate(
        self,
        parsed: ParsedEDI856,
        context: ContextPackage,
        state: WorkflowState,
    ) -> ValidationResult:
        """Run the ValidationAgent."""
        return self._validation.validate(parsed, context)

    def _diagnose(
        self,
        val_result: ValidationResult,
        context: ContextPackage,
        state: WorkflowState,
    ) -> DiagnosticResult:
        """Run the DiagnosticAgent."""
        return self._diagnostic.diagnose(val_result, context)

    def _apply_fix_recommendations(
        self, diag: DiagnosticResult, state: WorkflowState
    ) -> None:
        """
        Log the top fix recommendation from the DiagnosticAgent.

        In a real integration, this step would trigger a workflow action
        (e.g. notify the EDI team, auto-patch the document, update the
        carrier mapping table) based on the error code and recommendation.

        For this demo we log the recommendation and simulate the fix being applied
        so the revalidation cycle can proceed.
        """
        if diag.top_diagnoses:
            top = diag.top_diagnoses[0]
            state.log(
                f"Applying fix: [{top.description}] → {top.fix_recommendation}"
            )
            state.log(
                "NOTE (demo): In production, this would trigger an automated or "
                "human-assisted correction workflow before resubmission."
            )
        else:
            state.log("No fix recommendations available from DiagnosticAgent.")

    # ── Terminal outcomes ──────────────────────────────────────────────────────

    def _submit(self, raw_edi: str, state: WorkflowState) -> WorkflowResult:
        """Submit the validated document to Orderful and return a SUBMITTED result."""
        state.phase = "SUBMIT"
        state.log("Submitting validated 856 to Orderful …")

        # Pre-submission structural validation (belt-and-suspenders).
        struct = self._orderful.validate_structure(raw_edi)
        if not struct.valid:
            structural_issues = "; ".join(i.message for i in struct.issues if i.severity == "error")
            state.log(f"Orderful structural validation failed: {structural_issues}")
            return self._escalate(
                state,
                reason=f"Orderful structural validation rejected the document: {structural_issues}",
            )

        sub_result = self._orderful.submit_856(raw_edi)
        state.submission_result = sub_result

        if sub_result.success:
            state.log(
                f"✅ Submitted. Orderful txn_id={sub_result.transaction_id}, "
                f"ack={sub_result.ack_status}."
            )
            state.phase = "SUBMITTED"
        else:
            state.log(f"Orderful rejected submission: {sub_result.message}")
            return self._escalate(
                state,
                reason=f"Orderful rejected submission: {sub_result.message}",
            )

        return self._build_result(state, "SUBMITTED")

    def _escalate(self, state: WorkflowState, reason: str) -> WorkflowResult:
        """Mark the workflow as escalated and return a ESCALATED result."""
        state.phase = "ESCALATED"
        state.escalation_reason = reason
        state.log(f"⚠️  ESCALATED: {reason}")
        return self._build_result(state, "ESCALATED")

    def _error_result(self, state: WorkflowState, reason: str) -> WorkflowResult:
        """Return an ERROR result for unrecoverable pipeline failures."""
        state.phase = "ERROR"
        state.escalation_reason = reason
        state.log(f"💥 ERROR: {reason}")
        return self._build_result(state, "ERROR")

    def _build_result(self, state: WorkflowState, status: str) -> WorkflowResult:
        """Assemble the final WorkflowResult from current state."""
        duration = (datetime.utcnow() - state.started_at).total_seconds()
        context_warnings = state.context.warnings if state.context else []

        return WorkflowResult(
            document_id=state.document_id,
            partner_name=state.partner_name,
            status=status,
            revalidation_cycles=state.revalidation_cycles,
            duration_seconds=round(duration, 3),
            events=state.events,
            validation_result=state.validation_result,
            diagnostic_result=state.diagnostic_result,
            submission_result=state.submission_result,
            escalation_reason=state.escalation_reason,
            context_warnings=context_warnings,
        )
