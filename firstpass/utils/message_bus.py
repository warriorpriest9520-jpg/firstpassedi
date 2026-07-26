"""
message_bus.py — Inter-agent pub/sub messaging.

Replaces passive polling with event-driven messaging.  Agents publish
discoveries, warnings, and insights; subscribers consume relevant messages
asynchronously on their next tick.

Design:
  - File-backed (JSONL) for simplicity and crash-safety
  - Pattern-based subscriptions (glob matching)
  - Dead-letter queue for undeliverable messages
  - TTL-based message expiry (default: 24h)

Source lineage: message_bus.py (ceo-bot)
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("firstpass.utils.message_bus")

# ── Default subscriptions ─────────────────────────────────────────────────────

DEFAULT_SUBSCRIPTIONS: Dict[str, List[str]] = {
    "orchestrator": ["*"],
    "edi_agent": ["edi_*", "order_status_*", "integration_*"],
    "inbox_agent": ["email_*", "customer_complaint", "draft_*"],
    "audit_agent": ["partner_compliance_*", "edi_parse_error", "sla_*"],
    "watchdog_agent": ["watchdog_*", "bot_*", "platform_*", "stuck_order"],
}

EVENT_TYPES = {"discovery", "warning", "request", "insight", "anomaly", "status"}
PRIORITIES = {"urgent", "normal", "background"}
MESSAGE_TTL_HOURS = 24


class MessageBus:
    """
    Simple file-backed pub/sub bus for inter-agent messaging.

    Usage::

        bus = MessageBus()

        # Publish
        bus.publish("edi_agent", "warning", "edi_parse_error",
                    "Failed to parse 850 from Big Box Retail",
                    payload={"po_number": "BB-001"}, priority="urgent")

        # Consume
        messages = bus.consume("audit_agent")
    """

    def __init__(self, bus_dir: Optional[str] = None):
        _root = Path(__file__).parent.parent.parent
        self._bus_dir = Path(bus_dir) if bus_dir else _root / "logs" / "message_bus"
        self._bus_dir.mkdir(parents=True, exist_ok=True)
        self._messages_file = self._bus_dir / "messages.jsonl"
        self._subscribers_file = self._bus_dir / "subscribers.json"
        self._dead_letters_file = self._bus_dir / "dead_letters.jsonl"
        self._subscribers = self._load_subscribers()
        if not self._subscribers:
            self._bootstrap_subscriptions()

    # ── Publishing ────────────────────────────────────────────────────────

    def publish(
        self,
        source: str,
        event_type: str,
        topic: str,
        subject: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: str = "normal",
        requires_response: bool = False,
    ) -> str:
        """Publish a message to the bus. Returns message ID."""
        if event_type not in EVENT_TYPES:
            event_type = "status"
        if priority not in PRIORITIES:
            priority = "normal"

        msg_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        msg = {
            "id": msg_id,
            "source": source,
            "event_type": event_type,
            "topic": topic,
            "subject": subject[:500],
            "payload": payload or {},
            "priority": priority,
            "requires_response": requires_response,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=MESSAGE_TTL_HOURS)).isoformat(),
            "consumed_by": [],
        }

        try:
            with self._messages_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")
        except Exception as exc:
            log.error(f"Failed to write message to bus: {exc}")

        log.debug(f"Published [{priority}] {source}→{topic}: {subject[:60]}")
        return msg_id

    # ── Consuming ─────────────────────────────────────────────────────────

    def consume(self, subscriber: str) -> List[Dict]:
        """
        Return all unconsumed messages matching the subscriber's topic patterns.
        Marks returned messages as consumed by this subscriber.
        """
        patterns = self._subscribers.get(subscriber, ["*"])
        now = datetime.now(timezone.utc)
        matched: List[Dict] = []
        updated: List[Dict] = []

        try:
            if not self._messages_file.exists():
                return []
            lines = self._messages_file.read_text(encoding="utf-8").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Expiry check
                expires = msg.get("expires_at")
                if expires:
                    try:
                        exp_dt = datetime.fromisoformat(expires)
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        if exp_dt < now:
                            continue  # skip expired
                    except Exception:
                        pass
                # Already consumed?
                if subscriber in (msg.get("consumed_by") or []):
                    updated.append(msg)
                    continue
                # Topic match?
                topic = msg.get("topic", "")
                if any(fnmatch.fnmatch(topic, p) for p in patterns):
                    msg.setdefault("consumed_by", []).append(subscriber)
                    matched.append(msg)
                updated.append(msg)
            # Rewrite file with updated consumed_by lists
            with self._messages_file.open("w", encoding="utf-8") as f:
                for msg in updated:
                    f.write(json.dumps(msg) + "\n")
        except Exception as exc:
            log.error(f"consume({subscriber}) failed: {exc}")

        return matched

    # ── Subscriptions ──────────────────────────────────────────────────────

    def subscribe(self, subscriber: str, topics: List[str]) -> None:
        """Register or update a subscriber's topic patterns."""
        self._subscribers[subscriber] = topics
        self._save_subscribers()

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_subscribers(self) -> Dict[str, List[str]]:
        try:
            if self._subscribers_file.exists():
                return json.loads(self._subscribers_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_subscribers(self) -> None:
        try:
            self._subscribers_file.write_text(
                json.dumps(self._subscribers, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.error(f"Failed to save subscribers: {exc}")

    def _bootstrap_subscriptions(self) -> None:
        self._subscribers = {k: v for k, v in DEFAULT_SUBSCRIPTIONS.items()}
        self._save_subscribers()
