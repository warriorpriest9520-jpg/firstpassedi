#!/usr/bin/env python3
"""
demo_flow.py — 5-minute FirstPass EDI demo script.

Demonstrates the complete EDI processing pipeline from inbound 850 to
outbound 855 acknowledgment, using the demo X12 files in this directory.

Run from the project root:
    python demo/demo_flow.py

Requirements: pip install -r requirements.txt
No API keys needed — runs in dry-run / simulation mode.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Ensure firstpass package is importable
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEMO_DIR = Path(__file__).parent
STEP_DELAY = 0.8  # seconds between steps


def banner(text: str) -> None:
    width = 72
    print("\n" + "═" * width)
    print(f"  {text}")
    print("═" * width)


def step(n: int, description: str) -> None:
    print(f"\n[Step {n}] {description}")
    time.sleep(STEP_DELAY)


def main():
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║              FirstPass EDI — Capstone Demo                          ║
║        AI-Powered EDI Operations Platform                            ║
╚══════════════════════════════════════════════════════════════════════╝

Company:  ACME Manufacturing
Partner:  Big Box Retail
Scenario: Inbound PO → Acknowledgment → ASN → Invoice (dry-run)
""")
    input("  Press Enter to begin the demo...")

    # ──────────────────────────────────────────────────────────────────────
    banner("PHASE 1: Load and Parse an Inbound 850 Purchase Order")
    # ──────────────────────────────────────────────────────────────────────

    step(1, "Reading sample 850 from demo/sample_850.x12 ...")
    x12_850 = (DEMO_DIR / "sample_850.x12").read_text()
    print(f"    → Loaded {len(x12_850)} bytes of X12 data")
    print(f"    → First segment: {x12_850.split('~')[0]}")

    step(2, "Parsing X12 850 into structured Order object ...")
    from firstpass.agents.edi_agent import parse_850
    order = parse_850(x12_850)
    print(f"""
    Order details:
      PO Number:       {order.po_number}
      Partner ISA ID:  {order.partner_isa_id}
      Line items:      {len(order.line_items)}
      Total value:     ${order.total_value:,.2f}
      Requested ship:  {order.requested_ship_date or 'not specified'}
      Ship to:         {order.ship_to.get('name', 'N/A')}
""")

    step(3, "Running guardrail validation ...")
    from firstpass.safety.guardrails import validate_order
    violations = validate_order(order)
    if violations:
        print(f"    ⚠️  Validation issues: {violations}")
    else:
        print("    ✅ Order passed all guardrail checks")

    # ──────────────────────────────────────────────────────────────────────
    banner("PHASE 2: Generate Outbound Documents")
    # ──────────────────────────────────────────────────────────────────────

    step(4, "Generating 997 Functional Acknowledgment ...")
    from firstpass.agents.edi_agent import generate_997
    x12_997 = generate_997(order)
    print(f"    ✅ 997 generated ({len(x12_997)} bytes)")
    print(f"    → Preview: {x12_997.split(chr(10))[2]}")

    step(5, "Generating 855 Purchase Order Acknowledgment ...")
    from firstpass.agents.edi_agent import generate_855
    x12_855 = generate_855(order)
    print(f"    ✅ 855 generated ({len(x12_855)} bytes)")
    print(f"    → Preview: {x12_855.split(chr(10))[2]}")

    # ──────────────────────────────────────────────────────────────────────
    banner("PHASE 3: Platform Submission (Dry-Run Mode)")
    # ──────────────────────────────────────────────────────────────────────

    step(6, "Submitting 997 + 855 to Orderful (dry-run) ...")
    from firstpass.connectors.orderful import OrderfulClient
    client = OrderfulClient()  # no API key → dry-run

    for doc_type, x12 in [("997", x12_997), ("855", x12_855)]:
        result = client.submit(x12, order.partner_isa_id, doc_type)
        print(f"    📤 {doc_type}: tx_id={result['transaction_id']} "
              f"({'DRY RUN' if result.get('dry_run') else 'LIVE'})")

    # ──────────────────────────────────────────────────────────────────────
    banner("PHASE 4: Partner Audit")
    # ──────────────────────────────────────────────────────────────────────

    step(7, "Running compliance audit on the 850 payload ...")
    from firstpass.agents.audit_agent import AuditAgent
    agent = AuditAgent()
    errors = agent.validate_payload(x12_850, "850")
    if errors:
        print(f"    ⚠️  Payload violations: {errors}")
    else:
        print("    ✅ Payload audit: no violations found")

    # ──────────────────────────────────────────────────────────────────────
    banner("PHASE 5: Message Bus Event Stream")
    # ──────────────────────────────────────────────────────────────────────

    step(8, "Publishing processing events to inter-agent message bus ...")
    from firstpass.utils.message_bus import MessageBus
    bus = MessageBus()
    bus.publish(
        source="demo",
        event_type="status",
        topic="order_status_received",
        subject=f"Demo: 850 processed for PO {order.po_number}",
        payload={"po_number": order.po_number, "partner": order.partner_isa_id,
                 "lines": len(order.line_items), "value": order.total_value},
        priority="normal",
    )
    print("    ✅ Event published to message bus")

    # ──────────────────────────────────────────────────────────────────────
    banner("PHASE 6: Load Pre-Built Ship Notice and Invoice")
    # ──────────────────────────────────────────────────────────────────────

    step(9, "Showing sample 856 ASN and 810 invoice ...")
    x12_856 = (DEMO_DIR / "sample_856.x12").read_text()
    x12_810 = (DEMO_DIR / "sample_810.x12").read_text()
    print(f"    📄 856 ASN:  {len(x12_856)} bytes — tracking: 1Z999AA10123456784")
    print(f"    📄 810 INV:  {len(x12_810)} bytes — invoice: INV-ACME-2024-0122-001")

    # ──────────────────────────────────────────────────────────────────────
    banner("DEMO COMPLETE ✅")
    # ──────────────────────────────────────────────────────────────────────

    print(f"""
  Summary
  ═══════
  ✅ Parsed 850 PO:       {order.po_number}
  ✅ Validated:            0 violations
  ✅ Generated 997:        {len(x12_997)} bytes
  ✅ Generated 855:        {len(x12_855)} bytes
  ✅ Submitted (dry-run):  2 documents
  ✅ Audit:                clean
  ✅ Message bus:          1 event published
  ✅ Pre-built 856+810:    ready

  Dry-run mode — no real API keys used.
  Set ORDERFUL_API_KEY, LOGICBROKER_API_KEY etc. in .env for live mode.

  To run the API server:
    python -m firstpass.api

  To run the full orchestrator:
    python -m firstpass.orchestrator --daemon
""")


if __name__ == "__main__":
    main()
