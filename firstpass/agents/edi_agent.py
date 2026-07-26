"""
edi_agent.py — Core EDI processing agent.

Bridges inbound purchase orders (850) from trading platforms (Orderful,
LogicBroker) with the ERP system (via the ERP connector) and generates
outbound documents: 855 acknowledgment, 856 ASN, 810 invoice, 997 functional
acknowledgment.

Architecture (one cycle):
  1. Poll platforms for new 850s
  2. Parse each X12 document into an internal Order model
  3. Validate against partner-specific EDI specs (field rules, ISA IDs, etc.)
  4. Create/update the order in the ERP
  5. Generate 855 → submit back to platform
  6. When ship data is available: generate 856 + 810 → submit
  7. Log all activity; emit events on the message bus

Dry-run mode (no platform API keys): uses synthetic 850s from demo/ folder.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import config
from ..utils.message_bus import MessageBus
from ..utils.retry import llm_retry

log = logging.getLogger("firstpass.edi_agent")

BASE_DIR = Path(__file__).parent.parent.parent
DEMO_DIR = BASE_DIR / "demo"

# ── Internal order model (simplified) ────────────────────────────────────────

class Order:
    """Represents a parsed purchase order."""

    def __init__(self, po_number: str, partner: str, partner_isa_id: str,
                 line_items: List[Dict], ship_to: Dict, bill_to: Dict,
                 requested_ship_date: Optional[str] = None,
                 raw_x12: str = ""):
        self.po_number = po_number
        self.partner = partner
        self.partner_isa_id = partner_isa_id
        self.line_items = line_items          # [{line_num, sku, qty, unit_price}]
        self.ship_to = ship_to
        self.bill_to = bill_to
        self.requested_ship_date = requested_ship_date
        self.raw_x12 = raw_x12
        self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def total_value(self) -> float:
        return sum(li.get("qty", 0) * li.get("unit_price", 0.0) for li in self.line_items)

    def __repr__(self) -> str:
        return f"<Order po={self.po_number!r} partner={self.partner!r} lines={len(self.line_items)}>"


class OrderSession:
    """Everything the agent knows about one purchase order during processing."""

    def __init__(self, order: Order):
        self.order = order
        self.documents: Dict[str, str] = {}       # doc_type -> x12 string
        self.submissions: Dict[str, str] = {}      # doc_type -> transaction_id
        self.erp_order_no: Optional[str] = None
        self.parse_ok: bool = True
        self.parse_error: str = ""

    def status(self) -> dict:
        return {
            "po_number": self.order.po_number,
            "parse_ok": self.parse_ok,
            "documents_generated": sorted(self.documents.keys()),
            "submissions": dict(self.submissions),
            "erp_order_no": self.erp_order_no,
        }


# ── Parser (minimal X12 tokenizer) ───────────────────────────────────────────

class X12ParseError(Exception):
    pass


def parse_850(x12: str) -> Order:
    """
    Parse an X12 850 Purchase Order into an Order object.

    This is a simplified parser for demonstration. A production implementation
    should use a full X12 library (e.g. `pyx12`, `x12`, or a custom
    segment-by-segment walker) to handle all trading partner variations.
    """
    lines = [s.strip() for s in x12.replace("\n", "~").split("~") if s.strip()]

    po_number = ""
    partner_isa_id = ""
    line_items: List[Dict] = []
    ship_to: Dict = {}
    bill_to: Dict = {}
    requested_ship_date: Optional[str] = None

    for seg in lines:
        parts = seg.split("*")
        tag = parts[0].upper()

        if tag == "ISA" and len(parts) > 7:
            partner_isa_id = parts[6].strip()

        elif tag == "BEG" and len(parts) > 4:
            po_number = parts[3].strip()

        elif tag == "DTM" and len(parts) > 2:
            qualifier = parts[1].strip()
            if qualifier == "002":  # Delivery requested
                requested_ship_date = parts[2].strip()

        elif tag == "N1" and len(parts) > 2:
            entity = parts[1].strip()
            name = parts[2].strip() if len(parts) > 2 else ""
            if entity == "ST":
                ship_to["name"] = name
            elif entity == "BT":
                bill_to["name"] = name

        elif tag == "N3" and len(parts) > 1:
            addr = parts[1].strip()
            if not ship_to.get("address"):
                ship_to["address"] = addr
            else:
                bill_to["address"] = addr

        elif tag == "N4" and len(parts) > 2:
            city = parts[1].strip()
            state = parts[2].strip()
            zip_code = parts[3].strip() if len(parts) > 3 else ""
            if not ship_to.get("city"):
                ship_to.update({"city": city, "state": state, "zip": zip_code})
            else:
                bill_to.update({"city": city, "state": state, "zip": zip_code})

        elif tag == "PO1" and len(parts) > 5:
            try:
                line_items.append({
                    "line_num": parts[1].strip(),
                    "qty": int(float(parts[2].strip() or "0")),
                    "unit_of_measure": parts[3].strip(),
                    "unit_price": float(parts[4].strip() or "0"),
                    "sku": parts[7].strip() if len(parts) > 7 else "",
                })
            except (ValueError, IndexError) as exc:
                log.warning(f"Could not parse PO1 segment: {exc}")

    if not po_number:
        raise X12ParseError("Could not extract PO number from 850")

    return Order(
        po_number=po_number,
        partner="unknown",
        partner_isa_id=partner_isa_id,
        line_items=line_items,
        ship_to=ship_to,
        bill_to=bill_to,
        requested_ship_date=requested_ship_date,
        raw_x12=x12,
    )


# ── Document generators (stubs — replace with full X12 library) ───────────────

def generate_855(order: Order) -> str:
    """Generate an 855 Purchase Order Acknowledgment."""
    ts = datetime.now(timezone.utc)
    date = ts.strftime("%Y%m%d")
    time_ = ts.strftime("%H%M")
    return (
        f"ISA*00*          *00*          *ZZ*{config.COMPANY_ISA_ID:<15}*ZZ*"
        f"{order.partner_isa_id:<15}*{date[2:]}*{time_}*^*00501*000000001*0*P*>~\n"
        f"GS*PR*{config.COMPANY_ISA_ID}*{order.partner_isa_id}*{date}*{time_}*1*X*005010~\n"
        f"ST*855*0001~\n"
        f"BAK*00*AC*{order.po_number}*{date}~\n"
        f"SE*3*0001~\n"
        f"GE*1*1~\n"
        f"IEA*1*000000001~"
    )


def generate_997(order: Order, isa_ctrl: str = "000000001") -> str:
    """Generate a 997 Functional Acknowledgment."""
    ts = datetime.now(timezone.utc)
    date = ts.strftime("%Y%m%d")
    time_ = ts.strftime("%H%M")
    return (
        f"ISA*00*          *00*          *ZZ*{config.COMPANY_ISA_ID:<15}*ZZ*"
        f"{order.partner_isa_id:<15}*{date[2:]}*{time_}*^*00501*000000002*0*P*>~\n"
        f"GS*FA*{config.COMPANY_ISA_ID}*{order.partner_isa_id}*{date}*{time_}*2*X*005010~\n"
        f"ST*997*0001~\n"
        f"AK1*PO*1~\n"
        f"AK9*A*1*1*1~\n"
        f"SE*4*0001~\n"
        f"GE*1*2~\n"
        f"IEA*1*000000002~"
    )


# ── Main agent ────────────────────────────────────────────────────────────────

class EDIAgent:
    """
    Core EDI processing agent.

    Polls configured platforms, processes 850s, and drives outbound
    document generation + submission.
    """

    def __init__(self):
        self.bus = MessageBus()
        self._sessions: Dict[str, OrderSession] = {}

    # ── Public interface ──────────────────────────────────────────────────

    def run_cycle(self) -> dict:
        """Execute one EDI polling cycle."""
        inbound = self._poll_platforms()
        log.info(f"EDIAgent: {len(inbound)} inbound 850(s)")

        processed = 0
        errors = 0

        for doc in inbound:
            try:
                session = self._process_850(doc["x12"], source=doc.get("platform", "unknown"))
                self._sessions[session.order.po_number] = session
                processed += 1
            except Exception as exc:
                log.error(f"Failed to process 850: {exc}", exc_info=True)
                errors += 1
                self.bus.publish(
                    source="edi_agent",
                    event_type="warning",
                    topic="edi_parse_error",
                    subject=f"850 parse/process error: {exc}",
                    priority="normal",
                )

        # Also attempt 856/810 generation for orders that are now shipped
        shipped = self._check_shipped_orders()

        summary = {
            "inbound_850s": len(inbound),
            "processed": processed,
            "errors": errors,
            "shipped_orders_updated": shipped,
        }
        log.info(f"EDIAgent cycle: {summary}")
        return summary

    def process_document(self, x12: str, platform: str = "manual") -> OrderSession:
        """
        Public entry point for processing a single X12 document.
        Called by the REST API (/edi/process endpoint).
        """
        return self._process_850(x12, source=platform)

    # ── Internal pipeline ─────────────────────────────────────────────────

    def _process_850(self, x12: str, source: str = "unknown") -> OrderSession:
        """Full 850 → 855 → ERP pipeline for one document."""
        order = parse_850(x12)
        session = OrderSession(order)

        log.info(f"Processing 850: PO={order.po_number} partner={order.partner_isa_id}")

        # Validate against guardrails
        self._validate_order(session)

        # Generate 997 functional acknowledgment
        session.documents["997"] = generate_997(order)

        # Generate 855 acknowledgment
        session.documents["855"] = generate_855(order)

        # Submit 997 + 855 to platform
        self._submit_documents(session, source, ["997", "855"])

        # Create ERP order
        erp_no = self._create_erp_order(session)
        session.erp_order_no = erp_no

        # Emit event
        self.bus.publish(
            source="edi_agent",
            event_type="status",
            topic="order_status_received",
            subject=f"850 processed: {order.po_number} (ERP: {erp_no})",
            payload=session.status(),
            priority="normal",
        )

        return session

    def _validate_order(self, session: OrderSession) -> None:
        """Run guardrail checks on an inbound order."""
        from ..safety.guardrails import validate_order
        issues = validate_order(session.order)
        if issues:
            session.parse_ok = False
            session.parse_error = "; ".join(issues)
            log.warning(f"Order {session.order.po_number} validation issues: {issues}")

    def _submit_documents(self, session: OrderSession, platform: str,
                          doc_types: List[str]) -> None:
        """Submit generated documents back to the trading platform."""
        connector = self._get_connector(platform)
        if connector is None:
            log.info(f"No connector for platform={platform!r} — dry-run, skipping submit")
            return
        for doc_type in doc_types:
            x12 = session.documents.get(doc_type)
            if not x12:
                continue
            try:
                result = connector.submit(x12, session.order.partner_isa_id, doc_type)
                session.submissions[doc_type] = result.get("transaction_id", "unknown")
                log.info(f"Submitted {doc_type} for {session.order.po_number}: "
                         f"tx_id={session.submissions[doc_type]}")
            except Exception as exc:
                log.error(f"Submit {doc_type} failed: {exc}")

    def _create_erp_order(self, session: OrderSession) -> Optional[str]:
        """Create or update the order in the ERP system."""
        try:
            from ..connectors.erp import ERPConnector
            erp = ERPConnector()
            return erp.create_order(session.order)
        except Exception as exc:
            log.error(f"ERP order creation failed: {exc}")
            return None

    def _check_shipped_orders(self) -> int:
        """Check ShipStation for orders that have been shipped; generate 856+810."""
        updated = 0
        for po_number, session in self._sessions.items():
            if "856" in session.documents:
                continue  # already done
            try:
                from ..connectors.shipstation import ShippingConnector
                ss = ShippingConnector()
                ship_data = ss.get_shipment(po_number)
                if ship_data:
                    # TODO: generate 856 ASN and 810 invoice from ship_data
                    log.info(f"Order {po_number} shipped — 856/810 generation TBD")
                    updated += 1
            except Exception as exc:
                log.debug(f"ShipStation check for {po_number} failed: {exc}")
        return updated

    def _poll_platforms(self) -> List[Dict]:
        """Poll all configured platforms for new inbound 850s."""
        docs: List[Dict] = []

        # Orderful
        if config.ORDERFUL_API_KEY:
            try:
                from ..connectors.orderful import OrderfulClient
                client = OrderfulClient()
                for item in client.fetch_inbound("850"):
                    docs.append({"x12": item["x12"], "platform": "orderful",
                                 "id": item.get("id")})
            except Exception as exc:
                log.error(f"Orderful poll failed: {exc}")

        # LogicBroker
        if config.LOGICBROKER_API_KEY:
            try:
                from ..connectors.logicbroker import LogicBrokerClient
                client = LogicBrokerClient()
                for item in client.fetch_inbound():
                    docs.append({"x12": item["x12"], "platform": "logicbroker",
                                 "id": item.get("id")})
            except Exception as exc:
                log.error(f"LogicBroker poll failed: {exc}")

        # Dry-run: load demo 850 if no platforms configured
        if not docs:
            demo_850 = DEMO_DIR / "sample_850.x12"
            if demo_850.exists():
                log.info("Dry-run mode: loading demo/sample_850.x12")
                docs.append({"x12": demo_850.read_text(), "platform": "demo", "id": "demo-850"})

        return docs

    def _get_connector(self, platform: str):
        """Return the appropriate connector for a platform, or None in dry-run."""
        try:
            if platform == "orderful" and config.ORDERFUL_API_KEY:
                from ..connectors.orderful import OrderfulClient
                return OrderfulClient()
            if platform == "logicbroker" and config.LOGICBROKER_API_KEY:
                from ..connectors.logicbroker import LogicBrokerClient
                return LogicBrokerClient()
        except Exception as exc:
            log.warning(f"Could not instantiate connector for {platform}: {exc}")
        return None
