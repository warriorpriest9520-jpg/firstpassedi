"""
halt_switch.py — HALT-ALL kill switch for the FirstPass EDI platform.

Any agent calls ``is_halted()`` before each work tick. If True, it skips
work and waits.  The operator (or the API) calls ``halt()`` / ``resume()``
to control the switch.

State persists in two places (belt + suspenders):
  1. Local JSON file (.halt_state.json in project root) — instant, no network
  2. Supabase bot_shared_context key "HALT_ALL" — visible across machines

API endpoints (registered in api.py):
  POST /halt          {"reason": "..."} — halt all agents
  POST /resume        {"reason": "..."} — resume all agents
  GET  /halt/status   — current state (no auth required)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import config

log = logging.getLogger("firstpass.safety.halt_switch")

_FLAG_FILE = config.HALT_FILE
_HALT_KEY = "HALT_ALL"


# ── Core state ────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        if _FLAG_FILE.exists():
            return json.loads(_FLAG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"halted": False, "reason": "", "halted_at": None, "halted_by": ""}


def _save(state: dict) -> None:
    try:
        tmp = _FLAG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(_FLAG_FILE)
    except Exception as exc:
        log.error(f"Cannot write halt state: {exc}")


def _sync_supabase(state: dict) -> None:
    """Mirror state to Supabase (best-effort)."""
    try:
        from ..memory.supabase_client import set_shared_context
        set_shared_context(_HALT_KEY, state, source="halt_switch", ttl_hours=168)
    except Exception:
        pass


def _notify(msg: str) -> None:
    """Post Discord alert (best-effort)."""
    import urllib.request
    webhook = config.DISCORD_WEBHOOK_URL
    if not webhook:
        return
    try:
        data = json.dumps({"content": msg}).encode()
        req = urllib.request.Request(
            webhook, data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.debug(f"Discord notify failed: {exc}")


# ── Public API ────────────────────────────────────────────────────────────────

def is_halted() -> bool:
    """Return True if HALT-ALL is currently active."""
    state = _load()
    if state.get("halted"):
        return True
    # Also check Supabase (cross-machine awareness)
    try:
        from ..memory.supabase_client import get_shared_context
        remote = get_shared_context(_HALT_KEY)
        if isinstance(remote, dict) and remote.get("halted"):
            # Sync to local so next check is fast
            _save(remote)
            return True
    except Exception:
        pass
    return False


def halt(reason: str = "manual", halted_by: str = "unknown") -> dict:
    """Activate HALT-ALL."""
    state = {
        "halted": True,
        "reason": reason,
        "halted_at": datetime.now(timezone.utc).isoformat(),
        "halted_by": halted_by,
    }
    _save(state)
    _sync_supabase(state)
    _notify(f"🛑 **HALT-ALL activated** by `{halted_by}` — reason: {reason}")
    log.warning(f"HALT-ALL activated: reason={reason!r} by={halted_by!r}")
    return {"ok": True, "halted": True, "state": state}


def resume(reason: str = "manual", resumed_by: str = "unknown") -> dict:
    """Lift HALT-ALL."""
    state = {
        "halted": False,
        "reason": f"Resumed: {reason}",
        "halted_at": None,
        "halted_by": resumed_by,
    }
    _save(state)
    _sync_supabase(state)
    _notify(f"✅ **HALT-ALL lifted** by `{resumed_by}` — reason: {reason}")
    log.info(f"HALT-ALL lifted: reason={reason!r} by={resumed_by!r}")
    return {"ok": True, "halted": False, "state": state}


def status() -> dict:
    """Return current halt state."""
    state = _load()
    return {
        "halted": bool(state.get("halted")),
        "reason": state.get("reason", ""),
        "halted_at": state.get("halted_at"),
        "halted_by": state.get("halted_by", ""),
    }
