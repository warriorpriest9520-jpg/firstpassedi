"""
logicbroker.py — LogicBroker / CommerceHub REST API connector.

Polls for inbound 850 Purchase Orders and submits outbound documents
(855, 856, 810) via the LogicBroker REST API.

Dry-run mode (no API key): ``fetch_inbound()`` returns [] and ``submit()``
returns a simulated ID so the pipeline stays exercisable without credentials.

LogicBroker API docs: https://developers.logicbroker.com/
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import requests

from ..config import config

log = logging.getLogger("firstpass.connectors.logicbroker")


class LogicBrokerError(Exception):
    """Raised on a 4xx/5xx from the LogicBroker API."""


class LogicBrokerClient:
    """LogicBroker REST API adapter."""

    PLATFORM = "logicbroker"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key if api_key is not None else config.LOGICBROKER_API_KEY
        self.base_url = (base_url or config.LOGICBROKER_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "subscription-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Inbound ───────────────────────────────────────────────────────────

    def fetch_inbound(self, status: str = "Pending") -> List[Dict]:
        """
        Poll for new purchase orders with the given status.

        :returns: List of {"id": str, "x12": str, "partner": str} dicts.
        """
        if not self.configured:
            return []
        url = f"{self.base_url}/purchaseorders"
        try:
            resp = requests.get(
                url, headers=self._headers(),
                params={"filters.status": status, "filters.take": 50},
                timeout=30,
            )
            if resp.status_code >= 400:
                raise LogicBrokerError(
                    f"HTTP {resp.status_code} fetching POs: {resp.text[:200]}"
                )
            data = resp.json()
            orders = data.get("Body") or data.get("Data") or (data if isinstance(data, list) else [])
            out = []
            for order in orders:
                x12 = order.get("Content") or order.get("X12") or order.get("payload") or ""
                if x12:
                    out.append({
                        "id": str(order.get("LinkKey") or order.get("Id") or uuid.uuid4()),
                        "x12": x12,
                        "partner": order.get("TradingPartnerId") or order.get("SenderCompanyId", ""),
                    })
            return out
        except LogicBrokerError:
            raise
        except Exception as exc:
            log.error(f"LogicBroker fetch_inbound failed: {exc}")
            return []

    # ── Outbound ──────────────────────────────────────────────────────────

    def submit(self, x12_string: str, trading_partner: str, doc_type: str) -> Dict[str, Any]:
        """
        Submit an outbound X12 document to LogicBroker.

        :returns: {"transaction_id": str, "ok": bool}
        """
        if not self.configured:
            sim_id = f"SIM-LB-{doc_type}-{uuid.uuid4().hex[:8].upper()}"
            log.info(f"Dry-run submit to LogicBroker: {doc_type} → {sim_id}")
            return {"transaction_id": sim_id, "ok": True, "dry_run": True}

        # Map doc_type to LogicBroker endpoint
        endpoint_map = {
            "855": "purchaseorderconfirmations",
            "856": "shipmentnotices",
            "810": "invoices",
            "997": "acknowledgments",
        }
        endpoint = endpoint_map.get(doc_type, "documents")
        url = f"{self.base_url}/{endpoint}"
        payload = {
            "TradingPartnerId": trading_partner,
            "Content": x12_string,
            "Format": "X12",
        }
        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code >= 400:
                raise LogicBrokerError(
                    f"HTTP {resp.status_code} submitting {doc_type}: {resp.text[:200]}"
                )
            data = resp.json()
            tx_id = str(data.get("Id") or data.get("LinkKey") or "UNKNOWN")
            return {"transaction_id": tx_id, "ok": True}
        except LogicBrokerError:
            raise
        except Exception as exc:
            raise LogicBrokerError(f"Network error submitting {doc_type}: {exc}") from exc

    def acknowledge_order(self, link_key: str) -> bool:
        """Mark a purchase order as received/processing (best-effort)."""
        if not self.configured or not link_key:
            return False
        try:
            url = f"{self.base_url}/purchaseorders/{link_key}/status"
            resp = requests.put(
                url, headers=self._headers(),
                json={"StatusCode": "Processing"},
                timeout=10,
            )
            return resp.status_code < 400
        except Exception as exc:
            log.warning(f"LogicBroker acknowledge_order failed: {exc}")
            return False
