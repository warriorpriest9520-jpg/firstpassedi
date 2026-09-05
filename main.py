"""
FirstPass EDI — Main Entry Point
==================================
Demonstrates the complete FirstPass EDI pipeline by processing two sample
X12 856 Advance Ship Notice documents:

  Run 1 — Valid document (SHIP-2026-09-001) for RetailerA
           Expected outcome: SUBMITTED ✅

  Run 2 — Document with intentional errors (SHIP-2026-09-002) for RetailerA
           Expected outcome: ESCALATED ⚠️ (or revalidation + submit, depending
           on how the demo fix round resolves)

Usage
─────
  # Demo mode (no API key required):
  python main.py

  # Production mode (requires OPENAI_API_KEY in .env):
  DEMO_MODE=false python main.py

  # Single document from stdin:
  cat my_856.edi | python main.py --stdin

Architecture recap (printed at startup)
───────────────────────────────────────
  inbound 856
      │
  EDIParser              (parse X12 → dataclass)
      │
  RetrievalAgent         (vector store → ContextPackage)
      │
  ValidationAgent        (ReAct loop → ValidationResult)
      │
  ┌───┴────────────────────┐
  │ PASS                FAIL│
  │                        │
  Orderful.submit     DiagnosticAgent (ToT beam search)
                           │
                      confidence ≥ 0.7?
                           │
                   YES ────┤──── NO
                   │             │
                apply fix    ESCALATE
                revalidate
                (max 2 cycles)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from textwrap import dedent

# ── Attempt rich console output (falls back gracefully) ──────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import print as rprint
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]

from config import config
from agents.orchestrator import OrchestratorAgent, WorkflowResult
from tools.edi_parser import SAMPLE_856_RETAILER_A, SAMPLE_856_RETAILER_A_WITH_ERRORS
from tools.vector_store import VectorStore

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("firstpass")


# ── Display helpers ───────────────────────────────────────────────────────────


def _banner() -> None:
    """Print the project banner."""
    text = dedent("""
    ╔══════════════════════════════════════════════════════╗
    ║          F I R S T P A S S   E D I   v1.0           ║
    ║   Multi-Agent X12 856 ASN Compliance Validator       ║
    ╚══════════════════════════════════════════════════════╝
    """).strip()
    if _RICH:
        console.print(Panel(text, style="bold cyan"))
    else:
        print("\n" + text + "\n")


def _print_result(result: WorkflowResult, run_label: str) -> None:
    """Pretty-print a WorkflowResult to the console."""
    status_icon = {"SUBMITTED": "✅", "ESCALATED": "⚠️ ", "ERROR": "💥"}.get(result.status, "?")
    separator = "─" * 60

    if _RICH:
        _print_result_rich(result, run_label, status_icon)
    else:
        _print_result_plain(result, run_label, status_icon, separator)


def _print_result_rich(result: WorkflowResult, run_label: str, icon: str) -> None:
    """Rich-formatted result output."""
    status_color = {"SUBMITTED": "green", "ESCALATED": "yellow", "ERROR": "red"}.get(
        result.status, "white"
    )

    console.rule(f"[bold]{run_label}[/bold]")
    console.print(
        f"\n{icon}  [{status_color}]Status: {result.status}[/{status_color}]   "
        f"Partner: {result.partner_name}   "
        f"Document: {result.document_id}   "
        f"Duration: {result.duration_seconds:.2f}s\n"
    )

    # Context warnings
    if result.context_warnings:
        console.print("[yellow]Context Warnings:[/yellow]")
        for w in result.context_warnings:
            console.print(f"  ⚠️  {w}")
        console.print()

    # Validation summary
    if result.validation_result:
        vr = result.validation_result
        console.print(f"[bold]Validation:[/bold] {vr.summary}")
        if vr.errors:
            table = Table(title="Validation Errors", show_header=True)
            table.add_column("Segment", style="cyan")
            table.add_column("Element", style="dim")
            table.add_column("Severity", style="bold")
            table.add_column("Code", style="dim")
            table.add_column("Description")
            for e in vr.errors:
                color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(e.severity, "")
                table.add_row(
                    e.segment_type, e.element_id,
                    f"[{color}]{e.severity.upper()}[/{color}]",
                    e.code, e.description,
                )
            console.print(table)
        console.print()

    # ReAct steps (collapsed)
    if result.validation_result and result.validation_result.react_steps:
        steps = result.validation_result.react_steps
        console.print(f"[dim]ReAct Steps ({len(steps)} total — first 3 shown):[/dim]")
        for step in steps[:3]:
            console.print(
                f"  [dim]Step {step.step_number} [{step.segment_type}][/dim]\n"
                f"    THOUGHT:     {step.thought}\n"
                f"    ACTION:      {step.action}\n"
                f"    OBSERVATION: {step.observation[:120]}{'…' if len(step.observation) > 120 else ''}\n"
            )

    # Diagnostic summary
    if result.diagnostic_result:
        dr = result.diagnostic_result
        console.print(f"[bold]Diagnosis:[/bold] {dr.summary}")
        if dr.top_diagnoses:
            table = Table(title="Diagnostic Beam (Top Hypotheses)", show_header=True)
            table.add_column("Rank", style="bold")
            table.add_column("Description", style="cyan")
            table.add_column("Confidence", style="green")
            table.add_column("Fix Recommendation")
            for i, h in enumerate(dr.top_diagnoses, 1):
                table.add_row(
                    str(i), h.description, f"{h.score:.0%}", h.fix_recommendation[:80] + "…"
                )
            console.print(table)
            console.print(
                f"[dim]Beam iterations: {len(dr.beam_iterations)}   "
                f"Total hypotheses evaluated: "
                f"{sum(len(it.candidates) for it in dr.beam_iterations)}[/dim]"
            )
        console.print()

    # Submission
    if result.submission_result:
        sr = result.submission_result
        console.print(
            f"[bold]Submission:[/bold] txn_id={sr.transaction_id}  "
            f"ack={sr.ack_status}  {sr.message}"
        )
        console.print()

    # Escalation
    if result.status == "ESCALATED":
        console.print(f"[yellow]Escalation reason:[/yellow] {result.escalation_reason}")
        console.print()

    # Event log (collapsed)
    console.print(f"[dim]Pipeline event log ({len(result.events)} events):[/dim]")
    for ev in result.events:
        console.print(f"  [dim]{ev}[/dim]")
    console.print()


def _print_result_plain(
    result: WorkflowResult, run_label: str, icon: str, separator: str
) -> None:
    """Plain text result output (no rich dependency)."""
    print(f"\n{separator}")
    print(f" {run_label}")
    print(separator)
    print(f"{icon} Status:   {result.status}")
    print(f"   Partner:  {result.partner_name}")
    print(f"   Doc ID:   {result.document_id}")
    print(f"   Duration: {result.duration_seconds:.2f}s")
    print(f"   Cycles:   {result.revalidation_cycles}")

    if result.context_warnings:
        print("\n  Context Warnings:")
        for w in result.context_warnings:
            print(f"    ⚠️  {w}")

    if result.validation_result:
        vr = result.validation_result
        print(f"\n  Validation: {vr.summary}")
        print(f"  Segments checked: {vr.segment_count}")
        print(f"  Errors: {len(vr.errors)}")
        for e in vr.errors:
            print(f"    [{e.severity.upper():8s}] {e.element_id}: {e.description}")

    if result.validation_result and result.validation_result.react_steps:
        steps = result.validation_result.react_steps
        print(f"\n  ReAct Steps ({len(steps)} total — first 3 shown):")
        for step in steps[:3]:
            print(f"    Step {step.step_number} [{step.segment_type}]")
            print(f"      THOUGHT:     {step.thought}")
            print(f"      ACTION:      {step.action}")
            obs_short = step.observation.replace("\n", " ")[:100]
            print(f"      OBSERVATION: {obs_short}…")

    if result.diagnostic_result:
        dr = result.diagnostic_result
        print(f"\n  Diagnosis: {dr.summary}")
        for i, h in enumerate(dr.top_diagnoses, 1):
            print(f"    Rank {i} ({h.score:.0%}): {h.description}")
            print(f"      Root cause: {h.root_cause[:100]}…")
            print(f"      Fix:        {h.fix_recommendation[:100]}…")
        beam_count = sum(len(it.candidates) for it in dr.beam_iterations)
        print(f"    Iterations: {len(dr.beam_iterations)}  Hypotheses evaluated: {beam_count}")

    if result.submission_result:
        sr = result.submission_result
        print(f"\n  Submission: txn_id={sr.transaction_id}  ack={sr.ack_status}")
        print(f"    {sr.message}")

    if result.status == "ESCALATED":
        print(f"\n  Escalation reason: {result.escalation_reason}")

    print(f"\n  Event log ({len(result.events)} events):")
    for ev in result.events:
        print(f"    {ev}")

    print(separator)


# ── Bootstrap ─────────────────────────────────────────────────────────────────


def _seed_and_init() -> OrchestratorAgent:
    """
    Seed the vector store with demo data and build the orchestrator.

    Called once at startup.  In production the vector store would already
    be populated; this step only runs in DEMO_MODE.
    """
    logger.info("Initialising vector store …")
    store = VectorStore()

    if config.demo_mode:
        logger.info("DEMO_MODE=true — seeding demo partner specs and failure logs …")
        store.seed_demo_data()
    else:
        logger.info(
            "DEMO_MODE=false — using existing vector store at '%s'.",
            config.chroma_db_path,
        )

    orchestrator = OrchestratorAgent(vector_store=store)
    return orchestrator


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point for the FirstPass EDI demo."""
    parser = argparse.ArgumentParser(
        description="FirstPass EDI — multi-agent X12 856 validator demo"
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read a raw 856 from stdin instead of using the built-in samples.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print results as JSON instead of formatted text.",
    )
    args = parser.parse_args()

    _banner()

    mode_str = "DEMO" if config.demo_mode else f"PRODUCTION ({config.openai_model})"
    print(f"Mode: {mode_str}\n")

    # ── Initialise pipeline ───────────────────────────────────────────────────
    orchestrator = _seed_and_init()

    # ── Stdin mode ────────────────────────────────────────────────────────────
    if args.stdin:
        raw_edi = sys.stdin.read()
        if not raw_edi.strip():
            print("ERROR: No EDI content received on stdin.", file=sys.stderr)
            sys.exit(1)
        result = orchestrator.process(raw_edi)
        if args.json_output:
            print(json.dumps(_result_to_dict(result), indent=2))
        else:
            _print_result(result, "stdin document")
        return

    # ── Demo run 1: Valid document ────────────────────────────────────────────
    print("=" * 60)
    print(" RUN 1 of 2 — Valid 856 (expect: SUBMITTED)")
    print("=" * 60)
    result_1 = orchestrator.process(SAMPLE_856_RETAILER_A, document_id="DEMO-RUN-01")
    _print_result(result_1, "Run 1 — Valid 856 (RetailerA)")

    # ── Demo run 2: Document with intentional errors ───────────────────────────
    print("=" * 60)
    print(" RUN 2 of 2 — 856 with errors (expect: ESCALATED or fixed)")
    print("=" * 60)
    result_2 = orchestrator.process(
        SAMPLE_856_RETAILER_A_WITH_ERRORS, document_id="DEMO-RUN-02"
    )
    _print_result(result_2, "Run 2 — 856 with errors (RetailerA)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(" DEMO COMPLETE")
    print("=" * 60)
    for label, res in [("Run 1 (valid)", result_1), ("Run 2 (errors)", result_2)]:
        icon = {"SUBMITTED": "✅", "ESCALATED": "⚠️ ", "ERROR": "💥"}.get(res.status, "?")
        print(
            f"  {icon}  {label:20s} → {res.status:10s}  "
            f"(cycles={res.revalidation_cycles}, {res.duration_seconds:.2f}s)"
        )
    print()


def _result_to_dict(result: WorkflowResult) -> dict:
    """Serialise a WorkflowResult to a JSON-safe dict."""
    def _err(e):
        return {"segment": e.segment_type, "element": e.element_id,
                "severity": e.severity, "code": e.code, "description": e.description}

    def _hyp(h):
        return {"description": h.description, "score": h.score,
                "fix": h.fix_recommendation, "segments": h.affected_segments}

    return {
        "document_id": result.document_id,
        "partner_name": result.partner_name,
        "status": result.status,
        "revalidation_cycles": result.revalidation_cycles,
        "duration_seconds": result.duration_seconds,
        "escalation_reason": result.escalation_reason,
        "context_warnings": result.context_warnings,
        "validation": {
            "passed": result.validation_result.passed if result.validation_result else None,
            "confidence": result.validation_result.confidence if result.validation_result else None,
            "errors": [_err(e) for e in result.validation_result.errors] if result.validation_result else [],
            "summary": result.validation_result.summary if result.validation_result else "",
        } if result.validation_result else None,
        "diagnostic": {
            "confidence": result.diagnostic_result.confidence if result.diagnostic_result else None,
            "should_escalate": result.diagnostic_result.should_escalate if result.diagnostic_result else None,
            "diagnoses": [_hyp(h) for h in result.diagnostic_result.top_diagnoses] if result.diagnostic_result else [],
            "summary": result.diagnostic_result.summary if result.diagnostic_result else "",
            "beam_depth": len(result.diagnostic_result.beam_iterations) if result.diagnostic_result else 0,
        } if result.diagnostic_result else None,
        "submission": {
            "transaction_id": result.submission_result.transaction_id,
            "ack_status": result.submission_result.ack_status,
            "message": result.submission_result.message,
            "timestamp": result.submission_result.timestamp,
        } if result.submission_result else None,
        "events": result.events,
    }


if __name__ == "__main__":
    main()
