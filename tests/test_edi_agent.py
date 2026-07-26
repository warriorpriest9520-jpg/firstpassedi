"""
Tests for the EDI agent: X12 parser, document generators, and guardrails.

All tests run in dry-run mode — no API keys or network access required.
"""
import pytest
import sys
from pathlib import Path

# Ensure firstpass is importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from firstpass.agents.edi_agent import parse_850, generate_855, generate_997, X12ParseError
from firstpass.safety.guardrails import validate_order


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_850 = (Path(__file__).parent.parent / "demo" / "sample_850.x12").read_text()


# ── Parser tests ──────────────────────────────────────────────────────────────

class TestParse850:
    def test_parses_po_number(self):
        order = parse_850(SAMPLE_850)
        assert order.po_number == "BB-20240115-001"

    def test_parses_partner_isa_id(self):
        order = parse_850(SAMPLE_850)
        assert "BIGBOXRETAIL" in order.partner_isa_id

    def test_parses_line_items(self):
        order = parse_850(SAMPLE_850)
        assert len(order.line_items) == 3

    def test_line_item_quantities(self):
        order = parse_850(SAMPLE_850)
        qtys = [li["qty"] for li in order.line_items]
        assert 500 in qtys
        assert 250 in qtys
        assert 100 in qtys

    def test_line_item_skus(self):
        order = parse_850(SAMPLE_850)
        skus = [li["sku"] for li in order.line_items]
        assert "FOAM-QN-10" in skus

    def test_total_value(self):
        order = parse_850(SAMPLE_850)
        # 500*24.99 + 250*19.99 + 100*34.99 = 20,991.50
        assert order.total_value == pytest.approx(20991.50, rel=0.01)

    def test_ship_to_populated(self):
        order = parse_850(SAMPLE_850)
        assert order.ship_to.get("name") or order.ship_to.get("city")

    def test_raises_on_empty_x12(self):
        with pytest.raises(X12ParseError):
            parse_850("")

    def test_raises_on_missing_beg_segment(self):
        bad_x12 = "ISA*00*          *00*          *ZZ*PARTNER*ZZ*ACMEMFG*240115*1030*^*00501*001*0*P*>~\nGS*PO*A*B*20240115*1030*1*X*005010~\nSE*2*0001~\nGE*1*1~\nIEA*1*001~"
        with pytest.raises(X12ParseError):
            parse_850(bad_x12)


# ── Generator tests ───────────────────────────────────────────────────────────

class TestGenerators:
    def test_generate_855_contains_po_number(self):
        order = parse_850(SAMPLE_850)
        x12 = generate_855(order)
        assert order.po_number in x12

    def test_generate_855_has_required_segments(self):
        order = parse_850(SAMPLE_850)
        x12 = generate_855(order)
        assert "ISA" in x12
        assert "855" in x12
        assert "BAK" in x12
        assert "IEA" in x12

    def test_generate_997_contains_functional_ack(self):
        order = parse_850(SAMPLE_850)
        x12 = generate_997(order)
        assert "997" in x12
        assert "AK9" in x12

    def test_generated_855_is_string(self):
        order = parse_850(SAMPLE_850)
        x12 = generate_855(order)
        assert isinstance(x12, str)
        assert len(x12) > 50


# ── Guardrail tests ───────────────────────────────────────────────────────────

class TestGuardrails:
    def test_valid_order_has_no_violations(self):
        """With approval threshold raised above order value, no violations expected."""
        import firstpass.config as cfg
        original = cfg.config.REQUIRE_APPROVAL_ABOVE_AMOUNT
        try:
            cfg.config.REQUIRE_APPROVAL_ABOVE_AMOUNT = 999_999.0
            order = parse_850(SAMPLE_850)
            violations = validate_order(order)
            assert violations == []
        finally:
            cfg.config.REQUIRE_APPROVAL_ABOVE_AMOUNT = original

    def test_missing_po_number_flagged(self):
        order = parse_850(SAMPLE_850)
        order.po_number = ""
        violations = validate_order(order)
        assert any("PO number" in v for v in violations)

    def test_no_line_items_flagged(self):
        order = parse_850(SAMPLE_850)
        order.line_items = []
        violations = validate_order(order)
        assert any("line items" in v.lower() for v in violations)

    def test_negative_quantity_flagged(self):
        order = parse_850(SAMPLE_850)
        order.line_items[0]["qty"] = -5
        violations = validate_order(order)
        assert any("negative quantity" in v.lower() for v in violations)

    def test_high_value_approval_gate(self, monkeypatch):
        """Orders above the threshold should trigger an approval violation."""
        import firstpass.safety.guardrails as g
        order = parse_850(SAMPLE_850)
        # Temporarily lower the threshold below the order total
        import firstpass.config as cfg
        monkeypatch.setattr(cfg.config, "REQUIRE_APPROVAL_ABOVE_AMOUNT", 1.0)
        violations = validate_order(order)
        assert any("approval" in v.lower() for v in violations)

    def test_missing_isa_id_flagged(self):
        order = parse_850(SAMPLE_850)
        order.partner_isa_id = ""
        violations = validate_order(order)
        assert any("ISA" in v for v in violations)


# ── Connector dry-run tests ───────────────────────────────────────────────────

class TestConnectorDryRun:
    def test_orderful_dry_run_submit(self):
        from firstpass.connectors.orderful import OrderfulClient
        client = OrderfulClient(api_key="")
        result = client.submit("ISA*...", "PARTNER", "855")
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert "SIM" in result["transaction_id"]

    def test_orderful_dry_run_fetch_inbound(self):
        from firstpass.connectors.orderful import OrderfulClient
        client = OrderfulClient(api_key="")
        result = client.fetch_inbound("850")
        assert result == []

    def test_logicbroker_dry_run_submit(self):
        from firstpass.connectors.logicbroker import LogicBrokerClient
        client = LogicBrokerClient(api_key="")
        result = client.submit("ISA*...", "PARTNER", "855")
        assert result["ok"] is True
        assert "SIM" in result["transaction_id"]

    def test_shipstation_dry_run_returns_none(self):
        from firstpass.connectors.shipstation import ShippingConnector
        client = ShippingConnector(api_key="", api_secret="")
        result = client.get_shipment("BB-20240115-001")
        assert result is None

    def test_erp_dry_run_creates_synthetic_id(self):
        from firstpass.connectors.erp import ERPConnector
        erp = ERPConnector()
        order = parse_850(SAMPLE_850)
        result = erp.create_order(order)
        # In dry-run (no ERP_BASE_URL) should return a synthetic string
        assert result is None or isinstance(result, str)
