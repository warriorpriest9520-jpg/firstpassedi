"""
routes.py — Dashboard API route registration for FirstPass EDI.

Registers all /dashboard/* and /api/edi/* endpoints onto a FastAPI app.
Intended to be imported and mounted from api.py.

Usage in api.py:
    from firstpass.dashboard.routes import register_dashboard_routes
    register_dashboard_routes(app)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends

from ..config import config

log = logging.getLogger("firstpass.dashboard.routes")


def register_dashboard_routes(app: FastAPI) -> None:
    """Register all dashboard routes on the given FastAPI app instance."""

    # Auth helper (local scope)
    def _verify(x_api_key: Optional[str] = Header(None)):
        if config.API_KEY and x_api_key != config.API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")

    # ── Partner dashboard ─────────────────────────────────────────────────

    @app.get("/api/dashboard/partners")
    def dashboard_partners(x_api_key: Optional[str] = Header(None)):
        """All trading partners with health scores."""
        _verify(x_api_key)
        from .partner_health import get_partner_summary
        return {"partners": get_partner_summary(), "timestamp": _now()}

    @app.get("/api/dashboard/partners/{partner_id}")
    def dashboard_partner_detail(partner_id: str, x_api_key: Optional[str] = Header(None)):
        """Single partner details + health."""
        _verify(x_api_key)
        from .partner_health import get_partner
        partner = get_partner(partner_id)
        if not partner:
            raise HTTPException(status_code=404, detail="Partner not found")
        return partner

    # ── EDI pipeline metrics ───────────────────────────────────────────────

    @app.get("/api/edi/metrics")
    def edi_metrics(days: int = 7, x_api_key: Optional[str] = Header(None)):
        """EDI throughput metrics: volume by doc type + error rates."""
        _verify(x_api_key)
        try:
            from ..memory.supabase_client import get_client
            sb = get_client()
            if not sb:
                return {"metrics": {}, "dry_run": True}
            from datetime import timedelta
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            result = (
                sb.table("edi_transactions")
                  .select("doc_type,status,created_at")
                  .gte("created_at", since)
                  .execute()
            )
            by_type: dict = {}
            for row in (result.data or []):
                dt = row.get("doc_type", "unknown")
                st = row.get("status", "unknown")
                by_type.setdefault(dt, {"total": 0, "success": 0, "error": 0})
                by_type[dt]["total"] += 1
                if st in ("submitted", "acknowledged", "delivered"):
                    by_type[dt]["success"] += 1
                elif st in ("error", "failed", "rejected"):
                    by_type[dt]["error"] += 1
            return {"metrics": by_type, "days": days, "timestamp": _now()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/edi/orders")
    def edi_orders(status: Optional[str] = None, limit: int = 50,
                   x_api_key: Optional[str] = Header(None)):
        """List EDI orders with optional status filter."""
        _verify(x_api_key)
        try:
            from ..memory.supabase_client import get_client
            sb = get_client()
            if not sb:
                return {"orders": [], "dry_run": True}
            q = sb.table("edi_orders").select("*").order("created_at", desc=True).limit(limit)
            if status:
                q = q.eq("status", status)
            result = q.execute()
            return {"orders": result.data or [], "timestamp": _now()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Agent events feed ─────────────────────────────────────────────────

    @app.get("/api/dashboard/events")
    def dashboard_events(source: Optional[str] = None, limit: int = 100,
                         x_api_key: Optional[str] = Header(None)):
        """Recent agent events from the message bus / work_events table."""
        _verify(x_api_key)
        try:
            from ..memory.supabase_client import get_client
            sb = get_client()
            if not sb:
                return {"events": [], "dry_run": True}
            q = sb.table("work_events").select("*").order("created_at", desc=True).limit(limit)
            if source:
                q = q.eq("bot_name", source)
            result = q.execute()
            return {"events": result.data or [], "timestamp": _now()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    log.info("Dashboard routes registered at /api/dashboard/* and /api/edi/*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
