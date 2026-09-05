"""
FirstPass EDI — Orderful Client
==================================
Handles submission of X12 856 Advance Ship Notice documents to the Orderful
EDI network, along with pre-submission structural validation.

Production note
───────────────
  All Orderful calls in this module are mocked for demo purposes.
  In production, replace the ``_mock_*`` methods with real HTTP calls to the
  Orderful API:

      Base URL: https://api.orderful.com/v3/
      Auth:     Authorization: Bearer {ORDERFUL_API_KEY}

  Key endpoints:
      POST /transactions          — submit a transaction (856, 810, 850, etc.)
      GET  /transactions/{id}     — poll ack status
      POST /transactions/validate — structural pre-validation (sandbox only)

  Full API docs: https://docs.orderful.com/reference/transactions
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


# ── Return types ──────────────────────────────────────────────────────────────


@dataclass
class ValidationIssue:
    """A single structural issue found during pre-submission validation."""

    segment: str            # Segment type where the issue was found (e.g. "TD5")
    element: str            # Element position (e.g. "TD5-03")
    severity: str           # "error" | "warning" | "info"
    message: str            # Human-readable description


@dataclass
class StructureValidationResult:
    """Result of Orderful's pre-submission structural check."""

    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    raw_response: dict = field(default_factory=dict)


@dataclass
class SubmissionResult:
    """Result of a 856 submission attempt to the Orderful network."""

    success: bool
    transaction_id: str         # Orderful-assigned transaction ID
    ack_status: str             # "accepted" | "rejected" | "pending"
    message: str                # Human-readable status message
    timestamp: str              # ISO 8601 UTC timestamp
    raw_response: dict = field(default_factory=dict)


# ── Client ────────────────────────────────────────────────────────────────────


class OrderfulClient:
    """
    Client for submitting 856 ASN documents to the Orderful EDI network.

    In demo mode all network calls are intercepted and realistic mock responses
    are returned based on the document content (valid docs succeed; docs with
    obvious errors are rejected).

    Parameters
    ----------
    api_key:
        Orderful API key.  Defaults to ``config.orderful_api_key``.
    environment:
        ``"sandbox"`` or ``"production"``.  Defaults to ``config.orderful_env``.
    """

    _BASE_URL = "https://api.orderful.com/v3"

    def __init__(
        self,
        api_key: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or config.orderful_api_key
        self._environment = environment or config.orderful_env
        self._demo_mode = config.demo_mode or not self._api_key
        if self._demo_mode:
            logger.info(
                "OrderfulClient running in DEMO mode (env=%s, no real HTTP calls).",
                self._environment,
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def validate_structure(self, document: str) -> StructureValidationResult:
        """
        Run Orderful's structural pre-validation on a raw X12 856 string.

        This catches envelope-level and schema-level issues before the document
        enters the EDI network, avoiding failed acknowledgements from trading
        partners.

        Parameters
        ----------
        document:
            Raw X12 EDI string (the full 856 from ISA to IEA).

        Returns
        -------
        StructureValidationResult
            ``valid=True`` means the document passed structural checks.
            Issues list contains any warnings or errors found.
        """
        logger.debug("Orderful structural validation (demo=%s) …", self._demo_mode)
        if self._demo_mode:
            return self._mock_validate_structure(document)

        # ── Production path ────────────────────────────────────────────────────
        # import requests
        # resp = requests.post(
        #     f"{self._BASE_URL}/transactions/validate",
        #     headers={
        #         "Authorization": f"Bearer {self._api_key}",
        #         "Content-Type": "application/json",
        #     },
        #     json={"content": document, "format": "edi"},
        #     timeout=15,
        # )
        # resp.raise_for_status()
        # data = resp.json()
        # issues = [
        #     ValidationIssue(
        #         segment=i["segment"], element=i["element"],
        #         severity=i["severity"], message=i["message"],
        #     )
        #     for i in data.get("issues", [])
        # ]
        # return StructureValidationResult(
        #     valid=data["valid"], issues=issues, raw_response=data
        # )
        raise NotImplementedError(
            "Production Orderful integration not implemented. Set DEMO_MODE=true."
        )

    def submit_856(self, document: str) -> SubmissionResult:
        """
        Submit a validated X12 856 document to the Orderful network.

        The document must have already passed both the internal validation pipeline
        (ValidationAgent) and ``validate_structure``.  This call places the
        transaction into the Orderful routing queue where it will be delivered to
        the trading partner's EDI mailbox.

        Parameters
        ----------
        document:
            Raw X12 EDI string (full 856, ISA through IEA).

        Returns
        -------
        SubmissionResult
            ``success=True`` and an Orderful transaction ID on acceptance.
            ``success=False`` with a descriptive message on rejection.
        """
        logger.debug("Orderful submit_856 (demo=%s) …", self._demo_mode)
        if self._demo_mode:
            return self._mock_submit_856(document)

        # ── Production path ────────────────────────────────────────────────────
        # import requests
        # resp = requests.post(
        #     f"{self._BASE_URL}/transactions",
        #     headers={
        #         "Authorization": f"Bearer {self._api_key}",
        #         "Content-Type": "application/json",
        #     },
        #     json={
        #         "content": document,
        #         "format": "edi",
        #         "type": "856",
        #     },
        #     timeout=30,
        # )
        # resp.raise_for_status()
        # data = resp.json()
        # return SubmissionResult(
        #     success=data["status"] in ("accepted", "pending"),
        #     transaction_id=data["id"],
        #     ack_status=data["status"],
        #     message=data.get("message", ""),
        #     timestamp=data["createdAt"],
        #     raw_response=data,
        # )
        raise NotImplementedError(
            "Production Orderful integration not implemented. Set DEMO_MODE=true."
        )

    # ── Mock implementations ──────────────────────────────────────────────────

    def _mock_validate_structure(self, document: str) -> StructureValidationResult:
        """
        Lightweight heuristic check used in demo mode.

        Checks for the presence of required envelope segments and returns
        synthetic Orderful-style issues for any that are missing.
        """
        issues: list[ValidationIssue] = []
        required_segments = ["ISA", "GS", "ST*856", "BSN", "SE", "GE", "IEA"]

        for seg in required_segments:
            if seg not in document:
                issues.append(
                    ValidationIssue(
                        segment=seg.split("*")[0],
                        element="",
                        severity="error",
                        message=f"Required segment '{seg}' not found in document.",
                    )
                )

        # Warn if document looks like a test (ISA15 = T)
        if "*T*" in document:
            issues.append(
                ValidationIssue(
                    segment="ISA",
                    element="ISA-15",
                    severity="warning",
                    message="ISA15 is 'T' (Test). Confirm this is intended for the sandbox.",
                )
            )

        valid = not any(i.severity == "error" for i in issues)
        raw = {
            "valid": valid,
            "issues": [
                {"segment": i.segment, "element": i.element, "severity": i.severity, "message": i.message}
                for i in issues
            ],
            "_demo": True,
        }
        return StructureValidationResult(valid=valid, issues=issues, raw_response=raw)

    def _mock_submit_856(self, document: str) -> SubmissionResult:
        """
        Return a synthetic acceptance or rejection based on document content.

        In demo mode we accept well-formed documents and reject obviously broken ones.
        """
        # Quick check: if structural validation already flagged errors, reject.
        struct_result = self._mock_validate_structure(document)
        now_iso = datetime.utcnow().isoformat() + "Z"
        txn_id = f"TXN-{uuid.uuid4().hex[:10].upper()}"

        if not struct_result.valid:
            return SubmissionResult(
                success=False,
                transaction_id=txn_id,
                ack_status="rejected",
                message=(
                    "Structural validation failed: "
                    + "; ".join(i.message for i in struct_result.issues if i.severity == "error")
                ),
                timestamp=now_iso,
                raw_response={"_demo": True, "id": txn_id, "status": "rejected"},
            )

        # Well-formed document → accept.
        logger.info("Orderful mock: submission accepted (txn_id=%s).", txn_id)
        return SubmissionResult(
            success=True,
            transaction_id=txn_id,
            ack_status="accepted",
            message="856 ASN accepted and routed to trading partner EDI mailbox.",
            timestamp=now_iso,
            raw_response={"_demo": True, "id": txn_id, "status": "accepted"},
        )
