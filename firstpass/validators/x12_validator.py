"""
x12_validator.py — Data quality validator for X12 EDI transactions.

Part of the FirstPass EDI validation pipeline. Validates 850 / 856 / 810 / 846
transaction content against a partner spec loaded from ``partners.json``
(resolved via ``firstpass.config``). Returns a list of issue dicts so callers
can decide how to alert or block.

Each issue dict has the shape::

    {
        "field":    str,        # e.g. "tracking_number"
        "issue":    str,        # human-readable description
        "severity": "error" | "warning"
    }

Usage (standalone)::

    from firstpass.validators.x12_validator import validate_856
    issues = validate_856(transaction_dict, partner_spec_dict)

Usage (class-based)::

    from firstpass.validators.x12_validator import EDIValidator
    validator = EDIValidator()
    issues = validator.validate(transaction, partner_key)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from firstpass.config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Partners config path
# ---------------------------------------------------------------------------
# Resolved as: $FIRSTPASS_PARTNERS_CONFIG or <project_root>/config/partners.json
_DEFAULT_PARTNERS_CONFIG = (
    Path(__file__).parent.parent.parent / "config" / "partners.json"
)

# ---------------------------------------------------------------------------
# Known valid SCAC codes (subset — extend as needed)
# ---------------------------------------------------------------------------
VALID_SCAC_CODES = {
    "UPSN", "FDXG", "FDXE", "FXFE", "ONTC", "EXLA", "RDWY", "ABFS",
    "ESTES", "ODFL", "SAIA", "RLCA", "DHLG", "YFSY", "AACT", "CNWY",
    "PITD", "SEFL", "SMTL", "WARD", "NEMF", "PYLE", "RETL", "TSTNL",
    "UPGF", "UPSG", "USFC", "CTII", "AVRT", "HMES", "DDIC", "DHRN",
}


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _get(transaction: Dict[str, Any], *keys: str) -> Any:
    """Safely traverse nested dicts; return None if any key is missing."""
    obj = transaction
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _missing(value: Any) -> bool:
    """True if value is None, empty string, or empty list."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def _issue(field: str, issue: str, severity: str = "error") -> Dict[str, str]:
    return {"field": field, "issue": issue, "severity": severity}


# ---------------------------------------------------------------------------
# Per-document validators
# ---------------------------------------------------------------------------

def validate_850(transaction: Dict[str, Any], partner_spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate an inbound 850 Purchase Order.

    Checks for required fields per partner spec plus common structural rules.

    Args:
        transaction: Raw transaction dict (e.g. from Orderful API).
        partner_spec: The partner entry from partners.json.

    Returns:
        List of issue dicts (empty = clean).
    """
    issues: List[Dict[str, str]] = []
    msg = _get(transaction, "message") or transaction  # support both wrapped and unwrapped

    # --- Required fields from spec ---
    spec_required = _get(partner_spec, "required_fields", "850") or []
    field_paths = {
        "po_number":           ("purchaseOrder", "purchaseOrderNumber"),
        "ship_to_address":     ("purchaseOrder", "shipTo"),
        "requested_ship_date": ("purchaseOrder", "requestedShipDate"),
        "line_items":          ("purchaseOrder", "lineItems"),
        "supplier_id":         ("purchaseOrder", "supplierId"),
    }
    for field in spec_required:
        path = field_paths.get(field)
        if path:
            val = _get(msg, *path)
            if _missing(val):
                issues.append(_issue(field, f"Required field '{field}' is missing or empty"))

    # --- Structural checks ---
    po_number = _get(msg, "purchaseOrder", "purchaseOrderNumber")
    if po_number and len(str(po_number)) > 22:
        # EDI PO numbers are capped at 22 chars in X12
        issues.append(_issue("po_number", f"PO number exceeds 22 characters: '{po_number}'", "warning"))

    line_items = _get(msg, "purchaseOrder", "lineItems") or []
    for i, item in enumerate(line_items):
        if _missing(item.get("lineItemNumber")):
            issues.append(_issue(f"line_items[{i}].lineItemNumber", "Line item number missing", "warning"))
        if _missing(item.get("quantity")):
            issues.append(_issue(f"line_items[{i}].quantity", "Line item quantity missing"))
        if _missing(item.get("unitPrice")):
            issues.append(_issue(f"line_items[{i}].unitPrice", "Line item unit price missing", "warning"))

    return issues


def validate_856(transaction: Dict[str, Any], partner_spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate an outbound 856 Ship Notice / ASN.

    Critical fields: tracking number, carrier SCAC, ship date, PO reference,
    line items. Also checks HL segment split-shipment requirements when flagged
    in known_quirks.

    Args:
        transaction: Raw transaction dict.
        partner_spec: The partner entry from partners.json.

    Returns:
        List of issue dicts (empty = clean).
    """
    issues: List[Dict[str, str]] = []
    msg = _get(transaction, "message") or transaction

    # --- Required fields from spec ---
    spec_required = _get(partner_spec, "required_fields", "856") or []
    field_paths = {
        "tracking_number":  ("shipment", "trackingNumber"),
        "carrier_scac":     ("shipment", "carrierSCAC"),
        "ship_date":        ("shipment", "shipDate"),
        "po_reference":     ("shipment", "purchaseOrderNumber"),
        "line_items":       ("shipment", "lineItems"),
        "hl_segments":      ("shipment", "hlSegments"),
        "packaging_details": ("shipment", "packagingDetails"),
    }
    for field in spec_required:
        path = field_paths.get(field)
        if path:
            val = _get(msg, *path)
            if _missing(val):
                issues.append(_issue(field, f"Required field '{field}' is missing or empty"))

    # --- SCAC code validation ---
    scac = _get(msg, "shipment", "carrierSCAC")
    if scac and scac.upper() not in VALID_SCAC_CODES:
        issues.append(_issue(
            "carrier_scac",
            f"SCAC code '{scac}' not in known valid list. Verify it's correct before sending.",
            "warning",
        ))

    # --- Ship date format check ---
    ship_date = _get(msg, "shipment", "shipDate")
    if ship_date:
        date_str = str(ship_date).replace("-", "")
        if len(date_str) != 8 or not date_str.isdigit():
            issues.append(_issue("ship_date", f"Ship date format unexpected: '{ship_date}' (expected YYYYMMDD)"))

    # --- Split-shipment HL check (enabled via known_quirks in partner spec) ---
    quirks = partner_spec.get("known_quirks", [])
    if any("split-shipment" in q.lower() for q in quirks):
        hl_segments = _get(msg, "shipment", "hlSegments") or []
        shipment_loops = [hl for hl in hl_segments if isinstance(hl, dict) and hl.get("hlLevelCode") == "S"]
        if len(shipment_loops) > 1:
            issues.append(_issue(
                "hl_segments",
                f"Split-shipment ASN detected ({len(shipment_loops)} HL shipment loops). "
                "Send one 856 per shipment instead.",
                "error",
            ))

    # --- Line item checks ---
    line_items = _get(msg, "shipment", "lineItems") or []
    if not line_items:
        issues.append(_issue("line_items", "856 has no line items"))
    for i, item in enumerate(line_items):
        if _missing(item.get("quantity")):
            issues.append(_issue(f"line_items[{i}].quantity", "Line item shipped quantity missing"))
        if _missing(item.get("itemId")) and _missing(item.get("sku")):
            issues.append(_issue(f"line_items[{i}].itemId", "Line item has no item ID or SKU", "warning"))

    return issues


def validate_810(transaction: Dict[str, Any], partner_spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate an outbound 810 Invoice.

    Checks invoice number, PO reference, dates, and that line totals balance.

    Args:
        transaction: Raw transaction dict.
        partner_spec: The partner entry from partners.json.

    Returns:
        List of issue dicts (empty = clean).
    """
    issues: List[Dict[str, str]] = []
    msg = _get(transaction, "message") or transaction

    # --- Required fields ---
    spec_required = _get(partner_spec, "required_fields", "810") or []
    field_paths = {
        "invoice_number":       ("invoice", "invoiceNumber"),
        "po_reference":         ("invoice", "purchaseOrderNumber"),
        "invoice_date":         ("invoice", "invoiceDate"),
        "line_totals":          ("invoice", "lineItems"),
        "allowances_charges":   ("invoice", "allowancesCharges"),
    }
    for field in spec_required:
        path = field_paths.get(field)
        if path:
            val = _get(msg, *path)
            if _missing(val):
                issues.append(_issue(field, f"Required field '{field}' is missing or empty"))

    # --- Line total balance check ---
    line_items = _get(msg, "invoice", "lineItems") or []
    invoice_total = _get(msg, "invoice", "invoiceTotal")
    if line_items and invoice_total is not None:
        try:
            calculated = sum(
                float(item.get("lineTotal", 0) or 0)
                for item in line_items
            )
            ac_list = _get(msg, "invoice", "allowancesCharges") or []
            for ac in ac_list:
                amount = float(ac.get("amount", 0) or 0)
                if ac.get("type") == "allowance":
                    calculated -= amount
                else:
                    calculated += amount

            declared = float(invoice_total)
            if abs(calculated - declared) > 0.02:  # 2-cent tolerance for rounding
                issues.append(_issue(
                    "line_totals",
                    f"Invoice total mismatch: line items sum to {calculated:.2f} "
                    f"but invoiceTotal is {declared:.2f}",
                ))
        except (TypeError, ValueError):
            issues.append(_issue("line_totals", "Could not verify invoice totals — non-numeric values found", "warning"))

    # --- Invoice number length check ---
    invoice_number = _get(msg, "invoice", "invoiceNumber")
    if invoice_number and len(str(invoice_number)) > 22:
        issues.append(_issue("invoice_number", f"Invoice number exceeds 22 characters: '{invoice_number}'", "warning"))

    return issues


def validate_846(transaction: Dict[str, Any], partner_spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate an outbound 846 Inventory Inquiry / Advice.

    Checks required item fields and non-negative quantities.

    Args:
        transaction: Raw transaction dict.
        partner_spec: The partner entry from partners.json.

    Returns:
        List of issue dicts (empty = clean).
    """
    issues: List[Dict[str, str]] = []
    msg = _get(transaction, "message") or transaction

    # --- Required fields ---
    spec_required = _get(partner_spec, "required_fields", "846") or []
    field_paths = {
        "item_id":           ("inventory", "itemId"),
        "quantity_on_hand":  ("inventory", "quantityOnHand"),
        "warehouse_id":      ("inventory", "warehouseId"),
    }
    for field in spec_required:
        path = field_paths.get(field)
        if path:
            val = _get(msg, *path)
            if _missing(val):
                issues.append(_issue(field, f"Required field '{field}' is missing or empty"))

    # --- Validate individual items if present as array ---
    items = _get(msg, "inventory", "items") or []
    for i, item in enumerate(items):
        qty = item.get("quantityOnHand")
        try:
            if qty is not None and float(qty) < 0:
                issues.append(_issue(
                    f"items[{i}].quantityOnHand",
                    f"Negative inventory quantity ({qty}) for item {item.get('itemId', '?')}",
                    "warning",
                ))
        except (TypeError, ValueError):
            pass

    return issues


# ---------------------------------------------------------------------------
# EDIValidator class — partner-aware dispatch
# ---------------------------------------------------------------------------

class EDIValidator:
    """Loads partner specs and dispatches validation to the right function.

    Partner specs are loaded from ``partners.json`` at the path configured
    via the ``FIRSTPASS_PARTNERS_CONFIG`` environment variable, or
    ``<project_root>/config/partners.json`` by default.
    """

    # Map transaction type names (Orderful-style) to doc types
    _TYPE_MAP = {
        "850_PURCHASE_ORDER":            "850",
        "856_SHIP_NOTICE":               "856",
        "810_INVOICE":                   "810",
        "846_INVENTORY_INQUIRY":         "846",
        "846_INVENTORY_ADVICE":          "846",
        "997_FUNCTIONAL_ACKNOWLEDGMENT": "997",
    }
    _VALIDATORS = {
        "850": validate_850,
        "856": validate_856,
        "810": validate_810,
        "846": validate_846,
    }

    def __init__(self, config_path: Optional[str] = None) -> None:
        if config_path is None:
            config_path = os.getenv(
                "FIRSTPASS_PARTNERS_CONFIG",
                str(_DEFAULT_PARTNERS_CONFIG),
            )
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        if not self.config_path.exists():
            logger.warning("partners.json not found at %s", self.config_path)
            return
        try:
            self._config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load partners.json: %s", exc)

    def get_partner_spec(self, partner_key: str) -> Optional[Dict[str, Any]]:
        """Return the spec for a given partner key (e.g. 'acme_retail')."""
        return self._config.get("partners", {}).get(partner_key.lower())

    def find_partner_by_isa(self, isa_id: str) -> Optional[str]:
        """Return the partner key whose isa_sender_id matches (stripped comparison)."""
        isa_clean = isa_id.strip().upper()
        for key, spec in self._config.get("partners", {}).items():
            if spec.get("isa_sender_id", "").strip().upper() == isa_clean:
                return key
        return None

    def resolve_doc_type(self, type_name: str) -> Optional[str]:
        """Convert Orderful type name to doc type string ('850', '856', etc.)."""
        return self._TYPE_MAP.get(type_name.upper())

    def validate(
        self,
        transaction: Dict[str, Any],
        partner_key: str,
        doc_type: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Validate a transaction for a named partner.

        Args:
            transaction: Raw transaction dict.
            partner_key: Key from partners.json (e.g. 'acme_retail').
            doc_type: Override doc type ('850', '856', etc.).
                      If None, resolved from transaction['type']['name'].

        Returns:
            List of issue dicts.
        """
        spec = self.get_partner_spec(partner_key)
        if not spec:
            return [_issue("partner", f"Unknown partner key '{partner_key}'", "warning")]

        if doc_type is None:
            raw_type = _get(transaction, "type", "name") or ""
            doc_type = self.resolve_doc_type(raw_type) or raw_type[:3]

        validator_fn = self._VALIDATORS.get(doc_type)
        if not validator_fn:
            return []  # No validator for this type (e.g. 997)

        return validator_fn(transaction, spec)

    def all_partner_keys(self) -> List[str]:
        """Return all configured partner keys."""
        return list(self._config.get("partners", {}).keys())

    def partner_status(self, partner_key: str) -> Optional[str]:
        """Return status string for a partner ('active', 'go-live', 'setup', 'blocked', 'urgent')."""
        spec = self.get_partner_spec(partner_key)
        return spec.get("status") if spec else None
