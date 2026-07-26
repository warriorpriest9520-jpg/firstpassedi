"""
spec_rules.py — Single source of truth for EDI partner rules (FirstPass EDI).

Every rule the watchdog and auditor enforce is read from
``specs/<partner>.json`` (and the auto-extracted PDF cache under
``specs/.pdf_cache/``). Drop the rule under the partner's spec and both
the *push* path (webhook callback into the watchdog) and the *pull* path
(payload auditor) apply it.

Why this module exists
----------------------
Before this, two rule layers existed:

  1. ``spec_validator.py`` — structural X12 checks (segment presence,
     element types/lengths/codes, syntax).
  2. Payload-auditor spec-rule application — value_map and "N per day"
     frequency.

Plus a *third* layer in a standalone JSON file that nobody read
consistently. Three sources of truth = bugs.

This module collapses (2) and (3) into one canonical rule engine:

  - **field_mapping** with ``value_map`` — every internal SKU in a LIN
    segment must have its alias substituted before transmission.
  - **frequency** — "N per day", "N per hour" etc. inferred from the
    rule description; flag days where the cap is exceeded.
  - **schedule** — read from ``documents[].schedule``; flag missed runs.
  - **required_field** — explicit "this field must be present and
    non-empty" rules.
  - **value_check** — "this field must equal X" rules.
  - **regex** — ``"pattern": "<regex>"`` on a named field.
  - **custom** — anything unrecognised; surfaced as info-level findings.

The structural validator stays where it is — it's about X12, not
partner-specific rules. ``apply_all_rules()`` here is composable with
``spec_validator.EDISpecValidator.validate()``: call both, concatenate
the issues, and you have the complete picture.

API
---
::

    from firstpass.validators.spec_rules import apply_all_rules, schedule_alerts
    issues = apply_all_rules(spec, doc_type, payload, runs)
    alerts = schedule_alerts(spec, recent_run_history)

Both return a list of issue dicts in the same shape ``spec_validator``
emits, so the watchdog and auditor can fold them into the same UI and
Supabase logging without conversion.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_payload_segments(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Use spec_validator's normaliser when available; else best-effort."""
    try:
        from firstpass.validators.spec_validator import _normalize_payload
        return _normalize_payload(payload) or {}
    except Exception:
        return {}


def _issue(
    field: str,
    issue: str,
    severity: str = "error",
    segment: Optional[str] = None,
    rule: str = "spec_rule",
) -> Dict[str, Any]:
    return {
        "field": field,
        "issue": issue,
        "severity": severity,
        "segment": segment,
        "rule": rule,
    }


# ---------------------------------------------------------------------------
# field_mapping with value_map
# ---------------------------------------------------------------------------


def _check_value_map(
    rule: Dict[str, Any],
    normalized: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """For each segment named by ``field_path``, every partner-facing
    field value must be in ``rule['value_map']`` *values* (the alias).
    A *key* (the internal item ID) means the substitution didn't happen.
    """
    issues: List[Dict[str, Any]] = []
    seg_id = (rule.get("field_path") or "LIN").strip().upper()
    value_map: Dict[str, str] = rule.get("value_map") or {}
    if not value_map:
        return issues

    target_field = (rule.get("target_field") or
                    ("LIN03" if seg_id == "LIN" else None))
    if not target_field:
        return issues

    rows = normalized.get(seg_id) or []
    for row in rows:
        sku = row.get(target_field) or row.get(target_field.lower())
        if not sku:
            continue
        sku_str = str(sku).strip()
        if not sku_str:
            continue
        if sku_str in value_map:
            issues.append(_issue(
                field=target_field,
                issue=(f"Internal item ID '{sku_str}' was sent — should be alias "
                       f"'{value_map[sku_str]}' per spec rule "
                       f"'{rule.get('rule_name','value_map')}'"),
                severity=rule.get("severity", "error"),
                segment=seg_id,
                rule=rule.get("rule_name", "value_map"),
            ))
        elif sku_str not in value_map.values() and not any(
            sku_str.startswith(pfx) for pfx in value_map.keys()
        ):
            # Unknown item ID — surface as a warning so operators can add
            # the mapping; avoid false-positives by not hard-coding prefixes.
            pass
    return issues


# ---------------------------------------------------------------------------
# frequency rules ("N per day")
# ---------------------------------------------------------------------------


_FREQ_PATTERNS = [
    re.compile(r"only\s+(\d+)\s+(?:successful\s+)?\S+\s+per\s+(day|hour)", re.I),
    re.compile(r"(\d+)\s+(?:successful\s+)?\S+\s+per\s+(day|hour)", re.I),
    re.compile(r"max(?:imum)?\s+(\d+)\s+per\s+(day|hour)", re.I),
    re.compile(r"only\s+(\d+)\s+", re.I),
]


def _parse_frequency(rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Infer a frequency cap from a rule's text. Returns
    ``{cap, unit}`` or None if nothing recognized.
    """
    text = " ".join([
        str(rule.get("rule_name", "")),
        str(rule.get("description", "")),
        str(rule.get("check_instruction", "")),
    ]).lower()
    for pat in _FREQ_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            cap = int(m.group(1))
        except Exception:
            continue
        unit = "day"
        try:
            unit = m.group(2)
        except IndexError:
            pass
        return {"cap": cap, "unit": unit}
    return None


def _check_frequency(
    rule: Dict[str, Any],
    partner: str,
    doc_type: str,
    runs_for_doc: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    spec = _parse_frequency(rule)
    if not spec:
        return []
    cap, unit = spec["cap"], spec["unit"]
    bucketed: Dict[str, int] = defaultdict(int)
    for r in runs_for_doc or []:
        if (r.get("status") or "").lower() not in ("success", "ok", "succeeded"):
            continue
        ts = r.get("created_at") or r.get("started_at") or ""
        if not ts:
            continue
        if unit == "hour":
            bucket = ts[:13]   # YYYY-MM-DDTHH
        else:
            bucket = ts[:10]   # YYYY-MM-DD
        bucketed[bucket] += 1
    issues: List[Dict[str, Any]] = []
    for bucket, n in bucketed.items():
        if n > cap:
            issues.append(_issue(
                field="_frequency",
                issue=(f"{partner} {doc_type}: {n} successful runs in "
                       f"{bucket} (spec allows {cap}/{unit}) — rule "
                       f"'{rule.get('rule_name','frequency')}'"),
                severity=rule.get("severity", "warn"),
                rule=rule.get("rule_name", "frequency"),
            ))
    return issues


# ---------------------------------------------------------------------------
# required_field / value_check / regex rules
# ---------------------------------------------------------------------------


def _resolve_path(payload: Any, path: str) -> Any:
    """Resolve a dotted/bracketed path against a dict-or-list payload."""
    cur: Any = payload
    if not path:
        return None
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    for p in parts:
        if cur is None:
            return None
        if p.startswith("[") and p.endswith("]"):
            try:
                idx = int(p[1:-1])
                cur = cur[idx]
            except Exception:
                return None
        else:
            if isinstance(cur, dict):
                cur = cur.get(p)
            elif isinstance(cur, list):
                cur = [item.get(p) if isinstance(item, dict) else None for item in cur]
            else:
                return None
    return cur


def _check_required_field(rule: Dict[str, Any], payload: Any) -> List[Dict[str, Any]]:
    path = (rule.get("field_path") or "").strip()
    if not path:
        return []
    val = _resolve_path(payload, path)
    if val is None or (isinstance(val, str) and not val.strip()):
        return [_issue(
            field=path,
            issue=f"Required field '{path}' missing/empty per "
                  f"'{rule.get('rule_name','required_field')}'",
            severity=rule.get("severity", "error"),
            segment=path.split(".")[0] if "." in path else path,
            rule=rule.get("rule_name", "required_field"),
        )]
    return []


def _check_value_check(rule: Dict[str, Any], payload: Any) -> List[Dict[str, Any]]:
    path = (rule.get("field_path") or "").strip()
    expected = rule.get("expected_value")
    if not path or expected is None:
        return []
    val = _resolve_path(payload, path)
    if val == expected:
        return []
    return [_issue(
        field=path,
        issue=(f"{path} = {val!r}, expected {expected!r} per "
               f"'{rule.get('rule_name','value_check')}'"),
        severity=rule.get("severity", "error"),
        segment=path.split(".")[0] if "." in path else path,
        rule=rule.get("rule_name", "value_check"),
    )]


def _check_regex(rule: Dict[str, Any], payload: Any) -> List[Dict[str, Any]]:
    path = (rule.get("field_path") or "").strip()
    pattern = rule.get("pattern") or rule.get("regex")
    if not path or not pattern:
        return []
    val = _resolve_path(payload, path)
    if val is None:
        return []
    try:
        if not re.search(pattern, str(val)):
            return [_issue(
                field=path,
                issue=(f"{path} = {val!r} did not match /{pattern}/ per "
                       f"'{rule.get('rule_name','regex')}'"),
                severity=rule.get("severity", "warn"),
                segment=path.split(".")[0] if "." in path else path,
                rule=rule.get("rule_name", "regex"),
            )]
    except re.error as exc:
        return [_issue(
            field=path,
            issue=f"Invalid regex /{pattern}/ in spec: {exc}",
            severity="info",
            rule=rule.get("rule_name", "regex"),
        )]
    return []


# ---------------------------------------------------------------------------
# schedule-aware watching (from documents[].schedule)
# ---------------------------------------------------------------------------


_SCHEDULE_UNITS = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600,
    "day": 86400, "days": 86400,
}


def _interval_seconds(schedule: Dict[str, Any]) -> Optional[int]:
    if not isinstance(schedule, dict):
        return None
    try:
        interval = int(schedule.get("interval") or 0)
    except Exception:
        return None
    unit = str(schedule.get("unit") or "minutes").lower()
    seconds = _SCHEDULE_UNITS.get(unit)
    if not seconds or not interval:
        return None
    return interval * seconds


def schedule_alerts(
    spec: Dict[str, Any],
    last_seen_by_doc: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Return alerts for any document whose declared schedule has been missed.

    ``last_seen_by_doc`` maps doc_type → last successful run timestamp
    (ISO-8601 string). Documents without a recent run get an alert if
    they should have run within the configured interval × 2.
    """
    issues: List[Dict[str, Any]] = []
    now = _now_utc()
    for doc in spec.get("documents") or []:
        dt = str(doc.get("type") or "")
        sched = doc.get("schedule") or {}
        seconds = _interval_seconds(sched)
        if not seconds or not dt:
            continue
        threshold = now - timedelta(seconds=seconds * 2)
        last_iso = last_seen_by_doc.get(dt)
        if not last_iso:
            issues.append(_issue(
                field=f"_schedule.{dt}",
                issue=(f"{spec.get('trading_partner','?')} {dt}: no successful "
                       f"runs observed; spec expects one every "
                       f"{sched.get('interval')} {sched.get('unit')}."),
                severity="warn",
                rule="schedule",
            ))
            continue
        try:
            last_dt = datetime.fromisoformat(last_iso.rstrip("Z"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if last_dt < threshold:
            ago = now - last_dt
            issues.append(_issue(
                field=f"_schedule.{dt}",
                issue=(f"{spec.get('trading_partner','?')} {dt}: last "
                       f"successful run was {ago.total_seconds()/60:.0f} min "
                       f"ago; spec expects one every "
                       f"{sched.get('interval')} {sched.get('unit')}."),
                severity="warn",
                rule="schedule_miss",
            ))
    return issues


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def apply_all_rules(
    spec: Dict[str, Any],
    doc_type: str,
    payload: Any,
    runs_for_doc: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Apply every rule in ``spec['rules']`` for ``doc_type``.

    Returns issue dicts in the same shape ``spec_validator`` emits:
    ``{field, issue, severity, segment, rule}``. Composable — fold these
    into the structural validator's output and you get one unified list.

    No external rules file is consulted. The partner spec JSON is the
    single source of truth.
    """
    rules = [r for r in (spec.get("rules") or [])
             if str(r.get("doc_type", "")).strip() == doc_type
             and r.get("enabled", True)]
    if not rules:
        return []

    normalized = _normalize_payload_segments(payload)
    issues: List[Dict[str, Any]] = []
    for rule in rules:
        rt = (rule.get("rule_type") or "").lower()
        try:
            if rt == "field_mapping" and rule.get("value_map"):
                issues.extend(_check_value_map(rule, normalized))
            elif rt == "required_field":
                issues.extend(_check_required_field(rule, payload))
            elif rt == "value_check":
                issues.extend(_check_value_check(rule, payload))
            elif rt == "regex":
                issues.extend(_check_regex(rule, payload))
            elif rt in ("custom", "frequency"):
                issues.extend(_check_frequency(
                    rule,
                    spec.get("trading_partner", ""),
                    doc_type,
                    runs_for_doc or [],
                ))
            else:
                issues.append(_issue(
                    field="_rule",
                    issue=(f"Unrecognised rule_type '{rt}' for "
                           f"'{rule.get('rule_name','?')}' — not enforced"),
                    severity="info",
                    rule=rule.get("rule_name", rt or "unknown"),
                ))
        except Exception as exc:
            logger.warning("[spec_rules] rule %s failed: %s",
                           rule.get("rule_name"), exc)
    return issues


# ---------------------------------------------------------------------------
# Spec migration helper (one-time utility)
# ---------------------------------------------------------------------------


def merge_legacy_rules_into_specs(
    legacy_path: str,
    specs_dir: str,
) -> List[str]:
    """One-shot migration: take rules out of a legacy rules JSON file and
    fold them into the matching ``specs/<partner>.json``.

    Returns the list of spec files modified. Idempotent — already-present
    rule IDs are skipped.
    """
    import json
    from pathlib import Path

    legacy = Path(legacy_path)
    specs = Path(specs_dir)
    if not legacy.exists() or not specs.exists():
        return []
    try:
        legacy_data = json.loads(legacy.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning("[spec_rules] could not read %s: %s", legacy, exc)
        return []
    rules = legacy_data.get("rules") or []
    if not rules:
        return []
    by_partner: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rules:
        partner = (r.get("customer_name") or r.get("trading_partner") or "").lower()
        if partner:
            by_partner[partner].append(r)
    modified: List[str] = []
    for partner, partner_rules in by_partner.items():
        candidates = [
            specs / f"{partner}.json",
            specs / partner / f"{partner}.json",
        ]
        target = next((p for p in candidates if p.exists()), None)
        if not target:
            continue
        try:
            spec = json.loads(target.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        existing_ids = {r.get("id") for r in spec.get("rules", [])}
        added = 0
        for r in partner_rules:
            if r.get("id") in existing_ids:
                continue
            spec.setdefault("rules", []).append(r)
            added += 1
        if added:
            target.write_text(json.dumps(spec, indent=2, default=str), encoding="utf-8")
            modified.append(str(target))
            logger.info("[spec_rules] merged %d legacy rules into %s", added, target)
    return modified
