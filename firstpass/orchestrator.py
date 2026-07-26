"""
orchestrator.py — Main coordinator for the FirstPass EDI platform.

Drives all agents in a configurable event loop:

  1. Health check   — verify all connectors are reachable
  2. Inbox triage   — process inbound emails (inbox_agent)
  3. EDI cycle      — poll platforms, parse 850s, generate 855/856/810 (edi_agent)
  4. Partner audit  — compliance checks and SLA monitoring (audit_agent)
  5. Watchdog tick  — health scoring, anomaly detection (watchdog_agent)
  6. Memory update  — persist insights to corporate knowledge store

Usage:
    python -m firstpass.orchestrator              # single cycle
    python -m firstpass.orchestrator --daemon     # continuous (POLL_INTERVAL_SECONDS)
    python -m firstpass.orchestrator --status     # print current state
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from .config import config
from .utils.message_bus import MessageBus

log = logging.getLogger("firstpass.orchestrator")

# ── Lazy agent imports (avoid circular deps) ─────────────────────────────────

def _inbox_agent():
    from .agents.inbox_agent import InboxAgent
    return InboxAgent()

def _edi_agent():
    from .agents.edi_agent import EDIAgent
    return EDIAgent()

def _audit_agent():
    from .agents.audit_agent import AuditAgent
    return AuditAgent()

def _watchdog_agent():
    from .agents.watchdog_agent import WatchdogAgent
    return WatchdogAgent()

def _risk_scorer():
    from .intelligence.risk_scoring import RiskScorer
    return RiskScorer()

def _intelligence_engine():
    from .intelligence.analytics import IntelligenceEngine
    return IntelligenceEngine()

def _briefing_generator():
    from .intelligence.briefing import BriefingGenerator
    return BriefingGenerator()


class Orchestrator:
    """
    Top-level coordinator.  Each ``run_cycle()`` call executes one full
    pass through all agents in sequence.  The daemon loop wraps this with
    POLL_INTERVAL_SECONDS sleep between cycles.
    """

    def __init__(self):
        self.bus = MessageBus()
        self._stop_event = asyncio.Event()
        self._cycle_count = 0
        self._last_cycle_at: Optional[str] = None

    # ── Single cycle ──────────────────────────────────────────────────────

    def run_cycle(self) -> dict:
        """Execute one full orchestration cycle. Returns a summary dict."""
        from .safety.halt_switch import is_halted

        if is_halted():
            log.warning("HALT-ALL is active — skipping cycle")
            return {"skipped": True, "reason": "halt_all"}

        self._cycle_count += 1
        start = time.time()
        results: dict = {"cycle": self._cycle_count, "agents": {}}

        log.info(f"── Cycle {self._cycle_count} starting ──────────────────")

        # 1. EDI agent (highest priority — time-sensitive SLAs)
        try:
            agent = _edi_agent()
            results["agents"]["edi"] = agent.run_cycle()
        except Exception as exc:
            log.error(f"EDI agent failed: {exc}", exc_info=True)
            results["agents"]["edi"] = {"error": str(exc)}

        # 2. Inbox triage
        try:
            agent = _inbox_agent()
            results["agents"]["inbox"] = agent.run_cycle()
        except Exception as exc:
            log.error(f"Inbox agent failed: {exc}", exc_info=True)
            results["agents"]["inbox"] = {"error": str(exc)}

        # 3. Partner audit
        try:
            agent = _audit_agent()
            results["agents"]["audit"] = agent.run_cycle()
        except Exception as exc:
            log.error(f"Audit agent failed: {exc}", exc_info=True)
            results["agents"]["audit"] = {"error": str(exc)}

        # 4. Watchdog
        try:
            agent = _watchdog_agent()
            results["agents"]["watchdog"] = agent.run_cycle()
        except Exception as exc:
            log.error(f"Watchdog agent failed: {exc}", exc_info=True)
            results["agents"]["watchdog"] = {"error": str(exc)}

        # 5. Risk scoring (runs after audit + watchdog feed data)
        try:
            scorer = _risk_scorer()
            results["intelligence"] = {"risk": scorer.score_all_partners()}
        except Exception as exc:
            log.error(f"Risk scoring failed: {exc}", exc_info=True)
            results["intelligence"] = {"risk": {"error": str(exc)}}

        # 6. Intelligence analysis (pattern detection, anomaly flagging)
        try:
            engine = _intelligence_engine()
            results["intelligence"]["analysis"] = engine.run_analysis()
        except Exception as exc:
            log.error(f"Intelligence engine failed: {exc}", exc_info=True)
            results.setdefault("intelligence", {})["analysis"] = {"error": str(exc)}

        elapsed = time.time() - start
        self._last_cycle_at = datetime.now(timezone.utc).isoformat()
        results["elapsed_s"] = round(elapsed, 2)
        results["completed_at"] = self._last_cycle_at

        log.info(f"── Cycle {self._cycle_count} done in {elapsed:.1f}s ──")
        self._publish_cycle_summary(results)
        return results

    # ── Daemon loop ───────────────────────────────────────────────────────

    def run_daemon(self) -> None:
        """Block forever, running cycles every POLL_INTERVAL_SECONDS."""
        interval = config.POLL_INTERVAL_SECONDS
        log.info(f"Orchestrator daemon started (interval={interval}s). Ctrl-C to stop.")

        def _handle_signal(signum, frame):
            log.info(f"Signal {signum} received — stopping daemon")
            self._stop_event.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        while not self._stop_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:
                log.error(f"Unhandled cycle error: {exc}", exc_info=True)
            # Sleep in short chunks so Ctrl-C is responsive
            for _ in range(interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        log.info("Orchestrator daemon stopped.")

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        from .safety.halt_switch import status as halt_status
        return {
            "cycles_run": self._cycle_count,
            "last_cycle_at": self._last_cycle_at,
            "halt": halt_status(),
            "config": repr(config),
        }

    # ── Internal ──────────────────────────────────────────────────────────

    def _publish_cycle_summary(self, results: dict) -> None:
        errors = [k for k, v in results.get("agents", {}).items() if "error" in v]
        if errors:
            self.bus.publish(
                source="orchestrator",
                event_type="warning",
                topic="orchestrator_cycle_error",
                subject=f"Cycle {results['cycle']}: agent errors in {errors}",
                payload=results,
                priority="normal",
            )
        else:
            self.bus.publish(
                source="orchestrator",
                event_type="status",
                topic="orchestrator_cycle_ok",
                subject=f"Cycle {results['cycle']} completed in {results['elapsed_s']}s",
                priority="background",
            )


# ── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(description="FirstPass EDI Orchestrator")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    args = parser.parse_args()

    orch = Orchestrator()

    if args.status:
        import json
        print(json.dumps(orch.status(), indent=2))
    elif args.daemon:
        orch.run_daemon()
    else:
        import json
        result = orch.run_cycle()
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
