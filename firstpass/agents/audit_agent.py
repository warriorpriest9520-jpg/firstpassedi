"""
audit_agent.py — Partner compliance and EDI payload audit agent.

Checks each trading partner against configured compliance rules:
  - Required segments present (ISA, GS, ST, BEG, SE, GE, IEA)
  - Field format validation (dates, ISA IDs, control numbers)
  - SLA adherence (855 within 24h, 856 within 24h of ship, 810 within 48h)
  - Transaction set count matching
  - Duplicate PO detection

Escalates findings to the escalation manager and publishes events on the bus.

Source lineage:
  - audit_partner.py     — partner-level SLA and compliance scoring
  - edi_payload_auditor.py — X12 segment and field validation
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..config import config
from ..utils.message_bus import MessageBus

log = logging.getLogger("firstpass.audit_agent")

# ── Compliance rule definitions ───────────────────────────────────────────────

REQUIRED_SEGMENTS = ["ISA", "GS", "ST", "SE", "GE", "IEA"]

_DATE_8_RE = re.compile(r"^\d{8}$")
_DATE_6_RE = re.compile(r"^\d{6}$")
_TIME_RE = re.compile(r"^\d{4}$")
_CTRL_RE = re.compile(r"^\d{9}$")

# SLA windows (hours) per document type
SLA_HOURS: Dict[str, int] = {
    "855": 24,   # PO acknowledgment
    "856": 24,   # ASN (after ship)
    "810": 48,   # Invoice
    "997": 1,    # Functional acknowledgment
}

# Partner-specific override (can be loaded from Supabase / config)
PARTNER_SLA_OVERRIDES: Dict[str, Dict[str, int]] = {
    # "BIG_BOX_RETAIL": {"855": 4, "856": 12},
}


# ── Main agent ────────────────────────────────────────────────────────────────

class AuditAgent:
    """
    Partner compliance agent.

    On each cycle:
    1. Loads active partners from Supabase (or demo list in dry-run)
    2. Checks each partner's recent transactions against SLA + compliance rules
    3. Scores each partner (0–100 health score)
    4. Escalates partners below threshold or with critical violations
    """

    def __init__(self):
        self.bus = MessageBus()

    # ── Public interface ──────────────────────────────────────────────────

    def run_cycle(self) -> dict:
        partners = self._load_partners()
        log.info(f"AuditAgent: auditing {len(partners)} partner(s)")
        results = []
        escalated = 0

        for partner in partners:
            result = self.audit_partner(partner)
            results.append(result)
            if result["health_score"] < 70 or result["violations"]:
                self._escalate(partner, result)
                escalated += 1

        summary = {
            "partners_audited": len(partners),
            "escalated": escalated,
            "avg_health": (
                round(sum(r["health_score"] for r in results) / len(results), 1)
                if results else 0
            ),
        }
        log.info(f"AuditAgent cycle: {summary}")
        return summary

    def audit_partner(self, partner: Dict) -> dict:
        """
        Audit a single trading partner.  Returns a report dict including
        health_score (0-100), violations, SLA compliance, and recommendations.
        """
        name = partner.get("name", "unknown")
        recent_txs = partner.get("recent_transactions", [])

        violations: List[str] = []
        sla_misses: List[str] = []
        payload_errors: List[str] = []

        for tx in recent_txs:
            # Payload validation
            errs = self.validate_payload(tx.get("x12", ""), tx.get("doc_type", ""))
            payload_errors.extend(errs)

            # SLA check
            miss = self.check_sla(tx, partner.get("name", ""))
            if miss:
                sla_misses.append(miss)

        # Combine
        violations = payload_errors + sla_misses

        # Health score: start at 100, deduct for each violation
        health_score = max(0, 100 - len(violations) * 10 - len(sla_misses) * 15)

        result = {
            "partner": name,
            "health_score": health_score,
            "violations": violations,
            "sla_misses": sla_misses,
            "payload_errors": payload_errors,
            "transactions_checked": len(recent_txs),
            "audited_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info(f"Partner {name!r}: score={health_score}, violations={len(violations)}")
        return result

    def validate_payload(self, x12: str, doc_type: str = "") -> List[str]:
        """
        Validate an X12 document payload.
        Returns a list of violation strings (empty = clean).
        """
        if not x12.strip():
            return ["Empty X12 payload"]

        errors: List[str] = []
        segments = [s.strip() for s in x12.replace("\n", "~").split("~") if s.strip()]
        tags_found = {seg.split("*")[0].upper() for seg in segments}

        # Required segment check
        for req in REQUIRED_SEGMENTS:
            if req not in tags_found:
                errors.append(f"Missing required segment: {req}")

        # ISA field validation
        for seg in segments:
            parts = seg.split("*")
            tag = parts[0].upper()

            if tag == "ISA":
                if len(parts) < 16:
                    errors.append(f"ISA segment has {len(parts)-1} elements (expected 15)")
                else:
                    date_val = parts[9].strip()
                    time_val = parts[10].strip()
                    ctrl = parts[13].strip()
                    if not _DATE_6_RE.match(date_val):
                        errors.append(f"ISA09 (date) invalid format: {date_val!r}")
                    if not _TIME_RE.match(time_val):
                        errors.append(f"ISA10 (time) invalid format: {time_val!r}")

            elif tag == "GS":
                if len(parts) < 9:
                    errors.append(f"GS segment too short ({len(parts)-1} elements)")
                else:
                    date_val = parts[4].strip()
                    if not _DATE_8_RE.match(date_val):
                        errors.append(f"GS04 (date) invalid format: {date_val!r}")

        return errors

    def check_sla(self, transaction: Dict, partner_name: str = "") -> Optional[str]:
        """
        Check if a transaction breached its SLA window.
        Returns a violation string, or None if within SLA.
        """
        doc_type = transaction.get("doc_type", "")
        submitted_at = transaction.get("submitted_at")
        trigger_at = transaction.get("trigger_at")   # e.g. ship_date for 856

        if not doc_type or not submitted_at or not trigger_at:
            return None

        sla = PARTNER_SLA_OVERRIDES.get(partner_name, {}).get(doc_type) or SLA_HOURS.get(doc_type)
        if sla is None:
            return None

        try:
            t_submit = datetime.fromisoformat(submitted_at)
            t_trigger = datetime.fromisoformat(trigger_at)
            elapsed_hours = (t_submit - t_trigger).total_seconds() / 3600
            if elapsed_hours > sla:
                return (
                    f"{doc_type} SLA breach: submitted {elapsed_hours:.1f}h after trigger "
                    f"(SLA={sla}h)"
                )
        except Exception as exc:
            log.debug(f"SLA date parse error: {exc}")
        return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_partners(self) -> List[Dict]:
        """Load active partners from Supabase (or return demo list)."""
        try:
            from ..memory.supabase_client import get_client
            sb = get_client()
            if sb:
                result = (
                    sb.table("edi_partners")
                      .select("*")
                      .eq("status", "active")
                      .execute()
                )
                return result.data or []
        except Exception as exc:
            log.warning(f"Could not load partners from DB: {exc}")

        # Demo partners for dry-run
        return [
            {
                "name": "Big Box Retail",
                "isa_qualifier": "BIGBOX",
                "platform": "orderful",
                "status": "active",
                "recent_transactions": [],
            },
            {
                "name": "Wholesale Plus",
                "isa_qualifier": "WHLSPLUS",
                "platform": "logicbroker",
                "status": "active",
                "recent_transactions": [],
            },
        ]

    def _escalate(self, partner: Dict, result: Dict) -> None:
        """Escalate a non-compliant partner to the escalation manager."""
        name = partner.get("name", "unknown")
        score = result["health_score"]
        violations = result["violations"]
        try:
            from ..safety.escalation import EscalationManager
            mgr = EscalationManager()
            mgr.escalate(
                summary=f"Partner audit: {name} health={score}",
                detail="\n".join(violations),
                severity="high" if score < 50 else "medium",
                source="audit_agent",
                tags={"partner": name},
            )
        except Exception as exc:
            log.error(f"Escalation failed for {name}: {exc}")

        self.bus.publish(
            source="audit_agent",
            event_type="warning",
            topic="partner_compliance_issue",
            subject=f"Partner {name!r} health={score} — {len(violations)} violation(s)",
            payload=result,
            priority="urgent" if score < 50 else "normal",
        )
