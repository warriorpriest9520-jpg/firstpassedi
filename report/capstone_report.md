# FirstPass EDI: Autonomous 856 ASN Validation Agent

## 1. Project Title

FirstPass EDI: An Autonomous Agent for X12 856 Advance Ship Notice Validation

## 2. Problem and User

Suppliers shipping to major retailers are required to send 856 Advance Ship Notices before goods arrive. Each retailer enforces their own compliance rules on top of the X12 standard, covering required segments, qualifier codes, date formats, and label specifications. When an 856 is late, malformatted, or missing a required segment, the retailer issues a chargeback, typically $200 to $500 per incident. Most suppliers do not catch the error until the deduction appears on a remittance weeks later.

The intended user is an EDI operations manager at a small to mid-sized supplier, usually one or two people managing dozens of retail trading partners through an ERP system. They spend hours every day manually checking order status, fixing formatting errors, and resubmitting documents. The volume of partner-specific rules makes manual validation unreliable, and the financial penalties for mistakes are significant.

This problem matters because it sits at the intersection of high volume, high variability, and high cost. Each partner has different rules, those rules change without notice, and every mistake has a direct dollar cost attached to it.

## 3. System Goal and Scope

The system validates outbound 856 ASN documents against each trading partner's specific compliance rules, catches errors before submission, diagnoses the root cause of failures, and learns from past outcomes so the same mistake does not repeat.

Successful performance means a chargeback prevention rate of 98% or higher on submitted documents, a false positive rate below 5%, and an escalation rate between 10% and 15% of total documents processed. The system should handle new trading partners gracefully by escalating to a human when no validation history exists rather than guessing.

Boundaries: the agent validates and submits 856 documents only. It does not generate 856s from scratch, modify ERP data, or handle other X12 transaction types like 850 purchase orders or 810 invoices. It operates as a validation and quality gate, not a document generator.

## 4. Final System Architecture

The system uses four agents, each owning a distinct reasoning pattern.

The **Orchestrator** receives inbound 856 documents, identifies the trading partner, and routes the document through the pipeline. It manages workflow state, tracking whether a document is in validation, diagnosis, or awaiting resubmission. It decides when to escalate to the user. It does not validate or diagnose. Its job is sequencing and state management.

The **Retrieval Agent** queries the vector store for the target partner's routing guide spec, recent validation failures, and resolution logs. It returns a context package with the top five relevant documents ranked by recency and relevance. Isolating retrieval lets this agent handle chunking strategy, relevance scoring, and staleness detection without polluting the validation agent's reasoning loop.

The **Validation Agent** runs a ReAct loop. It receives the 856 document and the retrieval agent's context package, then checks each segment against the partner spec. It classifies errors by severity (critical, warning, info) and either passes the document for submission or routes failures to the diagnostic agent. This agent is deterministic and fast.

The **Diagnostic Agent** runs a Tree-of-Thought beam search. It activates only when the validation agent flags an ambiguous failure. It generates three root cause hypotheses, gathers evidence via tool calls, prunes to the two strongest, and returns a ranked diagnosis with a recommended fix. If confidence is below 0.7, it escalates to the orchestrator with the full hypothesis tree for human review.

The workflow is sequential with a conditional branch: Orchestrator → Retrieval → Validation → Submission (if pass) or Diagnostic (if fail) → back to Orchestrator. The orchestrator caps revalidation at two cycles to prevent infinite loops.

Agents communicate through structured messages passed by the orchestrator. The retrieval agent outputs a context package with partner name, spec version, and ranked document chunks. The validation agent outputs a structured result with pass/fail status, error list, and severity scores. The diagnostic agent returns hypotheses with confidence scores and evidence summaries.

Supporting infrastructure includes:
- A vector database (pgvector via Supabase) storing partner routing guides, validation history, and resolution logs
- ShipStation API integration for shipment data retrieval
- Orderful API integration for document submission
- An append-only audit table logging every decision

## 5. Design Evolution Across the Program

The system evolved through six checkpoints, each adding a distinct architectural layer.

**Module 1 (Checkpoint 1.2)** established the core problem and the ReAct reasoning loop. The agent received an 856, reasoned about which partner rules apply, checked segments sequentially, and classified errors. This was a single-agent design with short-term memory for the current validation pass and long-term memory for partner history.

**Module 3 (Checkpoint 3.1)** added the retrieval layer. The key insight was that partner-specific compliance rules cannot live in a prompt. Each retailer has different requirements, those requirements change, and the undocumented rules that actually cause chargebacks only exist in transaction history. The semantic retrieval layer with pgvector made it possible to validate against actual partner requirements instead of generic X12 standards. Event-level chunking (one chunk per routing guide section, one per validation result) was chosen over arbitrary text splits to keep retrieved context coherent.

**Module 4 (Checkpoint 4.1)** introduced Tree-of-Thought reasoning for the diagnostic phase. The validation loop stayed ReAct because segment checking is sequential and deterministic. But root cause diagnosis needed structured exploration. When a validation fails, the cause could be an ERP export issue, a partner spec change, incomplete shipment data, or an outdated template. Linear chain of thought picks the first plausible explanation and commits. ToT with beam search (width 2, depth 3) explores multiple hypotheses in parallel and selects the one with the strongest evidence.

**Module 5 (Checkpoint 5.1)** split the single agent into four. The motivation was that ReAct validation, RAG retrieval, ToT diagnosis, and workflow orchestration are architecturally distinct reasoning strategies. Running them all in one agent meant every phase competed for context window space. Separating them meant each agent operates with only the context it needs and can be tested independently. A fifth submission agent was considered and rejected because submission is a single API call, not a reasoning task.

**Module 6 (Checkpoint 6.1)** added the safety and intervention layer. Schema validation on input, confidence gating on output, spec staleness detection, scoped tool access, revalidation caps, and audit logging. The escalation criteria were designed so the agent acts autonomously where knowledge is strong and defers where it is thin.

The most important refinement across the program was learning to separate concerns. The early single-agent design tried to do everything in one reasoning loop. Each module revealed a reason to isolate a specific capability into its own component.

## 6. Implementation Overview

The system is built in Python 3.10+ using the following stack:

- **LangChain** for agent control flow, prompt templating, and structured output parsing. The ReAct loop in the validation agent and the beam search in the diagnostic agent are both implemented as LangChain chains.
- **OpenAI GPT-4** as the underlying language model for reasoning, hypothesis generation, and evidence interpretation.
- **ChromaDB** as the vector database for the demo implementation (pgvector/Supabase in production). Stores partner routing guide embeddings, validation history, and resolution logs.
- **OpenAI Embeddings** (text-embedding-3-small) for document vectorization.
- **Python dataclasses** for structured data passing between agents (context packages, validation results, diagnostic hypotheses).
- **Pytest** for evaluation test cases demonstrating system behavior across scenarios.

External API integrations (ShipStation, Orderful) are implemented as client classes with mock responses for the demo. In production, these would connect to live endpoints. The mock layer preserves the architectural pattern while keeping the demo self-contained and runnable without API credentials.

The orchestrator manages state through a simple in-memory state tracker for the demo. In production, state would persist to Supabase with an append-only audit log.

## 7. Evaluation and Results

The system is evaluated against six metrics:

**Chargeback prevention rate** (target: 98%) measures submitted documents that do not result in partner chargebacks within 30 days. In test scenarios, the system correctly identifies partner-specific violations that generic X12 validation misses, including undocumented requirements surfaced only through retrieval of historical failure patterns.

**False positive rate** (target: below 5%) measures valid documents incorrectly blocked. The confidence gating threshold of 0.85 balances thoroughness against unnecessary escalation.

**Escalation rate** (target: 10-15%) tracks documents routed to human review. Deviation in either direction signals miscalibration. Too low means the agent may be overconfident. Too high means guardrails are too aggressive.

**Retrieval relevance** measured by mean reciprocal rank of the top 5 returned documents. Event-level chunking and recency weighting ensure the most operationally relevant context reaches the validation agent.

**Diagnostic resolution rate** tracks failures the diagnostic agent resolves without human input. The ToT beam search with a 0.7 confidence threshold provides structured analysis even when escalating.

**End-to-end latency** per document. The validation loop targets completion in seconds, with the diagnostic phase adding up to five seconds when activated (three LLM calls plus up to three tool calls).

Five test scenarios demonstrate system behavior: a valid 856 passing all checks, a partner-specific DTM*063 violation caught by retrieval-informed validation, a structurally invalid document caught by the schema guardrail before the reasoning loop, an ambiguous failure triggering ToT diagnostic with hypothesis ranking, and a new partner with no history triggering escalation.

## 8. Safety and Reliability Considerations

Six guardrails constrain agent behavior:

1. **Schema validation** on every inbound 856 before the agent processes it. Structural failures are rejected before the reasoning loop begins.
2. **Spec staleness detection** flags documents for human review when the most recent routing guide is older than 90 days.
3. **Scoped tool access** gives the validation agent read-only permissions for retrieval and shipment lookups. Write access is limited to the final submission call, gated behind confidence thresholds.
4. **Revalidation caps** limit the diagnostic loop to two cycles, preventing infinite retries.
5. **Confidence gating** routes any document scoring below 0.85 to human review.
6. **Append-only audit logging** records every validation result, diagnostic hypothesis, and submission decision. No output is fire-and-forget.

Human intervention is required when a new partner has no validation history, when confidence on severity classification falls below 0.85, when the diagnostic agent cannot reach 0.7 confidence, when a spec version mismatch is detected, and when processing the first submission after a routing guide change.

The three layers form defense in depth. Guardrails prevent action on bad inputs or stale knowledge. Evaluation metrics detect drift over time. Human intervention catches novel situations where no historical pattern exists.

## 9. Limitations and Next Steps

**Current limitations:**

The system handles only 856 ASN documents. Extending to other transaction types (850, 810, 846) would require new validation logic but could reuse the same orchestrator, retrieval, and diagnostic architecture.

The demo uses mock API responses. Production deployment requires live integration with ShipStation and Orderful, including error handling for API downtime and rate limiting.

The confidence thresholds (0.85 for validation, 0.7 for diagnosis) are set based on domain reasoning, not empirical calibration. Production deployment would benefit from threshold tuning based on observed chargeback rates.

The vector store in the demo uses ChromaDB. Production would use pgvector in PostgreSQL for persistence, scalability, and integration with the existing Supabase infrastructure.

**Next steps:**

1. Extend validation to 810 invoices and 850 purchase orders using the same multi-agent pattern
2. Calibrate confidence thresholds empirically against 90 days of production validation data
3. Add a feedback loop where chargeback outcomes automatically update the validation history in the vector store
4. Implement real-time monitoring dashboards for escalation rate and chargeback trends
5. Deploy to production with live API integrations and persistent state management

## 10. Public GitHub Repository

Repository: https://github.com/[username]/firstpass-edi

The repository contains:
- **README.md** explaining the project, architecture, setup, and usage
- **agents/** with the four agent implementations (orchestrator, retrieval, validation, diagnostic)
- **tools/** with EDI parser, vector store interface, and API client modules
- **guardrails/** with schema validation, confidence gating, and staleness checking
- **evaluation/** with metrics tracking and five test scenarios
- **samples/** with sample 856 input files, validation output JSON, and a partner routing guide spec
- **report/** with this capstone report
- **docs/** with architecture documentation and design evolution notes
