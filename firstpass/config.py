"""
config.py — Centralised configuration for FirstPass EDI.

All settings are read from environment variables (or a .env file loaded at
import time). Defaults are safe for local development / dry-run mode.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (two levels up from this file)
_root = Path(__file__).parent.parent
load_dotenv(_root / ".env", override=False)


class _Config:
    # ── API / auth ─────────────────────────────────────────────────────────
    API_KEY: str = os.getenv("FIRSTPASS_API_KEY", "")
    API_PORT: int = int(os.getenv("FIRSTPASS_API_PORT", "8080"))
    API_HOST: str = os.getenv("FIRSTPASS_API_HOST", "0.0.0.0")

    # ── LLM providers ─────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    DEFAULT_MODEL: str = os.getenv("FIRSTPASS_LLM_MODEL", "claude-3-5-sonnet-20241022")

    # ── Database (Supabase) ───────────────────────────────────────────────
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    # ── EDI platforms ─────────────────────────────────────────────────────
    ORDERFUL_API_KEY: str = os.getenv("ORDERFUL_API_KEY", "")
    ORDERFUL_BASE_URL: str = os.getenv("ORDERFUL_BASE_URL", "https://api.orderful.com/v3")

    LOGICBROKER_API_KEY: str = os.getenv("LOGICBROKER_API_KEY", "")
    LOGICBROKER_BASE_URL: str = os.getenv(
        "LOGICBROKER_BASE_URL", "https://commercehub.logicbroker.com/api/v1"
    )

    # ── Shipping ──────────────────────────────────────────────────────────
    SHIPSTATION_API_KEY: str = os.getenv("SHIPSTATION_API_KEY", "")
    SHIPSTATION_API_SECRET: str = os.getenv("SHIPSTATION_API_SECRET", "")
    SHIPSTATION_BASE_URL: str = os.getenv(
        "SHIPSTATION_BASE_URL", "https://ssapi.shipstation.com"
    )

    # ── Notifications ─────────────────────────────────────────────────────
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")

    # ── Company identity (generic defaults for demo) ──────────────────────
    COMPANY_NAME: str = os.getenv("COMPANY_NAME", "ACME Manufacturing")
    COMPANY_ISA_ID: str = os.getenv("COMPANY_ISA_ID", "ACMEMFG")
    COMPANY_EMAIL: str = os.getenv("COMPANY_EMAIL", "edi@acme-manufacturing.example.com")

    # ── Agent loop ────────────────────────────────────────────────────────
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    MAX_EMAILS_PER_CYCLE: int = int(os.getenv("MAX_EMAILS_PER_CYCLE", "50"))

    # ── ERP / database ────────────────────────────────────────────────────
    ERP_BASE_URL: str = os.getenv("ERP_BASE_URL", "")
    ERP_DB_HOST: str = os.getenv("ERP_DB_HOST", "")
    ERP_DB_NAME: str = os.getenv("ERP_DB_NAME", "")
    ERP_DB_USER: str = os.getenv("ERP_DB_USER", "")
    ERP_DB_PASSWORD: str = os.getenv("ERP_DB_PASSWORD", "")

    # ── Data / state directories ──────────────────────────────────────────
    DATA_DIR: Path = Path(os.getenv("FIRSTPASS_DATA_DIR", str(_root / "data")))
    LOG_DIR: Path = Path(os.getenv("FIRSTPASS_LOG_DIR", str(_root / "logs")))

    # ── Intelligence ──────────────────────────────────────────────────────
    PARTNER_COUNT: int = int(os.getenv("FIRSTPASS_EDI_PARTNER_COUNT", "10"))
    CALLBACK_URL: str = os.getenv("FIRSTPASS_CALLBACK_URL", "")

    # ── Safety ────────────────────────────────────────────────────────────
    HALT_FILE: Path = _root / ".halt_state.json"
    REQUIRE_APPROVAL_ABOVE_AMOUNT: float = float(
        os.getenv("REQUIRE_APPROVAL_ABOVE_AMOUNT", "10000.0")
    )

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_KEY)

    @property
    def llm_configured(self) -> bool:
        return bool(self.ANTHROPIC_API_KEY or self.OPENAI_API_KEY)

    @property
    def erp_configured(self) -> bool:
        return bool(self.ERP_DB_HOST and self.ERP_DB_NAME)

    def __repr__(self) -> str:
        return (
            f"<FirstPassConfig api_port={self.API_PORT} "
            f"supabase={'✓' if self.supabase_configured else '✗'} "
            f"llm={'✓' if self.llm_configured else '✗'}>"
        )


# Singleton — import this everywhere
config = _Config()
