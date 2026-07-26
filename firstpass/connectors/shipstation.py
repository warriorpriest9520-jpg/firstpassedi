"""
shipstation.py — ShipStation REST API connector.

Pulls shipped-order data per PO number and normalizes it to the internal shape
that the 856 ASN and 810 invoice generators expect.

Auth: HTTP Basic with base64(API_KEY:API_SECRET).

Operates in dry-run mode (returns None from ``get_shipment()``) when API keys
are not configured, so the pipeline stays exercisable locally.

ShipStation API docs: https://www.shipstation.com/docs/api/
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from ..config import config

log = logging.getLogger("firstpass.connectors.shipstation")

# ShipStation carrierCode → X12 SCAC-style carrier code
CARRIER_MAP = {
    "ups": "UPSN",
    "ups_walleted": "UPSN",
    "fedex": "FDXG",
    "fedex_international": "FDXG",
    "usps": "USPS",
    "stamps_com": "USPS",
    "dhl_express": "DHLC",
    "ontrac": "ONTC",
    "spee_dee": "SPDE",
    "estes": "ESTS",
}


class ShippingError(Exception):
    """Raised on a non-2xx response from ShipStation."""


class ShippingConnector:
    """ShipStation REST API adapter."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key if api_key is not None else config.SHIPSTATION_API_KEY
        self.api_secret = api_secret if api_secret is not None else config.SHIPSTATION_API_SECRET
        self.base_url = (base_url or config.SHIPSTATION_BASE_URL).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _headers(self) -> Dict[str, str]:
        token = base64.b64encode(f"{self.api_key}:{self.api_secret}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    # ── Main interface ─────────────────────────────────────────────────────

    def get_shipment(self, po_number: str) -> Optional[Dict]:
        """
        Return normalized ship data for a PO number, or None if not shipped yet.

        Returned shape::

            {
                "ship_date":        "YYYYMMDD",
                "ship_time":        "HHMM",
                "carrier_code":     "UPSN",         # X12 SCAC code
                "service_level":    "GROUND",
                "tracking_numbers": ["1Z..."],
                "packages": [
                    {
                        "tracking":    "1Z...",
                        "weight_lbs":  12.5,
                        "lines": [{"line_num": "1", "qty_shipped": 10}]
                    }
                ],
            }
        """
        if not self.configured:
            return None

        # Try direct orderNumber search first
        result = self._fetch_by_order_number(po_number)
        if result is not None:
            return result

        # Fallback: search via orders API (some partners store PO in customField1)
        return self._fetch_via_order_search(po_number)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _fetch_by_order_number(self, order_number: str) -> Optional[Dict]:
        """Query /shipments?orderNumber=<po>."""
        url = f"{self.base_url}/shipments"
        try:
            resp = requests.get(
                url, headers=self._headers(),
                params={"orderNumber": order_number},
                timeout=15,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                log.warning(f"ShipStation rate limited — sleeping {retry_after}s")
                time.sleep(retry_after)
                resp = requests.get(url, headers=self._headers(),
                                    params={"orderNumber": order_number}, timeout=15)
            if resp.status_code >= 400:
                raise ShippingError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            shipments = data.get("shipments") or (data if isinstance(data, list) else [])
            if shipments:
                return self._normalize(shipments)
        except ShippingError:
            raise
        except Exception as exc:
            log.debug(f"_fetch_by_order_number({order_number}) failed: {exc}")
        return None

    def _fetch_via_order_search(self, po_number: str) -> Optional[Dict]:
        """Search /orders?orderNumber=<po> then look up shipments."""
        url = f"{self.base_url}/orders"
        try:
            resp = requests.get(
                url, headers=self._headers(),
                params={"orderNumber": po_number, "orderStatus": "shipped"},
                timeout=15,
            )
            if resp.status_code >= 400:
                return None
            orders = resp.json().get("orders") or []
            if not orders:
                return None
            order_id = orders[0].get("orderId")
            if not order_id:
                return None
            ship_resp = requests.get(
                f"{self.base_url}/shipments",
                headers=self._headers(),
                params={"orderId": order_id},
                timeout=15,
            )
            if ship_resp.status_code >= 400:
                return None
            shipments = ship_resp.json().get("shipments") or []
            return self._normalize(shipments) if shipments else None
        except Exception as exc:
            log.debug(f"_fetch_via_order_search({po_number}) failed: {exc}")
        return None

    def _normalize(self, shipments: List[Dict]) -> Optional[Dict]:
        """Normalize raw ShipStation shipment records to internal shape."""
        if not shipments:
            return None
        s = shipments[0]
        ship_date_raw = s.get("shipDate") or s.get("createDate") or ""
        ship_date = ship_date_raw[:10].replace("-", "") if ship_date_raw else ""
        ship_time_raw = ship_date_raw[11:16].replace(":", "") if len(ship_date_raw) > 10 else ""
        carrier_raw = (s.get("carrierCode") or "").lower()
        carrier_code = CARRIER_MAP.get(carrier_raw, carrier_raw.upper()[:4])

        packages = []
        for pkg in s.get("shipmentItems") or []:
            tracking = s.get("trackingNumber") or s.get("tracking_number") or ""
            packages.append({
                "tracking": tracking,
                "weight_lbs": float(pkg.get("weight", {}).get("value", 0) or 0),
                "lines": [
                    {
                        "line_num": str(pkg.get("lineItemKey", "1")),
                        "qty_shipped": int(pkg.get("quantity", 0)),
                    }
                ],
            })

        return {
            "ship_date": ship_date,
            "ship_time": ship_time_raw or "0000",
            "carrier_code": carrier_code,
            "service_level": (s.get("serviceCode") or "GROUND").upper(),
            "tracking_numbers": [
                p["tracking"] for p in packages if p["tracking"]
            ] or [s.get("trackingNumber", "")],
            "packages": packages,
        }
