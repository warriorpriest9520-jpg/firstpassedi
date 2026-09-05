"""
FirstPass EDI — ShipStation Client
=====================================
Retrieves shipment details (carrier, tracking, weight, ship date) from
ShipStation to cross-validate against the 856 ASN before submission.

Production note
───────────────
  This module uses mock/demo data instead of real HTTP calls.
  In production, replace ``_fetch_shipment_mock`` with an actual call to the
  ShipStation API:

      GET https://ssapi.shipstation.com/shipments?orderNumber={order_ref}
      Authorization: Basic base64(API_KEY:API_SECRET)

  Full API docs: https://www.shipstation.com/docs/api/shipments/list/

  The ``ShipmentInfo`` dataclass and method signatures stay identical —
  only the HTTP call inside ``_fetch_shipment_mock`` changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


# ── Return type ───────────────────────────────────────────────────────────────


@dataclass
class ShipmentInfo:
    """Shipment details from ShipStation (or mock)."""

    order_ref: str          # Purchase order / order reference number
    carrier: str            # Carrier name (e.g. "UPS", "FedEx Ground")
    carrier_scac: str       # SCAC code (e.g. "UPSN", "FDEG")
    tracking_number: str    # Carrier tracking number
    weight_lbs: float       # Gross shipment weight in pounds
    dimensions: dict        # {"length": f, "width": f, "height": f, "unit": "IN"}
    ship_date: str          # CCYYMMDD format
    estimated_delivery: str # CCYYMMDD format (empty string if unknown)
    found: bool             # False if the order ref wasn't found


# ── Mock data ─────────────────────────────────────────────────────────────────

# Simulates the ShipStation shipment database for demo purposes.
# Keys are order/PO reference numbers.
_MOCK_SHIPMENTS: dict[str, dict] = {
    "PO-12345": {
        "carrier": "UPS",
        "carrier_scac": "UPSN",
        "tracking_number": "1Z999AA10123456784",
        "weight_lbs": 245.5,
        "dimensions": {"length": 18.0, "width": 12.0, "height": 10.0, "unit": "IN"},
        "ship_date": "20260901",
        "estimated_delivery": "20260903",
    },
    "PO-67890": {
        "carrier": "FedEx Ground",
        "carrier_scac": "FDEG",
        "tracking_number": "449044304137821",
        "weight_lbs": 112.0,
        "dimensions": {"length": 24.0, "width": 18.0, "height": 14.0, "unit": "IN"},
        "ship_date": "20260901",
        "estimated_delivery": "20260904",
    },
    "PO-99999": {
        # Intentionally has a mismatch vs the 856 error sample (weight = 0, SCAC missing)
        "carrier": "UNKNOWN",
        "carrier_scac": "",
        "tracking_number": "",
        "weight_lbs": 0.0,
        "dimensions": {"length": 0.0, "width": 0.0, "height": 0.0, "unit": "IN"},
        "ship_date": "20260901",
        "estimated_delivery": "",
    },
}


# ── Client ────────────────────────────────────────────────────────────────────


class ShipStationClient:
    """
    Client for retrieving shipment data from ShipStation.

    In demo mode (``config.demo_mode == True`` or no API key configured),
    all requests are served from the in-memory ``_MOCK_SHIPMENTS`` dict.
    In production, swap ``_fetch_shipment_mock`` for a real HTTP request.

    Parameters
    ----------
    api_key:
        ShipStation API key.  Defaults to ``config.shipstation_api_key``.
    """

    # Production base URL for ShipStation REST API v1
    _BASE_URL = "https://ssapi.shipstation.com"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or config.shipstation_api_key
        self._demo_mode = config.demo_mode or not self._api_key
        if self._demo_mode:
            logger.info("ShipStationClient running in DEMO mode (no real HTTP calls).")

    def get_shipment(self, order_ref: str) -> ShipmentInfo:
        """
        Retrieve shipment details for a given order/PO reference.

        Parameters
        ----------
        order_ref:
            The purchase order number or internal order reference.
            Example: ``"PO-12345"``

        Returns
        -------
        ShipmentInfo
            Populated with carrier, tracking, weight, and dates.
            ``found=False`` when the reference is not in ShipStation.
        """
        logger.debug("ShipStation lookup for order_ref='%s'", order_ref)

        if self._demo_mode:
            return self._fetch_shipment_mock(order_ref)

        # ── Production path ────────────────────────────────────────────────────
        # In production, make an authenticated GET request:
        #
        #   import requests, base64
        #   auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        #   resp = requests.get(
        #       f"{self._BASE_URL}/shipments",
        #       headers={"Authorization": f"Basic {auth}"},
        #       params={"orderNumber": order_ref, "shipmentStatus": "shipped"},
        #       timeout=10,
        #   )
        #   resp.raise_for_status()
        #   data = resp.json()["shipments"][0]
        #   return ShipmentInfo(
        #       order_ref=order_ref,
        #       carrier=data["carrierCode"],
        #       carrier_scac=self._scac_for(data["carrierCode"]),
        #       tracking_number=data["trackingNumber"],
        #       weight_lbs=data["weight"]["value"],
        #       dimensions={...},
        #       ship_date=data["shipDate"].replace("-", ""),
        #       estimated_delivery=data.get("estimatedDeliveryDate", ""),
        #       found=True,
        #   )
        raise NotImplementedError(
            "Production ShipStation integration not implemented. Set DEMO_MODE=true."
        )

    def _fetch_shipment_mock(self, order_ref: str) -> ShipmentInfo:
        """Return demo shipment data for a given order reference."""
        record = _MOCK_SHIPMENTS.get(order_ref)
        if record is None:
            logger.warning("ShipStation mock: order_ref '%s' not found.", order_ref)
            return ShipmentInfo(
                order_ref=order_ref,
                carrier="",
                carrier_scac="",
                tracking_number="",
                weight_lbs=0.0,
                dimensions={},
                ship_date="",
                estimated_delivery="",
                found=False,
            )

        return ShipmentInfo(
            order_ref=order_ref,
            carrier=record["carrier"],
            carrier_scac=record["carrier_scac"],
            tracking_number=record["tracking_number"],
            weight_lbs=record["weight_lbs"],
            dimensions=record["dimensions"],
            ship_date=record["ship_date"],
            estimated_delivery=record["estimated_delivery"],
            found=True,
        )
