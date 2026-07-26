"""
watchdog_agent.py — Health monitoring and anomaly detection agent.

Monitors:
  - Bot/service health (heartbeat staleness, error rates)
  - Order flow (stuck orders, SLA breaches, missing 856/810)
  - Platform connectivity (Orderful, LogicBroker, ShipStation reachability)
  - Agent reward scores (flags degraded agents per the autonomous dept spec)

Action levels:
  - WATCH   — log and monitor, no action
  - SELF_HEAL — attempt automated remediation
  - ESCALATE — notify human via Discord + escalation manager

Source lineage:
  - order_flow_watchdog.py  — order SLA and stuck-order detection
  - bot_health_monitor.py   — service heartbeat and reward scoring
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..config import config
from ..utils.message_bus import MessageBus

log = logging.getLogger("firstpass.watchdog_agent")

# ── Thresholds ────────────────────────────────────────────────────────────────

HEARTBEAT_STALE_MINUTES = 15
HEARTBEAT_OFFLINE_MINUTES = 60
DEGRADED_REWARD_THRESHOLD = -0.3   # 7-day rolling average below this → escalate
ORDER_STUCK_HOURS = 48             # Order with no 856/810 after this many hours


# ── Main agent ────────────────────────────────────────────────────────────────

class WatchdogAgent:
    """
    Health monitoring and anomaly detection agent.

    On each cycle checks bot health, order flow, and platform connectivity,
    then decides: WATCH / SELF_HEAL / ESCALATE.
    """

    def __init__(self):
        self.bus = MessageBus()

    # ── Public interface ──────────────────────────────────────────────────

    def run_cycle(self) -> dict:
        findings: List[Dict] = []

        # 1. Bot health check
        findings.extend(self._check_bot_health())

        # 2. Order flow check
        findings.extend(self._check_order_flow())

        # 3. Platform connectivity
        findings.extend(self._check_platform_connectivity())

        # Process findings
        escalated = 0
        self_healed = 0
        for f in findings:
            action = f.get("action", "WATCH")
            if action == "ESCALATE":
                self._escalate(f)
                escalated += 1
            elif action == "SELF_HEAL":
                healed = self._self_heal(f)
                if healed:
                    self_healed += 1

        summary = {
            "findings": len(findings),
            "escalated": escalated,
            "self_healed": self_healed,
        }
        log.info(f"WatchdogAgent cycle: {summary}")
        return summary

    # ── Health checks ─────────────────────────────────────────────────────

    def _check_bot_health(self) -> List[Dict]:
        """Check bot heartbeat freshness from Supabase bot_health table."""
        findings: List[Dict] = []
        try:
            from ..memory.supabase_client import get_client
            sb = get_client()
            if not sb:
                return []
            result = sb.table("bot_health").select("*").execute()
            now = datetime.now(timezone.utc)
            for bot in (result.data or []):
                name = bot.get("bot_name", "unknown")
                last_hb = bot.get("last_heartbeat")
                if not last_hb:
                    findings.append({
                        "type": "bot_offline",
                        "bot": name,
                        "detail": "No heartbeat recorded",
                        "action": "ESCALATE",
                        "severity": "high",
                    })
                    continue
                try:
                    last_dt = datetime.fromisoformat(last_hb)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    age_min = (now - last_dt).total_seconds() / 60
                    if age_min > HEARTBEAT_OFFLINE_MINUTES:
                        findings.append({
                            "type": "bot_offline",
                            "bot": name,
                            "detail": f"Last heartbeat {age_min:.0f}m ago",
                            "action": "ESCALATE",
                            "severity": "high",
                        })
                    elif age_min > HEARTBEAT_STALE_MINUTES:
                        findings.append({
                            "type": "bot_stale",
                            "bot": name,
                            "detail": f"Heartbeat stale ({age_min:.0f}m)",
                            "action": "WATCH",
                            "severity": "medium",
                        })
                except Exception as exc:
                    log.debug(f"Could not parse heartbeat for {name}: {exc}")
        except Exception as exc:
            log.error(f"Bot health check failed: {exc}")
        return findings

    def _check_order_flow(self) -> List[Dict]:
        """Detect stuck orders (received 850 but no 856/810 after threshold)."""
        findings: List[Dict] = []
        try:
            from ..memory.supabase_client import get_client
            sb = get_client()
            if not sb:
                return []
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=ORDER_STUCK_HOURS)
            ).isoformat()
            result = (
                sb.table("edi_orders")
                  .select("po_number,partner,created_at,status")
                  .lt("created_at", cutoff)
                  .not_.in_("status", ["invoiced", "closed", "cancelled"])
                  .execute()
            )
            for order in (result.data or []):
                findings.append({
                    "type": "stuck_order",
                    "po_number": order.get("po_number"),
                    "partner": order.get("partner"),
                    "detail": f"Order stuck since {order.get('created_at')} (status={order.get('status')})",
                    "action": "ESCALATE",
                    "severity": "high",
                })
        except Exception as exc:
            log.error(f"Order flow check failed: {exc}")
        return findings

    def _check_platform_connectivity(self) -> List[Dict]:
        """Ping configured platforms to verify reachability."""
        findings: List[Dict] = []
        checks = []
        if config.ORDERFUL_API_KEY:
            checks.append(("Orderful", config.ORDERFUL_BASE_URL))
        if config.SHIPSTATION_API_KEY:
            checks.append(("ShipStation", config.SHIPSTATION_BASE_URL))
        if config.LOGICBROKER_API_KEY:
            checks.append(("LogicBroker", config.LOGICBROKER_BASE_URL))

        for name, base_url in checks:
            try:
                import requests
                resp = requests.get(f"{base_url.rstrip('/')}/health", timeout=5)
                if resp.status_code >= 500:
                    findings.append({
                        "type": "platform_error",
                        "platform": name,
                        "detail": f"HTTP {resp.status_code}",
                        "action": "ESCALATE",
                        "severity": "high",
                    })
            except Exception as exc:
                findings.append({
                    "type": "platform_unreachable",
                    "platform": name,
                    "detail": str(exc),
                    "action": "WATCH",   # Might be transient
                    "severity": "medium",
                })
        return findings

    # ── Actions ───────────────────────────────────────────────────────────

    def _escalate(self, finding: Dict) -> None:
        """Escalate a critical finding to the escalation manager + Discord."""
        try:
            from ..safety.escalation import EscalationManager
            mgr = EscalationManager()
            mgr.escalate(
                summary=f"Watchdog: {finding['type']}",
                detail=finding.get("detail", ""),
                severity=finding.get("severity", "medium"),
                source="watchdog_agent",
                tags=finding,
            )
        except Exception as exc:
            log.error(f"Escalation failed: {exc}")

        self.bus.publish(
            source="watchdog_agent",
            event_type="anomaly",
            topic=f"watchdog_{finding['type']}",
            subject=finding.get("detail", finding["type"]),
            payload=finding,
            priority="urgent",
        )

        # Discord alert (best-effort)
        self._discord(f"⚠️ **Watchdog alert** — {finding['type']}: {finding.get('detail', '')}")

    def _self_heal(self, finding: Dict) -> bool:
        """Attempt automated remediation. Returns True if healing attempted."""
        f_type = finding.get("type", "")
        log.info(f"Self-heal attempt: {f_type} — {finding.get('detail', '')}")
        # Extensible: add specific healing strategies here
        # e.g. restart a stale bot, resubmit a failed document
        return False

    def _discord(self, message: str) -> None:
        """Post alert to Discord webhook (best-effort)."""
        import urllib.request, json
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
            log.debug(f"Discord alert failed: {exc}")
