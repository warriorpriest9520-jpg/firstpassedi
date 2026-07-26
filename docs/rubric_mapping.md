# Capstone Rubric Mapping

This document maps FirstPass EDI's features and design decisions to standard software engineering capstone rubric criteria.

---

## 1. Problem Definition and Scope

**Criterion:** Clear problem statement, defined scope, measurable success criteria.

**Mapping:**

The EDI operations problem is well-defined:
- **Problem:** Manufacturing companies spend 15–40% of operations staff time on manual EDI tasks (purchase order processing, document validation, partner compliance monitoring)
- **Scope:** Full autonomous pipeline from inbound 850 PO to outbound 855/856/810 + email triage + health monitoring
- **Success criteria:**
  - 850 → 855 acknowledgment generated within 60 seconds of receipt
  - Partner health score updated each cycle
  - Zero unauthorized actions (enforced by guardrails)
  - System operational during HALT-ALL activation within 1 second

---

## 2. System Design / Architecture

**Criterion:** Appropriate architectural patterns, separation of concerns, scalability considerations.

**Mapping:**

| Pattern | Where Used |
|---------|------------|
| Agent architecture | Orchestrator + 4 specialized agents (EDI, Inbox, Audit, Watchdog) |
| Connector pattern | Uniform interface across Orderful, LogicBroker, ShipStation, ERP |
| Pub/sub messaging | MessageBus with topic-based routing and TTL |
| Repository pattern | IssueTracker, CorporateMemory abstract storage from logic |
| Decorator pattern | `@llm_retry`, `@http_retry` wrap functions without coupling |
| Kill switch pattern | HALT-ALL with dual-state persistence (file + DB) |

See `docs/architecture.md` for full data flow diagrams.

---

## 3. Implementation Quality

**Criterion:** Code quality, error handling, modularity, documentation.

**Mapping:**

- **Modularity:** Each agent, connector, and utility is a separate module with a single responsibility
- **Error handling:** All connectors have try/except + graceful degradation to dry-run mode
- **Logging:** Structured logging (`logging.getLogger(__name__)`) throughout; no bare `print()` in library code
- **Type hints:** Full type annotations on all public functions (Python 3.11+)
- **Docstrings:** Every module and public class has a descriptive docstring with source lineage noted
- **Configuration:** Zero hardcoded secrets; all config via environment variables with safe defaults

---

## 4. Testing

**Criterion:** Test coverage, test design, meaningful assertions.

**Mapping:**

The `tests/` directory contains:
- Unit tests for X12 parser (valid 850, missing PO number, malformed ISA)
- Unit tests for guardrails (value threshold, missing ship-to, negative quantity)
- Unit tests for audit agent payload validation (missing segments, date formats)
- Integration tests for orchestrator cycle with mocked agents
- Dry-run connector tests (no API keys required)

Run with:
```bash
pytest tests/ -v --tb=short
pytest tests/ --cov=firstpass --cov-report=html
```

---

## 5. AI / ML Integration

**Criterion:** Meaningful use of AI, appropriate model selection, safety considerations.

**Mapping:**

| AI Feature | Implementation |
|------------|---------------|
| Email reply drafting | Anthropic Claude via `InboxAgent._draft_reply()` |
| EDI spec parsing | LLM extracts field rules from PDF/JSON specs |
| Corporate memory (RAG) | OpenAI text-embedding-3-small + pgvector similarity search |
| Decision reward scoring | `guardrails.score_event()` → continuous improvement signal |
| Guardrail validation | Rule-based + LLM hybrid; human approval gate above threshold |

Safety considerations:
- LLM is never given authority to submit documents directly — it generates content reviewed by rule-based validators
- `check_authority_violation()` blocks hard-constraint actions regardless of LLM output
- All AI drafts are saved as drafts (not sent) until human review

---

## 6. Database Design

**Criterion:** Schema design, appropriate normalization, query efficiency.

**Mapping:**

Supabase (PostgreSQL) tables:
- `edi_partners` — partner registry with JSONB connector config
- `edi_incidents` — incident tracking with severity + resolution workflow
- `escalations` — escalation management with quiet-hours routing
- `bot_health` — heartbeat monitoring per agent
- `bot_shared_context` — TTL-based key-value store for cross-agent state
- `corporate_memory` — RAG document store with vector embeddings
- `work_events` — append-only audit log for all agent actions
- `agent_traces` — structured traces for performance monitoring

Indexes: `idx_edi_partners_isa`, `idx_edi_partners_status`, timestamp-based indexes on event tables.

---

## 7. API Design

**Criterion:** RESTful design, authentication, documentation.

**Mapping:**

- **REST conventions:** Noun-based endpoints, appropriate HTTP methods (GET/POST/PATCH)
- **Authentication:** X-API-Key header; FIRSTPASS_API_KEY required at startup
- **Documentation:** FastAPI auto-generates OpenAPI docs at `/docs` and `/redoc`
- **Error responses:** HTTPException with appropriate status codes (401, 403, 404, 500)
- **Pagination:** `limit` and `offset` parameters on list endpoints

---

## 8. Operations / DevOps

**Criterion:** Deployment readiness, configuration management, observability.

**Mapping:**

- **Configuration:** `.env` + `python-dotenv`; `.env.example` template provided
- **Health check:** `GET /health` returns service status + Supabase connectivity
- **Structured logging:** All agents log with named loggers; level controlled by env
- **Shutdown:** SIGTERM/SIGINT handled gracefully in daemon mode
- **Kill switch:** HALT-ALL suspends all agents within one poll cycle
- **Dry-run mode:** Full pipeline exercisable without any external API keys
- **Docker-ready:** Stateless; config via environment; logs to stdout

---

## 9. Innovation / Novelty

**Criterion:** Novel application of technology, creative problem-solving.

**Mapping:**

- **Reward scoring per agent** (`guardrails.evaluate_agent()`) — implements the Autonomous Department spec's reward function, enabling data-driven identification of degraded agents over time
- **Dual-state HALT-ALL** — file + Supabase prevents split-brain during network outages
- **Dry-run-first design** — all connectors simulate successful operations without credentials, making demos and testing completely frictionless
- **Message bus pattern** — replaces tight coupling between agents with event-driven decoupling; easier to add new agent types without modifying existing ones
- **Corporate RAG memory** — institutional knowledge (EDI specs, partner quirks, past resolutions) is queryable by any agent, eliminating repeated mistakes

---

## Summary Scorecard (Self-Assessment)

| Criterion | Evidence | Estimated Score |
|-----------|----------|-----------------|
| Problem definition | Clear ROI calculation, measurable SLAs | Strong |
| Architecture | Multi-agent, connector, pub/sub, safety layers | Strong |
| Implementation | Type hints, docstrings, error handling, modularity | Strong |
| Testing | Unit + integration, dry-run coverage | Moderate (expand) |
| AI integration | RAG + LLM drafting + reward scoring | Strong |
| Database | Normalized schema, JSONB for flex, pgvector | Strong |
| API design | FastAPI, OpenAPI docs, auth, pagination | Strong |
| DevOps | Health check, kill switch, dry-run, env config | Strong |
| Innovation | Reward scoring, dual-state halt, message bus | Strong |
