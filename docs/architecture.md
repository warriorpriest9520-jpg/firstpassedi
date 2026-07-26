# FirstPass EDI — System Architecture

## Overview

FirstPass EDI is an autonomous agent platform for EDI (Electronic Data Interchange) operations in manufacturing and distribution.  It replaces the traditional model of human-in-the-loop EDI processing with a coordinated team of AI agents operating within well-defined safety constraints.

---

## Design Principles

### 1. Autonomous Department Model
Each agent has a defined domain, authority limits, and reward signal.  Agents self-heal within their authority and escalate when they hit a hard constraint.  This mirrors the "Autonomous Department" specification pattern from the AI engineering literature.

### 2. Safety Before Completion
Every action passes through guardrails before execution.  Hard constraints (high-value orders, payment detail changes, shipped-order modifications) require human approval and cannot be overridden by any agent.

### 3. Dry-Run by Default
All connectors operate in simulation mode when API keys are absent.  The full pipeline — parsing, validation, document generation, audit, and health monitoring — runs without external services.

### 4. Belt-and-Suspenders State
Critical state (halt switch, shared context) persists both locally (JSON file) and in Supabase, so a single failure cannot cause a split-brain condition.

---

## Component Deep-Dive

### Orchestrator (`firstpass/orchestrator.py`)

The top-level coordinator runs a configurable event loop:

```
run_cycle()
  ├── is_halted() check  ← bail immediately if HALT-ALL active
  ├── edi_agent.run_cycle()     ← poll platforms, process 850s
  ├── inbox_agent.run_cycle()   ← triage email inbox
  ├── audit_agent.run_cycle()   ← compliance checks
  └── watchdog_agent.run_cycle() ← health scoring
```

Each agent failure is isolated — one agent crashing doesn't stop the others.

### EDI Agent (`firstpass/agents/edi_agent.py`)

Pipeline per inbound 850:
1. `parse_850(x12)` → `Order` object
2. `guardrails.validate_order(order)` → violations list
3. `generate_997(order)` → functional acknowledgment X12
4. `generate_855(order)` → purchase order acknowledgment X12
5. `connector.submit(...)` → platform-specific submission
6. `erp.create_order(order)` → ERP sales order
7. Message bus event → downstream agents

### Connector Layer (`firstpass/connectors/`)

All connectors follow the same interface:
```python
client.submit(x12_string, trading_partner, doc_type) -> {"transaction_id": str, "ok": bool}
client.fetch_inbound(doc_type) -> [{"id": str, "x12": str}]
```

In dry-run mode (no API key), `submit()` returns `{"transaction_id": "SIM-xxx", "dry_run": True}`.

### Message Bus (`firstpass/utils/message_bus.py`)

File-backed pub/sub (JSONL) with:
- **Pattern matching** — subscribers use glob patterns (e.g. `edi_*`)
- **Message TTL** — 24h expiry prevents stale message buildup
- **Dead-letter queue** — undeliverable messages are preserved for inspection

### Safety Architecture

```
┌─────────────────────────────────────────┐
│             HALT-ALL Switch             │ ← .halt_state.json + Supabase
└─────────────────────────────────────────┘
                    │
          ┌─────────┴─────────┐
          │   Guardrails      │ ← order validation + authority checks
          └─────────┬─────────┘
                    │
          ┌─────────┴─────────┐
          │  Escalation Mgr   │ ← severity-based routing + quiet hours
          └─────────────────--┘
```

---

## Data Flow

### Inbound 850 → Outbound 855

```
Orderful / LogicBroker
       │  (850 Purchase Order)
       ▼
  EDI Agent
       │  parse_850() → Order
       │  validate_order() → []  (no violations)
       │  generate_997() → X12
       │  generate_855() → X12
       │  connector.submit(997)
       │  connector.submit(855)
       │  erp.create_order()
       ▼
  Message Bus: "order_status_received"
       │
       ├──► Audit Agent: checks SLA + payload compliance
       └──► Watchdog Agent: updates order flow health
```

### Shipment → 856 ASN + 810 Invoice

```
ShipStation
    │  (shipment confirmed)
    ▼
EDI Agent (next cycle)
    │  shipstation.get_shipment(po_number)
    │  generate_856() → ASN X12
    │  generate_810() → Invoice X12
    │  connector.submit(856)
    │  connector.submit(810)
    ▼
Message Bus: "order_status_shipped"
```

---

## Scaling Considerations

For production deployment beyond a single server:
- **Message Bus** → replace file-backed JSONL with Redis Streams or a message broker
- **State** → all Supabase-backed state already scales horizontally
- **Connectors** → stateless; can run in parallel workers
- **Orchestrator** → add leader election (e.g. via Supabase advisory locks) for multi-instance

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| File-backed message bus | Zero infrastructure dependency for demo; swap to Redis for production |
| Supabase (PostgreSQL) | pgvector support for RAG; real-time subscriptions; free tier sufficient for demo |
| X12 generator stubs | Full X12 library integration is connector-specific; stubs make the architecture testable |
| Dry-run-first connectors | Every connector works without credentials — reduces onboarding friction |
| HALT-ALL dual storage | Local file ensures zero-network halt; Supabase ensures cross-machine propagation |
