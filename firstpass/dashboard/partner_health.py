"""
partner_health.py — Partner health scoring and registry.

Maintains a registry of trading partners with health scores, onboarding
status, and EDI configuration.  Health scores are computed from:
  - Recent SLA adherence
  - Error rates (parse failures, 997 rejects)
  - Transmission frequency (expected vs actual)
  - Active incident count

Source lineage: partner_registry.py (SageOrderAPI), audit_partner.py (ceo-bot)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("firstpass.dashboard.partner_health")

TABLE = "edi_partners"


def get_partner_summary() -> List[Dict]:
    """
    Return a summary list of all registered partners with health scores.
    Uses Supabase when configured; falls back to a demo list.
    """
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if sb:
            result = sb.table(TABLE).select("*").order("name").execute()
            partners = result.data or []
            # Enrich with computed health scores
            return [_enrich_partner(p) for p in partners]
    except Exception as exc:
        log.warning(f"Could not load partners from DB: {exc}")

    # Demo fallback
    return _demo_partners()


def get_partner(partner_id: str) -> Optional[Dict]:
    """Get a single partner by ID or name."""
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
        log.warning(f"get_partner({partner_id}) failed: {exc}")
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

    :param name: Human-readable partner name (e.g. "Big Box Retail")
    :param platform: EDI platform ("orderful" | "logicbroker" | "rest_api")
    :param isa_qualifier: X12 ISA sender/receiver qualifier
    :param connector_config: Platform-specific config dict
    :param spec_path: Path to parsed EDI spec JSON
    """
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if not sb:
            log.warning("Supabase not configured — partner not persisted")
            return None
        row = {
            "name": name,
            "platform": platform,
            "isa_qualifier": isa_qualifier,
            "connector_config": connector_config or {},
            "spec_path": spec_path,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "onboarding_log": [],
        }
        result = sb.table(TABLE).insert(row).execute()
        if result.data:
            partner_id = result.data[0].get("id")
            log.info(f"Partner registered: {name} (id={partner_id})")
            return partner_id
    except Exception as exc:
        log.error(f"register_partner({name}) failed: {exc}")
    return None


def update_partner_status(partner_id: str, status: str, note: str = "") -> bool:
    """Update a partner's status (pending | active | error | suspended)."""
    valid = {"pending", "active", "error", "suspended"}
    if status not in valid:
        log.warning(f"Invalid partner status: {status!r}")
        return False
    try:
        from ..memory.supabase_client import get_client
        sb = get_client()
        if not sb:
            return False
        sb.table(TABLE).update({
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", partner_id).execute()
        if note:
            _append_log(sb, partner_id, f"Status → {status}: {note}")
        return True
    except Exception as exc:
        log.error(f"update_partner_status failed: {exc}")
        return False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _enrich_partner(p: Dict) -> Dict:
    """Add computed health_score and display fields to a raw partner record."""
    score = _compute_health_score(p)
    return {
        **p,
        "health_score": score,
        "health_label": _health_label(score),
    }


def _compute_health_score(p: Dict) -> int:
    """
    Compute a 0–100 health score based on recent partner metrics.

    In production this would query edi_incidents and transaction logs.
    For now it uses the last_health_score field or defaults to 100 for
    active partners.
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


def _append_log(sb, partner_id: str, entry: str) -> None:
    try:
        existing = (
            sb.table(TABLE).select("onboarding_log").eq("id", partner_id).execute()
        )
        log_list = (existing.data or [{}])[0].get("onboarding_log") or []
        log_list.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "entry": entry,
        })
        sb.table(TABLE).update({"onboarding_log": log_list}).eq("id", partner_id).execute()
    except Exception as exc:
        log.debug(f"_append_log failed: {exc}")


def _demo_partners() -> List[Dict]:
    return [
        {
            "id": "demo-001",
            "name": "Big Box Retail",
            "isa_qualifier": "BIGBOX",
            "platform": "orderful",
            "status": "active",
            "health_score": 95,
            "health_label": "healthy",
            "last_transmission": datetime.now(timezone.utc).isoformat(),
        },
        {
            "id": "demo-002",
            "name": "Wholesale Plus",
            "isa_qualifier": "WHLSPLUS",
            "platform": "logicbroker",
            "status": "active",
            "health_score": 82,
            "health_label": "degraded",
            "last_transmission": None,
        },
        {
            "id": "demo-003",
            "name": "Online Megastore",
            "isa_qualifier": "ONLMEGA",
            "platform": "orderful",
            "status": "pending",
            "health_score": 70,
            "health_label": "degraded",
            "last_transmission": None,
        },
    ]
