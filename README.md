# FirstPass EDI

> **AI-powered EDI operations platform** — autonomous order processing, partner compliance, and real-time incident management for manufacturing and distribution companies.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-green.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What Is FirstPass EDI?

Traditional EDI (Electronic Data Interchange) operations require constant human oversight — manually checking for new purchase orders, generating acknowledgments, validating documents against partner specs, and chasing down compliance issues.

**FirstPass EDI** eliminates that overhead.  It's an autonomous agent platform that:

- 📥 **Polls** trading platforms (Orderful, LogicBroker) for inbound 850 Purchase Orders
- 🤖 **Parses and validates** X12 documents against partner-specific EDI specs
- 📤 **Generates and submits** 855 acknowledgments, 856 ASNs, and 810 invoices automatically
- 🔍 **Audits** each partner for SLA compliance and document quality
- 🏥 **Monitors** system health with self-healing capabilities
- 📬 **Triages** email inbox with AI-generated draft replies
- 🚨 **Escalates** critical issues via Discord/Slack with full context
- 🛑 **Provides** a HALT-ALL kill switch for instant agent suspension

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI REST Layer                        │
│  /health  /cycle  /halt  /edi/*  /escalations  /api/dashboard/  │
└────────────────────────┬────────────────────────────────────────┘
                         │
                ┌────────▼────────┐
                │   Orchestrator  │  ← drives all agents each cycle
                └────────┬────────┘
           ┌─────────────┼──────────────┬──────────────┐
           ▼             ▼              ▼              ▼
    ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌──────────┐
    │   EDI    │  │  Inbox   │  │   Audit    │  │ Watchdog │
    │  Agent   │  │  Agent   │  │   Agent    │  │  Agent   │
    └────┬─────┘  └────┬─────┘  └─────┬──────┘  └────┬─────┘
         │              │               │               │
         └──────────────┴───────────────┴───────────────┘
                                │
                        ┌───────▼───────┐
                        │  Message Bus  │  (file-backed pub/sub)
                        └───────┬───────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  ┌───────────┐          ┌──────────────┐        ┌──────────┐
  │ Orderful  │          │  ShipStation │        │  Sage /  │
  │LogicBroker│          │  Connector   │        │  ERP API │
  └───────────┘          └──────────────┘        └──────────┘
```

### Agent Roles

| Agent | Responsibility |
|-------|---------------|
| **Orchestrator** | Coordinates all agents; enforces HALT-ALL |
| **EDIAgent** | Parse 850s → generate 855/856/810 → submit |
| **InboxAgent** | Email triage; draft AI replies |
| **AuditAgent** | Partner SLA compliance; payload validation |
| **WatchdogAgent** | Health scoring; anomaly detection; self-healing |

### Safety Layers

- **HALT-ALL kill switch** — suspend all agents instantly via API or message
- **Approval gates** — orders above a configured threshold require human review
- **Guardrails** — validate every order before processing; block authority violations
- **Escalation manager** — route critical findings to Discord/Slack with full context
- **Reward scoring** — continuous agent performance tracking (per Autonomous Dept spec)

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/your-org/firstpass-edi.git
cd firstpass-edi
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and set at minimum:
#   FIRSTPASS_API_KEY=your-secret-key
# All other settings are optional — the platform runs in dry-run mode without API keys.
```

### 3. Run the 5-minute demo

```bash
python demo/demo_flow.py
```

This exercises the full pipeline using the sample X12 files in `demo/` — no API keys required.

### 4. Start the API server

```bash
python -m firstpass.api
# API available at http://localhost:8080
# Docs at         http://localhost:8080/docs
```

### 5. Run the orchestrator

```bash
# Single cycle
python -m firstpass.orchestrator

# Continuous daemon (POLL_INTERVAL_SECONDS)
python -m firstpass.orchestrator --daemon

# Status check
python -m firstpass.orchestrator --status
```

---

## Demo Instructions

### 5-Minute Live Demo

```bash
# Terminal 1: Start the API
python -m firstpass.api

# Terminal 2: Run the demo flow
python demo/demo_flow.py
```

The demo shows:
1. Loading a realistic 850 Purchase Order (Big Box Retail → ACME Manufacturing)
2. Parsing 850 segments into a structured Order object
3. Running guardrail validation
4. Generating 997 and 855 response documents
5. Submitting via Orderful connector (dry-run simulation)
6. Compliance audit of the payload
7. Message bus event publishing
8. Pre-built 856 ASN and 810 invoice

### Viewing the Dashboard

```bash
# Start API server, then open:
curl http://localhost:8080/api/dashboard/partners
curl http://localhost:8080/api/edi/metrics?days=7
curl http://localhost:8080/halt/status
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI + uvicorn |
| AI/LLM | Anthropic Claude (claude-3-5-sonnet) |
| Database | Supabase (PostgreSQL + pgvector) |
| EDI Platform | Orderful v3 API, LogicBroker REST |
| Shipping | ShipStation REST API |
| Messaging | Internal pub/sub (file-backed JSONL) |
| Auth | API key (header-based) |
| Config | python-dotenv |

---

## Project Structure

```
firstpass-edi/
├── firstpass/
│   ├── orchestrator.py       Main coordinator
│   ├── api.py                FastAPI control layer
│   ├── config.py             Centralised config
│   ├── agents/
│   │   ├── inbox_agent.py    Email triage
│   │   ├── edi_agent.py      EDI processing pipeline
│   │   ├── audit_agent.py    Partner compliance
│   │   └── watchdog_agent.py Health monitoring
│   ├── connectors/
│   │   ├── orderful.py       Orderful v3 API
│   │   ├── logicbroker.py    LogicBroker REST
│   │   ├── shipstation.py    ShipStation API
│   │   └── erp.py            Generic ERP adapter
│   ├── dashboard/
│   │   ├── partner_health.py Partner health scoring
│   │   └── routes.py         Dashboard API routes
│   ├── memory/
│   │   ├── corporate_memory.py RAG knowledge store
│   │   ├── issue_tracker.py  Incident management
│   │   └── supabase_client.py Database layer
│   ├── safety/
│   │   ├── halt_switch.py    HALT-ALL kill switch
│   │   ├── escalation.py     Escalation manager
│   │   └── guardrails.py     Approval gates + reward scoring
│   └── utils/
│       ├── message_bus.py    Inter-agent pub/sub
│       └── retry.py          LLM + HTTP retry decorators
├── demo/
│   ├── sample_850.x12        Purchase order (Big Box Retail)
│   ├── sample_856.x12        ASN (ACME → Big Box)
│   ├── sample_810.x12        Invoice (ACME → Big Box)
│   └── demo_flow.py          Interactive demo script
├── tests/
├── docs/
│   ├── architecture.md
│   └── rubric_mapping.md
├── .env.example
├── requirements.txt
└── setup.py
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness check |
| GET | `/status` | Platform status |
| POST | `/cycle` | Trigger one orchestration cycle |
| POST | `/halt` | Activate HALT-ALL |
| POST | `/resume` | Lift HALT-ALL |
| GET | `/halt/status` | Current halt state |
| GET | `/edi/incidents` | Open EDI incidents |
| GET | `/edi/partners` | Partner list + health |
| GET | `/escalations` | Open escalations |
| GET | `/api/dashboard/partners` | Partner health scores |
| GET | `/api/edi/metrics` | EDI throughput metrics |
| GET | `/api/issues` | Unified issue feed |

Interactive API docs: http://localhost:8080/docs

---

## Supabase Schema (Optional)

When SUPABASE_URL and SUPABASE_KEY are configured, the platform uses these tables:

```sql
-- Trading partner registry
CREATE TABLE edi_partners (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    isa_qualifier   TEXT,
    platform        TEXT NOT NULL,   -- orderful | logicbroker | rest_api
    connector_config JSONB,
    status          TEXT DEFAULT 'pending',
    last_health_score INT,
    last_transmission TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- EDI incidents
CREATE TABLE edi_incidents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    summary     TEXT,
    detail      TEXT,
    severity    TEXT DEFAULT 'medium',
    partner     TEXT,
    resolved    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Escalations
CREATE TABLE escalations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    summary     TEXT,
    detail      TEXT,
    severity    TEXT DEFAULT 'medium',
    source      TEXT,
    resolved    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Bot health heartbeats
CREATE TABLE bot_health (
    bot_name        TEXT PRIMARY KEY,
    last_heartbeat  TIMESTAMPTZ,
    status          TEXT
);

-- Shared key-value context
CREATE TABLE bot_shared_context (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    source_bot  TEXT,
    expires_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

## License

MIT License — see [LICENSE](LICENSE).

---

*FirstPass EDI — built as a capstone project demonstrating autonomous AI agent architecture for industrial EDI operations.*
