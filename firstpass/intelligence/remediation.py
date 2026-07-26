"""
remediation.py — Closed-loop auto-remediation for EDI and workflow failures.

When the watchdog detects a problem, don't just alert — attempt to fix it.

Rules:
  1. Missing 856 ASN        — 850 received, no 856 sent within 24 h (half 48 h SLA)
  2. Silent EDI workflow    — active profile, no callback in alert_if_no_activity_hours
  3. Repeated validation    — same partner + field failed 3+ times

Wire into the orchestrator by calling RemediationEngine().run_check() each cycle.

Configuration (env vars):
  FIRSTPASS_CALLBACK_URL      — Webhook to trigger workflow retries
  FIRSTPASS_DATA_DIR          — Directory containing watchdog state files
  DISCORD_WEBHOOK_URL         — Discord channel for alerts
  SLACK_WEBHOOK_URL           — Slack channel for alerts (alternative)
  FIRSTPASS_MAX_AUTO_ATTEMPTS — Max auto-remediation attempts before escalation (default: 2)

Source lineage: ceo-bot/remediation_engine.py — ported for FirstPass EDI.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import config

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

logger = logging.getLogger("firstpass.intelligence.remediation")

# ── Data directory ─────────────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("FIRSTPASS_DATA_DIR", str(Path(__file__).parent.parent.parent / "data")))

WATCHDOG_STATE_FILE = DATA_DIR / "watchdog_state.json"
UNIVERSAL_STATE_FILE = DATA_DIR / "watchdog_universal_state.json"
PROFILES_FILE = DATA_DIR / "watchdog_profiles.json"
REMEDIATION_LOG_FILE = DATA_DIR / "logs" / "remediation_log.json"

# Configurable via env — override with your workflow callback URL
CALLBACK_URL: str = os.getenv("FIRSTPASS_CALLBACK_URL", "")

# Escalate to human after this many auto-attempts per PO
MAX_AUTO_REMEDIATIONS: int = int(os.getenv("FIRSTPASS_MAX_AUTO_ATTEMPTS", "2"))


class RemediationEngine:
    """
    Check watchdog state and fire automated remediation actions.

    Usage::

        engine = RemediationEngine()
        actions = engine.run_check()
        # actions → list of {"rule": ..., "partner": ..., ...}

    State files are read from DATA_DIR (env: FIRSTPASS_DATA_DIR).
    """

    def __init__(
        self,
        watchdog_state_file: Optional[Path] = None,
        universal_state_file: Optional[Path] = None,
        profiles_file: Optional[Path] = None,
        remediation_log_file: Optional[Path] = None,
    ):
        self._watchdog_path = watchdog_state_file or WATCHDOG_STATE_FILE
        self._universal_path = universal_state_file or UNIVERSAL_STATE_FILE
        self._profiles_path = profiles_file or PROFILES_FILE
        self._log_path = remediation_log_file or REMEDIATION_LOG_FILE

        self.watchdog_state: Dict[str, Any] = self._load_watchdog_state()
        self.universal_state: Dict[str, Any] = self._load_universal_state()
        self.profiles: Dict[str, Any] = self._load_profiles()
        self.remediation_log: Dict[str, int] = self._load_remediation_log()

        # Notification targets from centralised config
        self.discord_url: str = config.DISCORD_WEBHOOK_URL
        self.slack_url: str = config.SLACK_WEBHOOK_URL

    # ── Public API ─────────────────────────────────────────────────────────────

    def run_check(self) -> List[Dict[str, Any]]:
        """Run all remediation rules. Returns list of action dicts taken."""
        actions: List[Dict[str, Any]] = []
        actions += self._check_missing_856()
        actions += self._check_silent_workflows()
        actions += self._check_repeated_failures()
        if actions:
            self._save_remediation_log()
            logger.info(f"RemediationEngine fired {len(actions)} action(s)")
        return actions

    # ── Rule 1 — Missing 856 ASN ───────────────────────────────────────────────

    def _check_missing_856(self) -> List[Dict[str, Any]]:
        """Rule 1: 850 received but no 856 sent within 24 h (half the 48 h SLA)."""
        actions = []
        sla_timers: Dict[str, Any] = self.universal_state.get("sla_timers", {})

        for timer_key, timer in sla_timers.items():
            if not isinstance(timer, dict):
                continue
            doc_type = timer.get("doc_type", "")
            if doc_type != "856":
                continue

            triggered_at_str = timer.get("triggered_at") or timer.get("started_at", "")
            if not triggered_at_str:
                continue

            try:
                triggered_at = datetime.fromisoformat(triggered_at_str)
                if triggered_at.tzinfo is None:
                    triggered_at = triggered_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            age_hours = (datetime.now(timezone.utc) - triggered_at).total_seconds() / 3600
            if age_hours < 24:
                continue  # Not yet at half-SLA

            partner = timer.get("partner", timer.get("customer", "unknown"))
            po_number = timer.get("po_number", timer_key)
            action_key = f"{partner}_{po_number}"
            attempt_count = self.remediation_log.get(action_key, 0)

            if attempt_count >= MAX_AUTO_REMEDIATIONS:
                msg = (
                    f"🆘 **Human Escalation Required** — {partner.upper()} PO `{po_number}`\n"
                    f"856 ASN not sent after {age_hours:.0f}h. Auto-remediation attempted "
                    f"{attempt_count}x — manual intervention needed."
                )
                self._alert(msg)
                actions.append({
                    "rule": "missing_856_escalation",
                    "partner": partner,
                    "po_number": po_number,
                    "attempts": attempt_count,
                })
                continue

            # Attempt auto-remediation
            self._trigger_workflow_retry(partner, po_number, doc_type)
            msg = (
                f"⚡ **Auto-remediation**: triggering 856 re-check for "
                f"{partner.upper()} PO `{po_number}` (age: {age_hours:.0f}h, "
                f"attempt #{attempt_count + 1})"
            )
            self._alert(msg)
            self._log_incident({
                "type": "auto_remediation",
                "partner": partner,
                "po_number": po_number,
                "doc_type": "856",
                "age_hours": round(age_hours, 1),
                "attempt": attempt_count + 1,
            })
            self.remediation_log[action_key] = attempt_count + 1
            actions.append({
                "rule": "missing_856",
                "partner": partner,
                "po_number": po_number,
                "attempt": attempt_count + 1,
            })

        return actions

    # ── Rule 2 — Silent EDI workflow ───────────────────────────────────────────

    def _check_silent_workflows(self) -> List[Dict[str, Any]]:
        """Rule 2: Active profile with no callback in alert_if_no_activity_hours."""
        actions = []
        profiles = self.profiles.get("profiles", {})
        last_activity: Dict[str, Any] = self.universal_state.get("last_activity", {})

        for profile_key, profile in profiles.items():
            if not profile.get("enabled", True):
                continue
            if profile.get("needs_config"):
                continue

            partner = profile.get("partner", profile.get("customer", "unknown"))
            doc_type = profile.get("doc_type", "")
            silence_hours = profile.get("alert_if_no_activity_hours", 48)

            activity_key = profile_key
            last_ts_str = (
                last_activity.get(activity_key)
                or last_activity.get(f"{partner}_{doc_type}")
            )

            if not last_ts_str:
                continue

            try:
                last_ts = datetime.fromisoformat(str(last_ts_str))
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            age_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
            if age_hours < silence_hours:
                continue

            # Check cooldown via shared context (Supabase-backed)
            cooldown_key = f"edi_silent_{partner}_{doc_type}"
            try:
                from firstpass.memory.supabase_client import get_shared_context
                existing = get_shared_context(cooldown_key)
                if existing and isinstance(existing, dict):
                    alerted_at_str = existing.get("timestamp", "")
                    if alerted_at_str:
                        alerted_at = datetime.fromisoformat(alerted_at_str)
                        if alerted_at.tzinfo is None:
                            alerted_at = alerted_at.replace(tzinfo=timezone.utc)
                        hours_since_alert = (
                            datetime.now(timezone.utc) - alerted_at
                        ).total_seconds() / 3600
                        if hours_since_alert < 12:
                            continue
            except Exception:
                pass

            msg = (
                f"🔇 **{partner.upper()}** `{doc_type}` workflow appears silent — "
                f"last activity {age_hours:.0f}h ago. "
                f"Check workflow is enabled."
            )
            self._alert(msg)

            # Write cooldown to shared context
            try:
                from firstpass.memory.supabase_client import set_shared_context
                set_shared_context(
                    cooldown_key,
                    {"timestamp": datetime.now(timezone.utc).isoformat(), "age_hours": round(age_hours, 1)},
                    source="RemediationEngine",
                    ttl_hours=12,
                )
            except Exception:
                pass

            actions.append({
                "rule": "silent_workflow",
                "partner": partner,
                "doc_type": doc_type,
                "age_hours": round(age_hours, 1),
            })

        return actions

    # ── Rule 3 — Repeated validation failure ───────────────────────────────────

    def _check_repeated_failures(self) -> List[Dict[str, Any]]:
        """Rule 3: Same partner + same validation error field seen 3+ times."""
        actions = []
        workflow_events = self.watchdog_state.get("workflow_events", [])

        # Aggregate: (partner, field) → count
        failure_counts: Dict[str, int] = {}
        for event in workflow_events:
            if not isinstance(event, dict):
                continue
            if event.get("status") != "error":
                continue
            partner = event.get("partner", event.get("customer", ""))
            val_errors = event.get("validation_errors", [])
            if isinstance(val_errors, list):
                for err in val_errors:
                    field = err if isinstance(err, str) else err.get("field", str(err))
                    key = f"{partner}||{field}"
                    failure_counts[key] = failure_counts.get(key, 0) + 1
            elif isinstance(val_errors, dict):
                for field in val_errors:
                    key = f"{partner}||{field}"
                    failure_counts[key] = failure_counts.get(key, 0) + 1

        for key, count in failure_counts.items():
            if count < 3:
                continue
            partner, field = key.split("||", 1)
            cooldown_log_key = f"repeated_failure_{partner}_{field}"
            if self.remediation_log.get(cooldown_log_key, 0) >= count:
                continue  # Already alerted for this count

            msg = (
                f"🔁 **Repeated validation error**: `{partner}` field `{field}` "
                f"has failed **{count}** times. Consider updating the EDI spec rule."
            )
            self._alert(msg)

            # Publish discovery to message bus for morning briefing
            try:
                from firstpass.utils.message_bus import MessageBus
                bus = MessageBus()
                bus.publish(
                    "RemediationEngine",
                    "discovery",
                    "edi_spec_insight",
                    f"Repeated EDI validation failure: {partner} / {field} ({count}x)",
                    payload={"partner": partner, "field": field, "count": count},
                    priority="normal",
                )
            except Exception as e:
                logger.warning(f"Bus publish failed: {e}")

            self.remediation_log[cooldown_log_key] = count
            actions.append({
                "rule": "repeated_validation_failure",
                "partner": partner,
                "field": field,
                "count": count,
            })

        return actions

    # ── Workflow retry ─────────────────────────────────────────────────────────

    def _trigger_workflow_retry(self, partner: str, po_number: str, doc_type: str):
        """POST a retry request to the configured callback URL."""
        if not CALLBACK_URL:
            logger.warning("FIRSTPASS_CALLBACK_URL not set — cannot POST workflow retry")
            return
        if not _HAS_HTTPX:
            logger.warning("httpx not available — cannot POST workflow retry")
            return
        api_key = config.API_KEY
        try:
            resp = httpx.post(
                CALLBACK_URL,
                json={
                    "partner": partner,
                    "doc_type": doc_type,
                    "po_number": po_number,
                    "status": "retry",
                    "error_msg": "Auto-remediation: triggering re-check",
                    "payload": {"auto_remediation": True},
                },
                headers={"X-API-Key": api_key} if api_key else {},
                timeout=10,
            )
            logger.info(f"Workflow retry POST → {resp.status_code}")
        except Exception as e:
            logger.warning(f"Workflow retry failed: {e}")

    # ── Alerts ─────────────────────────────────────────────────────────────────

    def _alert(self, msg: str):
        logger.info(f"[Remediation] {msg}")
        if not _HAS_HTTPX:
            return
        # Try Discord first, then Slack
        for url in filter(None, [self.discord_url, self.slack_url]):
            try:
                resp = httpx.post(url, json={"content": msg}, timeout=10)
                if resp.status_code in (200, 204):
                    return
                logger.warning(f"Webhook returned {resp.status_code}")
            except Exception as e:
                logger.error(f"Webhook alert failed: {e}")

    def _log_incident(self, action: Dict[str, Any]):
        """Log a remediation incident to Supabase."""
        try:
            from firstpass.memory.supabase_client import get_client
            sb = get_client()
            if sb:
                sb.table("edi_incidents").insert({
                    "partner": action.get("partner"),
                    "doc_type": action.get("doc_type", ""),
                    "incident_type": action.get("type", "auto_remediation"),
                    "status": "auto_remediated",
                    "payload": json.dumps(action, default=str),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
        except Exception as e:
            logger.warning(f"Incident log failed: {e}")

    # ── State loading ──────────────────────────────────────────────────────────

    def _load_watchdog_state(self) -> Dict[str, Any]:
        try:
            if self._watchdog_path.exists():
                return json.loads(self._watchdog_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load watchdog_state.json: {e}")
        return {}

    def _load_universal_state(self) -> Dict[str, Any]:
        try:
            if self._universal_path.exists():
                return json.loads(self._universal_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load watchdog_universal_state.json: {e}")
        return {}

    def _load_profiles(self) -> Dict[str, Any]:
        try:
            if self._profiles_path.exists():
                return json.loads(self._profiles_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load watchdog_profiles.json: {e}")
        return {}

    def _load_remediation_log(self) -> Dict[str, int]:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_path.exists():
                return json.loads(self._log_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load remediation_log.json: {e}")
        return {}

    def _save_remediation_log(self):
        try:
            self._log_path.write_text(
                json.dumps(self.remediation_log, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"Could not save remediation_log.json: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [RemediationEngine] %(levelname)s %(message)s",
    )
    engine = RemediationEngine()
    actions = engine.run_check()
    print(f"\nRemediation complete — {len(actions)} action(s) taken")
    for a in actions:
        print(f"  {a}")
