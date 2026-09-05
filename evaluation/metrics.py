"""
evaluation/metrics.py
──────────────────────
Evaluation metrics for FirstPass EDI validation runs.

Design rationale:
  We track six metrics that map directly to business outcomes. Each has a
  target derived from the project spec and industry benchmarks:

    • chargeback_prevention_rate  ≥ 98%   — primary business value metric
    • false_positive_rate         <  5%   — compliance team workload
    • escalation_rate             10–15%  — agent uncertainty calibration
    • retrieval_relevance (MRR)   higher is better — RAG quality signal
    • diagnostic_resolution_rate  higher is better — agent usefulness
    • latency_per_document (sec)  lower is better — SLA metric

  Records are plain dicts so that results from any source (live agent,
  test harness, replay) can be fed in uniformly.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Expected record schema (for documentation purposes)
# ---------------------------------------------------------------------------
# Each record dict should look like:
# {
#   "document_id":          str,
#   "partner":              str,
#   "ground_truth":         "compliant" | "non_compliant",
#   "agent_verdict":        "pass" | "fail" | "escalated",
#   "chargeback_avoided":   bool,   # True = agent prevented a chargeback
#   "false_positive":       bool,   # True = agent failed a compliant document
#   "escalated":            bool,   # True = routed to human review
#   "retrieval_ranks":      list[int],  # rank of correct result in top-5 (1-based, 0 if not found)
#   "diagnostic_resolved":  bool,   # True = root cause identified without escalation
#   "latency_seconds":      float,
# }


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------

@dataclass
class MetricsReport:
    """Computed evaluation metrics for a batch of validation records."""
    record_count: int
    chargeback_prevention_rate: float   # 0.0–1.0
    false_positive_rate: float          # 0.0–1.0
    escalation_rate: float              # 0.0–1.0
    mean_reciprocal_rank: float         # 0.0–1.0 (MRR over top-5)
    diagnostic_resolution_rate: float   # 0.0–1.0
    avg_latency_seconds: float
    p95_latency_seconds: float
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Targets (for comparison in generate_report)
# ---------------------------------------------------------------------------

TARGETS: dict[str, Any] = {
    "chargeback_prevention_rate": {"min": 0.98, "label": "≥ 98%"},
    "false_positive_rate":        {"max": 0.05, "label": "< 5%"},
    "escalation_rate":            {"min": 0.10, "max": 0.15, "label": "10–15%"},
    "mean_reciprocal_rank":       {"min": 0.70, "label": "≥ 0.70"},
    "diagnostic_resolution_rate": {"min": 0.80, "label": "≥ 80%"},
    "avg_latency_seconds":        {"max": 10.0, "label": "< 10 s"},
}


# ---------------------------------------------------------------------------
# EvaluationMetrics
# ---------------------------------------------------------------------------

class EvaluationMetrics:
    """
    Compute evaluation metrics from a list of validation records.

    Usage::

        em = EvaluationMetrics()
        report = em.compute(records)
        em.generate_report(report)
    """

    # ── Individual metric computations ──────────────────────────────────────

    def chargeback_prevention_rate(self, records: list[dict]) -> float:
        """
        Fraction of records where a chargeback was avoided.

        Only counts records where the ground truth was non_compliant and the
        agent correctly flagged the issue before submission.
        """
        at_risk = [r for r in records if r.get("ground_truth") == "non_compliant"]
        if not at_risk:
            return 1.0  # No non-compliant docs → no chargebacks possible
        avoided = sum(1 for r in at_risk if r.get("chargeback_avoided", False))
        return avoided / len(at_risk)

    def false_positive_rate(self, records: list[dict]) -> float:
        """
        Fraction of compliant documents incorrectly flagged as non-compliant.

        A false positive wastes the compliance team's time and can delay
        legitimate shipments.
        """
        compliant_docs = [r for r in records if r.get("ground_truth") == "compliant"]
        if not compliant_docs:
            return 0.0
        fp = sum(1 for r in compliant_docs if r.get("false_positive", False))
        return fp / len(compliant_docs)

    def escalation_rate(self, records: list[dict]) -> float:
        """
        Fraction of records that were escalated to human review.

        Target 10–15%: too low means the agent is overconfident; too high
        means it lacks discriminative ability.
        """
        if not records:
            return 0.0
        escalated = sum(1 for r in records if r.get("escalated", False))
        return escalated / len(records)

    def mean_reciprocal_rank(self, records: list[dict]) -> float:
        """
        Mean Reciprocal Rank (MRR) over top-5 retrieval results.

        Each record's ``retrieval_ranks`` is a list of ranks (1-based) where
        the correct spec section appeared in the retrieval results.
        If not found in top-5, rank is 0 (contributes 0 to MRR).
        """
        rr_values: list[float] = []
        for r in records:
            ranks: list[int] = r.get("retrieval_ranks", [])
            if not ranks:
                rr_values.append(0.0)
                continue
            # Best rank found (lowest rank number = most relevant)
            best_rank = min((rank for rank in ranks if rank > 0), default=0)
            rr_values.append(1.0 / best_rank if best_rank > 0 else 0.0)

        return statistics.mean(rr_values) if rr_values else 0.0

    def diagnostic_resolution_rate(self, records: list[dict]) -> float:
        """
        Fraction of non-compliant documents where root cause was identified
        without requiring human escalation.
        """
        failed = [r for r in records if r.get("ground_truth") == "non_compliant"]
        if not failed:
            return 1.0
        resolved = sum(1 for r in failed if r.get("diagnostic_resolved", False))
        return resolved / len(failed)

    def latency_stats(self, records: list[dict]) -> tuple[float, float]:
        """
        Return (average_latency, p95_latency) in seconds.
        """
        latencies = [r.get("latency_seconds", 0.0) for r in records if "latency_seconds" in r]
        if not latencies:
            return (0.0, 0.0)
        avg = statistics.mean(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        return avg, p95

    # ── Combined compute ────────────────────────────────────────────────────

    def compute(self, records: list[dict]) -> MetricsReport:
        """
        Compute all metrics from a list of validation records.

        Returns a ``MetricsReport`` dataclass with all computed values and
        any threshold warnings.
        """
        if not records:
            raise ValueError("Cannot compute metrics on an empty record list.")

        avg_lat, p95_lat = self.latency_stats(records)
        cp_rate  = self.chargeback_prevention_rate(records)
        fp_rate  = self.false_positive_rate(records)
        esc_rate = self.escalation_rate(records)
        mrr      = self.mean_reciprocal_rank(records)
        dr_rate  = self.diagnostic_resolution_rate(records)

        warnings: list[str] = []
        if cp_rate < TARGETS["chargeback_prevention_rate"]["min"]:
            warnings.append(
                f"⚠️  chargeback_prevention_rate {cp_rate:.1%} is below target "
                f"{TARGETS['chargeback_prevention_rate']['label']}"
            )
        if fp_rate > TARGETS["false_positive_rate"]["max"]:
            warnings.append(
                f"⚠️  false_positive_rate {fp_rate:.1%} exceeds target "
                f"{TARGETS['false_positive_rate']['label']}"
            )
        esc_min = TARGETS["escalation_rate"]["min"]
        esc_max = TARGETS["escalation_rate"]["max"]
        if not (esc_min <= esc_rate <= esc_max):
            warnings.append(
                f"⚠️  escalation_rate {esc_rate:.1%} outside target band "
                f"{TARGETS['escalation_rate']['label']}"
            )
        if mrr < TARGETS["mean_reciprocal_rank"]["min"]:
            warnings.append(
                f"⚠️  mean_reciprocal_rank {mrr:.3f} below target "
                f"{TARGETS['mean_reciprocal_rank']['label']}"
            )
        if dr_rate < TARGETS["diagnostic_resolution_rate"]["min"]:
            warnings.append(
                f"⚠️  diagnostic_resolution_rate {dr_rate:.1%} below target "
                f"{TARGETS['diagnostic_resolution_rate']['label']}"
            )
        if avg_lat > TARGETS["avg_latency_seconds"]["max"]:
            warnings.append(
                f"⚠️  avg_latency {avg_lat:.2f}s exceeds target "
                f"{TARGETS['avg_latency_seconds']['label']}"
            )

        return MetricsReport(
            record_count=len(records),
            chargeback_prevention_rate=cp_rate,
            false_positive_rate=fp_rate,
            escalation_rate=esc_rate,
            mean_reciprocal_rank=mrr,
            diagnostic_resolution_rate=dr_rate,
            avg_latency_seconds=avg_lat,
            p95_latency_seconds=p95_lat,
            warnings=warnings,
        )

    # ── Report printer ──────────────────────────────────────────────────────

    def generate_report(self, report: MetricsReport) -> None:
        """
        Print a human-readable metrics summary to stdout.

        Format is intentionally plain-text so it displays well in terminals,
        notebooks, and CI logs without any external dependencies.
        """
        divider = "─" * 60
        print(f"\n{divider}")
        print("  FirstPass EDI — Evaluation Metrics Report")
        print(divider)
        print(f"  Records evaluated : {report.record_count}")
        print(divider)

        def _check(value: float, target_key: str) -> str:
            t = TARGETS[target_key]
            if "min" in t and value < t["min"]:
                return "✗ BELOW TARGET"
            if "max" in t and value > t["max"]:
                return "✗ ABOVE TARGET"
            return "✓ on target"

        rows: list[tuple[str, str, str, str]] = [
            ("Chargeback prevention rate",
             f"{report.chargeback_prevention_rate:.1%}",
             TARGETS["chargeback_prevention_rate"]["label"],
             _check(report.chargeback_prevention_rate, "chargeback_prevention_rate")),

            ("False positive rate",
             f"{report.false_positive_rate:.1%}",
             TARGETS["false_positive_rate"]["label"],
             _check(report.false_positive_rate, "false_positive_rate")),

            ("Escalation rate",
             f"{report.escalation_rate:.1%}",
             TARGETS["escalation_rate"]["label"],
             _check(report.escalation_rate, "escalation_rate")),

            ("Retrieval relevance (MRR@5)",
             f"{report.mean_reciprocal_rank:.3f}",
             TARGETS["mean_reciprocal_rank"]["label"],
             _check(report.mean_reciprocal_rank, "mean_reciprocal_rank")),

            ("Diagnostic resolution rate",
             f"{report.diagnostic_resolution_rate:.1%}",
             TARGETS["diagnostic_resolution_rate"]["label"],
             _check(report.diagnostic_resolution_rate, "diagnostic_resolution_rate")),

            ("Avg latency / document",
             f"{report.avg_latency_seconds:.2f}s",
             TARGETS["avg_latency_seconds"]["label"],
             _check(report.avg_latency_seconds, "avg_latency_seconds")),

            ("P95 latency / document",
             f"{report.p95_latency_seconds:.2f}s",
             "—",
             ""),
        ]

        col_widths = (33, 10, 14, 20)
        for name, value, target, status in rows:
            print(
                f"  {name:<{col_widths[0]}} "
                f"{value:>{col_widths[1]}}  "
                f"target {target:<{col_widths[2]}} "
                f"{status}"
            )

        print(divider)
        if report.warnings:
            print("  Warnings:")
            for w in report.warnings:
                print(f"    {w}")
        else:
            print("  All metrics within target ranges. ✓")
        print(f"{divider}\n")
