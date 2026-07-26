"""
guardrails.py — Approval gates and validation for autonomous EDI actions.

Implements the "Judge" pattern from the Autonomous Department spec:
  - Validates inbound orders against business rules
  - Enforces approval gates for high-value / high-risk transactions
  - Computes decision rewards for continuous improvement
  - Flags constraint violations immediately

Source lineage: judge.py (SageOrderAPI), guardrails in ceo-bot
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..config import config

log = logging.getLogger("firstpass.safety.guardrails")

# ── Reward table (from Autonomous Department spec Section 6.2) ────────────────

REWARD_TABLE = {
    ("self_heal", "confirmed"): 1.0,
    ("self_heal", "resolved"): 0.8,
    ("self_heal", "corrected"): -1.0,
    ("escalate", "warranted"): 0.5,
    ("escalate", "over"): -0.3,
    ("escalate", "constraint"): 1.0,
    ("watch", "resolved"): 0.3,
    ("watch", "missed"): -0.5,
    ("any", "authority_violation"): -2.0,
    ("any", "constraint"): 0.0,
}

_OUTCOME_FALLBACK = {
    "resolved": 0.8,
    "failed": -1.0,
    "escalated": 0.5,
    "ignored": 0.0,
    "pending": None,
}

DEGRADED_THRESHOLD = -0.3  # 7-day rolling avg below this → flag agent


# ── Order validation ──────────────────────────────────────────────────────────

def validate_order(order) -> List[str]:
    """
    Run guardrail checks on an Order object.

    :returns: List of violation strings (empty = all clear)
    """
    violations: List[str] = []

    # 1. PO number required
    if not order.po_number:
        violations.append("Missing PO number")

    # 2. Line items required
    if not order.line_items:
        violations.append("Order has no line items")

    # 3. Negative quantities / prices
    for li in order.line_items:
        if li.get("qty", 0) < 0:
            violations.append(f"Line {li.get('line_num')}: negative quantity {li['qty']}")
        if li.get("unit_price", 0) < 0:
            violations.append(f"Line {li.get('line_num')}: negative unit price")

    # 4. High-value approval gate
    total = order.total_value
    if total > config.REQUIRE_APPROVAL_ABOVE_AMOUNT:
        violations.append(
            f"Order value ${total:,.2f} exceeds approval threshold "
            f"${config.REQUIRE_APPROVAL_ABOVE_AMOUNT:,.2f} — human review required"
        )

    # 5. Ship-to address required
    if not order.ship_to:
        violations.append("Missing ship-to address")

    # 6. Valid ISA ID
    if not order.partner_isa_id or len(order.partner_isa_id.strip()) < 2:
        violations.append(f"Invalid or missing partner ISA ID: {order.partner_isa_id!r}")

    return violations


# ── Decision scoring ──────────────────────────────────────────────────────────

def score_event(event: Dict) -> Optional[float]:
    """
    Compute the reward for one agent decision event.

    :param event: Dict with "decision", "outcome", and optional "metadata" fields.
    :returns: Scalar reward, or None if the outcome carries no signal yet.
    """
    if (event.get("event_type") or "").lower() == "heartbeat":
        return None

    meta = event.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("reward_signal") is not None:
        try:
            return float(meta["reward_signal"])
        except (TypeError, ValueError):
            pass

    decision = (event.get("decision") or "").lower()
    outcome = (event.get("outcome") or "").lower()

    if (decision, outcome) in REWARD_TABLE:
        return REWARD_TABLE[(decision, outcome)]
    if ("any", outcome) in REWARD_TABLE:
        return REWARD_TABLE[("any", outcome)]
    return _OUTCOME_FALLBACK.get(outcome)


def evaluate_agent(events: List[Dict]) -> Dict:
    """
    Roll up a list of events into a per-agent verdict.

    :returns: {
        "source": str,
        "events": int,
        "avg_reward": float | None,
        "verdict": "healthy" | "degraded" | "unknown",
        "last_at": str | None,
    }
    """
    if not events:
        return {"source": "unknown", "events": 0, "avg_reward": None, "verdict": "unknown"}

    rewards = []
    last_at = None
    source = events[0].get("source", "unknown")

    for ev in events:
        r = score_event(ev)
        if r is not None:
            rewards.append(r)
        ts = ev.get("created_at") or ev.get("timestamp")
        if ts and (last_at is None or ts > last_at):
            last_at = ts

    avg = (sum(rewards) / len(rewards)) if rewards else None
    verdict = "unknown"
    if avg is not None:
        verdict = "degraded" if avg < DEGRADED_THRESHOLD else "healthy"

    return {
        "source": source,
        "events": len(events),
        "rewards_scored": len(rewards),
        "avg_reward": round(avg, 3) if avg is not None else None,
        "verdict": verdict,
        "last_at": last_at,
    }


# ── Constraint checks ─────────────────────────────────────────────────────────

def check_authority_violation(action: str, actor: str, context: Dict) -> bool:
    """
    Return True if the proposed action violates authority constraints.

    Examples of hard constraints (always block):
      - Deleting or modifying already-shipped orders
      - Reducing agreed-upon quantities without approval
      - Changing bank/payment details
    """
    hard_blocks = {
        "delete_shipped_order",
        "modify_payment_details",
        "reduce_shipped_quantity",
        "cancel_invoiced_order",
    }
    if action in hard_blocks:
        log.warning(f"Authority violation: actor={actor!r} action={action!r}")
        return True
    return False
