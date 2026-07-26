"""
models.py — Shared EDI data models for FirstPass EDI.

Provides Pydantic models for EDI documents, workflows, connectors,
and workflow-builder artefacts used throughout the FirstPass EDI platform.

Note: ``from __future__ import annotations`` is intentionally omitted to
avoid PEP 563 conflicts with Pydantic v1/v2 field resolution.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class TrayWorkflow(BaseModel):
    """A Tray.io workflow discovered via UI scraping or API."""

    id: str = Field(default="", description="Workflow ID from Tray.io URL")
    name: str = Field(description="Workflow display name")
    status: str = Field(default="unknown", description="enabled, disabled, error, or unknown")
    trigger_type: str = Field(default="unknown", description="webhook, schedule, manual, or unknown")
    description: str = Field(default="", description="Workflow description")
    connectors_used: List[str] = Field(default_factory=list, description="Connector names used in workflow")
    edi_related: bool = Field(default=False, description="Whether workflow is EDI-related")
    last_run: Optional[str] = Field(default=None, description="ISO timestamp of last execution")
    url: Optional[str] = Field(default=None, description="Direct URL to workflow in Tray.io")

    def summary(self) -> str:
        """Human-readable summary."""
        parts = [
            f"Workflow: {self.name}",
            f"  Status: {self.status}",
            f"  Trigger: {self.trigger_type}",
        ]
        if self.edi_related:
            parts.append("  EDI: Yes")
        if self.connectors_used:
            parts.append(f"  Connectors: {', '.join(self.connectors_used)}")
        if self.last_run:
            parts.append(f"  Last run: {self.last_run}")
        if self.description:
            parts.append(f"  Description: {self.description}")
        return "\n".join(parts)


class EDIDocument(BaseModel):
    """An EDI document transaction record."""

    doc_type: str = Field(description="EDI document type: 850, 810, 856, 846, 997")
    direction: str = Field(description="inbound or outbound")
    trading_partner: str = Field(description="Trading partner name")
    status: str = Field(default="unknown", description="success, error, pending, or unknown")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp")
    workflow_name: Optional[str] = Field(default=None, description="Associated automation workflow")
    error_message: Optional[str] = Field(default=None, description="Error details if status is error")

    def summary(self) -> str:
        """Human-readable summary."""
        parts = [
            f"EDI {self.doc_type} ({self.direction})",
            f"  Partner: {self.trading_partner}",
            f"  Status: {self.status}",
        ]
        if self.timestamp:
            parts.append(f"  Time: {self.timestamp}")
        if self.workflow_name:
            parts.append(f"  Workflow: {self.workflow_name}")
        if self.error_message:
            parts.append(f"  Error: {self.error_message}")
        return "\n".join(parts)


class TrayConnector(BaseModel):
    """A Tray.io connector/integration."""

    name: str = Field(description="Connector name")
    type: str = Field(default="unknown", description="HTTP, SFTP, Database, Email, etc.")
    status: str = Field(default="unknown", description="connected, disconnected, error")
    connected_to: Optional[str] = Field(default=None, description="System this connector links to")


class WorkflowLog(BaseModel):
    """A single workflow execution log entry."""

    workflow_name: str = Field(description="Name of the workflow")
    execution_id: str = Field(default="", description="Execution ID")
    status: str = Field(default="unknown", description="success, failed, running, or unknown")
    started_at: Optional[str] = Field(default=None, description="ISO timestamp")
    completed_at: Optional[str] = Field(default=None, description="ISO timestamp")
    error_message: Optional[str] = Field(default=None, description="Error details if failed")
    steps_completed: int = Field(default=0, description="Number of steps completed")

    def summary(self) -> str:
        """Human-readable summary."""
        parts = [
            f"Execution: {self.execution_id or '(no id)'}",
            f"  Workflow: {self.workflow_name}",
            f"  Status: {self.status}",
        ]
        if self.started_at:
            parts.append(f"  Started: {self.started_at}")
        if self.completed_at:
            parts.append(f"  Completed: {self.completed_at}")
        if self.error_message:
            parts.append(f"  Error: {self.error_message}")
        return "\n".join(parts)


class EnterpriseSystemStatus(BaseModel):
    """Status of an enterprise system."""

    name: str = Field(description="System name")
    type: str = Field(description="System type")
    status: str = Field(default="unknown", description="online, offline, degraded, unknown")
    interfaces: List[str] = Field(default_factory=list, description="Connected interfaces")
    last_checked: Optional[str] = Field(default=None, description="ISO timestamp")


# ---------------------------------------------------------------------------
# Workflow Builder models
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """A single step within a workflow template."""

    connector: str = Field(description="Connector name (e.g. 'SFTP', 'HTTP Client', 'Script')")
    operation: str = Field(description="Operation to select (e.g. 'Download File', 'Run Script')")
    name: str = Field(description="Display name for the step in the builder")
    config: Dict[str, str] = Field(default_factory=dict, description="Key-value config for the step")

    def summary(self) -> str:
        return f"{self.name} ({self.connector} -> {self.operation})"


class WorkflowTemplate(BaseModel):
    """Complete template for building an EDI workflow."""

    name: str = Field(description="Workflow display name")
    template_key: str = Field(description="Short key for lookup (e.g. '850', '810', 'error_monitor')")
    trigger_type: str = Field(default="manual", description="webhook, schedule, or manual")
    trigger_config: Dict[str, str] = Field(default_factory=dict, description="Trigger-specific config")
    steps: List[WorkflowStep] = Field(default_factory=list, description="Ordered list of workflow steps")
    description: str = Field(default="", description="Human-readable description of the workflow")
    edi_doc_type: Optional[str] = Field(default=None, description="EDI doc type if applicable (850, 810, etc.)")
    direction: Optional[str] = Field(default=None, description="inbound, outbound, or both")

    def summary(self) -> str:
        parts = [
            f"Template: {self.name} [{self.template_key}]",
            f"  Trigger: {self.trigger_type}",
            f"  Steps: {len(self.steps)}",
        ]
        if self.description:
            parts.append(f"  Description: {self.description}")
        for i, step in enumerate(self.steps, 1):
            parts.append(f"  {i}. {step.summary()}")
        return "\n".join(parts)


class WorkflowBuildResult(BaseModel):
    """Result from building a workflow."""

    template_name: str = Field(description="Template that was used")
    workflow_name: str = Field(description="Name of the created workflow")
    success: bool = Field(description="Whether the build completed successfully")
    steps_completed: int = Field(default=0, description="Number of steps successfully added")
    steps_total: int = Field(default=0, description="Total steps in the template")
    errors: List[str] = Field(default_factory=list, description="Error messages for failed steps")
    url: str = Field(default="", description="URL of the created workflow")

    def summary(self) -> str:
        status = "SUCCESS" if self.success else "PARTIAL" if self.steps_completed > 0 else "FAILED"
        parts = [
            f"Build {status}: {self.workflow_name}",
            f"  Template: {self.template_name}",
            f"  Steps: {self.steps_completed}/{self.steps_total}",
        ]
        if self.url:
            parts.append(f"  URL: {self.url}")
        for err in self.errors:
            parts.append(f"  ERROR: {err}")
        return "\n".join(parts)
