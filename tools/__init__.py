"""
FirstPass EDI — Tools Package
===============================
Exports all tool classes used by the agent pipeline.

Available tools
───────────────
  EDIParser         — Parse raw X12 856 strings into structured dataclasses
  ParsedEDI856      — The top-level 856 document dataclass
  VectorStore       — ChromaDB wrapper for partner specs and failure history
  ShipStationClient — Shipment lookup (mock in demo mode)
  ShipmentInfo      — Shipment details dataclass
  OrderfulClient    — EDI network submission (mock in demo mode)
  SubmissionResult  — Submission outcome dataclass
"""

from .edi_parser import EDIParser, ParsedEDI856, SAMPLE_856_RETAILER_A, SAMPLE_856_RETAILER_A_WITH_ERRORS
from .orderful_client import OrderfulClient, SubmissionResult, StructureValidationResult
from .shipstation_client import ShipStationClient, ShipmentInfo
from .vector_store import VectorStore, SpecRecord, SearchResult

__all__ = [
    "EDIParser",
    "ParsedEDI856",
    "SAMPLE_856_RETAILER_A",
    "SAMPLE_856_RETAILER_A_WITH_ERRORS",
    "VectorStore",
    "SpecRecord",
    "SearchResult",
    "ShipStationClient",
    "ShipmentInfo",
    "OrderfulClient",
    "SubmissionResult",
    "StructureValidationResult",
]
