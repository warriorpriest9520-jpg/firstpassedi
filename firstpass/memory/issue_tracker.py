"""
issue_tracker.py — Cross-agent issue and incident lifecycle management.

An *issue* is a multi-agent problem that no single agent can resolve alone, e.g.:
  - "Big Box Retail went live on Logicbroker — reconcile invoices/shipments/pricing"
  - "Partner says orders arrive too slowly — tune integration polling speed"

Issues carry an optional *playbook*: an ordered list of steps, each owned by an
agent.  ``advance_all()`` runs the next runnable step of every open issue on each
call, records findings, and pauses at ``confirm=True`` steps until approved.

For simpler one-shot incidents, use ``open_issue()`` without a playbook.

Storage: local ``issues.json`` (authoritative, survives offline) with best-effort
mirror to Supabase ``edi_incidents`` / ``escalations`` tables.  All activity also
logs to ``work_events`` so it surfaces in briefings and the dashboard.

Bus events published: issue.opened / issue.finding / issue.awaiting_approval /
issue.resolved

Source lineage: issue_manager.py (ceo-bot)

Usage::

    from firstpass.memory.issue_tracker import IssueTracker

    tracker = IssueTracker()
    tracker.open_issue(title="Big Box Retail reconciliation",
                       partner="Big Box Retail",
                       playbook="partner_reconciliation",
                       opened_by="edi_agent")
    tracker.advance_all()
    tracker.approve(issue_id, "user")
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("firstpass.memory.issue_tracker")

ISSUES_FILE = Path(__file__).parent / "issues.json"

# Optional integrations — degrade gracefully if unavailable
try:
    from firstpass.memory.supabase_client import (
        get_client as _sb_client,
        log_work_event as _log_work,
    )
except Exception:  # pragma: no cover
    _sb_client = None  # type: ignore[assignment]
    _log_work = None   # type: ignore[assignment]

try:
    from firstpass.utils.message_bus import MessageBus as _BusClass
    _bus: Optional[_BusClass] = _BusClass()
except Exception:  # pragma: no cover
    _bus = None

VALID_STATUSES = ("open", "in_progress", "awaiting_approval", "resolved", "abandoned")

# Auto-abandon after this many consecutive failures on the same step
MAX_CONSECUTIVE_FAILURES = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IssueTracker:
    """Track and advance EDI incidents and multi-step issues through their lifecycle."""

    def __init__(self, issues_file: Path = ISSUES_FILE):
        self.issues_file = Path(issues_file)

    # ------------------------------------------------------------------ I/O

    def _load(self) -> List[Dict[str, Any]]:
        if self.issues_file.exists():
            try:
                return json.loads(
                    self.issues_file.read_text(encoding="utf-8")
                ).get("issues", [])
            except Exception as e:
                # Never treat an unreadable store as empty — back up and fail loud.
                backup = self.issues_file.with_suffix(
                    f".corrupt-{datetime.now():%Y%m%d_%H%M%S}.json"
                )
                try:
                    backup.write_bytes(self.issues_file.read_bytes())
                except Exception:
                    pass
                raise RuntimeError(
                    f"issues.json unreadable ({e}) — backed up to {backup.name}; "
                    "refusing to proceed so existing issues are not overwritten"
                ) from e
        return []

    def _save(self, issues: List[Dict[str, Any]]) -> None:
        self.issues_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.issues_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"_updated": _now(), "issues": issues}, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(self.issues_file)

    def _mirror(self, issue: Dict[str, Any]) -> None:
        """Best-effort upsert to Supabase issues table."""
        if not _sb_client:
            return
        try:
            sb = _sb_client()
            if not sb:
                return
            # Route to appropriate table based on issue_type
            issue_type = issue.get("issue_type", "operational")
            table = "edi_incidents" if issue_type == "edi_incident" else "escalations"
            row = {k: issue.get(k) for k in (
                "id", "title", "partner", "issue_type", "status", "severity",
                "playbook", "playbook_step", "next_action", "opened_by",
                "created_at", "updated_at", "resolved_at",
            )}
            row["findings"] = json.dumps(issue.get("findings", []), default=str)
            row["assigned_agents"] = json.dumps(issue.get("assigned_agents", []))
            row["resolved"] = issue.get("status") == "resolved"
            sb.table(table).upsert(row, on_conflict="id").execute()
        except Exception as e:
            logger.debug(f"[issue_tracker] supabase mirror failed (non-fatal): {e}")

    def _publish(self, topic: str, issue: Dict[str, Any], extra: Dict = None) -> None:
        if not _bus:
            return
        try:
            _bus.publish(
                source="issue_tracker",
                event_type="status",
                topic=topic,
                subject=f"[{issue.get('partner') or 'general'}] {issue['title']}",
                payload={
                    "issue_id": issue["id"],
                    "status": issue["status"],
                    "next_action": issue.get("next_action"),
                    **(extra or {}),
                },
                priority="urgent" if issue.get("severity") == "critical" else "normal",
            )
        except Exception as e:
            logger.debug(f"[issue_tracker] bus publish failed (non-fatal): {e}")

    def _log(self, issue: Dict[str, Any], summary: str) -> None:
        if _log_work:
            try:
                _log_work(
                    bot_name="issue_tracker",
                    event_type="issue",
                    summary=summary,
                    payload={
                        "issue_id": issue["id"],
                        "partner": issue.get("partner"),
                        "status": issue["status"],
                        "step": issue.get("playbook_step"),
                    },
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ Public API

    def open_issue(
        self,
        title: str,
        playbook: str = None,
        partner: str = None,
        issue_type: str = "operational",
        severity: str = "normal",
        opened_by: str = "system",
        context: Dict = None,
        # Convenience fields (mirrors placeholder IssueTracker interface)
        detail: str = "",
        source: str = "",
        tags: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Open a new issue.

        Deduplicates: if an open issue with the same ``playbook`` + ``partner``
        already exists, returns the existing issue unchanged.

        :param title:      Short human-readable title.
        :param playbook:   Name of the playbook to execute (optional).
        :param partner:    Trading partner name (optional).
        :param issue_type: ``"operational"`` | ``"edi_incident"`` | ``"escalation"`` | …
        :param severity:   ``"low"`` | ``"normal"`` | ``"high"`` | ``"critical"``
        :param opened_by:  Agent or user that opened this issue.
        :param context:    Arbitrary context dict carried through playbook steps.
        :param detail:     Longer description (stored in context).
        :param source:     Source system or agent.
        :param tags:       Optional tag dict.
        :returns:          Full issue dict (use ``issue["id"]`` for the ID).
        """
        issues = self._load()
        # Dedupe: same playbook+partner still open → return existing
        if playbook:
            for i in issues:
                if (
                    i["status"] in ("open", "in_progress", "awaiting_approval")
                    and i.get("playbook") == playbook
                    and i.get("partner") == partner
                ):
                    logger.info(f"[issue_tracker] duplicate suppressed → {i['id']}")
                    return i

        issue: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "title": title[:500],
            "partner": partner,
            "issue_type": issue_type,
            "severity": severity,
            "status": "open",
            "playbook": playbook,
            "playbook_step": 0,
            "assigned_agents": [],
            "findings": [],
            "context": {
                **(context or {}),
                "detail": detail,
                "source": source or opened_by,
                "tags": tags or {},
            },
            "next_action": None,
            "opened_by": opened_by,
            "created_at": _now(),
            "updated_at": _now(),
            "resolved_at": None,
        }
        issues.append(issue)
        self._save(issues)
        self._mirror(issue)
        self._publish("issue.opened", issue)
        self._log(issue, f"Issue opened: {title}")
        return issue

    def list_issues(self, status: str = None) -> List[Dict[str, Any]]:
        """List issues, optionally filtered by status."""
        issues = self._load()
        if status:
            issues = [i for i in issues if i["status"] == status]
        return issues

    def get_issue(self, issue_id: str) -> Optional[Dict[str, Any]]:
        """Get a single issue by ID."""
        return next((i for i in self._load() if i["id"] == issue_id), None)

    def get_open_issues(
        self,
        issue_type: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List open issues, optionally filtered by type and severity."""
        issues = self._load()
        open_issues = [
            i for i in issues
            if i["status"] not in ("resolved", "abandoned")
            and (issue_type is None or i.get("issue_type") == issue_type)
            and (severity is None or i.get("severity") == severity)
        ]
        return sorted(open_issues, key=lambda x: x.get("created_at") or "", reverse=True)[:limit]

    def add_finding(
        self, issue_id: str, agent: str, note: str, data: Dict = None
    ) -> None:
        """Record a finding / observation for an issue."""
        issues = self._load()
        for i in issues:
            if i["id"] == issue_id:
                i["findings"].append(
                    {"ts": _now(), "agent": agent, "note": note, "data": data or {}}
                )
                if agent not in i["assigned_agents"]:
                    i["assigned_agents"].append(agent)
                i["updated_at"] = _now()
                self._save(issues)
                self._mirror(i)
                self._publish("issue.finding", i, {"agent": agent, "note": note[:300]})
                self._log(i, f"Finding from {agent}: {note[:120]}")
                return

    def approve(self, issue_id: str, approved_by: str = "user") -> bool:
        """Approve the pending CONFIRM step — issue resumes."""
        issues = self._load()
        for i in issues:
            if i["id"] == issue_id and i["status"] == "awaiting_approval":
                i["status"] = "in_progress"
                i["context"]["approved_step"] = i["playbook_step"]
                i["context"]["approved_by"] = approved_by
                i["updated_at"] = _now()
                self._save(issues)
                self._mirror(i)
                self._log(i, f"Step {i['playbook_step']} approved by {approved_by}")
                return True
        return False

    def update_issue(self, issue_id: str, status: str, note: str = "") -> bool:
        """Update issue status.  Valid: open | in_progress | resolved | abandoned."""
        valid = {"open", "in_progress", "awaiting_approval", "resolved", "abandoned"}
        if status not in valid:
            logger.warning(f"[issue_tracker] Invalid status: {status!r}")
            return False
        issues = self._load()
        for i in issues:
            if i["id"] == issue_id:
                i["status"] = status
                i["updated_at"] = _now()
                if status == "resolved":
                    i["resolved_at"] = _now()
                if note:
                    i["findings"].append(
                        {"ts": _now(), "agent": "user", "note": note, "data": {}}
                    )
                self._save(issues)
                self._mirror(i)
                self._log(i, f"Status → {status}" + (f": {note[:80]}" if note else ""))
                return True
        return False

    def resolve(self, issue_id: str, note: str = "") -> bool:
        """Shorthand: resolve an issue."""
        return self.resolve_issue(issue_id, note)

    def resolve_issue(
        self, issue_id: str, resolution: str, resolved_by: str = "system"
    ) -> bool:
        """Resolve an issue with a resolution summary."""
        issues = self._load()
        for i in issues:
            if i["id"] == issue_id:
                i["status"] = "resolved"
                i["resolved_at"] = _now()
                i["updated_at"] = _now()
                i["findings"].append(
                    {
                        "ts": _now(),
                        "agent": resolved_by,
                        "note": f"RESOLVED: {resolution}",
                        "data": {},
                    }
                )
                self._save(issues)
                self._mirror(i)
                self._publish("issue.resolved", i)
                self._log(i, f"Issue resolved: {resolution[:120]}")
                return True
        return False

    # ------------------------------------------------------------------ Execution

    def advance(self, issue_id: str) -> Dict[str, Any]:
        """Run the next playbook step for one issue.  Returns step result."""
        # Late import avoids circular dependencies and handles missing module
        try:
            from firstpass.intelligence.playbooks import get_playbook
        except ImportError:
            def get_playbook(name):  # type: ignore[misc]
                return None

        issues = self._load()
        issue = next((i for i in issues if i["id"] == issue_id), None)
        if not issue:
            return {"ok": False, "error": "not found"}
        if issue["status"] in ("resolved", "abandoned"):
            return {"ok": True, "done": True}
        if issue["status"] == "awaiting_approval":
            return {
                "ok": True,
                "waiting": True,
                "next_action": issue.get("next_action"),
            }

        pb = get_playbook(issue.get("playbook"))
        if not pb:
            issue["next_action"] = (
                f"No playbook named '{issue.get('playbook')}' — cannot advance"
            )
            self._save(issues)
            return {"ok": False, "error": issue["next_action"]}

        steps = pb["steps"]
        idx = issue["playbook_step"]
        if idx >= len(steps):
            self.resolve_issue(issue_id, "All playbook steps complete — auto-resolved.")
            return {"ok": True, "done": True}

        step = steps[idx]

        # Stuck-loop breaker
        recent_findings = [
            f for f in issue.get("findings", [])
            if f.get("data", {}).get("ok") is False
        ]
        if len(recent_findings) >= MAX_CONSECUTIVE_FAILURES:
            last_n = issue.get("findings", [])[-MAX_CONSECUTIVE_FAILURES:]
            all_same_step = (
                all(not f.get("data", {}).get("ok", True) for f in last_n)
                and issue["playbook_step"] == idx
            )
            if all_same_step:
                reason = (
                    f"Auto-abandoned: step {idx + 1} ({step['name']}) failed "
                    f"{MAX_CONSECUTIVE_FAILURES} consecutive times. "
                    f"Last error: {last_n[-1].get('note', '?')[:200]}"
                )
                issue["status"] = "abandoned"
                issue["next_action"] = reason
                issue["updated_at"] = _now()
                issue["findings"].append(
                    {"ts": _now(), "agent": "issue_tracker", "note": reason, "data": {}}
                )
                self._save(issues)
                self._mirror(issue)
                self._publish("issue.resolved", issue)
                self._log(issue, reason)
                return {"ok": False, "abandoned": True, "error": reason}

        # CONFIRM gate — pause for approval unless already approved
        if step.get("confirm") and issue["context"].get("approved_step") != idx:
            issue["status"] = "awaiting_approval"
            issue["next_action"] = (
                f"APPROVAL NEEDED — step {idx + 1}/{len(steps)}: "
                f"{step['name']} ({step.get('agent', step.get('bot', '?'))})"
            )
            issue["updated_at"] = _now()
            self._save(issues)
            self._mirror(issue)
            self._publish("issue.awaiting_approval", issue)
            self._log(issue, issue["next_action"])
            return {"ok": True, "waiting": True, "next_action": issue["next_action"]}

        # Run the step
        issue["status"] = "in_progress"
        self._save(issues)
        try:
            result = step["run"](issue)
            ok = bool(result.get("ok", True))
            note = result.get("note", step["name"] + (" — ok" if ok else " — FAILED"))
        except Exception as e:
            ok = False
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            note = f"{step['name']} — EXCEPTION: {e}"

        self.add_finding(issue_id, step.get("agent", step.get("bot", "unknown")), note, result)

        issues = self._load()
        issue = next(i for i in issues if i["id"] == issue_id)
        if ok:
            issue["playbook_step"] = idx + 1
            nxt = steps[idx + 1]["name"] if idx + 1 < len(steps) else "done"
            issue["next_action"] = f"Next: {nxt}"
        else:
            issue["next_action"] = (
                f"STUCK at step {idx + 1}: {step['name']} — needs attention"
            )
            issue["severity"] = "high"
        issue["updated_at"] = _now()
        self._save(issues)
        self._mirror(issue)
        return {"ok": ok, "step": step["name"], "result": result}

    def advance_all(self, max_steps_per_issue: int = 1) -> List[Dict[str, Any]]:
        """Advance every open issue.  Called by the orchestrator scheduler."""
        out = []
        for issue in self.list_issues():
            if issue["status"] in ("open", "in_progress"):
                for _ in range(max_steps_per_issue):
                    r = self.advance(issue["id"])
                    out.append({"issue": issue["title"], **r})
                    if not r.get("ok") or r.get("waiting") or r.get("done"):
                        break
        return out


# Backwards-compatibility alias
IssueManager = IssueTracker


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="FirstPass EDI issue tracker")
    p.add_argument("command", choices=["list", "advance", "approve", "resolve", "open"])
    p.add_argument("--id", help="issue id")
    p.add_argument("--title")
    p.add_argument("--partner")
    p.add_argument("--playbook")
    p.add_argument("--note", default="resolved manually")
    a = p.parse_args()

    tracker = IssueTracker()
    if a.command == "list":
        for i in tracker.list_issues():
            print(
                f"{i['id']}  [{i['status']:>18}]  step {i.get('playbook_step', '-')}  {i['title']}"
                f"\n{'':21}→ {i.get('next_action') or '-'}"
            )
    elif a.command == "advance":
        results = [tracker.advance(a.id)] if a.id else tracker.advance_all()
        print(json.dumps(results, indent=2, default=str))
    elif a.command == "approve":
        print("approved" if tracker.approve(a.id) else "nothing awaiting approval")
    elif a.command == "resolve":
        print("resolved" if tracker.resolve_issue(a.id, a.note) else "not found")
    elif a.command == "open":
        i = tracker.open_issue(
            title=a.title, partner=a.partner, playbook=a.playbook, opened_by="cli"
        )
        print(f"opened {i['id']}")
