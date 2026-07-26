"""
corporate_memory.py — RAG-powered corporate knowledge store.

Stores and retrieves institutional knowledge using vector embeddings
(via OpenAI/Anthropic) backed by Supabase's pgvector extension.

Knowledge types:
  - EDI specs and partner requirements
  - Historical issue resolutions
  - Process SOPs and runbooks
  - Partner communication history

When pgvector / embedding APIs are not configured, falls back to
simple keyword-based full-text search.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..config import config

log = logging.getLogger("firstpass.memory.corporate_memory")

TABLE = "corporate_memory"


class CorporateMemory:
    """
    RAG (Retrieval-Augmented Generation) knowledge store.

    In production mode: stores chunked documents with embeddings in Supabase
    and retrieves the top-k most semantically similar chunks for any query.

    In dry-run mode: stores in-memory and uses simple substring matching.
    """

    def __init__(self):
        self._in_memory: List[Dict] = []

    # ── Store ──────────────────────────────────────────────────────────────

    def store(
        self,
        content: str,
        doc_type: str = "general",
        source: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """
        Store a knowledge chunk.

        :param content: Text content to store (max ~2000 tokens recommended)
        :param doc_type: Category ("edi_spec", "sop", "issue_resolution", etc.)
        :param source: Origin (file path, partner name, etc.)
        :param tags: Optional list of searchable tags
        :param metadata: Optional extra metadata dict
        """
        embedding = self._embed(content)
        row = {
            "content": content,
            "doc_type": doc_type,
            "source": source,
            "tags": tags or [],
            "metadata": metadata or {},
            "embedding": embedding,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        sb = self._sb()
        if sb and embedding:
            try:
                sb.table(TABLE).insert(row).execute()
                return True
            except Exception as exc:
                log.error(f"corporate_memory.store failed: {exc}")
        # In-memory fallback (no persistence)
        self._in_memory.append(row)
        return True

    # ── Retrieve ───────────────────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        doc_type: Optional[str] = None,
    ) -> List[Dict]:
        """
        Retrieve the top-k most relevant knowledge chunks for a query.

        Uses vector similarity search when pgvector is available,
        otherwise falls back to substring matching.

        :returns: List of {"content": str, "doc_type": str, "similarity": float} dicts
        """
        sb = self._sb()
        if sb:
            try:
                return self._vector_search(sb, query, top_k, doc_type)
            except Exception as exc:
                log.warning(f"Vector search failed, falling back to FTS: {exc}")
        return self._fts_fallback(query, top_k, doc_type)

    def retrieve_for_partner(self, partner_name: str, top_k: int = 10) -> List[Dict]:
        """Retrieve all knowledge related to a specific trading partner."""
        sb = self._sb()
        if sb:
            try:
                q = (
                    sb.table(TABLE)
                      .select("content,doc_type,source,metadata,created_at")
                      .contains("tags", [partner_name])
                      .order("created_at", desc=True)
                      .limit(top_k)
                )
                result = q.execute()
                return result.data or []
            except Exception as exc:
                log.warning(f"retrieve_for_partner failed: {exc}")
        return [r for r in self._in_memory if partner_name.lower() in r.get("content", "").lower()]

    # ── Internal ───────────────────────────────────────────────────────────

    def _embed(self, text: str) -> Optional[List[float]]:
        """Generate text embedding. Returns None if no embedding API configured."""
        if not config.OPENAI_API_KEY and not config.ANTHROPIC_API_KEY:
            return None
        try:
            import openai
            client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
            resp = client.embeddings.create(
                model="text-embedding-3-small",
                input=text[:8000],
            )
            return resp.data[0].embedding
        except Exception as exc:
            log.debug(f"Embedding generation failed: {exc}")
            return None

    def _vector_search(self, sb, query: str, top_k: int, doc_type: Optional[str]) -> List[Dict]:
        """Supabase pgvector similarity search via RPC."""
        query_embedding = self._embed(query)
        if not query_embedding:
            return self._fts_fallback(query, top_k, doc_type)
        rpc_params = {"query_embedding": query_embedding, "match_count": top_k}
        if doc_type:
            rpc_params["filter_doc_type"] = doc_type
        result = sb.rpc("match_corporate_memory", rpc_params).execute()
        return result.data or []

    def _fts_fallback(self, query: str, top_k: int, doc_type: Optional[str]) -> List[Dict]:
        """Simple substring matching over in-memory store."""
        q = query.lower()
        matches = [
            r for r in self._in_memory
            if q in r.get("content", "").lower()
            and (doc_type is None or r.get("doc_type") == doc_type)
        ]
        return matches[:top_k]

    @staticmethod
    def _sb():
        from ..memory.supabase_client import get_client
        return get_client()
