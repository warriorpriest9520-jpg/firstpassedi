"""
supabase_client.py — Supabase database layer for FirstPass EDI.

Provides a singleton Supabase client plus convenience helpers for common
operations: logging work events, managing shared context, and recording
agent traces.

When SUPABASE_URL / SUPABASE_KEY are not configured, all functions degrade
gracefully (return None / False) so the system operates in local/dry-run mode.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import config

log = logging.getLogger("firstpass.memory.supabase_client")

_client = None


def get_client():
    """Return the Supabase client singleton, or None if not configured."""
    global _client
    if _client is not None:
        return _client
    if not config.supabase_configured:
        return None
    try:
        from supabase import create_client
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        return _client
    except ImportError:
        log.warning("supabase-py not installed. Run: pip install supabase")
        return None
    except Exception as exc:
        log.error(f"Supabase client init failed: {exc}")
        return None


# ── Shared context ────────────────────────────────────────────────────────────

def get_shared_context(key: str) -> Any:
    """Retrieve a value from the bot_shared_context table."""
    sb = get_client()
    if not sb:
        return None
    try:
        result = (
            sb.table("bot_shared_context")
              .select("value,expires_at")
              .eq("key", key)
              .limit(1)
              .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        expires = row.get("expires_at")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt < datetime.now(timezone.utc):
                    return None  # expired
            except Exception:
                pass
        val = row.get("value")
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val
    except Exception as exc:
        log.debug(f"get_shared_context({key!r}) failed: {exc}")
        return None


def set_shared_context(key: str, value: Any, source: str = "api", ttl_hours: int = 24) -> bool:
    """Upsert a value into the bot_shared_context table."""
    sb = get_client()
    if not sb:
        return False
    try:
        from datetime import timedelta
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        sb.table("bot_shared_context").upsert({
            "key": key,
            "value": json.dumps(value) if not isinstance(value, str) else value,
            "source_bot": source,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires,
        }).execute()
        return True
    except Exception as exc:
        log.error(f"set_shared_context({key!r}) failed: {exc}")
        return False


# ── Work event logging ────────────────────────────────────────────────────────

def log_work_event(
    bot_name: str,
    event_type: str,
    summary: str,
    payload: Optional[dict] = None,
) -> bool:
    """Append a work event to the work_events table."""
    sb = get_client()
    if not sb:
        return False
    try:
        sb.table("work_events").insert({
            "bot_name": bot_name,
            "event_type": event_type,
            "summary": summary,
            "payload": payload or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return True
    except Exception as exc:
        log.debug(f"log_work_event failed: {exc}")
        return False


# ── Agent trace logging ────────────────────────────────────────────────────────

def log_agent_trace(
    task_id: str,
    user_goal: str,
    tools: list,
    task_type: str,
    final_answer_summary: str,
    latency_ms: int = 0,
    error: Optional[str] = None,
    verification: str = "n/a",
    source: str = "firstpass",
) -> bool:
    """Record a structured agent trace for evaluation and monitoring."""
    sb = get_client()
    if not sb:
        return False
    try:
        sb.table("agent_traces").insert({
            "task_id": task_id,
            "user_goal": user_goal,
            "tools": tools,
            "task_type": task_type,
            "final_answer_summary": final_answer_summary[:500],
            "latency_ms": latency_ms,
            "error": error,
            "verification": verification,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return True
    except Exception as exc:
        log.debug(f"log_agent_trace failed: {exc}")
        return False
