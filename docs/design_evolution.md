# Design Evolution Across the Program

## Module 1 — Checkpoint 1.2: Agent Design

Started with a single-agent ReAct loop for 856 validation. The agent receives a draft 856, reasons about which partner rules apply, retrieves the partner's routing guide from memory, and checks each segment sequentially. Errors are classified by severity. Short-term memory holds the current validation state. Long-term memory stores partner compliance history and past failures.

Key insight: 856 validation is not a single-step task. Each partner has different rules, and the check sequence depends on what the agent finds along the way. A static prompt cannot adapt mid-document.

## Module 3 — Checkpoint 3.1: Retrieval Design

Added a semantic retrieval layer with pgvector. Three document categories: partner routing guide specs, validation history, and resolution logs. Event-level chunking keeps each chunk as one coherent unit (one spec section, one validation result, one resolution record).

Key insight: A partner might have an undocumented requirement that is not in their official routing guide but has triggered chargebacks three times. Without retrieval from transaction history, the agent has no way to know that. This was the single biggest improvement to validation accuracy.

Failure mode addressed: stale specs. Versioning and recency-weighted ranking prevent outdated rules from creating false positives.

## Module 4 — Checkpoint 4.1: Structured Reasoning

Added Tree-of-Thought beam search for the diagnostic phase. Validation stayed ReAct because segment checking is deterministic. But root cause diagnosis has multiple plausible explanations for any given failure, and linear chain of thought commits to the first one.

Design: 3 hypotheses generated, beam width 2 after evidence pruning, depth limit 3, confidence threshold 0.8 for autonomous action. Total compute budget: 3 LLM calls + 3 tool calls, under 5 seconds.

Key insight: ToT is not appropriate for every phase. Using it for validation would add latency without improving accuracy. Using it for diagnosis prevents premature commitment to wrong root causes.

## Module 5 — Checkpoint 5.1: Multi-Agent Architecture

Split the single agent into four: Orchestrator, Retrieval Agent, Validation Agent, Diagnostic Agent. Each owns a distinct reasoning pattern. Communication through structured messages via the orchestrator.

Design: Sequential pipeline with a conditional diagnostic branch. Two-cycle revalidation cap. A fifth submission agent was considered and rejected because submission is a single API call.

Key insight: Separating agents means each operates with only the context it needs. The retrieval agent handles chunking and relevance without polluting the validation loop. The diagnostic agent only activates on failures, keeping the happy path fast.

## Module 6 — Checkpoint 6.1: Safety and Intervention

Added six guardrails: schema validation on input, spec staleness detection, scoped tool access, revalidation caps, confidence gating on output, and audit logging. Defined escalation criteria for five specific situations where the system should defer to a human.

Key insight: The hardest design problem is not making the agent smarter. It is deciding where autonomy ends and human oversight begins. Every guardrail trades throughput for safety, and the current thresholds bias toward caution because the cost of a missed error exceeds the cost of an unnecessary escalation.

## Summary of Evolution

| Module | What Changed | Why |
|---|---|---|
| 1 | ReAct reasoning loop | Validation is sequential, adaptive |
| 3 | RAG retrieval layer | Partner rules cannot live in a prompt |
| 4 | ToT beam search | Diagnosis needs parallel exploration |
| 5 | 4-agent architecture | Separate reasoning strategies, independent testing |
| 6 | Guardrails + escalation | Bound autonomy, detect drift, catch edge cases |

The most important lesson across the program: start with one thing that works, then separate concerns as complexity grows. Every module revealed a reason to isolate a specific capability into its own component.
