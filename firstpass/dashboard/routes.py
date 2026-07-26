"""
routes.py — Combined Dashboard + EDI API routes for FirstPass EDI.

Merges logic from:
  - EDI-specific dashboard endpoints (partner health, transaction views,
    compliance metrics, spec manager)
  - General dashboard endpoints (system status, agent health, recent activity)

Source lineage:
  - edi_dashboard_routes.py (ceo-bot) — EDI pipeline, workflows, specs
  - dashboard_api_routes.py (ceo-bot) — summary KPI bar, activity feed

Endpoints registered by register_dashboard_routes(app):
  GET  /api/dashboard/summary           - KPI bar + full bot list
  GET  /api/dashboard/activity-feed     - Recent work_events feed (last 60)
  GET  /api/dashboard/partners          - All trading partners with health scores
  GET  /api/dashboard/partners/{id}     - Single partner details + health
  GET  /api/dashboard/events            - Recent agent events feed
  GET  /api/edi/pipeline                - Full order pipeline snapshot
  GET  /api/edi/events                  - Recent EDI work_events
  GET  /api/edi/health                  - EDI agent health summary (0-100 score)
  POST /api/edi/sync                    - Trigger order-flow sync
  GET  /api/edi/metrics                 - EDI throughput metrics by doc type
  GET  /api/edi/orders                  - List EDI orders (filterable by status)
  GET  /api/edi/partner/workflows       - Partner EDI workflow definitions
  GET  /api/edi/specs                   - List all trading-partner EDI specs
  GET  /api/edi/specs/{key}             - Full spec JSON for a trading partner
  POST /api/edi/specs/{key}             - Create/replace a spec (auth required)
  DEL  /api/edi/specs/{key}             - Soft-delete a spec (renames .json.bak)
  POST /api/edi/specs/{key}/{doc}/gen   - Generate payload template for a doc type
  GET  /api/edi/reconcile               - Latest reconcile snapshot from cache
  GET  /api/edi/reconcile/order/{so_no} - Single-order reconcile status

Usage in api.py:
    from firstpass.dashboard.routes import register_dashboard_routes
    register_dashboard_routes(app)
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import config

logger = logging.getLogger("firstpass.dashboard.routes")

# EDI spec files live under <package_root>/specs/
SPECS_ROOT = (Path(__file__).parent.parent.parent / "specs").resolve()

# ── EDI segment field stubs — used by the payload-template generator ──────────
_SEGMENT_STUBS: Dict[str, Dict[str, str]] = {
    "ISA": {"ISA01": "00", "ISA02": "          ", "ISA03": "00", "ISA04": "          ",
            "ISA05": "ZZ", "ISA06": "<sender_id_15>", "ISA07": "ZZ",
            "ISA08": "<receiver_id_15>", "ISA09": "<yymmdd>", "ISA10": "<hhmm>",
            "ISA11": "U", "ISA12": "00401", "ISA13": "<ctrl_no>", "ISA14": "0",
            "ISA15": "P", "ISA16": ">"},
    "GS":  {"GS01": "<functional_id>", "GS02": "<sender_id>", "GS03": "<receiver_id>",
            "GS04": "<yyyymmdd>", "GS05": "<hhmm>", "GS06": "<group_ctrl_no>",
            "GS07": "X", "GS08": "004010"},
    "ST":  {"ST01": "<transaction_set_id>", "ST02": "<control_number>"},
    "BSN": {"BSN01": "00", "BSN02": "<shipment_id>", "BSN03": "<yyyymmdd>", "BSN04": "<hhmm>"},
    "BEG": {"BEG01": "00", "BEG02": "SA", "BEG03": "<po_number>", "BEG05": "<yyyymmdd>"},
    "BIG": {"BIG01": "<invoice_date>", "BIG02": "<invoice_no>", "BIG04": "<po_number>"},
    "HL":  {"HL01": "<level_number>", "HL02": "<parent_level>", "HL03": "<level_code>", "HL04": "1"},
    "TD1": {"TD101": "<pack_code>", "TD102": "<qty>", "TD106": "G", "TD107": "<weight_lbs>"},
    "TD5": {"TD501": "B", "TD502": "2", "TD503": "<scac>", "TD504": "<method>"},
    "DTM": {"DTM01": "<qualifier_011=ship_date>", "DTM02": "<yyyymmdd>"},
    "FOB": {"FOB01": "<payment_code>"},
    "N1":  {"N101": "<entity_ST|BT|SH>", "N102": "<name>", "N103": "92", "N104": "<id>"},
    "N3":  {"N301": "<address_line_1>"},
    "N4":  {"N401": "<city>", "N402": "<state_2>", "N403": "<zip>"},
    "PRF": {"PRF01": "<po_number>", "PRF04": "<yyyymmdd>"},
    "PO1": {"PO101": "<line_seq>", "PO102": "<ordered_qty>", "PO103": "EA", "PO104": "<unit_price>",
            "PO106": "BP", "PO107": "<buyer_part_no>", "PO108": "VP", "PO109": "<vendor_part_no>"},
    "LIN": {"LIN01": "<line_seq>", "LIN02": "IN", "LIN03": "<vendor_sku>",
            "LIN04": "UP", "LIN05": "<upc>", "LIN06": "BP", "LIN07": "<buyer_part_no>"},
    "SN1": {"SN101": "<line_seq>", "SN102": "<shipped_qty>", "SN103": "EA"},
    "IT1": {"IT101": "<line_seq>", "IT102": "<qty>", "IT103": "EA", "IT104": "<unit_price>",
            "IT106": "BP", "IT107": "<buyer_part_no>", "IT108": "VP", "IT109": "<vendor_part_no>"},
    "TDS": {"TDS01": "<total_invoice_cents>"},
    "CTT": {"CTT01": "<line_item_count>"},
    "SE":  {"SE01": "<segment_count>", "SE02": "<control_number>"},
    "GE":  {"GE01": "<transaction_count>", "GE02": "<group_ctrl_no>"},
    "IEA": {"IEA01": "<group_count>", "IEA02": "<ctrl_no>"},
}

# ── Process / worker display metadata (configurable via env) ──────────────────

# System-level processes managed by a process manager (e.g. PM2)
_PM2_META: Dict[str, Dict[str, str]] = {
    "firstpass-api":    {"name": "FirstPass API",    "icon": "🏭", "title": "FirstPass EDI API server"},
    "firstpass-tunnel": {"name": "CF Tunnel",        "icon": "🔗", "title": "Cloudflare access tunnel"},
    "order-api":        {"name": "Order API",        "icon": "📋", "title": "EDI / order-flow agent"},
}

# Internal workers tracked in Supabase bot_health
_WORKER_META: Dict[str, Dict[str, str]] = {
    "EDIBot":            {"name": "EDI Bot",          "icon": "📦", "title": "EDI transaction monitor"},
    "EDIMonitor":        {"name": "EDI Monitor",      "icon": "🔍", "title": "EDI payload auditor"},
    "EDIAuditWorker":    {"name": "EDI Audit",        "icon": "✅", "title": "Platform auditor"},
    "EDIPayloadAuditor": {"name": "EDI Payload",      "icon": "🔬", "title": "X12 payload validator"},
    "WatchdogWorker":    {"name": "Watchdog",         "icon": "🐕", "title": "Order-flow watchdog"},
    "InboxBot":          {"name": "Inbox Bot",        "icon": "📬", "title": "Email triage"},
    "KnowledgeWorker":   {"name": "Knowledge Worker", "icon": "🧠", "title": "Knowledge crawler"},
    "WorkflowWorker":    {"name": "Workflow Worker",  "icon": "⚙️", "title": "Platform workflow builder"},
    "QueryWorker":       {"name": "Query Worker",     "icon": "🔎", "title": "Knowledge-base Q&A"},
}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _supabase():
    try:
        from ..memory.supabase_client import get_client
        return get_client()
    except Exception:
        return None


def _shared_context(key: str) -> Optional[Any]:
    try:
        from ..memory.supabase_client import get_shared_context
        return get_shared_context(key)
    except Exception:
        return None


def _ok(**kwargs) -> Dict:
    return {"ok": True, **kwargs}


def _err(msg: str, status: int = 500) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Pipeline data ─────────────────────────────────────────────────────────────

def _build_pipeline() -> Dict:
    """Pull from shared_context 'order_flow_snapshot' (pushed by the ERP sync
    process every 10 min).  Falls back to counting work_events if missing/stale."""

    snapshot = _shared_context("order_flow_snapshot")
    if snapshot and isinstance(snapshot, dict):
        pipeline = snapshot.get("pipeline", snapshot)
        as_of    = snapshot.get("as_of")
        source   = snapshot.get("source", "erp_sync")

        stale = True
        if as_of:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(as_of)).total_seconds()
                stale = age > 1200  # > 20 minutes
            except Exception:
                pass

        return {
            "pipeline": pipeline,
            "as_of":    as_of,
            "source":   source,
            "stale":    stale,
        }

    # Fallback: count from work_events
    sb = _supabase()
    pipeline: Dict[str, int] = {
        "received": 0, "processing": 0, "asn": 0,
        "invoice": 0, "asn_pending": 0, "inv_pending": 0, "total_imported": 0,
    }

    if sb:
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
            r = sb.table("work_events").select("event_type, summary").gte("created_at", since).execute()
            events = r.data or []
            counts: Dict[str, int] = {}
            for e in events:
                et = (e.get("event_type") or "").lower()
                counts[et] = counts.get(et, 0) + 1
            pipeline["received"]       = counts.get("850_received", 0)
            pipeline["asn"]            = counts.get("856_sent", 0)
            pipeline["invoice"]        = counts.get("810_sent", 0)
            pipeline["processing"]     = counts.get("erp_import", 0) or counts.get("sage_import", 0)
            pipeline["total_imported"] = counts.get("850_received", 0)
            pipeline["asn_pending"]    = max(0, pipeline["received"] - pipeline["asn"])
            pipeline["inv_pending"]    = max(0, pipeline["asn"] - pipeline["invoice"])
        except Exception as exc:
            logger.warning("[edi_pipeline] work_events fallback failed: %s", exc)

    return {
        "pipeline": pipeline,
        "as_of":    None,
        "source":   "work_events_fallback",
        "stale":    True,
    }


# ── Partner workflow definitions ──────────────────────────────────────────────

def _load_partner_workflows() -> List[Dict]:
    """Load partner EDI workflow definitions from the spec files.

    Returns a list of workflow dicts with document type, direction, enabled
    state, and required config keys.  Falls back to a minimal example set if
    no specs are found.
    """
    workflows: List[Dict] = []
    if SPECS_ROOT.exists():
        for f in sorted(SPECS_ROOT.rglob("*.json")):
            if ".bak" in f.name:
                continue
            try:
                spec = json.loads(f.read_text(encoding="utf-8"))
                partner = spec.get("trading_partner", f.stem)
                for doc in spec.get("documents", []):
                    doc_type  = doc.get("type", "")
                    direction = doc.get("direction", "inbound")
                    wf_id     = f"{f.stem}_{doc_type.lower()}"
                    missing   = [k for k in spec.get("required_config_keys", [])
                                 if not os.getenv(k)]
                    workflows.append({
                        "id":             wf_id,
                        "doc_type":       doc_type,
                        "direction":      direction,
                        "title":          f"{partner} {doc_type} – {direction}",
                        "description":    doc.get("description", ""),
                        "spec_key":       f.stem,
                        "enabled":        spec.get("enabled", True),
                        "config_keys":    spec.get("required_config_keys", []),
                        "missing_config": missing,
                    })
            except Exception as exc:
                logger.debug("[workflows] failed to parse %s: %s", f.name, exc)
    return workflows


def _enrich_workflow_activity(workflows: List[Dict]) -> List[Dict]:
    """Add 7-day activity counts from work_events to each workflow."""
    sb = _supabase()
    activity: Dict[str, int] = {}

    if sb:
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            r = (sb.table("work_events")
                   .select("event_type, summary, created_at")
                   .gte("created_at", since)
                   .order("created_at", desc=True)
                   .limit(500)
                   .execute())
            for e in (r.data or []):
                et = (e.get("event_type") or "").lower()
                activity[et] = activity.get(et, 0) + 1
        except Exception as exc:
            logger.warning("[edi] workflow activity query failed: %s", exc)

    doc_activity = {
        "850": activity.get("850_received", 0) + activity.get("edi_received", 0),
        "856": activity.get("856_sent", 0) + activity.get("asn_sent", 0),
        "810": activity.get("810_sent", 0) + activity.get("invoice_sent", 0),
        "997": activity.get("997_received", 0) + activity.get("997_sent", 0),
    }

    result = []
    for wf in workflows:
        enriched = dict(wf)
        enriched["activity_7d"] = doc_activity.get(wf.get("doc_type", ""), 0)
        enriched["status"] = (
            "active"       if wf.get("enabled") and enriched["activity_7d"] > 0 else
            "ready"        if wf.get("enabled") else
            "needs_config" if wf.get("missing_config") else
            "disabled"
        )
        result.append(enriched)
    return result


# ── EDI events ────────────────────────────────────────────────────────────────

def _edi_events(limit: int = 50) -> List[Dict]:
    sb = _supabase()
    if not sb:
        return []
    try:
        r = (sb.table("work_events")
               .select("*")
               .in_("event_type", [
                   "850_received", "855_sent", "856_sent", "810_sent",
                   "997_received", "997_sent", "edi_received", "edi_error",
                   "edi_correction", "asn_sent", "invoice_sent", "erp_import",
                   "sage_import", "edi_check", "edi_incident",
               ])
               .order("created_at", desc=True)
               .limit(limit)
               .execute())
        return r.data or []
    except Exception as exc:
        logger.warning("[edi_events] query failed: %s", exc)
        return []


def _edi_incidents(limit: int = 10) -> List[Dict]:
    sb = _supabase()
    if not sb:
        return []
    try:
        r = (sb.table("edi_incidents")
               .select("*")
               .order("created_at", desc=True)
               .limit(limit)
               .execute())
        return r.data or []
    except Exception:
        return []


# ── Dashboard summary (KPI bar + bot list) ────────────────────────────────────

def _pm2_processes() -> List[Dict]:
    """Return parsed PM2 jlist. Empty list on any error."""
    try:
        r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=8)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        return json.loads(r.stdout)
    except Exception as exc:
        logger.warning("pm2 jlist failed: %s", exc)
        return []


def _supabase_bot_health() -> List[Dict]:
    sb = _supabase()
    if not sb:
        return []
    try:
        r = sb.table("bot_health").select("*").execute()
        return r.data or []
    except Exception as exc:
        logger.warning("bot_health fetch failed: %s", exc)
        return []


def _supabase_escalations_count() -> int:
    sb = _supabase()
    if not sb:
        return 0
    try:
        r = sb.table("escalations").select("id").eq("resolved", False).execute()
        return len(r.data or [])
    except Exception:
        return 0


def _build_summary() -> Dict:
    """Merge PM2 process list + Supabase bot_health into a KPI + bots payload."""
    procs       = _pm2_processes()
    health_rows = _supabase_bot_health()
    esc_count   = _supabase_escalations_count()

    health_map: Dict[str, Dict] = {row["bot_name"]: row for row in health_rows}
    bots: List[Dict] = []

    # PM2 system processes
    for proc in procs:
        pm2_name = proc.get("name", "")
        meta     = _PM2_META.get(pm2_name)
        if not meta:
            continue
        env      = proc.get("pm2_env", {})
        raw_stat = env.get("status", "unknown")
        status   = "active" if raw_stat == "online" else "offline"
        restarts = env.get("restart_time", 0)
        memory   = proc.get("monit", {}).get("memory", 0)
        cpu      = proc.get("monit", {}).get("cpu", 0)
        uptime_ms = env.get("pm_uptime", 0)
        last_act  = (
            datetime.utcfromtimestamp(uptime_ms / 1000)
            .replace(tzinfo=timezone.utc).isoformat()
            if uptime_ms else None
        )
        bots.append({
            "name":            meta["name"],
            "title":           meta["title"],
            "icon":            meta["icon"],
            "pm2_name":        pm2_name,
            "status":          status,
            "current_focus":   f"CPU {cpu}%  RAM {memory // 1024 // 1024}MB  restarts={restarts}",
            "last_summary":    raw_stat,
            "total":           0,
            "success":         0,
            "last_activity":   last_act,
            "open_escalations": 0,
        })

    # Supabase bot_health workers
    stale_threshold = datetime.now(timezone.utc) - timedelta(hours=2)
    for bot_name, row in health_map.items():
        meta = _WORKER_META.get(bot_name) or {"name": bot_name, "icon": "🤖", "title": "EDI worker"}
        raw_status = (row.get("status") or "unknown").lower()
        last_seen  = row.get("last_seen")
        stale      = True
        if last_seen:
            try:
                dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                stale = dt < stale_threshold
            except Exception:
                pass
        if raw_status == "ok" and not stale:
            status = "active"
        elif raw_status == "error":
            status = "error"
        elif stale:
            status = "idle"
        else:
            status = "offline"
        bots.append({
            "name":            meta["name"],
            "title":           meta["title"],
            "icon":            meta["icon"],
            "pm2_name":        None,
            "status":          status,
            "current_focus":   row.get("details", ""),
            "last_summary":    raw_status,
            "total":           0,
            "success":         0,
            "last_activity":   last_seen,
            "open_escalations": row.get("error_count", 0),
        })

    _order = {"active": 0, "error": 1, "idle": 2, "offline": 3}
    bots.sort(key=lambda b: (_order.get(b["status"], 9), b["name"]))

    active_count  = sum(1 for b in bots if b["status"] == "active")
    offline_count = sum(1 for b in bots if b["status"] in ("offline", "error"))

    # Try to pull live partner/workflow counts from a running order-api
    _edi_partners = 0
    _enabled_wf   = 0
    try:
        import urllib.request as _ur
        _base = os.environ.get("ORDERAPI_BASE_URL", "http://localhost:8001").rstrip("/")
        with _ur.urlopen(f"{_base}/api/dashboard", timeout=3) as _r:
            _d = json.loads(_r.read().decode()).get("data", {})
            _edi_partners = len(_d.get("edi_pipeline", {}).get("by_phase", {})) or _edi_partners
            _conns = _d.get("connectors", {})
            _enabled_wf = sum(1 for v in _conns.values() if v)
    except Exception:
        pass

    kpis = {
        "active_bots":       active_count,
        "offline_bots":      offline_count,
        "open_escalations":  esc_count,
        "enabled_workflows": _enabled_wf,
        "edi_partners":      _edi_partners,
    }
    return {"kpis": kpis, "bots": bots}


def _build_activity_feed(limit: int = 60) -> Dict:
    sb = _supabase()
    if not sb:
        return {"events": [], "count": 0}
    try:
        r = (sb.table("work_events")
               .select("bot_name,event_type,summary,created_at")
               .order("created_at", desc=True)
               .limit(limit)
               .execute())
        rows = r.data or []
    except Exception as exc:
        logger.warning("work_events fetch failed: %s", exc)
        rows = []
    events = [
        {
            "type":    row.get("event_type", "event"),
            "bot":     row.get("bot_name", "?"),
            "source":  row.get("bot_name", "?"),
            "summary": row.get("summary", ""),
            "ts":      row.get("created_at"),
        }
        for row in rows
    ]
    return {"events": events, "count": len(events)}


# ── Spec manager helpers ──────────────────────────────────────────────────────

def _spec_path(key: str) -> Optional[Path]:
    """Find the spec JSON for a customer key (searches subdirs too)."""
    direct = SPECS_ROOT / f"{key}.json"
    if direct.exists():
        return direct
    for f in SPECS_ROOT.rglob(f"{key}.json"):
        if ".bak" not in f.name:
            return f
    return None


def _list_specs() -> List[Dict]:
    results = []
    if not SPECS_ROOT.exists():
        return results
    for f in sorted(SPECS_ROOT.rglob("*.json")):
        if ".bak" in f.name or ".pdf_cache" in str(f):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            partner = data.get("trading_partner") or f.stem
            docs    = [d.get("type") for d in data.get("documents", []) if d.get("type")]
            conn    = data.get("connection", {})
            results.append({
                "key":             f.stem,
                "file":            str(f.relative_to(SPECS_ROOT)),
                "trading_partner": partner,
                "company_no":      data.get("company_no") or data.get("sage_customer_no"),
                "doc_types":       docs,
                "connection_type": conn.get("type") or "unknown",
                "rules_count":     len(data.get("rules", [])),
                "alert_email":     data.get("alert_email"),
            })
        except Exception as exc:
            results.append({"key": f.stem, "file": str(f.relative_to(SPECS_ROOT)), "error": str(exc)})
    return results


def _build_payload_template(spec: Dict, doc_type: str) -> Dict:
    """Build the payload template / field guide for a given doc type."""
    doc_def = next((d for d in spec.get("documents", []) if d.get("type") == doc_type), None)
    if not doc_def:
        return {"error": f"Doc type '{doc_type}' not defined in spec"}

    req_segs       = doc_def.get("required_segments", [])
    field_rules    = []
    value_maps: Dict[str, Dict] = {}
    required_fields = []

    for rule in spec.get("rules", []):
        if rule.get("doc_type") not in (doc_type, "*", None):
            continue
        rt = rule.get("rule_type", "")
        field_rules.append({
            "id":          rule.get("id"),
            "type":        rt,
            "name":        rule.get("rule_name"),
            "description": rule.get("description"),
            "severity":    rule.get("severity", "error"),
            "field_path":  rule.get("field_path"),
        })
        if rt == "field_mapping" and rule.get("value_map"):
            seg = rule.get("field_path", "LIN")
            value_maps.setdefault(seg, {})
            value_maps[seg].update(rule["value_map"])
        if rt == "required_field":
            required_fields.append({"field": rule.get("field_path"), "rule": rule.get("rule_name")})

    payload_template = {}
    for seg in req_segs:
        stub = dict(_SEGMENT_STUBS.get(seg, {}))
        if seg in value_maps:
            stub["_value_map"] = value_maps[seg]
            stub["_note"] = "Substitute internal SKU using _value_map before transmission"
        if not stub:
            stub["_note"] = f"{seg} — refer to X12 {doc_def.get('version', '005010')} spec"
        payload_template[seg] = stub

    return {
        "trading_partner":      spec.get("trading_partner"),
        "doc_type":             doc_type,
        "direction":            doc_def.get("direction"),
        "version":              doc_def.get("version", "005010"),
        "schedule":             doc_def.get("schedule"),
        "required_segments":    req_segs,
        "special_requirements": doc_def.get("special_requirements", []),
        "active_rules":         field_rules,
        "value_maps":           value_maps,
        "required_fields":      required_fields,
        "payload_template":     payload_template,
        "item_filter":          doc_def.get("item_filter"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_dashboard_routes(app: FastAPI) -> None:
    """Register all dashboard + EDI API routes on a FastAPI app instance."""

    # ── Auth helper ───────────────────────────────────────────────────────────
    def _verify(x_api_key: Optional[str] = None) -> None:
        if config.API_KEY and x_api_key != config.API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")

    # ── Dashboard: summary & activity ─────────────────────────────────────────

    @app.get("/api/dashboard/summary", tags=["Dashboard"])
    async def dashboard_summary():
        """KPI bar + full bot list (PM2 system processes + Supabase EDI workers)."""
        try:
            data = _build_summary()
            return JSONResponse(_ok(**data))
        except Exception as exc:
            logger.exception("dashboard_summary error")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.get("/api/dashboard/activity-feed", tags=["Dashboard"])
    async def activity_feed(limit: int = 60):
        """Recent work_events from Supabase — powers the Activity tab feed."""
        try:
            data = _build_activity_feed(limit=min(limit, 200))
            return JSONResponse(_ok(**data))
        except Exception as exc:
            logger.exception("activity_feed error")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # ── Dashboard: partners ────────────────────────────────────────────────────

    @app.get("/api/dashboard/partners", tags=["Dashboard"])
    def dashboard_partners(x_api_key: Optional[str] = Header(None)):
        """All trading partners with computed health scores."""
        _verify(x_api_key)
        from .partner_health import get_partner_summary
        return {"partners": get_partner_summary(), "timestamp": _now()}

    @app.get("/api/dashboard/partners/{partner_id}", tags=["Dashboard"])
    def dashboard_partner_detail(partner_id: str, x_api_key: Optional[str] = Header(None)):
        """Single partner details + health score."""
        _verify(x_api_key)
        from .partner_health import get_partner
        partner = get_partner(partner_id)
        if not partner:
            raise HTTPException(status_code=404, detail="Partner not found")
        return partner

    @app.get("/api/dashboard/events", tags=["Dashboard"])
    def dashboard_events(source: Optional[str] = None, limit: int = 100,
                         x_api_key: Optional[str] = Header(None)):
        """Recent agent events from the work_events table."""
        _verify(x_api_key)
        try:
            sb = _supabase()
            if not sb:
                return {"events": [], "dry_run": True}
            q = sb.table("work_events").select("*").order("created_at", desc=True).limit(limit)
            if source:
                q = q.eq("bot_name", source)
            result = q.execute()
            return {"events": result.data or [], "timestamp": _now()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── EDI: pipeline & health ────────────────────────────────────────────────

    @app.get("/api/edi/pipeline", tags=["EDI"])
    async def edi_pipeline():
        """Full EDI order pipeline snapshot — ERP sync + fallback to work_events."""
        data = _build_pipeline()
        return JSONResponse(_ok(**data))

    @app.get("/api/edi/health", tags=["EDI"])
    async def edi_health():
        """EDI agent health: pipeline freshness, workflow readiness, incident count (0-100 score)."""
        from .partner_health import compute_edi_health_score
        pipeline_data = _build_pipeline()
        workflows     = _enrich_workflow_activity(_load_partner_workflows())
        incidents     = _edi_incidents(limit=5)

        enabled_wf  = sum(1 for w in workflows if w.get("enabled"))
        total_wf    = len(workflows)
        all_missing = {
            k for wf in workflows for k in wf.get("missing_config", [])
        }

        score, status = compute_edi_health_score(pipeline_data, workflows, incidents)

        return JSONResponse(_ok(
            status=status,
            score=score,
            pipeline_stale=pipeline_data.get("stale", True),
            pipeline_source=pipeline_data.get("source"),
            workflows_enabled=f"{enabled_wf}/{total_wf}",
            missing_config=list(all_missing),
            open_incidents=len(incidents),
            checks={
                "order_flow_snapshot": not pipeline_data.get("stale"),
                "workflows_ready":     enabled_wf > 0,
                "no_open_incidents":   len(incidents) == 0,
            },
        ))

    @app.get("/api/edi/events", tags=["EDI"])
    async def edi_events(limit: int = 50):
        """Recent EDI work events (850/856/810/997/corrections)."""
        events    = _edi_events(limit=min(limit, 100))
        incidents = _edi_incidents(limit=10)
        return JSONResponse(_ok(
            events=events,
            incidents=incidents,
            count=len(events),
        ))

    @app.get("/api/edi/metrics", tags=["EDI"])
    def edi_metrics(days: int = 7, x_api_key: Optional[str] = Header(None)):
        """EDI throughput metrics: volume by doc type + error rates."""
        _verify(x_api_key)
        try:
            sb = _supabase()
            if not sb:
                return {"metrics": {}, "dry_run": True}
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            result = (
                sb.table("edi_transactions")
                  .select("doc_type,status,created_at")
                  .gte("created_at", since)
                  .execute()
            )
            by_type: Dict = {}
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

    @app.get("/api/edi/orders", tags=["EDI"])
    def edi_orders(status: Optional[str] = None, limit: int = 50,
                   x_api_key: Optional[str] = Header(None)):
        """List EDI orders with optional status filter."""
        _verify(x_api_key)
        try:
            sb = _supabase()
            if not sb:
                return {"orders": [], "dry_run": True}
            q = sb.table("edi_orders").select("*").order("created_at", desc=True).limit(limit)
            if status:
                q = q.eq("status", status)
            result = q.execute()
            return {"orders": result.data or [], "timestamp": _now()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/edi/sync", tags=["EDI"])
    async def edi_sync(x_api_key: Optional[str] = Header(None)):
        """Trigger the ERP order-flow sync.  Requires X-Api-Key.

        Attempts to call a running order-api service first, then falls back
        to a configurable shell command (SYNC_COMMAND env var).
        """
        _verify(x_api_key)

        orderapi_base = os.getenv("ORDERAPI_BASE_URL", "http://localhost:8001").rstrip("/")
        try:
            import urllib.request as _ur
            req = _ur.Request(f"{orderapi_base}/api/sync", method="POST",
                              headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
                return JSONResponse(_ok(triggered=True, method="order_api", result=result))
        except Exception:
            pass

        # Configurable fallback command
        sync_cmd = os.getenv("SYNC_COMMAND", "")
        if sync_cmd:
            return JSONResponse(_ok(
                triggered=False,
                message="Order-API not reachable. Run the configured sync command manually:",
                command=sync_cmd,
            ))

        return JSONResponse(_ok(
            triggered=False,
            message="Order-API not reachable and SYNC_COMMAND not configured.",
            hint="Set ORDERAPI_BASE_URL or SYNC_COMMAND in your .env file.",
        ))

    # ── EDI: partner workflows ────────────────────────────────────────────────

    @app.get("/api/edi/partner/workflows", tags=["EDI"])
    async def partner_workflows():
        """EDI workflow definitions for all configured trading partners + live status."""
        workflows = _enrich_workflow_activity(_load_partner_workflows())
        enabled   = sum(1 for w in workflows if w.get("enabled"))
        ready     = sum(1 for w in workflows if w.get("status") in ("active", "ready"))
        return JSONResponse(_ok(
            workflows=workflows,
            summary={
                "total":        len(workflows),
                "enabled":      enabled,
                "ready":        ready,
                "needs_config": sum(1 for w in workflows if w.get("missing_config")),
            },
            company_isa_id=config.COMPANY_ISA_ID,
        ))

    # ── EDI: spec manager ─────────────────────────────────────────────────────

    @app.get("/api/edi/specs", tags=["EDI"])
    async def list_specs():
        """List all trading-partner EDI specs."""
        specs = _list_specs()
        return JSONResponse(_ok(specs=specs, count=len(specs)))

    @app.get("/api/edi/specs/{customer_key}", tags=["EDI"])
    async def get_spec(customer_key: str):
        """Return full spec JSON for a trading partner."""
        path = _spec_path(customer_key)
        if not path:
            return JSONResponse({"ok": False, "error": f"Spec not found: {customer_key}"}, status_code=404)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return JSONResponse(_ok(spec=data, file=str(path.relative_to(SPECS_ROOT))))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/edi/specs/{customer_key}", tags=["EDI"])
    async def upsert_spec(customer_key: str, request: Request,
                          x_api_key: Optional[str] = Header(None)):
        """Create or replace a trading-partner spec. Requires X-Api-Key."""
        _verify(x_api_key)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
        SPECS_ROOT.mkdir(parents=True, exist_ok=True)
        existing = _spec_path(customer_key)
        target = existing if existing else SPECS_ROOT / f"{customer_key}.json"
        target.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        return JSONResponse(_ok(
            saved=str(target.relative_to(SPECS_ROOT)),
            trading_partner=body.get("trading_partner", customer_key),
        ))

    @app.delete("/api/edi/specs/{customer_key}", tags=["EDI"])
    async def delete_spec(customer_key: str, x_api_key: Optional[str] = Header(None)):
        """Soft-delete a spec (renames to .json.bak). Requires X-Api-Key."""
        _verify(x_api_key)
        path = _spec_path(customer_key)
        if not path:
            return JSONResponse({"ok": False, "error": f"Spec not found: {customer_key}"}, status_code=404)
        bak = path.with_name(path.name.replace(".json", ".json.bak"))
        path.rename(bak)
        return JSONResponse(_ok(archived=str(bak.relative_to(SPECS_ROOT))))

    @app.post("/api/edi/specs/{customer_key}/{doc_type}/generate", tags=["EDI"])
    async def generate_spec_template(customer_key: str, doc_type: str):
        """Generate the payload template / field guide for a given partner + doc type.

        Returns required segments, value maps, validation rules, and a populated
        field stub ready for transmission via Orderful, Logicbroker, or direct EDI.
        """
        path = _spec_path(customer_key)
        if not path:
            return JSONResponse({"ok": False, "error": f"Spec not found: {customer_key}"}, status_code=404)
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        template = _build_payload_template(spec, doc_type.upper())
        if "error" in template:
            return JSONResponse({"ok": False, **template}, status_code=404)
        return JSONResponse(_ok(**template))

    # ── EDI: reconcile (read-only dashboard endpoints) ─────────────────────────
    # Full reconcile engine lives in firstpass.pipeline.reconciliation.
    # These routes expose cached snapshots for the dashboard.

    @app.get("/api/edi/reconcile", tags=["EDI"])
    async def get_reconcile_snapshot():
        """Return the latest EDI reconcile snapshot from the pipeline cache."""
        try:
            from ..pipeline.reconciliation import Reconciler
            snapshot = Reconciler.get_snapshot()
            if snapshot:
                return JSONResponse(snapshot)
            raise HTTPException(
                status_code=404,
                detail="No reconcile snapshot yet — trigger /api/edi/reconcile/run first",
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/edi/reconcile/order/{sales_order_no}", tags=["EDI"])
    async def get_reconcile_order(sales_order_no: str):
        """Return reconcile status for a single sales order."""
        try:
            from ..pipeline.reconciliation import Reconciler
            snapshot = Reconciler.get_snapshot()
            if not snapshot:
                raise HTTPException(status_code=404, detail="No snapshot yet")
            for order in snapshot.get("orders", []):
                if order.get("sales_order_no", "").strip().upper() == sales_order_no.upper():
                    return JSONResponse(order)
            raise HTTPException(status_code=404, detail=f"Order {sales_order_no} not in snapshot")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    logger.info("[dashboard] routes registered at /api/dashboard/* and /api/edi/*")
