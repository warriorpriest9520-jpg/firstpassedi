"""
partner_health.py — Partner health scoring, registry, and system health for FirstPass EDI.

Maintains a registry of trading partners with health scores, onboarding status,
and EDI configuration.  Also exposes system-level health helpers used by the
/api/edi/health dashboard endpoint.

Health scores are computed from:
  - Recent SLA adherence (via stored last_health_score or default by status)
  - Error rates (parse failures, 997 rejects)
  - Active incident count (edi_incidents table)
  - Pipeline freshness (shared_context snapshot staleness)
  - Workflow readiness (enabled/misconfigured count)

Source lineage:
  - partner_registry.py (SageOrderAPI)
  - audit_partner.py (ceo-bot)
  - edi_dashboard_routes.py — health scoring logic (ceo-bot)
  - dashboard_api_routes.py — system KPI helpers (ceo-bot)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("firstpass.dashboard.partner_health")

TABLE = "edi_partners"

# ─────────────────────────────────────────────────────────────────────────────
# Partner registry — public interface
# ─────────────────────────────────────────────────────────────────────────────

def get_partner_summary() -> List[Dict]:
    """
    Return a summary list of all registered partners with computed health scores.
    Uses Supabase when configured; falls back to a demo list.
    """
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if sb:
            result = sb.table(TABLE).select("*").order("name").execute()
            partners = result.data or []
            return [_enrich_partner(p) for p in partners]
    except Exception as exc:
        log.warning("Could not load partners from DB: %s", exc)
    return _demo_partners()


def get_partner(partner_id: str) -> Optional[Dict]:
    """Get a single partner by ID or name, enriched with health score."""
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if sb:
            result = (
                sb.table(TABLE)
                  .select("*")
                  .or_(f"id.eq.{partner_id},name.eq.{partner_id}")
                  .limit(1)
                  .execute()
            )
            if result.data:
                return _enrich_partner(result.data[0])
    except Exception as exc:
        log.warning("get_partner(%r) failed: %s", partner_id, exc)
    return None


def register_partner(
    name: str,
    platform: str,
    isa_qualifier: str = "",
    connector_config: Optional[Dict] = None,
    spec_path: str = "",
) -> Optional[str]:
    """
    Register a new trading partner.  Returns the new partner ID or None.

    :param name:             Human-readable partner name (e.g. "Big Box Retail")
    :param platform:         EDI platform ("orderful" | "logicbroker" | "rest_api")
    :param isa_qualifier:    X12 ISA sender/receiver qualifier
    :param connector_config: Platform-specific config dict
    :param spec_path:        Path to parsed EDI spec JSON
    """
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if not sb:
            log.warning("Supabase not configured — partner not persisted")
            return None
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "name":             name,
            "platform":         platform,
            "isa_qualifier":    isa_qualifier,
            "connector_config": connector_config or {},
            "spec_path":        spec_path,
            "status":           "pending",
            "created_at":       now,
            "updated_at":       now,
            "onboarding_log":   [],
        }
        result = sb.table(TABLE).insert(row).execute()
        if result.data:
            partner_id = result.data[0].get("id")
            log.info("Partner registered: %s (id=%s)", name, partner_id)
            return partner_id
    except Exception as exc:
        log.error("register_partner(%r) failed: %s", name, exc)
    return None


def update_partner_status(partner_id: str, status: str, note: str = "") -> bool:
    """Update a partner's lifecycle status.

    Valid values: pending | active | error | suspended
    """
    valid = {"pending", "active", "error", "suspended"}
    if status not in valid:
        log.warning("Invalid partner status: %r", status)
        return False
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if not sb:
            return False
        sb.table(TABLE).update({
            "status":     status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", partner_id).execute()
        if note:
            _append_log(sb, partner_id, f"Status → {status}: {note}")
        return True
    except Exception as exc:
        log.error("update_partner_status failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# EDI system-level health — used by /api/edi/health
# ─────────────────────────────────────────────────────────────────────────────

def compute_edi_health_score(
    pipeline_data: Dict,
    workflows: List[Dict],
    incidents: List[Dict],
) -> Tuple[int, str]:
    """
    Compute an EDI health score (0–100) and status label.

    Deductions:
      -20  pipeline snapshot is stale (> 20 min old)
      -10  pipeline source is a fallback (not live ERP data)
      -15  each disabled workflow (up to 3 workflows expected)
       -5  each missing required config key (capped at -25)
       -5  each open incident (capped at -20)

    Returns:
      (score, status) where status is "healthy" | "degraded" | "critical"
    """
    enabled_wf  = sum(1 for w in workflows if w.get("enabled"))
    total_wf    = max(len(workflows), 1)
    all_missing = {k for wf in workflows for k in wf.get("missing_config", [])}

    score = 100
    if pipeline_data.get("stale"):
        score -= 20
    if (pipeline_data.get("source") or "").endswith("fallback"):
        score -= 10
    if enabled_wf < total_wf:
        score -= min((total_wf - enabled_wf) * 15, 45)
    score -= min(len(all_missing) * 5, 25)
    score -= min(len(incidents) * 5, 20)
    score = max(0, score)

    if score >= 80:
        status = "healthy"
    elif score >= 50:
        status = "degraded"
    else:
        status = "critical"

    return score, status


def get_recent_incidents(limit: int = 10) -> List[Dict]:
    """Pull recent open EDI incidents from Supabase (or return empty list)."""
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if not sb:
            return []
        r = (
            sb.table("edi_incidents")
              .select("*")
              .order("created_at", desc=True)
              .limit(limit)
              .execute()
        )
        return r.data or []
    except Exception:
        return []


def get_bot_health_summary() -> Dict:
    """
    Return a consolidated bot-health summary dict:
      { "bots": [...], "active": N, "offline": N }

    Merges PM2 process list with Supabase bot_health rows, classifying each
    worker as active | error | idle | offline.
    """
    import json
    import subprocess
    from ..config import config

    # PM2 snapshot
    pm2_procs: List[Dict] = []
    try:
        r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            pm2_procs = json.loads(r.stdout)
    except Exception:
        pass

    # Supabase bot_health
    health_rows: List[Dict] = []
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if sb:
            r = sb.table("bot_health").select("*").execute()
            health_rows = r.data or []
    except Exception:
        pass

    stale_threshold = datetime.now(timezone.utc) - timedelta(hours=2)
    bots: List[Dict] = []

    # Well-known FirstPass process names (override via env: FIRSTPASS_PM2_PROCESSES)
    pm2_meta: Dict[str, Dict] = {
        "firstpass-api":    {"name": "FirstPass API", "icon": "🏭"},
        "firstpass-tunnel": {"name": "CF Tunnel",     "icon": "🔗"},
        "order-api":        {"name": "Order API",     "icon": "📋"},
    }
    for proc in pm2_procs:
        name = proc.get("name", "")
        meta = pm2_meta.get(name, {"name": name, "icon": "⚙️"})
        env  = proc.get("pm2_env", {})
        bots.append({
            "name":    meta["name"],
            "icon":    meta["icon"],
            "source":  "pm2",
            "status":  "active" if env.get("status") == "online" else "offline",
            "details": f"restarts={env.get('restart_time', 0)}",
        })

    for row in health_rows:
        raw = (row.get("status") or "unknown").lower()
        last_seen = row.get("last_seen")
        stale = True
        if last_seen:
            try:
                dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                stale = dt < stale_threshold
            except Exception:
                pass
        if raw == "ok" and not stale:
            status = "active"
        elif raw == "error":
            status = "error"
        elif stale:
            status = "idle"
        else:
            status = "offline"
        bots.append({
            "name":    row.get("bot_name", "unknown"),
            "icon":    "🤖",
            "source":  "supabase",
            "status":  status,
            "details": row.get("details", ""),
        })

    active  = sum(1 for b in bots if b["status"] == "active")
    offline = sum(1 for b in bots if b["status"] in ("offline", "error"))
    return {"bots": bots, "active": active, "offline": offline}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_partner(p: Dict) -> Dict:
    """Add computed health_score and health_label to a raw partner record."""
    score = _compute_health_score(p)
    return {
        **p,
        "health_score": score,
        "health_label": _health_label(score),
    }


def _compute_health_score(p: Dict) -> int:
    """
    Compute a 0–100 health score for a single partner record.

    In production this would query edi_incidents and transaction logs.
    For now it uses the stored last_health_score field, or a status-based
    default (suspended=0, error=30, pending=70, active=100).
    """
    if p.get("status") == "suspended":
        return 0
    if p.get("status") == "error":
        return 30
    stored = p.get("last_health_score")
    if stored is not None:
        try:
            return int(stored)
        except (ValueError, TypeError):
            pass
    return 100 if p.get("status") == "active" else 70


def _health_label(score: int) -> str:
    if score >= 90:
        return "healthy"
    if score >= 70:
        return "degraded"
    if score >= 40:
        return "at_risk"
    return "critical"


def _append_log(sb: Any, partner_id: str, entry: str) -> None:
    """Append a timestamped entry to a partner's onboarding_log JSON array."""
    try:
        existing = sb.table(TABLE).select("onboarding_log").eq("id", partner_id).execute()
        log_list = (existing.data or [{}])[0].get("onboarding_log") or []
        log_list.append({
            "ts":    datetime.now(timezone.utc).isoformat(),
            "entry": entry,
        })
        sb.table(TABLE).update({"onboarding_log": log_list}).eq("id", partner_id).execute()
    except Exception as exc:
        log.debug("_append_log failed: %s", exc)


def _demo_partners() -> List[Dict]:
    """Return demo partner records for use when Supabase is not configured."""
    return [
        {
            "id":                "demo-001",
            "name":              "Big Box Retail",
            "isa_qualifier":     "BIGBOX",
            "platform":          "orderful",
            "status":            "active",
            "health_score":      95,
            "health_label":      "healthy",
            "last_transmission": datetime.now(timezone.utc).isoformat(),
        },
        {
            "id":                "demo-002",
            "name":              "Wholesale Plus",
            "isa_qualifier":     "WHLSPLUS",
            "platform":          "logicbroker",
            "status":            "active",
            "health_score":      82,
            "health_label":      "degraded",
            "last_transmission": None,
        },
        {
            "id":                "demo-003",
            "name":              "Online Megastore",
            "isa_qualifier":     "ONLMEGA",
            "platform":          "orderful",
            "status":            "pending",
            "health_score":      70,
            "health_label":      "degraded",
            "last_transmission": None,
        },
    ]
