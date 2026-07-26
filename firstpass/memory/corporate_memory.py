"""
corporate_memory.py — Persistent corporate knowledge store for FirstPass EDI.

RAG-powered (Retrieval-Augmented Generation) institutional knowledge base.
Facts accumulate over time, gain confidence when confirmed, and slowly decay
when not reinforced.  Provides two complementary APIs:

  1. Key-value memory (remember / recall) — structured facts with confidence
     scoring, decay, contradiction tracking, and keyword search.

  2. Document-chunk store (store / retrieve) — full text chunks backed by
     pgvector similarity search when Supabase is available, with an
     in-memory substring-match fallback for local/dry-run mode.

Knowledge categories:
  - EDI specs and partner requirements
  - Historical issue resolutions
  - Process SOPs and runbooks
  - Partner communication history
  - Recurring patterns and anomalies

Usage::

    from firstpass.memory.corporate_memory import CorporateMemory

    memory = CorporateMemory()

    # Fact-based API
    memory.remember("pattern", "wayfair_monday_errors",
                    "Wayfair EDI 850 errors spike on Mondays",
                    source="edi_agent", confidence=0.6)
    facts = memory.recall("wayfair errors")
    print(memory.recall_for_context("process wayfair 850"))

    # Document-chunk API
    memory.store("Wayfair requires ISA qualifier ZZ...", doc_type="edi_spec",
                 tags=["wayfair"])
    chunks = memory.retrieve("Wayfair ISA qualifier", top_k=3)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import config

# ---------------------------------------------------------------------------
# Supabase helpers (lazy — module loads without supabase installed)
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent.parent   # firstpass-edi/
STORE_DIR = _ROOT / "knowledge_store"
FAILSAFE_FILE_NAME = "corporate_memory_failsafe.jsonl"
TABLE = "corporate_memory"

CATEGORIES = [
    "decision",
    "pattern",
    "domain_fact",
    "relationship",
    "lesson_learned",
    "bot_expertise",
]

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "and", "but", "or",
    "nor", "not", "so", "yet", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "only", "own", "same", "than", "too",
    "very", "just", "because", "if", "when", "where", "how", "what",
    "which", "who", "whom", "this", "that", "these", "those", "it", "its",
    "all", "any", "about", "up", "down", "here", "there", "run", "check",
    "get", "set", "use", "new", "old",
}

logger = logging.getLogger("firstpass.memory.corporate_memory")


def _get_supabase_client():
    """Return a configured Supabase client or None."""
    try:
        from ..memory.supabase_client import get_client
        if config.supabase_configured:
            return get_client()
    except Exception:
        pass
    return None


def _write_failsafe(store_dir: Path, key: str, value: Any, category: str, metadata: dict) -> None:
    """Append a failed upsert to the local failsafe JSONL file."""
    try:
        entry = {
            "key": key,
            "value": str(value),
            "category": category,
            "metadata": metadata or {},
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        failsafe = store_dir / FAILSAFE_FILE_NAME
        with failsafe.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _recover_failsafe(store_dir: Path) -> None:
    """On startup, retry any pending failsafe writes to Supabase."""
    failsafe = store_dir / FAILSAFE_FILE_NAME
    if not failsafe.exists():
        return
    sb = _get_supabase_client()
    if not sb:
        return
    pending: list = []
    recovered = 0
    try:
        lines = failsafe.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            sb.table(TABLE).upsert(
                {
                    "key": entry["key"],
                    "value": entry["value"],
                    "category": entry.get("category", "general"),
                    "metadata": entry.get("metadata") or {},
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="key",
            ).execute()
            recovered += 1
        except Exception:
            pending.append(line)
    if recovered:
        logger.info(f"[corporate_memory] Recovered {recovered} failsafe writes")
    try:
        if pending:
            failsafe.write_text("\n".join(pending) + "\n", encoding="utf-8")
        else:
            failsafe.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class CorporateMemory:
    """
    Persistent institutional knowledge store for FirstPass EDI.

    Provides two complementary APIs:
    - Fact-based: ``remember`` / ``recall`` / ``reinforce`` / ``contradict``
    - Document-chunk RAG: ``store`` / ``retrieve`` / ``retrieve_for_partner``
    """

    def __init__(self, store_dir: str = None):
        self._store_dir = Path(store_dir) if store_dir else STORE_DIR
        self._memory_file = self._store_dir / "corporate_memory.json"
        self._audit_file = self._store_dir / "memory_audit.jsonl"
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._facts: Dict[str, Dict[str, Any]] = self._load()
        self._counter = len(self._facts)
        # In-memory list for document-chunk store fallback
        self._chunks: List[Dict] = []
        # Retry any failsafe writes from prior sessions
        _recover_failsafe(self._store_dir)

    # ------------------------------------------------------------------
    # Fact-based API (remember / recall)
    # ------------------------------------------------------------------

    def remember(
        self,
        category: str,
        topic: str,
        content: str,
        source: str = "unknown",
        confidence: float = 0.5,
        tags: List[str] = None,
        metadata: Dict[str, Any] = None,
    ) -> str:
        """Store a new fact or reinforce an existing one if the topic matches.

        Returns the memory ID.
        """
        if category not in CATEGORIES:
            category = "domain_fact"

        existing = self._find_by_topic(topic)
        if existing:
            self.reinforce(existing["id"], new_evidence=content)
            return existing["id"]

        self._counter += 1
        mem_id = f"mem_{datetime.now().strftime('%Y%m%d')}_{self._counter:04d}"

        fact = {
            "id": mem_id,
            "category": category,
            "topic": topic,
            "content": content,
            "confidence": max(0.01, min(1.0, confidence)),
            "source": source,
            "evidence_count": 1,
            "tags": tags or self._auto_tags(topic, content),
            "first_seen": datetime.now().isoformat(),
            "last_confirmed": datetime.now().isoformat(),
            "last_contradicted": None,
            "related_facts": [],
            "metadata": metadata or {},
        }

        self._facts[mem_id] = fact
        self._save()
        self._audit("remember", mem_id, f"New fact: {topic}")
        logger.info(f"[corporate_memory] Stored: {topic} (conf={confidence:.2f})")
        return mem_id

    def recall(
        self,
        query: str = None,
        category: str = None,
        tags: List[str] = None,
        min_confidence: float = 0.0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search memory by keyword query, category, and/or tags.

        Results sorted by (confidence × recency_weight).
        """
        results = []
        now = datetime.now()
        query_lower = query.lower() if query else None
        query_words: set = set()
        if query_lower:
            query_words = {
                w for w in re.split(r"\W+", query_lower) if w and w not in STOPWORDS
            }

        for fact in self._facts.values():
            if category and fact["category"] != category:
                continue
            if fact["confidence"] < min_confidence:
                continue
            if tags:
                fact_tags = {t.lower() for t in fact.get("tags", [])}
                if not any(t.lower() in fact_tags for t in tags):
                    continue

            score = fact["confidence"]
            if query_words:
                searchable = (
                    fact["topic"].lower()
                    + " "
                    + fact["content"].lower()
                    + " "
                    + " ".join(t.lower() for t in fact.get("tags", []))
                )
                matches = sum(1 for w in query_words if w in searchable)
                if matches == 0:
                    continue
                score *= matches / len(query_words)

            try:
                last_confirmed = datetime.fromisoformat(fact["last_confirmed"])
                days_ago = (now - last_confirmed).days
                recency_weight = max(0.5, 1.0 - (days_ago / 365))
            except Exception:
                recency_weight = 0.5

            score *= recency_weight
            results.append((score, fact))

        results.sort(key=lambda x: x[0], reverse=True)
        return [fact for _, fact in results[:limit]]

    def recall_for_context(self, task_description: str, max_facts: int = 10) -> str:
        """Extract keywords from task_description and return a formatted context block.

        Returns text suitable for injection into an LLM system prompt.
        """
        words = set(re.split(r"\W+", task_description.lower()))
        keywords = [w for w in words if w and w not in STOPWORDS and len(w) > 2]
        if not keywords:
            return ""

        facts = self.recall(query=" ".join(keywords), min_confidence=0.3, limit=max_facts)
        if not facts:
            return ""

        lines = ["\nRELEVANT CORPORATE MEMORY:"]
        for fact in facts:
            conf_pct = int(fact["confidence"] * 100)
            lines.append(
                f"  [{fact['category'].upper()}] (conf:{conf_pct}%) {fact['content']}"
            )
        return "\n".join(lines) + "\n"

    def reinforce(self, memory_id: str, new_evidence: str = "") -> None:
        """Increase confidence when a fact is confirmed."""
        fact = self._facts.get(memory_id)
        if not fact:
            return
        old_conf = fact["confidence"]
        fact["confidence"] = min(0.99, fact["confidence"] + 0.05 * (1 - fact["confidence"]))
        fact["evidence_count"] += 1
        fact["last_confirmed"] = datetime.now().isoformat()
        if new_evidence:
            existing = fact.get("metadata", {})
            evidence_log = existing.get("evidence_log", [])
            evidence_log.append(
                {"timestamp": datetime.now().isoformat(), "evidence": new_evidence[:200]}
            )
            fact["metadata"]["evidence_log"] = evidence_log[-10:]
        self._save()
        self._audit(
            "reinforce",
            memory_id,
            f"Confidence {old_conf:.2f} -> {fact['confidence']:.2f} (count={fact['evidence_count']})",
        )

    def contradict(self, memory_id: str, contradiction: str = "") -> None:
        """Decrease confidence when evidence contradicts a fact."""
        fact = self._facts.get(memory_id)
        if not fact:
            return
        old_conf = fact["confidence"]
        fact["confidence"] = max(0.01, fact["confidence"] * 0.85)
        fact["last_contradicted"] = datetime.now().isoformat()
        if contradiction:
            existing = fact.get("metadata", {})
            contradictions = existing.get("contradictions", [])
            contradictions.append(
                {"timestamp": datetime.now().isoformat(), "details": contradiction[:200]}
            )
            fact["metadata"]["contradictions"] = contradictions[-5:]
        self._save()
        self._audit(
            "contradict",
            memory_id,
            f"Confidence {old_conf:.2f} -> {fact['confidence']:.2f}: {contradiction[:100]}",
        )

    def forget_weak(self, threshold: float = 0.1, min_age_days: int = 30) -> int:
        """Remove facts with confidence below threshold that are old enough.

        Returns count of facts removed.
        """
        cutoff = datetime.now() - timedelta(days=min_age_days)
        to_remove = []
        for mem_id, fact in self._facts.items():
            if fact["confidence"] < threshold:
                try:
                    last_confirmed = datetime.fromisoformat(fact["last_confirmed"])
                    if last_confirmed < cutoff:
                        to_remove.append(mem_id)
                except Exception:
                    to_remove.append(mem_id)
        for mem_id in to_remove:
            self._audit(
                "forget",
                mem_id,
                f"Weak fact removed (conf={self._facts[mem_id]['confidence']:.2f})",
            )
            del self._facts[mem_id]
        if to_remove:
            self._save()
            logger.info(f"[corporate_memory] Forgot {len(to_remove)} weak facts")
        return len(to_remove)

    # ------------------------------------------------------------------
    # Memory Consolidation
    # ------------------------------------------------------------------

    def consolidate(
        self,
        work_log_entries: List[Dict[str, Any]],
        shared_context_entries: Dict[str, Any],
        llm_callable=None,
    ) -> List[str]:
        """Extract durable insights from recent work_log and shared_context.

        Uses an LLM (if provided) to identify patterns, domain facts, and
        lessons worth remembering long-term.  Returns list of new/updated
        memory IDs.

        Args:
            work_log_entries:    Recent work-log entries to analyse.
            shared_context_entries: Current shared-context data.
            llm_callable:        Callable(system_prompt, user_prompt) -> str.
                                 If None, LLM consolidation is skipped.
        """
        if not work_log_entries and not shared_context_entries:
            return []

        last_marker = self._load_consolidation_marker()
        last_ts = last_marker.get("last_timestamp", "1970-01-01T00:00:00")

        new_entries = [e for e in work_log_entries if e.get("timestamp", "") > last_ts]
        if not new_entries and not shared_context_entries:
            return []

        if not llm_callable:
            logger.info("[corporate_memory] No LLM callable provided — skipping consolidation")
            return []

        existing_topics = [f["topic"] for f in self._facts.values()]
        work_data = json.dumps(new_entries[-30:], indent=2, default=str)
        ctx_data = (
            json.dumps(shared_context_entries, indent=2, default=str)
            if shared_context_entries
            else "{}"
        )
        existing_data = json.dumps(existing_topics[:50], indent=2)

        prompt = f"""Analyse these recent EDI agent activity logs and shared context to extract
DURABLE insights worth remembering permanently.

RECENT WORK LOG (newest first):
{work_data}

CURRENT SHARED CONTEXT:
{ctx_data}

ALREADY KNOWN TOPICS (avoid duplicates):
{existing_data}

Extract 3-8 insights that are worth remembering permanently.  Focus on:
1. Recurring patterns (same type of error/task appearing 2+ times)
2. Domain facts learned (partner X uses EDI type Y, report Z runs on Mondays)
3. Successful strategies (what approach worked well)
4. Relationships between entities (partner-to-integration mappings)
5. Lessons learned from failures

Return a JSON array of objects:
[{{"category": "pattern|domain_fact|lesson_learned|relationship|bot_expertise",
   "topic": "short_slug_for_this_fact",
   "content": "One clear sentence describing the insight",
   "confidence": 0.3 to 0.8,
   "tags": ["tag1", "tag2"],
   "source": "which agent's data revealed this"}}]

Return ONLY valid JSON array.  If no meaningful insights, return [].
"""
        system = (
            "You are an intelligence analyst extracting permanent institutional "
            "knowledge from EDI operational data.  Be specific and factual.  Only "
            "extract insights useful to remember weeks or months from now.  "
            "Return only valid JSON."
        )

        try:
            response = llm_callable(system, prompt)
            if not response:
                return []

            response = response.strip()
            if response.startswith("```"):
                lines = response.split("\n")
                response = "\n".join(l for l in lines if not l.startswith("```"))

            insights = json.loads(response)
            if not isinstance(insights, list):
                return []

            new_ids = []
            for insight in insights:
                mem_id = self.remember(
                    category=insight.get("category", "domain_fact"),
                    topic=insight.get("topic", "unknown"),
                    content=insight.get("content", ""),
                    source=insight.get("source", "consolidation"),
                    confidence=insight.get("confidence", 0.5),
                    tags=insight.get("tags", []),
                )
                new_ids.append(mem_id)

            if new_entries:
                latest_ts = max(e.get("timestamp", "") for e in new_entries)
                self._save_consolidation_marker(latest_ts)

            logger.info(f"[corporate_memory] Consolidated {len(new_ids)} insights")
            return new_ids

        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"[corporate_memory] Consolidation error: {e}")
            return []

    # ------------------------------------------------------------------
    # Confidence Decay
    # ------------------------------------------------------------------

    def apply_confidence_decay(
        self, days_since_confirm: int = 30, decay_rate: float = 0.02
    ) -> int:
        """Slowly decay confidence of facts not recently confirmed.

        Returns count of facts affected.
        """
        cutoff = datetime.now() - timedelta(days=days_since_confirm)
        affected = 0
        for fact in self._facts.values():
            try:
                last_confirmed = datetime.fromisoformat(fact["last_confirmed"])
                if last_confirmed < cutoff:
                    old_conf = fact["confidence"]
                    periods = (datetime.now() - last_confirmed).days / days_since_confirm
                    fact["confidence"] = max(
                        0.01, fact["confidence"] * (1 - decay_rate) ** periods
                    )
                    if abs(old_conf - fact["confidence"]) > 0.001:
                        affected += 1
            except Exception:
                continue
        if affected:
            self._save()
            logger.info(f"[corporate_memory] Decayed {affected} stale facts")
        return affected

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> Dict[str, Any]:
        """Return intelligence metrics about the knowledge store."""
        if not self._facts:
            return {
                "total_facts": 0,
                "by_category": {},
                "avg_confidence": 0,
                "confidence_distribution": {},
                "knowledge_growth_rate": 0,
                "most_active_topics": [],
                "stalest_facts": [],
            }
        facts = list(self._facts.values())
        confidences = [f["confidence"] for f in facts]
        by_category: Dict[str, int] = {}
        for f in facts:
            cat = f["category"]
            by_category[cat] = by_category.get(cat, 0) + 1
        buckets = {"0-20%": 0, "20-40%": 0, "40-60%": 0, "60-80%": 0, "80-100%": 0}
        for c in confidences:
            pct = c * 100
            if pct < 20:
                buckets["0-20%"] += 1
            elif pct < 40:
                buckets["20-40%"] += 1
            elif pct < 60:
                buckets["40-60%"] += 1
            elif pct < 80:
                buckets["60-80%"] += 1
            else:
                buckets["80-100%"] += 1
        one_week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        new_this_week = sum(1 for f in facts if f["first_seen"] > one_week_ago)
        most_active = sorted(facts, key=lambda f: f["evidence_count"], reverse=True)[:5]
        stalest = sorted(facts, key=lambda f: f["last_confirmed"])[:5]
        return {
            "total_facts": len(facts),
            "by_category": by_category,
            "avg_confidence": round(sum(confidences) / len(confidences), 3),
            "confidence_distribution": buckets,
            "knowledge_growth_rate": new_this_week,
            "most_active_topics": [
                {
                    "topic": f["topic"],
                    "evidence_count": f["evidence_count"],
                    "confidence": round(f["confidence"], 2),
                }
                for f in most_active
            ],
            "stalest_facts": [
                {
                    "topic": f["topic"],
                    "last_confirmed": f["last_confirmed"],
                    "confidence": round(f["confidence"], 2),
                }
                for f in stalest
            ],
        }

    # ------------------------------------------------------------------
    # Document-chunk API (store / retrieve)
    # ------------------------------------------------------------------

    def store(
        self,
        content: str,
        doc_type: str = "general",
        source: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """Store a knowledge chunk.

        :param content:  Text content to store (max ~2 000 tokens recommended).
        :param doc_type: Category (``"edi_spec"``, ``"sop"``, ``"issue_resolution"``, …).
        :param source:   Origin (file path, partner name, etc.).
        :param tags:     Optional list of searchable tags.
        :param metadata: Optional extra metadata dict.
        :returns: True on success.
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
        sb = _get_supabase_client()
        if sb and embedding:
            try:
                sb.table(TABLE).insert(row).execute()
                return True
            except Exception as exc:
                logger.error(f"corporate_memory.store failed: {exc}")
        # In-memory fallback (no persistence across restarts)
        self._chunks.append(row)
        return True

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        doc_type: Optional[str] = None,
    ) -> List[Dict]:
        """Retrieve the top-k most relevant knowledge chunks for a query.

        Uses vector similarity search when pgvector is available, otherwise
        falls back to substring matching.

        :returns: List of ``{"content": str, "doc_type": str, "similarity": float}`` dicts.
        """
        sb = _get_supabase_client()
        if sb:
            try:
                return self._vector_search(sb, query, top_k, doc_type)
            except Exception as exc:
                logger.warning(f"Vector search failed, falling back to FTS: {exc}")
        return self._fts_fallback(query, top_k, doc_type)

    def retrieve_for_partner(self, partner_name: str, top_k: int = 10) -> List[Dict]:
        """Retrieve all knowledge related to a specific trading partner."""
        sb = _get_supabase_client()
        if sb:
            try:
                result = (
                    sb.table(TABLE)
                    .select("content,doc_type,source,metadata,created_at")
                    .contains("tags", [partner_name])
                    .order("created_at", desc=True)
                    .limit(top_k)
                    .execute()
                )
                return result.data or []
            except Exception as exc:
                logger.warning(f"retrieve_for_partner failed: {exc}")
        return [
            r
            for r in self._chunks
            if partner_name.lower() in r.get("content", "").lower()
        ]

    # ------------------------------------------------------------------
    # Private helpers — fact store
    # ------------------------------------------------------------------

    def _find_by_topic(self, topic: str) -> Optional[Dict[str, Any]]:
        topic_lower = topic.lower()
        for fact in self._facts.values():
            if fact["topic"].lower() == topic_lower:
                return fact
        return None

    def _auto_tags(self, topic: str, content: str) -> List[str]:
        words = set(re.split(r"[\W_]+", (topic + " " + content).lower()))
        return [w for w in words if w and w not in STOPWORDS and len(w) > 2][:10]

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._memory_file.exists():
            try:
                data = json.loads(self._memory_file.read_text(encoding="utf-8"))
                return data.get("facts", {})
            except Exception as e:
                logger.warning(f"[corporate_memory] Could not load: {e}")
        return {}

    def _save(self) -> None:
        """Persist all in-memory facts.

        Primary: Supabase upsert per-fact into ``corporate_memory`` table.
        Failsafe: append to ``corporate_memory_failsafe.jsonl`` if Supabase fails.
        Always writes a local JSON cache for fast reads.
        """
        sb = _get_supabase_client()
        if sb:
            for mem_id, fact in self._facts.items():
                try:
                    sb.table(TABLE).upsert(
                        {
                            "key": mem_id,
                            "value": json.dumps(fact, default=str),
                            "category": fact.get("category", "general"),
                            "metadata": {
                                "topic": fact.get("topic", ""),
                                "confidence": fact.get("confidence", 0.5),
                                "source": fact.get("source", "unknown"),
                            },
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        on_conflict="key",
                    ).execute()
                except Exception as exc:
                    logger.warning(
                        f"[corporate_memory] Supabase upsert failed for {mem_id}: {exc}"
                    )
                    _write_failsafe(
                        self._store_dir,
                        key=mem_id,
                        value=json.dumps(fact, default=str),
                        category=fact.get("category", "general"),
                        metadata={
                            "topic": fact.get("topic", ""),
                            "confidence": fact.get("confidence", 0.5),
                            "source": fact.get("source", "unknown"),
                        },
                    )
        try:
            data = {
                "version": 1,
                "last_updated": datetime.now().isoformat(),
                "fact_count": len(self._facts),
                "facts": self._facts,
            }
            self._memory_file.write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"[corporate_memory] Could not write local cache: {e}")

    def _audit(self, action: str, memory_id: str, details: str) -> None:
        try:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "memory_id": memory_id,
                "details": details,
            }
            with self._audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def _load_consolidation_marker(self) -> Dict[str, Any]:
        marker_file = self._store_dir / "last_consolidation.json"
        if marker_file.exists():
            try:
                return json.loads(marker_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_consolidation_marker(self, last_timestamp: str) -> None:
        marker_file = self._store_dir / "last_consolidation.json"
        try:
            marker_file.write_text(
                json.dumps(
                    {
                        "last_timestamp": last_timestamp,
                        "consolidated_at": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Private helpers — document-chunk store
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> Optional[List[float]]:
        """Generate text embedding.  Returns None if no embedding API configured."""
        api_key = config.OPENAI_API_KEY
        if not api_key:
            return None
        try:
            import openai
            client = openai.OpenAI(api_key=api_key)
            resp = client.embeddings.create(
                model="text-embedding-3-small",
                input=text[:8000],
            )
            return resp.data[0].embedding
        except Exception as exc:
            logger.debug(f"Embedding generation failed: {exc}")
            return None

    def _vector_search(
        self, sb, query: str, top_k: int, doc_type: Optional[str]
    ) -> List[Dict]:
        """Supabase pgvector similarity search via RPC."""
        query_embedding = self._embed(query)
        if not query_embedding:
            return self._fts_fallback(query, top_k, doc_type)
        rpc_params: Dict[str, Any] = {
            "query_embedding": query_embedding,
            "match_count": top_k,
        }
        if doc_type:
            rpc_params["filter_doc_type"] = doc_type
        result = sb.rpc("match_corporate_memory", rpc_params).execute()
        return result.data or []

    def _fts_fallback(
        self, query: str, top_k: int, doc_type: Optional[str]
    ) -> List[Dict]:
        """Simple substring matching over in-memory chunk store."""
        q = query.lower()
        matches = [
            r
            for r in self._chunks
            if q in r.get("content", "").lower()
            and (doc_type is None or r.get("doc_type") == doc_type)
        ]
        return matches[:top_k]
