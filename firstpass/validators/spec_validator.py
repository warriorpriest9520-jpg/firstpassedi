"""
spec_validator.py — Deep EDI payload validation against partner specs.

Part of the FirstPass EDI validation pipeline. Handles:
  - JSON-mapped EDI (Orderful / automation-platform format)
  - Raw X12 EDI string
  - Segment presence checks
  - Conditional/syntax rule evaluation
  - Element type/length/code validation
  - Silent failure detection (empty required fields, wrong formats)

Spec files are loaded from the configured specs directory
(``firstpass.config.SPECS_DIR``, defaulting to ``<package_root>/specs/``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from firstpass.config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Specs directory (configurable via config or env)
# ---------------------------------------------------------------------------

# Honour FIRSTPASS_SPECS_DIR env var if set, otherwise default to <project_root>/specs
_SPECS_DIR = Path(getattr(config, "SPECS_DIR", None) or
                  Path(__file__).parent.parent.parent / "specs")


# ---------------------------------------------------------------------------
# X12 element type validators
# ---------------------------------------------------------------------------

def _validate_type(value: Any, elem_type: str, min_len: int, max_len: int) -> Optional[str]:
    """Return None if valid, or an error string."""
    if value is None or str(value).strip() == "":
        return "empty value"
    s = str(value).strip()
    if elem_type == "DT":
        if not re.match(r"^\d{8}$", s):
            return f"DT must be YYYYMMDD (8 digits), got '{s}'"
    elif elem_type == "TM":
        if not re.match(r"^\d{4,8}$", s):
            return f"TM must be HHMM[SS[hh]] (4-8 digits), got '{s}'"
    elif elem_type in ("N0", "N1", "N2"):
        if not re.match(r"^-?\d+(\.\d+)?$", s):
            return f"{elem_type} must be numeric, got '{s}'"
    elif elem_type == "R":
        try:
            float(s)
        except ValueError:
            return f"R must be decimal number, got '{s}'"
    elif elem_type == "ID":
        pass  # code validation done separately
    # Length checks
    if len(s) < min_len:
        return f"too short (min {min_len}, got {len(s)})"
    if max_len and len(s) > max_len:
        return f"too long (max {max_len}, got {len(s)})"
    return None


# ---------------------------------------------------------------------------
# X12 segment element definitions
# Based on X12 005010 standard + common EDI docs
# ---------------------------------------------------------------------------

SEGMENT_ELEMENTS: Dict[str, List[Dict]] = {
    "ST": [
        {"id": "ST01", "name": "Transaction Set Identifier Code", "req": "M", "type": "ID", "min": 3, "max": 3,
         "codes": {"856": "856", "850": "850", "810": "810", "846": "846", "997": "997"}},
        {"id": "ST02", "name": "Transaction Set Control Number", "req": "M", "type": "AN", "min": 4, "max": 9},
    ],
    "BSN": [
        {"id": "BSN01", "name": "Transaction Set Purpose Code", "req": "M", "type": "ID", "min": 2, "max": 2,
         "codes": ["00", "05"]},
        {"id": "BSN02", "name": "Shipment Identification", "req": "M", "type": "AN", "min": 2, "max": 30},
        {"id": "BSN03", "name": "Date", "req": "M", "type": "DT", "min": 8, "max": 8},
        {"id": "BSN04", "name": "Time", "req": "M", "type": "TM", "min": 4, "max": 8},
    ],
    "HL": [
        {"id": "HL01", "name": "Hierarchical ID Number", "req": "M", "type": "AN", "min": 1, "max": 12},
        {"id": "HL03", "name": "Hierarchical Level Code", "req": "M", "type": "ID", "min": 1, "max": 2,
         "codes": ["S", "O", "T", "P", "I"]},
    ],
    "TD1": [
        {"id": "TD101", "name": "Packaging Code", "req": "M", "type": "ID", "min": 3, "max": 5,
         "codes": ["CTN", "PLT"]},
        {"id": "TD102", "name": "Lading Quantity", "req": "X", "type": "N0", "min": 1, "max": 7},
    ],
    "TD5": [
        {"id": "TD502", "name": "Identification Code Qualifier", "req": "M", "type": "ID", "min": 1, "max": 2,
         "codes": ["2"]},
        {"id": "TD503", "name": "Identification Code (SCAC)", "req": "X", "type": "AN", "min": 2, "max": 80},
    ],
    "DTM": [
        {"id": "DTM01", "name": "Date/Time Qualifier", "req": "M", "type": "ID", "min": 3, "max": 3,
         "codes": ["011", "017", "036", "094"]},
        {"id": "DTM02", "name": "Date", "req": "X", "type": "DT", "min": 8, "max": 8},
    ],
    "N1": [
        {"id": "N101", "name": "Entity Identifier Code", "req": "M", "type": "ID", "min": 2, "max": 3,
         "codes": ["SF", "ST", "BY", "SE", "VN"]},
        {"id": "N102", "name": "Name", "req": "X", "type": "AN", "min": 1, "max": 60},
    ],
    "N3": [
        {"id": "N301", "name": "Address Information", "req": "M", "type": "AN", "min": 1, "max": 55},
    ],
    "N4": [
        {"id": "N401", "name": "City Name", "req": "M", "type": "AN", "min": 2, "max": 30},
        {"id": "N403", "name": "Postal Code", "req": "M", "type": "ID", "min": 3, "max": 15},
    ],
    "PRF": [
        {"id": "PRF01", "name": "Purchase Order Number", "req": "M", "type": "AN", "min": 1, "max": 22},
    ],
    "LIN": [
        {"id": "LIN01", "name": "Assigned Identification", "req": "M", "type": "AN", "min": 1, "max": 20},
        {"id": "LIN02", "name": "Product/Service ID Qualifier", "req": "M", "type": "ID", "min": 2, "max": 2,
         "codes": ["BP", "EN", "UP", "VN", "UA", "UK", "IB"]},
        {"id": "LIN03", "name": "Product/Service ID", "req": "M", "type": "AN", "min": 1, "max": 48},
    ],
    "SN1": [
        {"id": "SN102", "name": "Number of Units Shipped", "req": "M", "type": "R", "min": 1, "max": 10},
        {"id": "SN103", "name": "Unit of Measure Code", "req": "M", "type": "ID", "min": 2, "max": 2,
         "codes": ["CA", "EA"]},
    ],
    "CTT": [
        {"id": "CTT01", "name": "Number of Line Items", "req": "M", "type": "N0", "min": 1, "max": 6},
    ],
    "SE": [
        {"id": "SE01", "name": "Number of Included Segments", "req": "M", "type": "N0", "min": 1, "max": 10},
        {"id": "SE02", "name": "Transaction Set Control Number", "req": "M", "type": "AN", "min": 4, "max": 9},
    ],
    "BEG": [
        {"id": "BEG01", "name": "Transaction Set Purpose Code", "req": "M", "type": "ID", "min": 2, "max": 2},
        {"id": "BEG02", "name": "Purchase Order Type Code", "req": "M", "type": "ID", "min": 2, "max": 2},
        {"id": "BEG03", "name": "Purchase Order Number", "req": "M", "type": "AN", "min": 1, "max": 22},
        {"id": "BEG05", "name": "Date", "req": "M", "type": "DT", "min": 8, "max": 8},
    ],
    "REF": [
        {"id": "REF01", "name": "Reference Identification Qualifier", "req": "M", "type": "ID", "min": 2, "max": 3,
         "codes": ["CN", "DP", "PO", "SI"]},
        {"id": "REF02", "name": "Reference Identification", "req": "X", "type": "AN", "min": 1, "max": 50},
    ],
    "MAN": [
        {"id": "MAN01", "name": "Marks and Numbers Qualifier", "req": "M", "type": "ID", "min": 1, "max": 2,
         "codes": ["GM", "AA"]},
        {"id": "MAN02", "name": "Marks and Numbers (SSCC)", "req": "M", "type": "AN", "min": 1, "max": 48},
    ],
}

# Syntax rules expressed as lambda checks
# (segment_id, rule_fn, description)
SYNTAX_RULES: Dict[str, List[Tuple]] = {
    "TD1": [
        (lambda seg: not (seg.get("TD101") and not seg.get("TD102")),
         "If TD101 is present, TD102 is required"),
        (lambda seg: not (seg.get("TD106") and not seg.get("TD107")),
         "If TD106 is present, TD107 is required"),
    ],
    "TD5": [
        (lambda seg: any(seg.get(f) for f in ["TD502", "TD504", "TD505", "TD506", "TD512"]),
         "At least one of TD502, TD504, TD505, TD506, TD512 is required"),
        (lambda seg: not (seg.get("TD502") and not seg.get("TD503")),
         "If TD502 is present, TD503 is required"),
    ],
    "DTM": [
        (lambda seg: any(seg.get(f) for f in ["DTM02", "DTM03", "DTM05"]),
         "At least one of DTM02, DTM03, DTM05 is required"),
        (lambda seg: not (seg.get("DTM04") and not seg.get("DTM03")),
         "If DTM04 is present, DTM03 is required"),
    ],
    "N1": [
        (lambda seg: seg.get("N102") or seg.get("N103"),
         "At least one of N102 or N103 is required"),
        (lambda seg: not ((seg.get("N103") or seg.get("N104")) and not (seg.get("N103") and seg.get("N104"))),
         "If N103 or N104 present, both are required"),
    ],
    "LIN": [
        (lambda seg: not ((seg.get("LIN04") or seg.get("LIN05")) and not (seg.get("LIN04") and seg.get("LIN05"))),
         "If LIN04 or LIN05 present, both are required"),
    ],
}


# ---------------------------------------------------------------------------
# Payload parsing (X12 raw string or JSON dict)
# ---------------------------------------------------------------------------

def _parse_x12(raw: str) -> Dict[str, List[Dict]]:
    """Parse raw X12 EDI string into { segment_id: [{ elem_id: value, ... }] }."""
    segments: Dict[str, List[Dict]] = {}
    # Detect delimiter from ISA if present
    elem_sep = "*"
    seg_sep = "~"
    if raw.startswith("ISA"):
        elem_sep = raw[3]
        seg_sep = raw[105] if len(raw) > 105 else "~"

    for seg_str in re.split(r"[~\n]", raw):
        seg_str = seg_str.strip()
        if not seg_str:
            continue
        parts = seg_str.split(elem_sep)
        seg_id = parts[0].strip()
        if not seg_id or len(seg_id) > 4:
            continue
        elem_defs = SEGMENT_ELEMENTS.get(seg_id, [])
        seg_dict: Dict[str, Any] = {}
        for i, part in enumerate(parts[1:], 1):
            elem_id = f"{seg_id}{i:02d}" if i < 100 else f"{seg_id}{i}"
            for elem_def in elem_defs:
                if elem_def["id"] == elem_id:
                    seg_dict[elem_id] = part.strip() if part.strip() else None
                    break
            else:
                seg_dict[elem_id] = part.strip() if part.strip() else None
        segments.setdefault(seg_id, []).append(seg_dict)
    return segments


def _normalize_payload(payload: Any) -> Dict[str, List[Dict]]:
    """
    Normalize any payload format to { segment_id: [{ elem_id: value }] }.
    Accepts: raw X12 string, Orderful JSON, flat dict, nested dict.
    """
    if isinstance(payload, str):
        payload = payload.strip()
        if payload.startswith("{") or payload.startswith("["):
            try:
                payload = json.loads(payload)
            except Exception:
                return _parse_x12(payload)
        elif "*" in payload or "~" in payload or payload.startswith("ISA"):
            return _parse_x12(payload)

    if not isinstance(payload, dict):
        return {}

    # Check if it's already segment-keyed: { "BSN": {...}, "HL": [...] }
    known_segs = set(SEGMENT_ELEMENTS.keys())
    if any(k in known_segs for k in payload.keys()):
        normalized = {}
        for k, v in payload.items():
            if k in known_segs:
                normalized[k] = v if isinstance(v, list) else [v] if isinstance(v, dict) else []
        return normalized

    # Orderful/flat JSON format: keys like "BSN01", "TD101", "N101", "HL01"
    segments: Dict[str, List[Dict]] = {}
    known_segs = set(SEGMENT_ELEMENTS.keys())

    def _extract_seg_id(key: str) -> Optional[str]:
        """Extract segment ID from element key, preferring known segment IDs."""
        for length in (4, 3, 2):
            prefix = key[:length]
            if prefix in known_segs and re.match(r'^\d', key[length:]):
                return prefix
        m = re.match(r'^([A-Za-z]+)', key)
        return m.group(1).upper() if m else None

    for key, val in payload.items():
        if not re.search(r'\d', key):
            continue
        seg_id = _extract_seg_id(key)
        if seg_id:
            if seg_id not in segments:
                segments[seg_id] = [{}]
            segments[seg_id][0][key] = val
    return segments


# ---------------------------------------------------------------------------
# Main validator
# ---------------------------------------------------------------------------

class EDISpecValidator:
    """Validate an EDI payload against a partner spec (FirstPass EDI)."""

    def __init__(self, spec: Dict):
        self.spec = spec
        self._doc_index = {d["type"]: d for d in spec.get("documents", [])}
        self._rule_index: Dict[str, List[Dict]] = {}
        for rule in spec.get("rules", []):
            dt = rule.get("doc_type", "")
            self._rule_index.setdefault(dt, []).append(rule)

    def validate(self, doc_type: str, payload: Any) -> Dict:
        """
        Full validation. Returns:
          { ok, score, issues: [{field, issue, severity, segment, rule}], summary }
        """
        issues: List[Dict] = []
        doc_type = str(doc_type).strip()
        doc_spec = self._doc_index.get(doc_type)

        if not doc_spec:
            return {
                "ok": True,
                "score": 100,
                "issues": [{"field": "_doc", "issue": f"No spec for doc_type={doc_type} — skipping", "severity": "warn"}],
                "summary": "No spec found",
            }

        # Parse payload
        try:
            segments = _normalize_payload(payload)
        except Exception as exc:
            return {
                "ok": False,
                "score": 0,
                "issues": [{"field": "_parse", "issue": f"Could not parse payload: {exc}", "severity": "error"}],
                "summary": "Payload parse failure",
            }

        if not segments:
            issues.append({"field": "_payload", "issue": "Payload is empty or unrecognized format", "severity": "error", "segment": None})

        # 1 — Required segment presence
        for seg_id in doc_spec.get("required_segments", []):
            if seg_id not in segments:
                issues.append({
                    "field": seg_id,
                    "issue": f"Required segment '{seg_id}' is missing from payload",
                    "severity": "error",
                    "segment": seg_id,
                    "rule": "segment_presence",
                })

        # 2 — Element-level validation for segments that ARE present
        for seg_id, seg_list in segments.items():
            elem_defs = SEGMENT_ELEMENTS.get(seg_id, [])
            if not elem_defs:
                continue
            for seg_idx, seg_data in enumerate(seg_list):
                for elem_def in elem_defs:
                    elem_id  = elem_def["id"]
                    req      = elem_def.get("req", "O")
                    val      = seg_data.get(elem_id)
                    is_empty = val is None or str(val).strip() == ""

                    # Required element missing
                    if req == "M" and is_empty:
                        issues.append({
                            "field":    elem_id,
                            "issue":    f"{elem_id} ({elem_def['name']}) is required but missing/empty",
                            "severity": "error",
                            "segment":  seg_id,
                            "rule":     "required_element",
                        })
                        continue

                    if is_empty:
                        continue  # Optional empty fields are fine

                    # Type/length validation
                    err = _validate_type(val, elem_def.get("type", "AN"),
                                         elem_def.get("min", 1), elem_def.get("max", 0))
                    if err:
                        issues.append({
                            "field":    elem_id,
                            "issue":    f"{elem_id} ({elem_def['name']}): {err}",
                            "severity": "error",
                            "segment":  seg_id,
                            "rule":     "type_validation",
                        })

                    # Code value validation
                    if "codes" in elem_def and elem_def["codes"]:
                        codes = elem_def["codes"]
                        if isinstance(codes, list) and str(val) not in codes:
                            issues.append({
                                "field":    elem_id,
                                "issue":    f"{elem_id}='{val}' not in allowed codes: {codes}",
                                "severity": "warn",
                                "segment":  seg_id,
                                "rule":     "code_validation",
                            })

        # 3 — Syntax rule evaluation (conditional requirements)
        for seg_id, rules in SYNTAX_RULES.items():
            if seg_id not in segments:
                continue
            for seg_data in segments[seg_id]:
                for rule_fn, rule_desc in rules:
                    try:
                        if not rule_fn(seg_data):
                            issues.append({
                                "field":    seg_id,
                                "issue":    f"Syntax rule violated: {rule_desc}",
                                "severity": "error",
                                "segment":  seg_id,
                                "rule":     "syntax_rule",
                            })
                    except Exception:
                        pass

        # 4 — Spec-defined rules (from parsed spec JSON)
        for rule in self._rule_index.get(doc_type, []):
            desc     = rule.get("description", "")
            seg_id   = (rule.get("field_path") or "").strip()
            severity = rule.get("severity", "warn")
            if seg_id and seg_id not in segments and "required" in desc.lower():
                issues.append({
                    "field":    seg_id,
                    "issue":    f"Spec rule: {desc}",
                    "severity": severity,
                    "segment":  seg_id,
                    "rule":     rule.get("rule_name", "spec_rule"),
                })

        # 5 — Silent failure detection: segments present but all values empty
        for seg_id, seg_list in segments.items():
            for seg_data in seg_list:
                all_empty = all(v is None or str(v).strip() == "" for v in seg_data.values())
                if all_empty and seg_data:
                    issues.append({
                        "field":    seg_id,
                        "issue":    f"Segment '{seg_id}' present but all elements are empty (silent failure)",
                        "severity": "warn",
                        "segment":  seg_id,
                        "rule":     "silent_failure",
                    })

        errors = [i for i in issues if i["severity"] == "error"]
        warns  = [i for i in issues if i["severity"] == "warn"]
        score  = max(0, 100 - (len(errors) * 15) - (len(warns) * 3))

        return {
            "ok":             len(errors) == 0,
            "score":          score,
            "errors":         len(errors),
            "warnings":       len(warns),
            "issues":         issues,
            "segments_found": list(segments.keys()),
            "required_segments": doc_spec.get("required_segments", []),
            "summary":        (
                f"Valid ({score}/100)" if not errors
                else f"{len(errors)} error(s), {len(warns)} warning(s) — score {score}/100"
            ),
        }


# ---------------------------------------------------------------------------
# Convenience helpers (used by watchdog / orchestrator)
# ---------------------------------------------------------------------------

_validators: Dict[str, "EDISpecValidator"] = {}


def load_validator(partner: str, doc_type: str) -> Optional[EDISpecValidator]:
    """Load or cache a validator for partner+doc_type.

    Spec files are resolved relative to ``_SPECS_DIR`` (configurable via
    the ``FIRSTPASS_SPECS_DIR`` environment variable).
    """
    key = f"{partner}_{doc_type}".lower()
    if key in _validators:
        return _validators[key]

    candidates = [
        _SPECS_DIR / partner.lower() / f"{partner.lower()}_{doc_type}.json",
        _SPECS_DIR / f"{partner.lower()}_{doc_type}.json",
        _SPECS_DIR / f"{partner.lower()}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    spec = json.load(f)
                v = EDISpecValidator(spec)
                _validators[key] = v
                return v
            except Exception as exc:
                logger.warning("Failed to load spec %s: %s", path, exc)
    return None


def validate(partner: str, doc_type: str, payload: Any) -> Dict:
    """Top-level validate call used by the watchdog / orchestrator."""
    v = load_validator(partner, doc_type)
    if not v:
        return {
            "ok": True,
            "score": 100,
            "issues": [{"field": "_spec", "issue": f"No spec for {partner}/{doc_type}", "severity": "info"}],
            "summary": "No spec — skipped",
        }
    return v.validate(doc_type, payload)
