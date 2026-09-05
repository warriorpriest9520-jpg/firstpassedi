"""
guardrails/staleness_check.py
──────────────────────────────
Routing guide freshness check for trading partner compliance specs.

Design rationale:
  Retail routing guides change frequently — new label requirements, updated
  carton dimensions, revised qualifier codes. An agent validating against a
  stale spec will produce confident-but-wrong verdicts that generate
  chargebacks. This guardrail surfaces staleness BEFORE the reasoning loop
  runs, so the compliance team can refresh specs proactively.

  90-day threshold is industry-informed: most major retailers update guides
  quarterly. Adjust per-partner if a retailer is known to update monthly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class StalenessResult:
    """
    Outcome of a freshness check on a trading partner's routing guide.

    Attributes:
        partner_name    – The trading partner this spec belongs to.
        spec_date       – The effective date of the routing guide version.
        days_old        – How many days since the spec was last updated.
        is_stale        – True when days_old exceeds the staleness threshold.
        threshold_days  – The threshold that was applied.
        warning_message – Human-readable summary; empty string if fresh.
        recommended_action – What the compliance team should do next.
    """
    partner_name: str
    spec_date: date
    days_old: int
    is_stale: bool
    threshold_days: int
    warning_message: str = ""
    recommended_action: str = ""

    # Convenience fields populated by check_spec_freshness
    checked_on: date = field(default_factory=date.today)


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class StalenessCheck:
    """
    Checks whether a trading partner's routing guide spec is current.

    Usage::

        checker = StalenessCheck()
        result = checker.check_spec_freshness(
            partner_name="RetailerA",
            spec_date=date(2025, 11, 15),
        )
        if result.is_stale:
            print(result.warning_message)

    The ``is_stale`` method can also be called standalone for quick checks
    without a full result object.
    """

    def __init__(self, today: date | None = None) -> None:
        """
        Args:
            today: Override "today" for testing purposes. Defaults to the
                   real current date via ``date.today()``.
        """
        # Injecting the clock makes this easily testable without mocking
        self._today: date = today or date.today()

    # ── Public API ─────────────────────────────────────────────────────────

    def check_spec_freshness(
        self,
        partner_name: str,
        spec_date: date | str,
        days_threshold: int = 90,
    ) -> StalenessResult:
        """
        Evaluate whether a routing guide is fresh enough to rely on.

        Args:
            partner_name:   Trading partner name (e.g., "RetailerA").
            spec_date:      Effective date of the routing guide. Accepts a
                            ``date`` object or an ISO 8601 string (YYYY-MM-DD).
            days_threshold: Number of days before a spec is considered stale.
                            Defaults to 90.

        Returns:
            A ``StalenessResult`` describing the freshness status.
        """
        # Normalize spec_date to a date object
        if isinstance(spec_date, str):
            spec_date = datetime.strptime(spec_date, "%Y-%m-%d").date()

        days_old: int = (self._today - spec_date).days
        stale: bool = days_old > days_threshold

        warning_message = ""
        recommended_action = ""

        if stale:
            warning_message = (
                f"⚠️  Routing guide for '{partner_name}' is {days_old} days old "
                f"(threshold: {days_threshold} days). "
                f"Last updated: {spec_date.isoformat()}. "
                "Validation results may not reflect current compliance requirements."
            )
            recommended_action = (
                f"Contact {partner_name}'s EDI coordinator or check their supplier "
                "portal for the latest routing guide. Do not submit documents until "
                "the spec has been verified or refreshed."
            )
        else:
            warning_message = (
                f"✅  Routing guide for '{partner_name}' is current "
                f"({days_old} days old, within {days_threshold}-day threshold)."
            )

        return StalenessResult(
            partner_name=partner_name,
            spec_date=spec_date,
            days_old=days_old,
            is_stale=stale,
            threshold_days=days_threshold,
            warning_message=warning_message,
            recommended_action=recommended_action,
            checked_on=self._today,
        )

    def is_stale(
        self,
        spec_date: date | str,
        days_threshold: int = 90,
    ) -> bool:
        """
        Quick boolean check — is this spec date beyond the staleness window?

        Args:
            spec_date:      Effective date of the routing guide.
            days_threshold: Staleness window in days (default 90).

        Returns:
            True if the spec is older than days_threshold days.
        """
        if isinstance(spec_date, str):
            spec_date = datetime.strptime(spec_date, "%Y-%m-%d").date()

        return (self._today - spec_date).days > days_threshold
