# Architecture Documentation

## System Overview

FirstPass EDI is a multi-agent system for validating X12 856 Advance Ship Notice documents against retail trading partner compliance specifications. The system catches formatting errors, missing segments, and partner-specific violations before documents are submitted to the EDI network.

## Agent Communication Flow

```
                    ┌─────────────────┐
                    │   Inbound 856   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Schema Guard   │──── Reject (structural failure)
                    └────────┬────────┘
                             │ (pass)
                    ┌────────▼────────┐
                    │  Orchestrator   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Retrieval Agent │
                    │  (top 5 docs)   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
              ┌─────│Validation Agent │─────┐
              │     │  (ReAct loop)   │     │
              │     └─────────────────┘     │
              │                             │
         PASS │                        FAIL │
              │                             │
    ┌─────────▼──────┐          ┌───────────▼─────────┐
    │Confidence Gate  │          │  Diagnostic Agent   │
    │  (>= 0.85?)    │          │  (ToT beam search)  │
    └────────┬───────┘          └───────────┬─────────┘
             │                              │
        ┌────┴────┐                 ┌───────┴───────┐
        │         │                 │               │
     Submit    Escalate         Fix Found      Low Confidence
   (Orderful)  (Human)            │               │
                              Revalidate       Escalate
                             (max 2 cycles)    (Human)
```

## Data Flow Between Agents

All inter-agent communication uses structured dataclasses passed through the orchestrator. Agents do not call each other directly.

**Retrieval Agent Output (ContextPackage):**
- partner_name: str
- spec_version: str
- spec_date: datetime
- is_stale: bool
- ranked_chunks: list of up to 5 document chunks

**Validation Agent Output (ValidationResult):**
- document_id: str
- partner: str
- status: "pass" | "fail"
- errors: list of ValidationError (segment, severity, description)
- confidence: float (0.0 to 1.0)
- segments_checked: int

**Diagnostic Agent Output (DiagnosticResult):**
- hypotheses: list of Hypothesis (cause, evidence, confidence, recommended_fix)
- selected_hypothesis: Hypothesis or None
- should_escalate: bool
- escalation_reason: str or None

## Reasoning Strategies by Agent

| Agent | Strategy | Why |
|---|---|---|
| Orchestrator | Sequential routing | State management, not reasoning |
| Retrieval | Semantic search + ranking | Information retrieval, not reasoning |
| Validation | ReAct (reason-act-observe) | Sequential segment checking is deterministic |
| Diagnostic | ToT beam search (width 2, depth 3) | Root cause diagnosis needs parallel hypothesis exploration |

## Guardrail Layers

1. **Input layer** — Schema validation rejects structurally invalid X12
2. **Process layer** — Staleness checks, scoped tool access, revalidation caps
3. **Output layer** — Confidence gating before submission
4. **Audit layer** — Append-only logging of all decisions

## Vector Store Design

Three document categories, each with event-level chunking:

1. **Partner routing guide specs** — one chunk per spec section (segment requirements, qualifier codes, label rules)
2. **Validation history** — one chunk per validation event (partner, segments, errors, resolution)
3. **Resolution logs** — one chunk per resolved failure (error context + correction applied)

Retrieval returns top 5 results ranked by recency and relevance. Superseded spec versions are deprioritized but retained for historical reference.
