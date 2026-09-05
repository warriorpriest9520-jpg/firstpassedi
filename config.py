"""
FirstPass EDI — Configuration
==============================
Single source of truth for all agent thresholds and system settings.

All values can be overridden via environment variables or by editing the
dataclass defaults below.  Import the pre-built ``config`` singleton from
this module rather than instantiating ``AppConfig`` yourself.

    from config import config
    print(config.validation.confidence_threshold)  # → 0.85
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# ── Sub-configs (one per agent/subsystem) ─────────────────────────────────────


@dataclass
class RetrieverConfig:
    """Settings for the RetrievalAgent."""

    #: Number of vector-store chunks returned per query.
    top_k: int = 5

    #: Number of days before a partner spec is considered stale.
    #: Stale specs trigger a warning in the context package.
    staleness_days: int = 90

    #: Minimum cosine-similarity score to include a chunk in results.
    #: 0.30 works well for demo with small corpora; raise to 0.60+ in production
    #: with a larger, well-tuned embedding model.
    min_relevance_score: float = 0.30


@dataclass
class ValidationConfig:
    """Settings for the ValidationAgent ReAct loop."""

    #: Minimum confidence (0–1) required for a PASS verdict.
    #: Below this threshold the document is routed to the DiagnosticAgent.
    confidence_threshold: float = 0.85

    #: Hard ceiling on ReAct reasoning steps per document.
    #: Prevents runaway loops on malformed input.
    max_react_steps: int = 12

    #: Weight applied to each error severity when computing confidence.
    #: confidence = 1 − Σ(weight × count) / total_segments
    severity_weights: dict[str, float] = field(
        default_factory=lambda: {
            "critical": 1.00,
            "warning": 0.40,
            "info": 0.10,
        }
    )


@dataclass
class DiagnosticConfig:
    """Settings for the DiagnosticAgent Tree-of-Thought beam search."""

    #: Number of root-cause hypotheses generated at the first depth level.
    initial_hypotheses: int = 3

    #: How many hypotheses survive each pruning step.
    beam_width: int = 2

    #: Maximum expansion depth before forcing a decision.
    max_depth: int = 3

    #: Confidence below this value → escalate instead of auto-fixing.
    confidence_threshold: float = 0.70

    #: Weights for the hypothesis scoring function.
    #: Scores are combined as a weighted sum (must sum to 1.0).
    scoring_weights: dict[str, float] = field(
        default_factory=lambda: {
            "evidence_match": 0.50,      # Evidence found in the 856 itself
            "historical_precedent": 0.30, # Match against past validation failures
            "consistency": 0.20,          # Internal consistency of the hypothesis
        }
    )


@dataclass
class OrchestratorConfig:
    """Settings for the OrchestratorAgent pipeline."""

    #: Maximum number of (diagnose → fix → revalidate) cycles before escalating.
    max_revalidation_cycles: int = 2

    #: If diagnostic confidence is below this, skip auto-fix and escalate.
    escalation_confidence_threshold: float = 0.70


# ── Root config ───────────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    """
    Top-level application configuration.

    Populated from environment variables at import time.  Nested sub-configs
    can be overridden per-test by replacing the dataclass fields.
    """

    # ── API keys & external services ──────────────────────────────────────────
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    openai_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o")
    )
    chroma_db_path: str = field(
        default_factory=lambda: os.getenv("CHROMA_DB_PATH", "./chroma_db")
    )
    shipstation_api_key: str = field(
        default_factory=lambda: os.getenv("SHIPSTATION_API_KEY", "")
    )
    orderful_api_key: str = field(
        default_factory=lambda: os.getenv("ORDERFUL_API_KEY", "")
    )
    orderful_env: str = field(
        default_factory=lambda: os.getenv("ORDERFUL_ENV", "sandbox")
    )

    # ── Demo mode ─────────────────────────────────────────────────────────────
    #: When True (or when OPENAI_API_KEY is absent), all LLM calls are
    #: replaced with realistic pre-scripted responses so the pipeline can
    #: be demonstrated without real API credentials.
    demo_mode: bool = field(
        default_factory=lambda: (
            os.getenv("DEMO_MODE", "").lower() == "true"
            or not os.getenv("OPENAI_API_KEY")
        )
    )

    # ── Sub-configs ───────────────────────────────────────────────────────────
    retriever: RetrieverConfig = field(default_factory=RetrieverConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    diagnostic: DiagnosticConfig = field(default_factory=DiagnosticConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)


# ── Singleton ─────────────────────────────────────────────────────────────────

#: Import this object everywhere instead of re-instantiating AppConfig.
config = AppConfig()
