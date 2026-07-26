"""
FirstPass EDI — AI-powered EDI operations platform.

Modules:
    orchestrator  — Main coordinator that drives all agents
    api           — FastAPI REST control layer
    config        — Centralised configuration from environment
    agents        — Inbox, EDI, audit, and watchdog agents
    connectors    — Orderful, LogicBroker, ShipStation, ERP adapters
    dashboard     — Partner health scoring and dashboard routes
    memory        — Corporate knowledge store, issue tracker, Supabase client
    safety        — Escalation manager, halt switch, guardrails
    utils         — Shared utilities (message bus, retry decorator)
"""

__version__ = "1.0.0"
__all__ = ["orchestrator", "api", "config"]
