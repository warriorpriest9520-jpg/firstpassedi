"""
FirstPass EDI — Retrieval Agent
==================================
Queries the vector store to assemble a **context package** for a given
trading partner and document type.

What it retrieves (top-5, ranked by recency + relevance)
─────────────────────────────────────────────────────────
  1. Partner routing guide spec (must-have)
  2. Recent validation failure logs for this partner
  3. Resolution logs (how similar errors were fixed)

The context package is passed downstream to the ValidationAgent and
DiagnosticAgent so they can ground their reasoning in real partner rules
rather than generic EDI knowledge.

Architecture note
─────────────────
  This is a pure retrieval agent — no LLM calls.  It assembles factual
  context from the vector store.  The downstream agents decide what to do
  with that context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import config
from tools.vector_store import SearchResult, SpecRecord, VectorStore

logger = logging.getLogger(__name__)


# ── Return types ──────────────────────────────────────────────────────────────


@dataclass
class ContextPackage:
    """
    Assembled retrieval context passed to Validation and Diagnostic agents.

    Attributes
    ----------
    partner_name:
        Trading partner identifier (e.g. ``"RetailerA"``).
    spec_version:
        Version string of the retrieved routing guide.
    spec_date:
        When the spec was effective.
    is_stale:
        ``True`` if the spec is older than ``RetrieverConfig.staleness_days``.
    spec_content:
        Full text of the routing guide spec (used as LLM context window input).
    ranked_chunks:
        Top-K search results ranked by recency × relevance.  Includes both
        the spec and any relevant failure / resolution logs.
    retrieval_timestamp:
        When this package was assembled (UTC).
    warnings:
        Non-fatal issues (e.g. stale spec) surfaced to the orchestrator.
    """

    partner_name: str
    spec_version: str
    spec_date: datetime
    is_stale: bool
    spec_content: str
    ranked_chunks: list[SearchResult]
    retrieval_timestamp: datetime = field(default_factory=datetime.utcnow)
    warnings: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        """
        Render the context package as a single plain-text block suitable for
        inclusion in an LLM prompt.

        Returns all retrieved chunks concatenated with separator lines so the
        model has both the spec rules and the historical failure examples in
        its context window.
        """
        lines: list[str] = [
            f"=== TRADING PARTNER: {self.partner_name} ===",
            f"Spec Version: {self.spec_version}  |  Effective: {self.spec_date.date()}",
        ]
        if self.is_stale:
            lines.append("⚠️  WARNING: This spec is older than 90 days — rules may be outdated.")
        lines.append("")

        for chunk in self.ranked_chunks:
            doc_type = chunk.metadata.get("doc_type", "unknown")
            chunk_date = chunk.metadata.get("spec_date", "")[:10]
            lines.append(f"--- [{doc_type.upper()}  rank={chunk.rank}  date={chunk_date}] ---")
            lines.append(chunk.content.strip())
            lines.append("")

        return "\n".join(lines)


# ── Agent ─────────────────────────────────────────────────────────────────────


class RetrievalAgent:
    """
    Queries the vector store and assembles a ``ContextPackage`` for downstream agents.

    Parameters
    ----------
    vector_store:
        An initialised ``VectorStore`` instance.  If not provided, a new one
        is created using the path from ``config.chroma_db_path``.
    """

    def __init__(self, vector_store: Optional[VectorStore] = None) -> None:
        self._store = vector_store or VectorStore()

    def query(
        self,
        partner_name: str,
        doc_type: str = "856",
        extra_context: str = "",
    ) -> ContextPackage:
        """
        Retrieve the top-K most relevant chunks for a partner + document type.

        Performs two searches:
          1. Partner spec collection for the routing guide
          2. Validation history collection for past failures + resolutions

        Results are merged, de-duplicated, and re-ranked by a combined
        recency × relevance score before being wrapped in a ContextPackage.

        Parameters
        ----------
        partner_name:
            Trading partner name (e.g. ``"RetailerA"``).
        doc_type:
            EDI transaction type (default ``"856"``).
        extra_context:
            Optional additional query terms (e.g. error segment names) for
            more targeted history retrieval.

        Returns
        -------
        ContextPackage
            Populated context package ready for the ValidationAgent.
        """
        logger.info(
            "RetrievalAgent querying for partner='%s', doc_type='%s'.",
            partner_name,
            doc_type,
        )

        warnings: list[str] = []

        # ── 1. Fetch partner spec ─────────────────────────────────────────────
        spec_record: Optional[SpecRecord] = self._store.get_partner_spec(partner_name)

        if spec_record is None:
            # No spec found — use a generic placeholder so the pipeline can
            # continue, but flag it as a warning.
            warnings.append(
                f"No routing guide spec found for partner '{partner_name}'. "
                "Validation will use generic EDI rules only."
            )
            spec_record = SpecRecord(
                partner_name=partner_name,
                spec_version="unknown",
                spec_date=datetime.min,
                content="No partner-specific spec available. Apply generic X12 856 rules.",
                is_stale=True,
                distance=1.0,
            )
        elif spec_record.is_stale:
            warnings.append(
                f"Partner spec for '{partner_name}' (version {spec_record.spec_version}, "
                f"dated {spec_record.spec_date.date()}) is older than "
                f"{config.retriever.staleness_days} days. Request an updated routing guide."
            )

        # ── 2. Search partner specs collection ────────────────────────────────
        spec_query = f"{partner_name} {doc_type} ASN compliance rules segments required"
        if extra_context:
            spec_query += f" {extra_context}"

        spec_chunks = self._store.search(
            query=spec_query,
            collection="partner_specs",
            top_k=config.retriever.top_k,
            where={"partner_name": partner_name},
        )

        # ── 3. Search validation history ──────────────────────────────────────
        history_query = (
            f"{partner_name} {doc_type} validation error failure "
            + (extra_context or "segment compliance")
        )
        history_chunks = self._store.search(
            query=history_query,
            collection="validation_history",
            top_k=config.retriever.top_k,
            where={"partner_name": partner_name},
        )

        # ── 4. Merge and re-rank ──────────────────────────────────────────────
        # Combined pool: spec chunks first (higher base relevance), then history.
        all_chunks = spec_chunks + history_chunks
        ranked = self._rerank(all_chunks)[: config.retriever.top_k]

        logger.info(
            "RetrievalAgent: %d spec chunks + %d history chunks → %d ranked results.",
            len(spec_chunks),
            len(history_chunks),
            len(ranked),
        )

        return ContextPackage(
            partner_name=partner_name,
            spec_version=spec_record.spec_version,
            spec_date=spec_record.spec_date,
            is_stale=spec_record.is_stale,
            spec_content=spec_record.content,
            ranked_chunks=ranked,
            warnings=warnings,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _rerank(chunks: list[SearchResult]) -> list[SearchResult]:
        """
        Re-rank a mixed list of chunks by a combined recency × relevance score.

        Score formula:
            combined = similarity_score × recency_weight

        ``similarity_score`` = 1 - cosine_distance  (from ChromaDB)
        ``recency_weight``   = days since spec_date mapped to [0.5, 1.0]:
            - Today's date → 1.0
            - 90+ days old → 0.5

        This ensures a highly relevant but recent failure log outranks an
        older but equally relevant spec chunk.
        """
        now = datetime.utcnow()
        max_age_days = float(config.retriever.staleness_days)  # 90 days → recency_weight = 0.5

        def _score(chunk: SearchResult) -> float:
            similarity = 1.0 - chunk.distance
            date_str = chunk.metadata.get("spec_date", "")
            try:
                chunk_date = datetime.fromisoformat(date_str)
                age_days = max(0.0, (now - chunk_date).days)
                # Linearly interpolate: 0 days → 1.0, 90+ days → 0.5
                recency = max(0.5, 1.0 - (age_days / max_age_days) * 0.5)
            except (ValueError, TypeError):
                recency = 0.75  # Unknown date → neutral weight

            return similarity * recency

        sorted_chunks = sorted(chunks, key=_score, reverse=True)

        # Re-assign ranks after sorting.
        for i, chunk in enumerate(sorted_chunks):
            chunk.rank = i + 1

        return sorted_chunks
