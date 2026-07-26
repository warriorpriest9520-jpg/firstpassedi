"""FirstPass EDI intelligence — pattern detection, analytics, and embedding engine.

Modules:
    analytics        — Pattern detection, trend analysis, decision journal,
                       intelligence briefings (IntelligenceEngine)
    embedding_engine — OpenAI text embeddings + Supabase pgvector search
"""

from firstpass.intelligence.analytics import IntelligenceEngine
from firstpass.intelligence.embedding_engine import (
    embed_and_store,
    get_embedding,
    semantic_search,
)

__all__ = [
    "IntelligenceEngine",
    "get_embedding",
    "embed_and_store",
    "semantic_search",
]
