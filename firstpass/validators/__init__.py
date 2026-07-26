"""
firstpass.validators — EDI spec parsing, validation, and rules engine.

Submodules
----------
spec_parser
    LLM-powered EDI partner spec → FieldMap, JS transform, and validator rules.
spec_validator
    Deep payload validation against partner specs (segment presence, element
    types, syntax rules, silent-failure detection).
x12_validator
    Data-quality validator for 850 / 856 / 810 / 846 transactions; dispatches
    to per-document validator functions via the partner spec.
spec_rules
    Composable partner-specific rule engine (value maps, frequency caps,
    schedule alerts, required-field checks, regex rules).
models
    Shared Pydantic models for EDI documents, workflows, connectors, and
    workflow-builder artefacts.

Typical import pattern::

    from firstpass.validators import spec_parser, spec_validator, x12_validator
    from firstpass.validators.spec_validator import EDISpecValidator, validate
    from firstpass.validators.x12_validator import EDIValidator
    from firstpass.validators.spec_rules import apply_all_rules, schedule_alerts
    from firstpass.validators.spec_parser import EDISpecParser
    from firstpass.validators.models import EDIDocument, WorkflowTemplate
"""

from firstpass.validators import models, spec_parser, spec_rules, spec_validator, x12_validator

# Convenience re-exports
from firstpass.validators.models import (
    EDIDocument,
    EnterpriseSystemStatus,
    TrayConnector,
    TrayWorkflow,
    WorkflowBuildResult,
    WorkflowLog,
    WorkflowStep,
    WorkflowTemplate,
)
from firstpass.validators.spec_parser import EDISpecParser, ParseResult, EDIFieldMap, SegmentField
from firstpass.validators.spec_rules import apply_all_rules, schedule_alerts
from firstpass.validators.spec_validator import EDISpecValidator, validate, load_validator
from firstpass.validators.x12_validator import (
    EDIValidator,
    validate_850,
    validate_856,
    validate_810,
    validate_846,
)

__all__ = [
    # Submodules
    "models",
    "spec_parser",
    "spec_rules",
    "spec_validator",
    "x12_validator",
    # Classes
    "EDISpecParser",
    "EDISpecValidator",
    "EDIValidator",
    "EDIDocument",
    "EDIFieldMap",
    "EnterpriseSystemStatus",
    "ParseResult",
    "SegmentField",
    "TrayConnector",
    "TrayWorkflow",
    "WorkflowBuildResult",
    "WorkflowLog",
    "WorkflowStep",
    "WorkflowTemplate",
    # Functions
    "apply_all_rules",
    "load_validator",
    "schedule_alerts",
    "validate",
    "validate_850",
    "validate_856",
    "validate_810",
    "validate_846",
]
