"""
escalation.py — Escalation manager for FirstPass EDI.

Routes critical findings to human operators via:
  1. Supabase escalations table (persistent)
  2. Discord webhook notification
  3. (Optional) Email alert via SMTP

Escalation levels (from the Autonomous Department spec):
  - low      → log only
  - medium   → Supabase + Discord (quiet hours aware)
  - high     → immediate Discord + Supabase
  - critical → all channels, bypass quiet hours

Source lineage: escalation_manager.py (ceo-bot)
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import config

log = logging.getLogger("firstpass.safety.escalation")

QUIET_HOURS_START = 23  # 11 PM
QUIET_HOURS_END = 8     # 8 AM


class EscalationManager:
    """Route critical findings to human operators."""

    def escalate(
        self,
        summary: str,
        detail: str = "",
        severity: str = "medium",
        source: str = "system",
        tags: Optional[Dict] = None,
    ) -> str:
        """
        Create a new escalation.

        :param severity: "low" | "medium" | "high" | "critical"
        :returns: Escalation ID
        """
        escalation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "id": escalation_id,
            "summary": summary[:500],
            "detail": detail[:2000],
            "severity": severity,
            "source": source,
            "tags": tags or {},
            "resolved": False,
            "created_at": now,
            "updated_at": now,
        }

        # Always persist to Supabase
        self._persist(row)

        # Notify based on severity + quiet hours
        if severity == "critical":
            self._discord(f"🚨 **CRITICAL**: {summary}")
        elif severity == "high":
            self._discord(f"⚠️ **HIGH**: {summary}")
        elif severity == "medium" and not self._quiet_hours():
            self._discord(f"📋 **MEDIUM**: {summary}")
        # low: log only, no notification

        log.warning(f"[escalation] [{severity.upper()}] {summary} (id={escalation_id})")
        return escalation_id

    def resolve(self, escalation_id: str, note: str = "", resolved_by: str = "system") -> bool:
        """Mark an escalation as resolved."""
        sb = self._sb()
        if not sb:
            return False
        try:
            sb.table("escalations").update({
                "resolved": True,
                "resolution_note": note,
                "resolved_by": resolved_by,
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", escalation_id).execute()
            log.info(f"Escalation resolved: {escalation_id}")
            return True
        except Exception as exc:
            log.error(f"resolve({escalation_id}) failed: {exc}")
            return False

    def get_open_escalations(self, severity: Optional[str] = None) -> List[Dict]:
        """List all open (unresolved) escalations."""
        sb = self._sb()
        if not sb:
            return []
        try:
            q = (
                sb.table("escalations")
                  .select("*")
                  .eq("resolved", False)
                  .order("created_at", desc=True)
                  .limit(50)
            )
            if severity:
                q = q.eq("severity", severity)
            return q.execute().data or []
        except Exception as exc:
            log.error(f"get_open_escalations failed: {exc}")
            return []

    # ── Internal ──────────────────────────────────────────────────────────

    def _persist(self, row: Dict) -> None:
        sb = self._sb()
        if not sb:
            return
        try:
            sb.table("escalations").insert(row).execute()
        except Exception as exc:
            log.error(f"escalation persist failed: {exc}")

    def _discord(self, message: str) -> None:
        import urllib.request
        webhook = config.DISCORD_WEBHOOK_URL
        if not webhook:
            return
        try:
            data = json.dumps({"content": message}).encode()
            req = urllib.request.Request(
                webhook, data=data,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            log.debug(f"Discord escalation alert failed: {exc}")

    @staticmethod
    def _quiet_hours() -> bool:
        hour = datetime.now().hour
        return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END

    @staticmethod
    def _sb():
        from ..memory.supabase_client import get_client
        return get_client()
