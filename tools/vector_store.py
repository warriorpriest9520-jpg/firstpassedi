"""
FirstPass EDI — Vector Store
==============================
Thin wrapper around ChromaDB for storing and querying:

  1. **Partner routing guide specs** — compliance rules per trading partner
  2. **Validation failure logs** — past errors with segment/element context
  3. **Resolution logs** — how past failures were resolved

Collections
───────────
  ``partner_specs``        — one document per partner per spec version
  ``validation_history``   — one document per historical validation event

Production note
───────────────
  ChromaDB is used here for demo simplicity (runs entirely in-process with
  local persistence).  In production, replace the ChromaDB client with a
  managed vector database such as Pinecone, Weaviate, or Qdrant.  The public
  interface of this class (``add_document``, ``search``, ``get_partner_spec``)
  stays the same — only the client initialisation changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import chromadb
from chromadb.config import Settings

from config import config

logger = logging.getLogger(__name__)


# ── Return types ──────────────────────────────────────────────────────────────


@dataclass
class SpecRecord:
    """A trading partner spec retrieved from the vector store."""

    partner_name: str
    spec_version: str
    spec_date: datetime
    content: str
    is_stale: bool          # True if older than RetrieverConfig.staleness_days
    distance: float         # Similarity distance (lower = more similar)


@dataclass
class SearchResult:
    """One chunk returned by a similarity search."""

    id: str
    content: str
    metadata: dict
    distance: float
    rank: int               # 1-based rank among returned results


# ── VectorStore ───────────────────────────────────────────────────────────────


class VectorStore:
    """
    Manages ChromaDB collections for partner specs and validation history.

    Parameters
    ----------
    db_path:
        Directory where ChromaDB persists data.  Defaults to the value in
        ``config.chroma_db_path``.
    """

    _SPECS_COLLECTION = "partner_specs"
    _HISTORY_COLLECTION = "validation_history"

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or config.chroma_db_path
        # In production, use chromadb.HttpClient(...) to point at a server.
        self._client = chromadb.PersistentClient(
            path=self._db_path,
            settings=Settings(anonymized_telemetry=False),
        )
        self._specs = self._client.get_or_create_collection(
            name=self._SPECS_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        self._history = self._client.get_or_create_collection(
            name=self._HISTORY_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("VectorStore initialised at %s", self._db_path)

    # ── Write ─────────────────────────────────────────────────────────────────

    def add_document(
        self,
        *,
        doc_id: str,
        content: str,
        metadata: dict,
        collection: str = "partner_specs",
    ) -> None:
        """
        Upsert a document into the specified collection.

        Parameters
        ----------
        doc_id:
            Unique identifier for this document (upsert semantics).
        content:
            Plain-text content that will be embedded and searched.
        metadata:
            Arbitrary key/value pairs stored alongside the embedding.
            Required keys for partner_specs: ``partner_name``, ``spec_version``,
            ``spec_date`` (ISO 8601 string).
        collection:
            ``"partner_specs"`` or ``"validation_history"``.
        """
        coll = self._specs if collection == "partner_specs" else self._history
        coll.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata],
        )
        logger.debug("Upserted doc '%s' into collection '%s'.", doc_id, collection)

    # ── Read ──────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        collection: str = "partner_specs",
        top_k: Optional[int] = None,
        where: Optional[dict] = None,
        min_score: Optional[float] = None,
    ) -> list[SearchResult]:
        """
        Semantic search against a collection.

        Parameters
        ----------
        query:
            Natural-language or structured query string.
        collection:
            Which collection to search.
        top_k:
            Number of results to return.  Defaults to ``config.retriever.top_k``.
        where:
            Optional ChromaDB metadata filter (e.g. ``{"partner_name": "RetailerA"}``).

        Returns
        -------
        list[SearchResult]
            Ranked by similarity (rank 1 = most similar).
        """
        k = top_k or config.retriever.top_k
        coll = self._specs if collection == "partner_specs" else self._history

        kwargs: dict = {"query_texts": [query], "n_results": min(k, coll.count() or 1)}
        if where:
            kwargs["where"] = where

        results = coll.query(**kwargs)

        search_results: list[SearchResult] = []
        if not results["ids"] or not results["ids"][0]:
            return search_results

        threshold = min_score if min_score is not None else config.retriever.min_relevance_score

        for rank, (doc_id, doc, meta, dist) in enumerate(
            zip(
                results["ids"][0],
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ),
            start=1,
        ):
            score = 1.0 - dist   # convert cosine distance → similarity
            if score >= threshold:
                search_results.append(
                    SearchResult(id=doc_id, content=doc, metadata=meta, distance=dist, rank=rank)
                )

        return search_results

    def get_partner_spec(self, partner_name: str) -> Optional[SpecRecord]:
        """
        Retrieve the most recent routing guide spec for a named partner.

        Checks the ``spec_date`` in metadata against ``config.retriever.staleness_days``
        and sets ``is_stale`` accordingly.

        Parameters
        ----------
        partner_name:
            Exact partner name as stored in metadata (e.g. ``"RetailerA"``).

        Returns
        -------
        SpecRecord | None
            The spec, or None if no spec exists for this partner.
        """
        # Use min_score=0.0 for a targeted partner lookup — we want the top result
        # for this specific partner regardless of cosine similarity score.
        results = self.search(
            query=f"{partner_name} 856 routing guide compliance rules",
            collection="partner_specs",
            top_k=1,
            where={"partner_name": partner_name},
            min_score=0.0,
        )
        if not results:
            logger.warning("No spec found in vector store for partner '%s'.", partner_name)
            return None

        hit = results[0]
        spec_date_str = hit.metadata.get("spec_date", "")
        try:
            spec_date = datetime.fromisoformat(spec_date_str)
        except ValueError:
            spec_date = datetime.min

        staleness_cutoff = datetime.utcnow() - timedelta(days=config.retriever.staleness_days)
        is_stale = spec_date < staleness_cutoff

        if is_stale:
            logger.warning(
                "Partner spec for '%s' (version %s, dated %s) is STALE (>%d days).",
                partner_name,
                hit.metadata.get("spec_version", "?"),
                spec_date_str,
                config.retriever.staleness_days,
            )

        return SpecRecord(
            partner_name=partner_name,
            spec_version=hit.metadata.get("spec_version", "unknown"),
            spec_date=spec_date,
            content=hit.content,
            is_stale=is_stale,
            distance=hit.distance,
        )

    # ── Demo seed data ────────────────────────────────────────────────────────

    def seed_demo_data(self) -> None:
        """
        Populate the vector store with synthetic partner specs and failure logs.

        Call this once at startup when DEMO_MODE is enabled.  Safe to call
        multiple times — upsert semantics prevent duplicates.
        """
        logger.info("Seeding vector store with demo data …")

        # ── RetailerA — current spec (fresh) ──────────────────────────────────
        self.add_document(
            doc_id="spec-retailer-a-v3",
            content="""
RetailerA 856 ASN Routing Guide — Version 3.2 (Effective 2026-06-01)

REQUIRED SEGMENTS (must appear in every 856):
- ISA: sender/receiver IDs must use qualifier ZZ
- GS: functional ID must be SH
- BSN01: must be 00 (original), 05 (replace), or 06 (cancel)
- BSN05: hierarchical structure code must be 0002
- HL (S level): TD1 and TD5 are mandatory at shipment level
- TD1: weight (TD1*08) and weight qualifier (TD1*07 = G/N) required
- TD5: carrier SCAC code (TD5*03) required; must be a valid 2-4 char SCAC
- HL (O level): PRF01 (PO number) is mandatory
- HL (I level): LIN must include UPC (LIN*02=UP) with 12-digit barcode
- SN1: shipped quantity (SN1*02) must be > 0; unit of measure (SN1*03) required

VALIDATION RULES:
- All date fields: CCYYMMDD format (8 digits)
- All time fields: HHMM or HHMMSS format
- PO number (PRF01) must match an open RetailerA purchase order
- Carrier SCAC codes accepted: UPS, UPSN, FDEG, FXFE, ONTRAC, ESTES
- UPC-A barcodes: exactly 12 digits, numeric only, valid check digit
- Shipment weight must be between 0.1 and 50,000 LB

CONDITIONAL RULES:
- If TD5*04 (service level) = AM (Air Mail), DTM*067 (estimated delivery) is required
- REF*BM (Bill of Lading) required for LTL shipments (TD5*03 in {ESTES, SAIA, ODFL})

ERROR SEVERITY GUIDE:
- CRITICAL: Missing required segment, invalid PO reference, zero quantity
- WARNING: Missing optional but recommended field, carrier SCAC not in preferred list
- INFO: Non-standard but parseable format
""",
            metadata={
                "partner_name": "RetailerA",
                "spec_version": "3.2",
                "spec_date": "2026-08-01T00:00:00",   # within 90 days → not stale
                "doc_type": "routing_guide",
            },
        )

        # ── RetailerB — stale spec (older than 90 days) ───────────────────────
        self.add_document(
            doc_id="spec-retailer-b-v1",
            content="""
RetailerB 856 ASN Routing Guide — Version 1.0 (Effective 2024-01-15)

REQUIRED SEGMENTS:
- BSN01: 00 or 05 only
- BSN05: 0002 required
- HL (S level): TD1 required; TD5 optional but recommended
- HL (O level): PRF01 (PO number) required
- HL (I level): LIN with UPC required; SN1 required

VALIDATION RULES:
- Dates must be CCYYMMDD
- PO numbers: alphanumeric, max 22 chars
- UPC: 12 digits
- Shipped quantity: positive integer

NOTE: This spec is Version 1.0. RetailerB updated their routing guide in 2025.
Please contact your RetailerB EDI coordinator for the current version.
""",
            metadata={
                "partner_name": "RetailerB",
                "spec_version": "1.0",
                "spec_date": "2024-01-15T00:00:00",   # intentionally old — triggers stale warning
                "doc_type": "routing_guide",
            },
        )

        # ── Validation failure logs ────────────────────────────────────────────
        self.add_document(
            doc_id="failure-001",
            collection="validation_history",
            content="""
Validation Failure Log — 2026-08-15
Partner: RetailerA
Document: SHIP-2026-08-123
Segment: LIN (Item level)
Error: UPC barcode '012345ABC901' contains non-numeric characters.
Severity: CRITICAL
Resolution: Corrected UPC to '012345123901' — verified against GS1 registry.
Outcome: Resubmitted and accepted.
""",
            metadata={
                "partner_name": "RetailerA",
                "doc_type": "failure_log",
                "segment": "LIN",
                "severity": "critical",
                "spec_date": "2026-08-15T00:00:00",
            },
        )

        self.add_document(
            doc_id="failure-002",
            collection="validation_history",
            content="""
Validation Failure Log — 2026-08-22
Partner: RetailerA
Document: SHIP-2026-08-201
Segment: SN1
Error: SN1*02 (shipped quantity) was 0. RetailerA spec requires quantity > 0.
Severity: CRITICAL
Root Cause: Inventory system exported unfulfilled line items with qty=0.
Resolution: Filtered out zero-quantity lines before EDI generation.
Outcome: Fixed at source; resubmitted successfully.
""",
            metadata={
                "partner_name": "RetailerA",
                "doc_type": "failure_log",
                "segment": "SN1",
                "severity": "critical",
                "spec_date": "2026-08-22T00:00:00",
            },
        )

        self.add_document(
            doc_id="failure-003",
            collection="validation_history",
            content="""
Validation Failure Log — 2026-09-01
Partner: RetailerA
Document: SHIP-2026-09-002
Segment: TD5
Error: TD5*03 (carrier SCAC) was missing.
Severity: CRITICAL
Root Cause: Carrier mapping table had null SCAC for new carrier 'FastFreight LLC'.
Resolution: Added SCAC 'FAST' for FastFreight LLC in carrier table.
Outcome: Resubmitted with TD5*03=FAST; accepted.
""",
            metadata={
                "partner_name": "RetailerA",
                "doc_type": "failure_log",
                "segment": "TD5",
                "severity": "critical",
                "spec_date": "2026-09-01T00:00:00",
            },
        )

        logger.info("Demo data seeded: 2 partner specs + 3 failure logs.")
