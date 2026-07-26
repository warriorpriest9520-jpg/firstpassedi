"""FirstPass EDI memory — knowledge store, issue tracking, and database layer.

Modules:
    corporate_memory — RAG-powered knowledge store with confidence scoring
                       (CorporateMemory)
    issue_tracker    — Multi-step issue and incident lifecycle management
                       (IssueTracker / IssueManager)
    supabase_client  — Supabase singleton client plus convenience helpers
"""

from firstpass.memory.corporate_memory import CorporateMemory
from firstpass.memory.issue_tracker import IssueManager, IssueTracker
from firstpass.memory.supabase_client import get_client

__all__ = [
    "CorporateMemory",
    "IssueTracker",
    "IssueManager",
    "get_client",
]
