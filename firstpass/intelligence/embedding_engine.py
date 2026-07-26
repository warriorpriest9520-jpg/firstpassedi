"""
embedding_engine.py — Embedding pipeline for FirstPass EDI.

Handles OpenAI text embeddings + Supabase vector storage and semantic search.
All functions are fail-safe: errors are logged but never raised to callers.

Requires environment variables:
    OPENAI_API_KEY  — OpenAI API key
    SUPABASE_URL    — Supabase project URL
    SUPABASE_KEY    — Supabase service key

Source lineage: embedding_engine.py (ceo-bot)
"""

import logging
import time
from typing import Optional

from firstpass.config import config

log = logging.getLogger("firstpass.intelligence.embedding_engine")

# ---------------------------------------------------------------------------
# Lazy clients
# ---------------------------------------------------------------------------

_openai_client = None
_supabase_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        api_key = config.OPENAI_API_KEY
        if not api_key:
            return None
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=api_key)
        except Exception as e:
            log.error(f"[embedding_engine] Failed to init OpenAI client: {e}")
    return _openai_client


def _get_supabase():
    global _supabase_client
    if _supabase_client is None:
        if not config.supabase_configured:
            return None
        try:
            from firstpass.memory.supabase_client import get_client
            _supabase_client = get_client()
        except Exception as e:
            log.error(f"[embedding_engine] Failed to init Supabase client: {e}")
    return _supabase_client


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def get_embedding(text: str) -> Optional[list]:
    """Generate a 1536-dim embedding for text using text-embedding-3-small.

    Returns ``None`` on any failure — never raises.
    """
    if not text or not text.strip():
        return None
    client = _get_openai()
    if client is None:
        return None
    try:
        text = text.replace("\n", " ").strip()[:8000]  # token safety
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        return response.data[0].embedding
    except Exception as e:
        log.error(f"[embedding_engine] get_embedding failed: {e}")
        return None


def embed_and_store(table: str, row_id: int, text: str) -> bool:
    """Generate embedding for ``text`` and PATCH it into the given table row.

    Returns ``True`` on success, ``False`` on any failure — never raises.
    """
    vector = get_embedding(text)
    if vector is None:
        return False
    client = _get_supabase()
    if client is None:
        return False
    try:
        client.table(table).update({"embedding": vector}).eq("id", row_id).execute()
        log.debug(f"[embedding_engine] Embedded {table} id={row_id}")
        return True
    except Exception as e:
        log.error(f"[embedding_engine] embed_and_store({table}, {row_id}) failed: {e}")
        return False


def semantic_search(table: str, query: str, match_count: int = 5) -> list:
    """Semantic similarity search via Supabase RPC ``match_{table}`` function.

    Returns a list of dicts with similarity scores, or an empty list on any
    failure.  Requires ``match_*`` RPC functions to be defined in Supabase
    (see ``migrations/create_match_functions.sql``).
    """
    vector = get_embedding(query)
    if vector is None:
        return []
    client = _get_supabase()
    if client is None:
        return []
    try:
        result = client.rpc(
            f"match_{table}",
            {"query_embedding": vector, "match_count": match_count},
        ).execute()
        return result.data if result.data else []
    except Exception as e:
        log.error(f"[embedding_engine] semantic_search({table}) failed: {e}")
        return []


def backfill_table(
    table: str,
    text_column: str,
    id_column: str = "id",
    batch_size: int = 20,
    batch_delay: float = 1.0,
    dry_run: bool = False,
) -> dict:
    """Backfill embeddings for all rows in a table where ``embedding IS NULL``.

    Processes in batches to avoid OpenAI rate limits.

    :returns: Dict with keys: ``table``, ``processed``, ``succeeded``, ``failed``, ``skipped``.
    """
    client = _get_supabase()
    if client is None:
        return {
            "table": table,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "error": "No Supabase client",
        }

    stats: dict = {
        "table": table,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
    }

    try:
        result = (
            client.table(table)
            .select(f"{id_column},{text_column}")
            .is_("embedding", "null")
            .execute()
        )
        rows = result.data or []
        stats["total_pending"] = len(rows)

        if dry_run:
            print(f"  [dry-run] {table}: {len(rows)} rows need embeddings")
            return stats

        print(f"  {table}: backfilling {len(rows)} rows in batches of {batch_size}...")

        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            for row in batch:
                row_id = row.get(id_column)
                text = row.get(text_column, "")
                if not text:
                    stats["skipped"] += 1
                    continue
                stats["processed"] += 1
                ok = embed_and_store(table, row_id, str(text))
                if ok:
                    stats["succeeded"] += 1
                else:
                    stats["failed"] += 1

            if i + batch_size < len(rows):
                time.sleep(batch_delay)

        print(
            f"  {table}: done — {stats['succeeded']} ok, "
            f"{stats['failed']} failed, {stats['skipped']} skipped"
        )
    except Exception as e:
        log.error(f"[embedding_engine] backfill_table({table}) failed: {e}")
        stats["error"] = str(e)

    return stats


# ---------------------------------------------------------------------------
# EDI-specific search wrappers
# ---------------------------------------------------------------------------

def search_edi_incidents(query: str, n: int = 5) -> list:
    """Semantic search over the ``edi_incidents`` table."""
    return semantic_search("edi_incidents", query, n)


def search_corporate_memory(query: str, n: int = 5) -> list:
    """Semantic search over the ``corporate_memory`` facts table."""
    return semantic_search("corporate_memory", query, n)


def search_work_events(query: str, n: int = 5) -> list:
    """Semantic search over the ``work_events`` table."""
    return semantic_search("work_events", query, n)


def search_partner_knowledge(query: str, n: int = 5) -> list:
    """Semantic search over partner-specific knowledge chunks."""
    return semantic_search("partner_knowledge", query, n)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    print("Testing FirstPass EDI embedding_engine...")
    vec = get_embedding("test embedding for FirstPass EDI")
    if vec:
        print(f"  OpenAI OK — got vector of length {len(vec)}")
    else:
        print("  OpenAI FAILED — check OPENAI_API_KEY")

    results = search_edi_incidents("850 purchase order parse error")
    print(f"  Supabase semantic search returned {len(results)} results")
    if results:
        print(f"  Top result: {json.dumps(results[0], indent=2)}")
