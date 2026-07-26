"""
issue_tracker.py — Persistent issue/incident tracking.

Manages the lifecycle of EDI incidents and escalations:
  open → investigating → resolved

Backed by Supabase when configured; uses an in-memory list otherwise.

Source lineage: issue_manager.py (ceo-bot)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("firstpass.memory.issue_tracker")


class IssueTracker:
    """Track EDI incidents and escalations through their lifecycle."""

    def __init__(self):
        self._in_memory: List[Dict] = []  # fallback when Supabase not available

    # ── Create ────────────────────────────────────────────────────────────

    def open_issue(
        self,
        summary: str,
        detail: str = "",
        severity: str = "medium",
        issue_type: str = "edi_incident",
        source: str = "system",
        partner: Optional[str] = None,
        tags: Optional[Dict] = None,
    ) -> str:
        """
        Open a new issue.

        :param severity: "low" | "medium" | "high" | "critical"
        :param issue_type: "edi_incident" | "escalation" | "partner_audit" | "system"
        :returns: Issue ID string
        """
        issue_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "id": issue_id,
            "summary": summary[:500],
            "detail": detail[:2000],
            "severity": severity,
            "issue_type": issue_type,
            "source": source,
            "partner": partner,
            "tags": tags or {},
            "status": "open",
            "resolved": False,
            "created_at": now,
            "updated_at": now,
        }
        sb = self._sb()
        table = "edi_incidents" if issue_type == "edi_incident" else "escalations"
        if sb:
            try:
                sb.table(table).insert(row).execute()
                log.info(f"Issue opened: {issue_id} [{severity}] {summary[:60]}")
                return issue_id
            except Exception as exc:
                log.error(f"open_issue DB insert failed: {exc}")
        self._in_memory.append(row)
        return issue_id

    # ── Update ────────────────────────────────────────────────────────────

    def update_issue(self, issue_id: str, status: str, note: str = "") -> bool:
        """Update issue status. Valid: open | investigating | resolved."""
        valid = {"open", "investigating", "resolved"}
        if status not in valid:
            log.warning(f"Invalid status: {status!r}")
            return False
        now = datetime.now(timezone.utc).isoformat()
        sb = self._sb()
        if sb:
            for table in ("edi_incidents", "escalations"):
                try:
                    update = {
                        "status": status,
                        "updated_at": now,
                        "resolved": status == "resolved",
                    }
                    if note:
                        update["resolution_note"] = note
                    if status == "resolved":
                        update["resolved_at"] = now
                    result = sb.table(table).update(update).eq("id", issue_id).execute()
                    if result.data:
                        return True
                except Exception as exc:
                    log.debug(f"update_issue table={table} failed: {exc}")
        # In-memory
        for issue in self._in_memory:
            if issue["id"] == issue_id:
                issue.update({"status": status, "updated_at": now,
                              "resolved": status == "resolved"})
                return True
        return False

    def resolve(self, issue_id: str, note: str = "") -> bool:
        """Shorthand: resolve an issue."""
        return self.update_issue(issue_id, "resolved", note)

    # ── Query ─────────────────────────────────────────────────────────────

    def get_open_issues(
        self,
        issue_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """List open issues, optionally filtered by type and severity."""
        sb = self._sb()
        if sb:
            issues = []
            for table in ("edi_incidents", "escalations"):
                try:
                    q = (
                        sb.table(table)
                          .select("*")
                          .eq("resolved", False)
                          .order("created_at", desc=True)
                          .limit(limit)
                    )
                    if severity:
                        q = q.eq("severity", severity)
                    result = q.execute()
                    for row in (result.data or []):
                        if issue_type is None or row.get("issue_type") == issue_type:
                            issues.append(row)
                except Exception as exc:
                    log.debug(f"get_open_issues table={table} failed: {exc}")
            return sorted(issues, key=lambda x: x.get("created_at") or "", reverse=True)[:limit]
        # In-memory
        return [
            i for i in self._in_memory
            if not i.get("resolved")
            and (issue_type is None or i.get("issue_type") == issue_type)
            and (severity is None or i.get("severity") == severity)
        ][:limit]

    def get_issue(self, issue_id: str) -> Optional[Dict]:
        """Get a single issue by ID."""
        sb = self._sb()
        if sb:
            for table in ("edi_incidents", "escalations"):
                try:
                    result = (
                        sb.table(table).select("*").eq("id", issue_id).limit(1).execute()
                    )
                    if result.data:
                        return result.data[0]
                except Exception:
                    pass
        return next((i for i in self._in_memory if i["id"] == issue_id), None)

    @staticmethod
    def _sb():
        from ..memory.supabase_client import get_client
        return get_client()
