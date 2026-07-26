"""
orderful.py — Orderful API v3 connector.

Submits outbound X12 documents (855, 856, 810, 997) and polls for inbound
transactions (850 purchase orders).

Network calls are made only when an API key is configured.  When unconfigured
(e.g. local development / demo mode), ``submit()`` returns a simulated
transaction ID so the rest of the pipeline can be exercised end-to-end.

Orderful docs: https://docs.orderful.com/
"""
from __future__ import annotations

import uuid
import logging
from typing import Any, Dict, List, Optional

import requests

from ..config import config

log = logging.getLogger("firstpass.connectors.orderful")


class OrderfulError(Exception):
    """Raised on a 4xx/5xx response from the Orderful API."""


class OrderfulClient:
    """Orderful v3 REST API adapter."""

    PLATFORM = "orderful"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key if api_key is not None else config.ORDERFUL_API_KEY
        self.base_url = (base_url or config.ORDERFUL_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "orderful-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    # ── Outbound (submit) ─────────────────────────────────────────────────

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> Dict[str, Any]:
        """
        POST an X12 document to Orderful.

        :param x12_string: Raw X12 content (segments delimited by ~).
        :param trading_partner: Orderful trading partner ID or ISA qualifier.
        :param doc_type: X12 transaction set type ("997", "855", "856", "810").
        :returns: {"transaction_id": str, "ok": bool}
        """
        if not self.configured:
            sim_id = f"SIM-{doc_type}-{uuid.uuid4().hex[:10].upper()}"
            log.info(f"Dry-run submit: {doc_type} → {sim_id}")
            return {"transaction_id": sim_id, "ok": True, "dry_run": True}

        url = f"{self.base_url}/transactions"
        payload = {
            "tradingPartnerId": trading_partner,
            "transactionType": doc_type,
            "format": "X12",
            "contents": x12_string,
        }
        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
        except requests.RequestException as exc:
            raise OrderfulError(f"Network error submitting {doc_type}: {exc}") from exc

        if resp.status_code >= 400:
            raise OrderfulError(
                f"Orderful returned HTTP {resp.status_code} for {doc_type}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except ValueError:
            return {"transaction_id": resp.text.strip() or "UNKNOWN", "ok": True}

        tx_id = str(data.get("id") or data.get("transactionId") or "UNKNOWN")
        return {"transaction_id": tx_id, "ok": True, "raw": data}

    # ── Inbound (poll) ────────────────────────────────────────────────────

    def fetch_inbound(self, doc_type: str = "850", status: str = "received") -> List[Dict]:
        """
        Poll for inbound X12 transactions of the given type.

        :param doc_type: X12 transaction set type to fetch (typically "850").
        :param status: Transaction status filter (e.g. "received", "pending").
        :returns: List of {"id": str, "x12": str} dicts.
        """
        if not self.configured:
            return []

        url = f"{self.base_url}/transactions"
        params = {"direction": "inbound", "status": status, "transactionType": doc_type, "limit": 50}
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        except requests.RequestException as exc:
            raise OrderfulError(f"Network error fetching inbound {doc_type}: {exc}") from exc

        if resp.status_code >= 400:
            raise OrderfulError(
                f"Orderful returned HTTP {resp.status_code} fetching {doc_type}: {resp.text[:200]}"
            )

        data = resp.json() if resp.content else {}
        items = (
            data.get("transactions") or data.get("data") or
            (data if isinstance(data, list) else [])
        )
        out = []
        for tx in items:
            x12 = tx.get("contents") or tx.get("x12") or tx.get("payload")
            if x12:
                out.append({"id": tx.get("id") or tx.get("transactionId"), "x12": x12})
        return out

    def acknowledge(self, transaction_id: str, status: str = "DELIVERED") -> bool:
        """Mark an inbound transaction as processed (best-effort, no-op in dry-run)."""
        if not self.configured or not transaction_id:
            return False
        url = f"{self.base_url}/transactions/{transaction_id}/status"
        try:
            resp = requests.post(
                url, headers=self._headers(), json={"status": status}, timeout=10
            )
            return resp.status_code < 400
        except Exception as exc:
            log.warning(f"Orderful acknowledge failed: {exc}")
            return False
