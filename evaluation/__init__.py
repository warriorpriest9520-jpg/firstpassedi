"""
evaluation/__init__.py
───────────────────────
Public exports for the FirstPass EDI evaluation layer.

Import pattern::

    from evaluation import EvaluationMetrics, MetricsReport
    from evaluation.test_cases import run_all
"""

from evaluation.metrics import EvaluationMetrics, MetricsReport, TARGETS

__all__ = [
    "EvaluationMetrics",
    "MetricsReport",
    "TARGETS",
]
