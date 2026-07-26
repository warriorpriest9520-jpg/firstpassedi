"""
briefing.py — Adaptive daily operations briefing for FirstPass EDI.

Reads partner risk scores, watchdog state, shared context, and the message bus
to build a structured briefing that adapts to current conditions:
  - Red partners surface first with recommended actions
  - Everything green → concise 3-line summary
  - Inbox / EDI status / optional trading section included

Configuration (env vars):
  FIRSTPASS_DATA_DIR       — Directory containing state JSON files
  FIRSTPASS_EDI_PARTNER_COUNT  — Known active partner count (for legacy fallback display)
  ORDERAPI_BASE_URL        — Base URL for FirstPass Order-API dashboard (default: http://localhost:8001)
  ORDERAPI_KEY             — API key for Order-API

Usage::

    from firstpass.intelligence.briefing import generate_briefing
    text = generate_briefing(include_trading=False)

Source lineage: ceo-bot/adaptive_briefing.py — ported for FirstPass EDI.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import config

logger = logging.getLogger("firstpass.intelligence.briefing")

# ── Data directory ─────────────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("FIRSTPASS_DATA_DIR", str(Path(__file__).parent.parent.parent / "data")))

RISK_SCORES_FILE = DATA_DIR / "partner_risk_scores.json"
WATCHDOG_STATE_FILE = DATA_DIR / "watchdog_state.json"

BRIEFING_TTL_HOURS = 24

# Fallback partner count when Order-API is unavailable (override via env)
_DEFAULT_PARTNER_COUNT: int = int(os.getenv("FIRSTPASS_EDI_PARTNER_COUNT", "0"))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not load {path.name}: {e}")
    return {}


def _get_shared(key: str) -> Any:
    """Read from Supabase-backed shared context."""
    try:
        from firstpass.memory.supabase_client import get_shared_context
        return get_shared_context(key)
    except Exception:
        return None


def _set_shared(key: str, value: Any, source: str = "BriefingGenerator", ttl_hours: int = 24):
    try:
        from firstpass.memory.supabase_client import set_shared_context
        set_shared_context(key, value, source=source, ttl_hours=ttl_hours)
    except Exception as e:
        logger.warning(f"Could not write shared context: {e}")


def _get_bus_insights(limit: int = 10) -> List[Dict[str, Any]]:
    try:
        from firstpass.utils.message_bus import MessageBus
        bus = MessageBus()
        return bus.consume("BriefingGenerator", limit=limit)
    except Exception:
        return []


# ── Recommendation engine ──────────────────────────────────────────────────────

def _recommend_action(partner: str, factors: List[str]) -> str:
    """Return a brief recommended action string based on risk factors."""
    factors_lower = " ".join(factors).lower()
    if "856" in factors_lower and "sla" in factors_lower:
        return f"Manually trigger 856 ASN for {partner} or check workflow"
    if "not configured" in factors_lower:
        return f"Complete EDI profile configuration for {partner}"
    if "complaint" in factors_lower and "late" in factors_lower:
        return f"Contact {partner} and provide shipment update"
    if "complaint" in factors_lower:
        return f"Review complaint for {partner} and draft response"
    if "validation" in factors_lower:
        return f"Review EDI spec for {partner} — repeated field errors detected"
    if "no edi" in factors_lower or "history" in factors_lower:
        return f"Verify {partner} EDI workflow is active"
    return f"Review {partner} situation and take appropriate action"


# ── Section builders ───────────────────────────────────────────────────────────

def _section_critical(red_partners: Dict[str, Any]) -> Optional[str]:
    if not red_partners:
        return None
    lines = ["🚨 **CRITICAL**"]
    for partner, info in sorted(red_partners.items(), key=lambda x: -x[1]["score"]):
        factors = info.get("factors", [])
        recommendation = _recommend_action(partner, factors)
        lines.append(f"  • **{partner.upper()}** (score {info['score']}/100)")
        for f in factors[:3]:
            lines.append(f"    - {f}")
        lines.append(f"    → **{recommendation}**")
    return "\n".join(lines)


def _section_watchlist(amber_partners: Dict[str, Any]) -> Optional[str]:
    if not amber_partners:
        return None
    lines = ["⚠️ **WATCH LIST**"]
    for partner, info in sorted(amber_partners.items(), key=lambda x: -x[1]["score"]):
        factors_short = "; ".join(info.get("factors", [])[:2])
        lines.append(f"  • **{partner.upper()}** ({info['score']}/100) — {factors_short}")
    return "\n".join(lines)


# ── Order-API / dashboard fetch ────────────────────────────────────────────────

def _fetch_order_api_dashboard() -> Optional[Dict[str, Any]]:
    """Call GET /api/dashboard on the FirstPass Order-API and normalise response."""
    base_url = os.environ.get("ORDERAPI_BASE_URL", "http://localhost:8001").rstrip("/")
    api_key = os.environ.get("ORDERAPI_KEY", config.API_KEY)
    url = f"{base_url}/api/dashboard"
    try:
        req = urllib.request.Request(
            url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as exc:
        logger.debug("Order-API dashboard fetch failed: %s", exc)
        return None

    if not raw.get("success"):
        return None

    data = raw.get("data", {})
    judge = data.get("judge", {})
    pipeline = data.get("edi_pipeline", {})
    connectors = data.get("connectors", {})
    agents = judge.get("agents", [])
    summary = judge.get("summary", {})

    bots = []
    for a in agents:
        status = "active" if a.get("status") == "active" else "offline"
        bots.append({
            "name": a.get("label", a.get("source", "")),
            "status": status,
            "current_focus": a.get("last_subject", ""),
            "last_summary": a.get("verdict", ""),
        })

    return {
        "ok": True,
        "source": "order_api",
        "kpis": {
            "edi_partners": len([a for a in agents if a.get("group") == "OrderAPI"]),
            "enabled_workflows": pipeline.get("orders", 0),
            "open_escalations": len(summary.get("degraded", [])),
            "connectors": connectors,
        },
        "bots": bots,
    }


# ── EDI status section ─────────────────────────────────────────────────────────

def _section_edi_status(
    dashboard: Optional[Dict[str, Any]],
    watchdog_state: Dict[str, Any],
) -> str:
    """Build EDI status section, preferring live Order-API data over local watchdog file."""
    EDI_BOT_NAMES = {"EDI Bot", "EDI Monitor", "EDI Audit", "EDI Payload"}

    if dashboard and dashboard.get("ok"):
        kpis = dashboard.get("kpis", {})
        bots = dashboard.get("bots", [])
        edi_bots = [b for b in bots if b.get("name") in EDI_BOT_NAMES]
        partners = kpis.get("edi_partners", "?")
        workflows = kpis.get("enabled_workflows", "?")
        escalations = kpis.get("open_escalations", 0)

        error_bots = [b for b in edi_bots if b.get("status") == "error"]
        idle_bots = [b for b in edi_bots if b.get("status") in ("idle", "offline")]
        healthy_bots = [b for b in edi_bots if b.get("status") == "active"]

        status_emoji = "🔴" if error_bots else ("🟡" if idle_bots else "✅")

        lines = [
            f"📡 **EDI STATUS** {status_emoji} — {len(healthy_bots)}/{len(edi_bots)} bots active"
            f"  |  {workflows} workflows  |  {partners} partners  |  {escalations} open escalations"
        ]

        for b in error_bots:
            detail = (b.get("current_focus") or b.get("last_summary") or "error")[:100]
            lines.append(f"  🔴 **{b['name']}**: {detail}")
        for b in idle_bots:
            detail = (b.get("current_focus") or b.get("last_summary") or "stale")[:100]
            lines.append(f"  🟡 **{b['name']}**: {detail}")
        if not error_bots and not idle_bots:
            lines.append("  All EDI workers healthy")

        return "\n".join(lines)

    # Fallback: read local watchdog file
    return _section_edi_status_legacy(watchdog_state)


def _section_edi_status_legacy(watchdog_state: Dict[str, Any]) -> str:
    """Legacy EDI status from local watchdog_state.json (fallback only)."""
    total_profiles = _DEFAULT_PARTNER_COUNT or len(
        watchdog_state.get("profiles", {})
    ) or "?"
    alerts = watchdog_state.get("alerts", [])
    recent_errors = [
        a for a in alerts
        if isinstance(a, dict) and a.get("status") == "error"
    ]

    if isinstance(total_profiles, int) and total_profiles > 0:
        healthy = total_profiles - len(recent_errors)
        lines = [f"📡 **EDI STATUS** — {healthy}/{total_profiles} workflows healthy (local cache)"]
    else:
        lines = [f"📡 **EDI STATUS** — {len(recent_errors)} error(s) detected (local cache)"]

    if recent_errors:
        lines.append("  Issues:")
        for err in recent_errors[:5]:
            partner = err.get("partner", err.get("customer", "?"))
            doc_type = err.get("doc_type", "?")
            msg = err.get("message", err.get("error_msg", "error"))[:80]
            lines.append(f"    - {partner.upper()} {doc_type}: {msg}")
        if len(recent_errors) > 5:
            lines.append(f"    … and {len(recent_errors) - 5} more")

    return "\n".join(lines)


# ── Inbox section ──────────────────────────────────────────────────────────────

def _section_inbox() -> str:
    """Read inbox data from shared context or message bus."""
    inbox_data = _get_shared("inbox_summary") or _get_shared("email_summary")
    if isinstance(inbox_data, dict):
        count = inbox_data.get("unread_count", inbox_data.get("count", 0))
        subjects = inbox_data.get("subjects", inbox_data.get("urgent_subjects", []))
        if count > 0:
            lines = [f"📬 **INBOX** — {count} email(s) need attention"]
            for s in subjects[:5]:
                lines.append(f"  • {s}")
            return "\n".join(lines)

    # Fallback: bus email events
    try:
        from firstpass.utils.message_bus import MessageBus
        bus = MessageBus()
        emails = bus.consume_by_topic("email_*", limit=5) if hasattr(bus, "consume_by_topic") else []
        if emails:
            lines = [f"📬 **INBOX** — {len(emails)} email event(s) on bus"]
            for e in emails[:3]:
                lines.append(f"  • {e.get('subject', '(no subject)')}")
            return "\n".join(lines)
    except Exception:
        pass

    return "📬 **INBOX** — No new emails flagged"


# ── Trading section (optional) ─────────────────────────────────────────────────

def _section_trading(include_trading: bool) -> Optional[str]:
    if not include_trading:
        return None

    trading_data = _get_shared("trading_summary") or _get_shared("crypto_status")
    if not trading_data:
        try:
            from firstpass.utils.message_bus import MessageBus
            bus = MessageBus()
            msgs = bus.consume_by_topic("trading_*", limit=3) if hasattr(bus, "consume_by_topic") else []
            if msgs:
                lines = ["📊 **TRADING**"]
                for m in msgs[:3]:
                    lines.append(f"  • {m.get('subject', '')}")
                return "\n".join(lines)
        except Exception:
            pass
        return None

    if isinstance(trading_data, dict):
        lines = ["📊 **TRADING**"]
        gemini = trading_data.get("gemini", trading_data.get("portfolio"))
        eth_signal = trading_data.get("eth_signal", trading_data.get("eth"))
        bot_status = trading_data.get("bot_status", trading_data.get("crypto_bot"))

        if gemini:
            lines.append(f"  • Gemini: {gemini}")
        if eth_signal:
            lines.append(f"  • ETH signal: {eth_signal}")
        if bot_status:
            lines.append(f"  • Crypto bot: {bot_status}")

        if len(lines) > 1:
            return "\n".join(lines)

    return None


# ── All clear footer ───────────────────────────────────────────────────────────

def _section_all_clear(green_partners: Dict[str, Any]) -> str:
    if not green_partners:
        return ""
    names = sorted(green_partners.keys())
    sample = [n.upper() for n in names[:8]]
    suffix = f" + {len(names) - 8} more" if len(names) > 8 else ""
    return f"✅ **ALL CLEAR** — {', '.join(sample)}{suffix}"


def _section_bus_insights(bus_messages: List[Dict[str, Any]]) -> Optional[str]:
    """Surface any notable insight/discovery messages from the bus."""
    insights = [
        m for m in bus_messages
        if m.get("event_type") in ("insight", "discovery")
    ]
    if not insights:
        return None
    lines = ["💡 **INSIGHTS**"]
    for m in insights[:3]:
        lines.append(f"  • {m.get('subject', '')}")
    return "\n".join(lines)


# ── Main briefing class ────────────────────────────────────────────────────────

class BriefingGenerator:
    """
    Generate adaptive daily operations briefings.

    Usage::

        gen = BriefingGenerator()
        text = gen.generate(include_trading=False)
    """

    def generate(self, include_trading: bool = False) -> str:
        """Generate and return the adaptive briefing as a markdown string."""
        return generate_briefing(include_trading=include_trading)

    def get_cached(self) -> Optional[str]:
        """Return cached briefing text if available and fresh."""
        return get_cached_briefing()


# ── Module-level convenience function ─────────────────────────────────────────

def generate_briefing(include_trading: bool = False) -> str:
    """
    Generate adaptive operations briefing. Returns formatted markdown string.

    :param include_trading: Include trading/crypto section if data available.
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %-d %Y at %-I:%M %p UTC")

    risk_data = _load_json(RISK_SCORES_FILE)
    watchdog_state = _load_json(WATCHDOG_STATE_FILE)
    bus_messages = _get_bus_insights(limit=20)
    order_api_dashboard = _fetch_order_api_dashboard()

    scores = risk_data.get("scores", {})
    risk_generated_at = risk_data.get("generated_at", "")

    red = {k: v for k, v in scores.items() if v.get("level") == "red"}
    amber = {k: v for k, v in scores.items() if v.get("level") == "amber"}
    green = {k: v for k, v in scores.items() if v.get("level") == "green"}

    header = f"# 🌅 Operations Briefing — {date_str}\n"

    # Short circuit: everything green
    if not red and not amber and scores:
        lines = [
            header,
            f"✅ All {len(green)} partners are green — no issues detected.",
            _section_edi_status(order_api_dashboard, watchdog_state),
            _section_inbox(),
        ]
        trading = _section_trading(include_trading)
        if trading:
            lines.append(trading)
        briefing = "\n\n".join(filter(None, lines))
        _store_briefing(briefing)
        return briefing

    sections = [header]

    critical = _section_critical(red)
    if critical:
        sections.append(critical)

    watchlist = _section_watchlist(amber)
    if watchlist:
        sections.append(watchlist)

    sections.append(_section_edi_status(order_api_dashboard, watchdog_state))
    sections.append(_section_inbox())

    trading = _section_trading(include_trading)
    if trading:
        sections.append(trading)

    bus_insights = _section_bus_insights(bus_messages)
    if bus_insights:
        sections.append(bus_insights)

    clear = _section_all_clear(green)
    if clear:
        sections.append(clear)

    if risk_generated_at:
        sections.append(f"_Risk scores last updated: {risk_generated_at[:16].replace('T', ' ')} UTC_")

    briefing = "\n\n".join(filter(None, sections))
    _store_briefing(briefing)
    return briefing


def _store_briefing(briefing: str):
    """Cache briefing in shared context with 24h TTL."""
    _set_shared(
        "firstpass_briefing_today",
        {"text": briefing, "generated_at": datetime.now(timezone.utc).isoformat()},
        ttl_hours=BRIEFING_TTL_HOURS,
    )


def get_cached_briefing() -> Optional[str]:
    """Return the cached briefing text if available and fresh."""
    data = _get_shared("firstpass_briefing_today")
    if not isinstance(data, dict):
        return None
    return data.get("text")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [BriefingGenerator] %(levelname)s %(message)s",
    )
    briefing = generate_briefing(include_trading=False)
    print(briefing)
