# FirstPass EDI

An autonomous agent system for validating X12 856 Advance Ship Notice (ASN) documents against retail trading partner compliance specifications before EDI network submission.

## Problem

Suppliers shipping to major retailers must send 856 ASN documents that comply with each partner's specific formatting rules. These rules vary by retailer, change without notice, and include undocumented requirements that only surface through chargeback history. When an 856 is malformatted or missing a required segment, the retailer issues a chargeback ($200–$500 per incident). Most errors are not caught until weeks later.

Manual validation does not scale when one operations manager handles 20+ trading partners with different specs.

## Architecture

The system uses four agents in a multi-agent pipeline:

```
Inbound 856 → Orchestrator → Retrieval Agent → Validation Agent → Submit (pass)
                                                      ↓ (fail)
                                              Diagnostic Agent → Fix → Revalidate (max 2 cycles)
                                                      ↓ (low confidence)
                                              Escalate to Human
```

**Orchestrator** — Routes documents through the pipeline, manages workflow state, decides when to escalate to a human operator.

**Retrieval Agent** — Queries a vector database for the target partner's routing guide spec, recent validation failures, and resolution logs. Returns the top 5 documents ranked by recency and relevance.

**Validation Agent** — Runs a ReAct reasoning loop. Checks each 856 segment against the partner spec, classifies errors by severity (critical/warning/info), and returns a structured validation result with confidence scores.

**Diagnostic Agent** — Activates on ambiguous validation failures. Uses Tree-of-Thought beam search to generate 3 root cause hypotheses, gather evidence, prune to 2, and return a ranked diagnosis. Escalates if confidence falls below 0.7.

### Guardrails

- Schema validation rejects structurally invalid X12 before the reasoning loop
- Confidence gating (0.85 threshold) routes low-confidence results to human review
- Spec staleness detection flags documents when the partner's routing guide is >90 days old
- Revalidation capped at 2 cycles to prevent infinite diagnostic loops
- Append-only audit logging on every decision

## Project Structure

```
firstpass-edi/
├── main.py                  # Entry point
├── config.py                # Settings and thresholds
├── agents/
│   ├── orchestrator.py      # Workflow orchestrator
│   ├── retrieval_agent.py   # RAG retrieval agent
│   ├── validation_agent.py  # ReAct validation loop
│   └── diagnostic_agent.py  # ToT diagnostic agent
├── tools/
│   ├── edi_parser.py        # X12 856 parser
│   ├── vector_store.py      # Vector database interface
│   ├── shipstation_client.py# ShipStation API client (mock)
│   └── orderful_client.py   # Orderful API client (mock)
├── guardrails/
│   ├── schema_validator.py  # X12 structural validation
│   ├── confidence_gate.py   # Confidence threshold checks
│   └── staleness_check.py   # Spec freshness monitoring
├── evaluation/
│   ├── metrics.py           # Performance metrics
│   └── test_cases.py        # Demo test scenarios
├── samples/
│   ├── input/               # Sample 856 EDI files
│   ├── output/              # Sample validation results
│   └── specs/               # Sample routing guide specs
├── docs/
│   ├── architecture.md      # Architecture documentation
│   └── presentation_outline.md
└── report/
    └── capstone_report.md   # Full capstone report
```

## Setup

### Requirements

- Python 3.10+
- OpenAI API key

### Installation

```bash
git clone https://github.com/[username]/firstpass-edi.git
cd firstpass-edi
pip install -r requirements.txt
cp .env.example .env
# Add your OpenAI API key to .env
```

### Running

Process a sample 856 document:
```bash
python main.py samples/input/valid_856.edi
```

Run evaluation test cases:
```bash
python -m evaluation.test_cases
```

## Tech Stack

- **Python 3.10+** — core language
- **LangChain** — agent control flow and structured output
- **OpenAI GPT-4** — language model for reasoning
- **ChromaDB** — vector database (demo); pgvector/Supabase in production
- **OpenAI Embeddings** — document vectorization (text-embedding-3-small)

## Key Design Decisions

1. **ReAct for validation, ToT for diagnosis** — Validation is sequential (segment present or not). Diagnosis needs parallel hypothesis exploration.
2. **4 agents, not 1** — Each uses a different reasoning strategy. Separation enables independent testing.
3. **Retrieval over prompt stuffing** — Partner rules change and include undocumented requirements from transaction history.
4. **Confidence-gated autonomy** — Acts autonomously where knowledge is strong, escalates where it is thin.

## Evaluation Metrics

| Metric | Target |
|---|---|
| Chargeback prevention rate | ≥98% |
| False positive rate | <5% |
| Escalation rate | 10–15% |
| End-to-end latency | <10s per document |

## Capstone Context

This project was developed as the capstone for the Agentic AI Program: Building Autonomous Systems for Real-World Applications. The design evolved across six module checkpoints, progressively adding the ReAct reasoning loop, RAG retrieval layer, Tree-of-Thought diagnostics, multi-agent architecture, and safety guardrails.

## License

MIT
