"""
erp.py — Generic ERP connector.

Provides a platform-agnostic interface for creating/updating orders in an ERP
system (Sage, SAP, NetSuite, etc.).  The default implementation uses a REST
API but can be swapped for a SQL or SFTP-based approach by overriding methods.

In dry-run mode (no ERP URL configured), order creation returns a synthetic
order number so the pipeline remains exercisable without a live ERP.

Source lineage: roi_order_importer.py (ROI InSynch / Sage integration)
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional

from ..config import config

log = logging.getLogger("firstpass.connectors.erp")


class ERPError(Exception):
    """Raised on ERP integration failures."""


class ERPConnector:
    """
    Generic ERP connector.

    Configure via environment variables:
        ERP_BASE_URL    — Base URL of the ERP REST adapter (e.g. http://erp-api.local:5000)
        ERP_API_KEY     — API key / bearer token for the ERP adapter
        ERP_SYSTEM      — ERP system name for logging (e.g. "Sage", "NetSuite")
    """

    def __init__(self):
        self.base_url = os.getenv("ERP_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("ERP_API_KEY", "")
        self.system = os.getenv("ERP_SYSTEM", "GenericERP")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ── Core interface ────────────────────────────────────────────────────

    def create_order(self, order) -> Optional[str]:
        """
        Create a sales order in the ERP from a parsed Order object.

        :param order: A firstpass.agents.edi_agent.Order instance.
        :returns: ERP order number (string), or None on failure.
        """
        if not self.configured:
            synthetic = f"ERP-{uuid.uuid4().hex[:8].upper()}"
            log.info(f"Dry-run: ERP order created with synthetic ID {synthetic}")
            return synthetic

        import requests
        url = f"{self.base_url}/orders"
        payload = self._order_to_erp(order)
        try:
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code >= 400:
                raise ERPError(f"{self.system} returned HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            erp_no = str(data.get("order_number") or data.get("orderId") or data.get("id") or "")
            log.info(f"ERP order created: PO={order.po_number} → {self.system} #{erp_no}")
            return erp_no
        except ERPError:
            raise
        except Exception as exc:
            log.error(f"ERP create_order failed: {exc}")
            return None

    def get_order(self, erp_order_no: str) -> Optional[Dict]:
        """Retrieve order details from the ERP by order number."""
        if not self.configured:
            return None
        import requests
        try:
            resp = requests.get(
                f"{self.base_url}/orders/{erp_order_no}",
                headers=self._headers(), timeout=15,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise ERPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        except ERPError:
            raise
        except Exception as exc:
            log.error(f"ERP get_order({erp_order_no}) failed: {exc}")
            return None

    def update_order_status(self, erp_order_no: str, status: str, note: str = "") -> bool:
        """Update order status in the ERP."""
        if not self.configured:
            log.info(f"Dry-run: ERP update {erp_order_no} → {status}")
            return True
        import requests
        try:
            resp = requests.patch(
                f"{self.base_url}/orders/{erp_order_no}",
                headers=self._headers(),
                json={"status": status, "note": note},
                timeout=15,
            )
            return resp.status_code < 400
        except Exception as exc:
            log.error(f"ERP update_order_status failed: {exc}")
            return False

    # ── Mapping ───────────────────────────────────────────────────────────

    def _order_to_erp(self, order) -> Dict[str, Any]:
        """
        Convert a FirstPass Order object to the ERP's expected payload shape.

        Override this method to match your ERP's field names and structure.
        """
        return {
            "customer_po": order.po_number,
            "trading_partner": order.partner,
            "ship_to": order.ship_to,
            "bill_to": order.bill_to,
            "requested_ship_date": order.requested_ship_date,
            "lines": [
                {
                    "line_number": li["line_num"],
                    "sku": li["sku"],
                    "quantity_ordered": li["qty"],
                    "unit_price": li["unit_price"],
                    "unit_of_measure": li.get("unit_of_measure", "EA"),
                }
                for li in order.line_items
            ],
            "total_value": order.total_value,
            "source": "firstpass_edi",
        }
