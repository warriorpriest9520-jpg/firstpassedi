"""Tests for the AuditAgent: payload validation and SLA checking."""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from firstpass.agents.audit_agent import AuditAgent

SAMPLE_850 = (Path(__file__).parent.parent / "demo" / "sample_850.x12").read_text()
SAMPLE_856 = (Path(__file__).parent.parent / "demo" / "sample_856.x12").read_text()
SAMPLE_810 = (Path(__file__).parent.parent / "demo" / "sample_810.x12").read_text()


class TestPayloadValidation:
    def setup_method(self):
        self.agent = AuditAgent()

    def test_valid_850_has_no_errors(self):
        errors = self.agent.validate_payload(SAMPLE_850, "850")
        assert errors == []

    def test_valid_856_has_no_errors(self):
        errors = self.agent.validate_payload(SAMPLE_856, "856")
        assert errors == []

    def test_valid_810_has_no_errors(self):
        errors = self.agent.validate_payload(SAMPLE_810, "810")
        assert errors == []

    def test_empty_payload_flagged(self):
        errors = self.agent.validate_payload("", "850")
        assert len(errors) > 0
        assert any("Empty" in e for e in errors)

    def test_missing_isa_flagged(self):
        bad = "GS*PO*A*B*20240115*1030*1*X*005010~\nST*850*0001~\nSE*2*0001~\nGE*1*1~"
        errors = self.agent.validate_payload(bad, "850")
        assert any("ISA" in e for e in errors)

    def test_missing_se_flagged(self):
        bad = SAMPLE_850.replace("SE*29*0001~", "")
        errors = self.agent.validate_payload(bad, "850")
        assert any("SE" in e for e in errors)


class TestSLACheck:
    def setup_method(self):
        self.agent = AuditAgent()

    def test_within_sla_returns_none(self):
        trigger = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        submitted = datetime.now(timezone.utc).isoformat()
        result = self.agent.check_sla(
            {"doc_type": "855", "trigger_at": trigger, "submitted_at": submitted}
        )
        assert result is None

    def test_breached_sla_returns_string(self):
        trigger = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        submitted = datetime.now(timezone.utc).isoformat()
        result = self.agent.check_sla(
            {"doc_type": "855", "trigger_at": trigger, "submitted_at": submitted}
        )
        assert result is not None
        assert "SLA breach" in result

    def test_missing_fields_returns_none(self):
        result = self.agent.check_sla({"doc_type": "855"})
        assert result is None
