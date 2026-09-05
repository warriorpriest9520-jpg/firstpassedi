"""
FirstPass EDI — Agents Package
================================
Exports all agent classes.

Agent roles in the pipeline
───────────────────────────
  OrchestratorAgent  — Top-level workflow coordinator.  Entry point for
                       processing an 856 document end-to-end.

  RetrievalAgent     — Queries the vector store for partner routing guide
                       specs and historical validation failures.

  ValidationAgent    — ReAct loop that checks each 856 segment against the
                       partner spec.  Returns pass/fail with confidence score.

  DiagnosticAgent    — Tree-of-Thought beam search for root-cause analysis
                       when validation fails.
"""

from .diagnostic_agent import DiagnosticAgent, DiagnosticResult, Hypothesis, BeamIteration
from .orchestrator import OrchestratorAgent, WorkflowResult, WorkflowState
from .retrieval_agent import RetrievalAgent, ContextPackage
from .validation_agent import ValidationAgent, ValidationResult, ValidationError, ReactStep

__all__ = [
    # Orchestrator
    "OrchestratorAgent",
    "WorkflowResult",
    "WorkflowState",
    # Retrieval
    "RetrievalAgent",
    "ContextPackage",
    # Validation
    "ValidationAgent",
    "ValidationResult",
    "ValidationError",
    "ReactStep",
    # Diagnostic
    "DiagnosticAgent",
    "DiagnosticResult",
    "Hypothesis",
    "BeamIteration",
]
