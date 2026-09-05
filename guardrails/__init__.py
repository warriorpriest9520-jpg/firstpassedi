"""
guardrails/__init__.py
──────────────────────
Public exports for the FirstPass EDI guardrail layer.

Import pattern::

    from guardrails import SchemaValidator, ConfidenceGate, StalenessCheck
    from guardrails import ValidationResult, GateableResult, StalenessResult
"""

from guardrails.schema_validator import SchemaValidator, ValidationResult, StructuralError
from guardrails.confidence_gate import ConfidenceGate, GateableResult
from guardrails.staleness_check import StalenessCheck, StalenessResult

__all__ = [
    # Schema validation
    "SchemaValidator",
    "ValidationResult",
    "StructuralError",
    # Confidence gating
    "ConfidenceGate",
    "GateableResult",
    # Staleness checking
    "StalenessCheck",
    "StalenessResult",
]
