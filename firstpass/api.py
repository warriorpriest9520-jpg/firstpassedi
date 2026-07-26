"""
api.py — FastAPI control layer for FirstPass EDI.

Provides REST endpoints for:
  - Orchestrator control (trigger cycle, halt/resume)
  - EDI incidents and partner status
  - Escalation management
  - Corporate memory / shared context
  - Dashboard data feeds
  - File management (operator use)

Authentication: X-API-Key header (set FIRSTPASS_API_KEY in .env).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from .config import config

log = logging.getLogger("firstpass.api")

# ── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="FirstPass EDI API",
    version="1.0.0",
    description="AI-powered EDI operations platform — control and monitoring API",
)

_allowed_origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:3000",   # dev UI
]
if os.getenv("FIRSTPASS_ORIGIN"):
    _allowed_origins.append(os.getenv("FIRSTPASS_ORIGIN"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_key(x_api_key: Optional[str] = Header(None)):
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Request models ────────────────────────────────────────────────────────────

class HaltRequest(BaseModel):
    reason: str = "manual"
    halted_by: str = "api"

class ContextRequest(BaseModel):
    key: str
    value: object
    ttl_hours: int = 24

class FileWriteRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Liveness probe — returns 200 when the API is up."""
    try:
        from .memory.supabase_client import get_client
        sb_ok = get_client() is not None
    except Exception:
        sb_ok = False
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "supabase": sb_ok,
        "version": "1.0.0",
    }


# ── Orchestrator control ──────────────────────────────────────────────────────

@app.post("/cycle", dependencies=[Depends(verify_key)])
async def trigger_cycle(background_tasks: BackgroundTasks):
    """Trigger one orchestration cycle in the background."""
    background_tasks.add_task(_run_cycle)
    return {"status": "started", "message": "Orchestration cycle running in background"}

async def _run_cycle():
    from .orchestrator import Orchestrator
    try:
        orch = Orchestrator()
        result = orch.run_cycle()
        log.info(f"Background cycle complete: {result}")
    except Exception as exc:
        log.error(f"Background cycle failed: {exc}", exc_info=True)

@app.get("/status")
def platform_status():
    """Overall platform status — bot health, halt state, cycle info."""
    result: dict = {"timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        from .memory.supabase_client import get_client
        sb = get_client()
        if sb:
            bots = sb.table("bot_health").select("*").execute()
            result["bots"] = {r["bot_name"]: r for r in (bots.data or [])}
    except Exception as exc:
        result["bots"] = {"error": str(exc)}
    try:
        from .safety.halt_switch import status as halt_status
        result["halt"] = halt_status()
    except Exception as exc:
        result["halt"] = {"error": str(exc)}
    return result


# ── Halt / Resume ─────────────────────────────────────────────────────────────

@app.post("/halt", dependencies=[Depends(verify_key)])
def trigger_halt(req: HaltRequest):
    """Activate HALT-ALL — all agents pause immediately."""
    from .safety.halt_switch import halt
    return halt(reason=req.reason, halted_by=req.halted_by)

@app.post("/resume", dependencies=[Depends(verify_key)])
def trigger_resume(req: HaltRequest):
    """Lift HALT-ALL — resume normal operations."""
    from .safety.halt_switch import resume
    return resume(reason=req.reason, resumed_by=req.halted_by)

@app.get("/halt/status")
def halt_status_endpoint():
    """Current halt state (no auth required for read)."""
    from .safety.halt_switch import status
    return status()


# ── EDI ───────────────────────────────────────────────────────────────────────

@app.post("/edi/check", dependencies=[Depends(verify_key)])
async def trigger_edi_check(background_tasks: BackgroundTasks):
    """Trigger an EDI polling cycle (850 ingestion + outbound generation)."""
    background_tasks.add_task(_run_edi_check)
    return {"status": "started", "message": "EDI check running in background"}

async def _run_edi_check():
    from .agents.edi_agent import EDIAgent
    try:
        result = EDIAgent().run_cycle()
        log.info(f"EDI cycle: {result}")
    except Exception as exc:
        log.error(f"EDI check failed: {exc}", exc_info=True)

@app.get("/edi/incidents")
def get_edi_incidents(resolved: bool = False, limit: int = 20):
    """Fetch open (or resolved) EDI incidents."""
    try:
        from .memory.supabase_client import get_client
        sb = get_client()
        if sb:
            result = (
                sb.table("edi_incidents")
                  .select("*")
                  .eq("resolved", resolved)
                  .order("created_at", desc=True)
                  .limit(limit)
                  .execute()
            )
            return {"incidents": result.data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"incidents": []}

@app.get("/edi/partners")
def get_edi_partners():
    """List all registered trading partners and their status."""
    try:
        from .dashboard.partner_health import get_partner_summary
        return {"partners": get_partner_summary()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Escalations ───────────────────────────────────────────────────────────────

@app.get("/escalations")
def get_escalations(resolved: bool = False, limit: int = 50):
    """Fetch open escalations requiring human review."""
    try:
        from .memory.supabase_client import get_client
        sb = get_client()
        if sb:
            result = (
                sb.table("escalations")
                  .select("*")
                  .eq("resolved", resolved)
                  .order("created_at", desc=True)
                  .limit(limit)
                  .execute()
            )
            return {"escalations": result.data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"escalations": []}

@app.post("/escalations/{escalation_id}/resolve", dependencies=[Depends(verify_key)])
def resolve_escalation(escalation_id: str, note: str = ""):
    """Mark an escalation as resolved."""
    try:
        from .memory.supabase_client import get_client
        sb = get_client()
        if sb:
            sb.table("escalations").update({
                "resolved": True,
                "resolution_note": note,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", escalation_id).execute()
            return {"ok": True, "id": escalation_id}
        raise HTTPException(status_code=503, detail="Database not configured")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Shared context ────────────────────────────────────────────────────────────

@app.get("/context/{key}", dependencies=[Depends(verify_key)])
def get_context(key: str):
    from .memory.supabase_client import get_shared_context
    return {"key": key, "value": get_shared_context(key)}

@app.post("/context", dependencies=[Depends(verify_key)])
def set_context(req: ContextRequest):
    from .memory.supabase_client import set_shared_context
    ok = set_shared_context(req.key, req.value, "api", req.ttl_hours)
    return {"ok": ok}


# ── Issues (unified feed) ─────────────────────────────────────────────────────

@app.get("/api/issues", dependencies=[Depends(verify_key)])
def get_issues(status: str = "open", limit: int = 50):
    """Unified issues feed: escalations + EDI incidents sorted by recency."""
    issues = []
    try:
        from .memory.supabase_client import get_client
        sb = get_client()
        if not sb:
            return {"issues": [], "total": 0}
        is_resolved = status == "resolved"
        for table, issue_type in [("escalations", "escalation"), ("edi_incidents", "edi_incident")]:
            try:
                rows = (
                    sb.table(table)
                      .select("*")
                      .eq("resolved", is_resolved)
                      .order("created_at", desc=True)
                      .limit(limit)
                      .execute()
                )
                for row in (rows.data or []):
                    issues.append({
                        "type": issue_type,
                        "id": row.get("id"),
                        "summary": row.get("summary") or row.get("description", ""),
                        "severity": row.get("severity", "medium"),
                        "partner": row.get("partner"),
                        "created_at": row.get("created_at"),
                        "resolved": row.get("resolved"),
                    })
            except Exception:
                pass
    except Exception as exc:
        return {"issues": [], "error": str(exc)}
    issues.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"issues": issues[:limit], "total": len(issues)}


# ── Audit summary ─────────────────────────────────────────────────────────────

@app.get("/api/audit/summary", dependencies=[Depends(verify_key)])
def audit_summary():
    """High-level audit overview: open issues + partner status."""
    result: dict = {
        "escalations": 0,
        "incidents": 0,
        "edi": {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from .memory.supabase_client import get_client
        sb = get_client()
        if sb:
            esc = sb.table("escalations").select("id", count="exact").eq("resolved", False).execute()
            result["escalations"] = len(esc.data or [])
            inc = sb.table("edi_incidents").select("id", count="exact").eq("resolved", False).execute()
            result["incidents"] = len(inc.data or [])
            try:
                partners = sb.table("edi_partners").select("name,status,last_transmission").execute()
                result["edi"]["partners"] = partners.data or []
                result["edi"]["active"] = sum(
                    1 for p in (partners.data or []) if p.get("status") == "active"
                )
            except Exception:
                result["edi"]["partners"] = []
    except Exception as exc:
        result["error"] = str(exc)
    return result


# ── File management (operator) ────────────────────────────────────────────────

import pathlib

_ROOT = pathlib.Path(__file__).parent.parent

def _safe_path(rel: str) -> pathlib.Path:
    full = (_ROOT / rel).resolve()
    if not str(full).startswith(str(_ROOT.resolve())):
        raise HTTPException(status_code=403, detail="Path outside project root")
    return full

@app.get("/files", dependencies=[Depends(verify_key)])
def read_file(path: str):
    full = _safe_path(path)
    if not full.is_file():
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    return {"path": path, "content": full.read_text("utf-8")}

@app.post("/files", dependencies=[Depends(verify_key)])
def write_file(req: FileWriteRequest):
    full = _safe_path(req.path)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(req.content, encoding=req.encoding)
    return {"ok": True, "path": req.path, "bytes_written": len(req.content)}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not config.API_KEY:
        log.warning("FIRSTPASS_API_KEY not set — API is unauthenticated!")
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
