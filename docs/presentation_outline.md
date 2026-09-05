# FirstPass EDI — Final Presentation Outline
*10-minute delivery to a technical audience*

---

## Slide 1: Opening and Project Overview (1 min)

**Title:** FirstPass EDI: Autonomous 856 ASN Validation

- Problem: Suppliers send 856 Advance Ship Notices to retailers. Each retailer enforces their own compliance rules. Malformatted documents trigger chargebacks ($200-$500 each). Most errors are caught weeks later on remittance.
- User: EDI operations managers at small/mid-size suppliers managing dozens of retail partners
- Goal: Validate outbound 856s against partner-specific rules, catch errors before submission, diagnose failures, learn from outcomes

---

## Slide 2: Why This Problem Matters (1 min)

- High volume + high variability + high cost = systematic risk
- Each partner has different rules, rules change without notice, undocumented requirements exist
- Manual validation does not scale: one person managing 20+ partners cannot catch everything
- Financial impact compounds: 10 chargebacks/week at $350 average = $182K/year in preventable losses
- Agent-based approach fits because the work requires live data access, partner-specific reasoning, and learning from history

---

## Slide 3: System Architecture (2 min)

**Visual: 4-agent pipeline diagram**

```
Inbound 856 → Orchestrator → Retrieval Agent → Validation Agent → Submit (pass)
                                                      ↓ (fail)
                                              Diagnostic Agent → Fix → Revalidate (max 2 cycles)
                                                      ↓ (low confidence)
                                              Escalate to Human
```

- **Orchestrator**: Routes documents, manages state, decides escalation
- **Retrieval Agent**: Queries vector store for partner specs + failure history (top 5, ranked by recency)
- **Validation Agent**: ReAct loop checking segments against spec, classifies errors by severity
- **Diagnostic Agent**: ToT beam search generating 3 hypotheses, pruning to 2, returning ranked root cause
- Communication via structured messages through orchestrator (loosely coupled, auditable)

---

## Slide 4: Key Design Decisions (2 min)

**Decision 1: ReAct for validation, ToT for diagnosis**
- Validation is sequential and deterministic (segment present or not). ReAct fits.
- Root cause diagnosis has multiple plausible explanations. Linear CoT commits too early. ToT explores in parallel.

**Decision 2: 4 agents, not 1**
- Each agent uses a different reasoning strategy. Running all in one agent means they compete for context window.
- Separation enables independent testing, debugging, and improvement.
- Rejected a 5th submission agent because submission is a single API call, not a reasoning task.

**Decision 3: Retrieval over prompt stuffing**
- Partner rules cannot live in a prompt. They change, they are partner-specific, and undocumented requirements only exist in transaction history.
- Event-level chunking (one routing guide section per chunk, one validation result per chunk) keeps context coherent.

**Decision 4: Confidence-gated autonomy**
- 0.85 threshold on validation, 0.7 on diagnosis
- System acts autonomously where knowledge is strong, escalates where it is thin
- Biased toward caution: cost of a missed error exceeds cost of an unnecessary escalation

---

## Slide 5: Evaluation and Results (2 min)

**Metrics:**
| Metric | Target | Purpose |
|---|---|---|
| Chargeback prevention rate | 98% | Primary success measure |
| False positive rate | <5% | Avoid blocking valid docs |
| Escalation rate | 10-15% | Calibration signal |
| Retrieval relevance (MRR) | High | Right context reaching agent |
| Diagnostic resolution rate | Track | ToT effectiveness |
| Latency per document | <10s | Operational feasibility |

**Demo scenarios:**
1. Valid 856 passes all checks (happy path)
2. Missing DTM*063 caught by retrieval-informed validation (partner-specific failure)
3. Structurally invalid 856 caught by schema guardrail before reasoning loop
4. Ambiguous failure triggers ToT diagnostic with ranked hypotheses
5. New partner with no history triggers escalation to human

**Key result:** Retrieval-informed validation catches partner-specific violations that generic X12 checking misses entirely (the DTM*063 example from the routing guide)

---

## Slide 6: Repository and Implementation (1 min)

**GitHub:** github.com/[username]/firstpass-edi

```
firstpass-edi/
├── agents/          # 4 agent implementations
├── tools/           # EDI parser, vector store, API clients
├── guardrails/      # Schema validation, confidence gate, staleness check
├── evaluation/      # Metrics + 5 test scenarios
├── samples/         # Input 856s, output JSON, routing guide spec
├── docs/            # Architecture docs
└── report/          # Capstone report
```

**Stack:** Python 3.10, LangChain, OpenAI GPT-4, ChromaDB (demo) / pgvector (production), dataclasses for structured inter-agent communication

**How to run:** Clone, install requirements, set OpenAI key, run test cases or process a sample 856

---

## Slide 7: Closing Reflection (1 min)

**What worked well:**
- Separating concerns made each component testable and debuggable independently
- Retrieval was the single biggest improvement: partner-specific rules are the whole problem
- Confidence-gated escalation provides a clean boundary between autonomy and human oversight

**What I would improve:**
- Calibrate thresholds empirically with production data instead of domain reasoning
- Extend to other transaction types (810, 850) using the same architecture
- Add a feedback loop where chargeback outcomes automatically update the vector store

**Main takeaway:**
The hardest design problem is not making the agent smarter. It is deciding where the agent should stop and ask for help. Every guardrail is a tradeoff between throughput and safety, and getting those boundaries right matters more than getting the reasoning loop perfect.

---

*Total: ~10 minutes*
