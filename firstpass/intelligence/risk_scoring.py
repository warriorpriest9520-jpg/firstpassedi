"""
risk_scoring.py — Per-partner risk scoring for FirstPass EDI.

Synthesises signals from watchdog state, SLA timers, shared context, and
partner profiles into a 0-100 risk score per trading partner.

Output: partner_risk_scores.json (written to DATA_DIR)

Score components (weights total 100):
  30 — EDI validation errors in last 7 days      (watchdog state)
  20 — Days since last successful EDI transaction (customer_health)
  25 — SLA timer active (856 not sent)            (universal watchdog state)
  15 — Partner in shared context complaints       (shared context)
  10 — Profile needs_config=true                  (watchdog profiles)

Levels: green 0-33 | amber 34-66 | red 67-100

Source lineage: ceo-bot/risk_scoring.py — ported for FirstPass EDI.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import config

logger = logging.getLogger("firstpass.intelligence.risk_scoring")

# ── Data directory (configurable via env) ──────────────────────────────────────
DATA_DIR = Path(os.getenv("FIRSTPASS_DATA_DIR", str(Path(__file__).parent.parent.parent / "data")))

WATCHDOG_STATE_FILE = DATA_DIR / "watchdog_state.json"
UNIVERSAL_STATE_FILE = DATA_DIR / "watchdog_universal_state.json"
PROFILES_FILE = DATA_DIR / "watchdog_profiles.json"
SHARED_CONTEXT_FILE = DATA_DIR / "shared_context.json"
OUTPUT_FILE = DATA_DIR / "partner_risk_scores.json"

# ── Weights ────────────────────────────────────────────────────────────────────
W_EDI_ERRORS = 30
W_DAYS_SINCE_SUCCESS = 20
W_SLA_ACTIVE = 25
W_COMPLAINTS = 15
W_NEEDS_CONFIG = 10

# Staleness threshold for "days since success" max penalty
MAX_DAYS_STALE = 14


def _level(score: int) -> str:
    if score <= 33:
        return "green"
    if score <= 66:
        return "amber"
    return "red"


def _trend(partner: str, new_score: int, previous: Dict[str, Any]) -> str:
    prev_entry = previous.get(partner, {})
    prev_score = prev_entry.get("score")
    if prev_score is None:
        return "new"
    diff = new_score - prev_score
    if abs(diff) <= 5:
        return "stable"
    return "worsening" if diff > 0 else "improving"


class RiskScorer:
    """
    Compute per-partner risk scores from watchdog state and shared context data.

    Usage::

        scorer = RiskScorer()
        result = scorer.score_all()
        # result["scores"]["acme_corp"] → {"score": 72, "level": "red", ...}

    State files are read from DATA_DIR (env: FIRSTPASS_DATA_DIR).
    Override individual file paths via constructor kwargs for testing.
    """

    def __init__(
        self,
        watchdog_state_file: Optional[Path] = None,
        universal_state_file: Optional[Path] = None,
        profiles_file: Optional[Path] = None,
        shared_context_file: Optional[Path] = None,
        output_file: Optional[Path] = None,
    ):
        self._watchdog_state_path = watchdog_state_file or WATCHDOG_STATE_FILE
        self._universal_state_path = universal_state_file or UNIVERSAL_STATE_FILE
        self._profiles_path = profiles_file or PROFILES_FILE
        self._shared_context_path = shared_context_file or SHARED_CONTEXT_FILE
        self._output_path = output_file or OUTPUT_FILE

        self.watchdog_state: Dict[str, Any] = self._load_json(self._watchdog_state_path)
        self.universal_state: Dict[str, Any] = self._load_json(self._universal_state_path)
        self.profiles_data: Dict[str, Any] = self._load_json(self._profiles_path)
        self.shared_context: Dict[str, Any] = self._load_json(self._shared_context_path)
        self.previous_scores: Dict[str, Any] = self._load_previous_scores()

    # ── Main entry ─────────────────────────────────────────────────────────────

    def score_all(self) -> Dict[str, Any]:
        """Compute scores for all known partners. Returns the full output dict."""
        partners = self._collect_partners()
        now_iso = datetime.now(timezone.utc).isoformat()

        scores: Dict[str, Any] = {}
        for partner in sorted(partners):
            score, factors = self._score_partner(partner)
            level = _level(score)
            trend = _trend(partner, score, self.previous_scores)
            scores[partner] = {
                "score": score,
                "level": level,
                "factors": factors,
                "trend": trend,
            }

        output = {"generated_at": now_iso, "scores": scores}
        self._save(output)
        self._publish_to_bus(scores)
        return output

    # ── Scoring logic ──────────────────────────────────────────────────────────

    def _score_partner(self, partner: str) -> Tuple[int, List[str]]:
        """Return (0-100 score, list of factor descriptions)."""
        score = 0
        factors: List[str] = []

        # ── Factor 1: EDI validation errors in last 7 days (weight 30) ──
        error_count = self._count_recent_edi_errors(partner, days=7)
        if error_count > 0:
            w1 = min(W_EDI_ERRORS, int(W_EDI_ERRORS * min(error_count, 3) / 3))
            score += w1
            factors.append(f"{error_count} EDI validation error(s) in last 7 days (+{w1})")

        # ── Factor 2: Days since last successful EDI transaction (weight 20) ──
        days_stale = self._days_since_last_success(partner)
        if days_stale is not None and days_stale > 0:
            w2 = int(W_DAYS_SINCE_SUCCESS * min(days_stale, MAX_DAYS_STALE) / MAX_DAYS_STALE)
            score += w2
            factors.append(f"{days_stale:.0f} day(s) since last successful EDI (+{w2})")
        elif days_stale is None:
            score += W_DAYS_SINCE_SUCCESS // 2
            factors.append(f"No EDI success history found (+{W_DAYS_SINCE_SUCCESS // 2})")

        # ── Factor 3: Active SLA timer (856 not sent) (weight 25) ──
        sla_active, sla_age_hours = self._has_active_sla_timer(partner)
        if sla_active:
            score += W_SLA_ACTIVE
            age_str = f"{sla_age_hours:.0f}h" if sla_age_hours else "unknown age"
            factors.append(f"856 SLA timer active ({age_str}) (+{W_SLA_ACTIVE})")

        # ── Factor 4: Partner in shared context complaints (weight 15) ──
        if self._has_complaint(partner):
            score += W_COMPLAINTS
            factors.append(f"Partner complaint on file (+{W_COMPLAINTS})")

        # ── Factor 5: Profile needs_config=true (weight 10) ──
        if self._needs_config(partner):
            score += W_NEEDS_CONFIG
            factors.append(f"EDI profile not configured (+{W_NEEDS_CONFIG})")

        if not factors:
            factors.append("No risk signals detected")

        return min(100, score), factors

    # ── Signal extractors ──────────────────────────────────────────────────────

    def _count_recent_edi_errors(self, partner: str, days: int = 7) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        events = self.watchdog_state.get("workflow_events", [])
        count = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("partner", ev.get("customer", "")).lower() != partner.lower():
                continue
            if ev.get("status") != "error":
                continue
            ts_str = ev.get("timestamp", ev.get("received_at", ""))
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    count += 1
            except Exception:
                continue
        return count

    def _days_since_last_success(self, partner: str) -> Optional[float]:
        """Return days since last successful EDI, or None if unknown."""
        health = self.watchdog_state.get("customer_health", {})
        partner_health = health.get(partner, health.get(partner.lower(), {}))

        last_success_str = None
        if isinstance(partner_health, dict):
            for key, val in partner_health.items():
                if isinstance(val, dict):
                    ts = val.get("last_success") or val.get("last_successful_callback")
                    if ts and (last_success_str is None or ts > last_success_str):
                        last_success_str = ts
                elif isinstance(val, str):
                    if last_success_str is None or val > last_success_str:
                        last_success_str = val

        if last_success_str is None:
            last_activity = self.universal_state.get("last_activity", {})
            for key, ts in last_activity.items():
                if partner.lower() in key.lower() and ts:
                    if last_success_str is None or str(ts) > last_success_str:
                        last_success_str = str(ts)

        if last_success_str is None:
            return None

        try:
            ts = datetime.fromisoformat(last_success_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        except Exception:
            return None

    def _has_active_sla_timer(self, partner: str) -> Tuple[bool, Optional[float]]:
        """Return (active, age_hours) for the partner's most critical SLA timer."""
        sla_timers = self.universal_state.get("sla_timers", {})
        for key, timer in sla_timers.items():
            if not isinstance(timer, dict):
                continue
            timer_partner = timer.get("partner", timer.get("customer", ""))
            if timer_partner.lower() != partner.lower():
                continue
            ts_str = timer.get("triggered_at") or timer.get("started_at", "")
            if not ts_str:
                return True, None
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                return True, age
            except Exception:
                return True, None
        return False, None

    def _has_complaint(self, partner: str) -> bool:
        """Return True if partner has a complaint in shared context."""
        for key, entry in self.shared_context.items():
            if not isinstance(entry, dict):
                continue
            val = entry.get("value", entry)
            if isinstance(val, dict):
                partners = val.get("partners", val.get("customers", []))
                if isinstance(partners, list):
                    for p in partners:
                        pname = p.get("partner", p.get("customer")) if isinstance(p, dict) else str(p)
                        if pname and pname.lower() == partner.lower():
                            return True
                # Direct partner field
                pname = val.get("partner", val.get("customer", ""))
                if pname.lower() == partner.lower():
                    return True
        return False

    def _needs_config(self, partner: str) -> bool:
        needs = self.profiles_data.get("needs_config", [])
        if isinstance(needs, list):
            return any(partner.lower() in n.lower() for n in needs)
        profiles = self.profiles_data.get("profiles", {})
        for key, p in profiles.items():
            if not isinstance(p, dict):
                continue
            pname = p.get("partner", p.get("customer", ""))
            if pname.lower() == partner.lower() and p.get("needs_config"):
                return True
        return False

    # ── Partner collection ─────────────────────────────────────────────────────

    def _collect_partners(self) -> set:
        partners = set()

        profiles = self.profiles_data.get("profiles", {})
        for p in profiles.values():
            if isinstance(p, dict):
                c = p.get("partner", p.get("customer"))
                if c:
                    partners.add(c.lower())

        for ev in self.watchdog_state.get("workflow_events", []):
            if isinstance(ev, dict):
                c = ev.get("partner", ev.get("customer"))
                if c:
                    partners.add(c.lower())

        for c in self.watchdog_state.get("customer_health", {}).keys():
            partners.add(c.lower())

        return partners

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_previous_scores(self) -> Dict[str, Any]:
        try:
            if self._output_path.exists():
                data = json.loads(self._output_path.read_text(encoding="utf-8"))
                return data.get("scores", {})
        except Exception:
            pass
        return {}

    def _save(self, output: Dict[str, Any]):
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            self._output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
            logger.info(f"Risk scores written to {self._output_path}")
        except Exception as e:
            logger.error(f"Could not save risk scores: {e}")

    def _publish_to_bus(self, scores: Dict[str, Any]):
        try:
            from firstpass.utils.message_bus import MessageBus
            bus = MessageBus()
            red_count = sum(1 for s in scores.values() if s["level"] == "red")
            amber_count = sum(1 for s in scores.values() if s["level"] == "amber")
            green_count = sum(1 for s in scores.values() if s["level"] == "green")
            bus.publish(
                "RiskScoring",
                "insight",
                "partner_risk_update",
                f"Risk scores updated: {red_count} red, {amber_count} amber, {green_count} green",
                payload=scores,
                priority="normal",
            )
            logger.info("Risk score summary published to message bus")
        except Exception as e:
            logger.warning(f"Bus publish failed: {e}")

    @staticmethod
    def _load_json(path: Path) -> Dict[str, Any]:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load {path.name}: {e}")
        return {}


# ── Entry point ────────────────────────────────────────────────────────────────

def run_scoring() -> Dict[str, Any]:
    """Run the nightly scoring pass. Returns the full scores dict."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [RiskScoring] %(levelname)s %(message)s",
    )
    scorer = RiskScorer()
    result = scorer.score_all()
    scores = result.get("scores", {})
    red = [k for k, v in scores.items() if v["level"] == "red"]
    amber = [k for k, v in scores.items() if v["level"] == "amber"]
    green_count = sum(1 for v in scores.values() if v["level"] == "green")
    print(f"\n{'='*50}")
    print(f"Risk Scoring Complete — {len(scores)} partners")
    print(f"  🔴 Red ({len(red)}): {', '.join(red) or 'none'}")
    print(f"  🟡 Amber ({len(amber)}): {', '.join(amber) or 'none'}")
    print(f"  🟢 Green: {green_count}")
    print(f"  Output: {scorer._output_path}")
    return result


if __name__ == "__main__":
    run_scoring()
