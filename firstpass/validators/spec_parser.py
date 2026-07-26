"""
spec_parser.py — LLM-powered EDI spec → workflow pipeline (FirstPass EDI).

Given a raw EDI spec (text, PDF, or JSON), this module:
  1. Parses the spec into a structured FieldMap (segments, elements,
     qualifiers, requirements, max-use, data-type, length, notes).
  2. Generates a platform-compatible JavaScript transform script that maps
     Orderful API response fields → ERP fields.
  3. Generates EDIValidator rule dicts consumable by spec_validator.py.
  4. Optionally calls a workflow-schema builder to produce a
     ready-to-import workflow JSON export.

CLI usage::

    python -m firstpass.validators.spec_parser \\
        --spec partner_850_spec.txt --doc-type 850 \\
        --partner acme_retail --direction inbound \\
        --out-dir /tmp/firstpass_output

Programmatic usage::

    from firstpass.validators.spec_parser import EDISpecParser
    parser = EDISpecParser()
    result = parser.parse_spec(spec_text, doc_type="850",
                               partner="acme_retail", direction="inbound")
    rules = result.validator_rules   # pass to EDIValidator
    js    = result.js_transform_script
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from firstpass.config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client — reuses OPENAI_API_KEY from firstpass.config
# ---------------------------------------------------------------------------

try:
    from openai import OpenAI as _OpenAI
    _openai_client = _OpenAI(api_key=config.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", ""))
    _OPENAI_AVAILABLE = True
except Exception:
    _openai_client = None  # type: ignore
    _OPENAI_AVAILABLE = False

# Model: env override → config default → fallback
MODEL = os.getenv("EDI_PARSER_MODEL", getattr(config, "DEFAULT_MODEL", "gpt-4o"))

# Target ERP system label and API endpoint (configurable via env)
_TARGET_SYSTEM = os.getenv("FIRSTPASS_ERP_SYSTEM", "ERP / Order Management System")
_TARGET_API_ENDPOINT = os.getenv("FIRSTPASS_ERP_API_ENDPOINT", "${ERP_BASE_URL}/api/orders")

# Default output directory for generated artefacts
_DEFAULT_OUT_DIR = Path(os.getenv("FIRSTPASS_PARSER_OUT_DIR", "output/spec_parser"))

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SegmentField:
    """A single EDI segment element (e.g. BEG03 = PO Number)."""
    segment: str          # e.g. "BEG"
    element_pos: int      # e.g. 3
    element_id: str       # e.g. "BEG03"
    name: str             # e.g. "Purchase Order Number"
    requirement: str      # M / O / C (mandatory/optional/conditional)
    data_type: str        # AN / DT / TM / ID / N0 / N2 / R etc.
    min_length: int
    max_length: int
    qualifier_code: Optional[str] = None   # for qualifier-controlled elements
    valid_values: List[str] = field(default_factory=list)
    notes: str = ""
    orderful_path: str = ""  # JSONPath in Orderful response, if known
    erp_field: str = ""      # Target ERP field name, if known


@dataclass
class EDIFieldMap:
    """Structured representation of an EDI partner spec."""
    partner: str
    doc_type: str           # "850", "856", "810", etc.
    direction: str          # "inbound" | "outbound"
    description: str
    segments: List[SegmentField] = field(default_factory=list)
    loop_structure: List[Dict[str, Any]] = field(default_factory=list)
    qualifiers: Dict[str, List[str]] = field(default_factory=dict)
    max_line_items: int = 999999
    notes: str = ""


@dataclass
class ParseResult:
    """Output of EDISpecParser.parse_spec()."""
    field_map: EDIFieldMap
    js_transform_script: str
    validator_rules: List[Dict[str, Any]]
    workflow_export_json: str     # ready to import into the automation platform
    template_key: str             # e.g. "acme_retail_850_inbound"
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_FIELD_MAP_PROMPT = """\
You are an EDI integration specialist. Parse the following {partner} {doc_type} ({direction}) \
EDI specification and return a JSON object describing the transaction set.

Return ONLY valid JSON — no markdown, no explanation.

Required JSON schema:
{{
  "partner": "{partner}",
  "doc_type": "{doc_type}",
  "direction": "{direction}",
  "description": "short description",
  "segments": [
    {{
      "segment": "XX",
      "element_pos": 1,
      "element_id": "XX01",
      "name": "Field Name",
      "requirement": "M|O|C",
      "data_type": "AN|DT|TM|ID|N0|N2|R",
      "min_length": 1,
      "max_length": 35,
      "qualifier_code": null,
      "valid_values": [],
      "notes": "",
      "orderful_path": "best-guess JSONPath in Orderful response e.g. $.purchase_order.po_number",
      "erp_field": "best-guess ERP field name e.g. SalesOrder.CustomerPONo"
    }}
  ],
  "loop_structure": [
    {{"loop_id": "2000", "name": "Order Level", "max_use": 1, "segments": ["BEG","REF","DTM","N1","PO1"]}}
  ],
  "qualifiers": {{
    "BEG01": ["00=Original", "05=Replace", "06=Cancel"],
    "DTM01": ["002=Delivery", "010=Requested Ship", "038=Ship Not Before"]
  }},
  "max_line_items": 9999,
  "notes": "any important partner-specific rules"
}}

EDI SPEC TEXT:
{spec_text}
"""

_JS_TRANSFORM_PROMPT = """\
You are a workflow automation JavaScript developer. Generate a script step function that \
transforms the Orderful API response for a {partner} {doc_type} {direction} transaction \
into the format expected by the {target_system} API.

The Orderful response is available in `input.orderful_response` (already parsed JSON).
The function must return the transformed object.

Rules:
- Use the Tray.io script pattern: exports.step = function(input, fileInput) {{ return {{...}}; }};
- Map every MANDATORY field. Skip optional fields only if they're truly not applicable.
- Add inline comments explaining each mapping.
- For dates: Orderful uses ISO 8601 (YYYY-MM-DD). Convert to EDI format (YYYYMMDD) or target \
  format as needed.
- For quantities and amounts: Orderful returns strings; parse to numbers where needed.
- If a field has no direct mapping, set it to null and add a TODO comment.
- The return value should be ready to POST to {target_api_endpoint}.

Field mappings (segment → Orderful path → {target_system} field):
{field_mappings_json}

Generate ONLY the JavaScript function body (the full exports.step = function... block).
"""

_VALIDATOR_RULES_PROMPT = """\
You are an EDI compliance engineer. Based on the following {partner} {doc_type} field map, \
generate a list of EDI validator rules for the EDIValidator class used by FirstPass EDI.

Return ONLY valid JSON — an array of rule objects with this schema:
[
  {{
    "rule_id": "unique-slug",
    "partner": "{partner}",
    "doc_type": "{doc_type}",
    "severity": "error|warning",
    "segment": "XX",
    "element_id": "XX01",
    "check": "required|max_length|min_length|valid_values|pattern|numeric|date_format",
    "params": {{}},
    "message": "Human-readable failure message"
  }}
]

Include rules for:
1. All MANDATORY fields — check=required
2. Max/min length constraints — check=max_length / check=min_length
3. Valid qualifier values — check=valid_values with params={{"values": [...]}}
4. Date fields — check=date_format with params={{"format": "YYYYMMDD"}}
5. Numeric fields — check=numeric
6. Any partner-specific rules noted in the spec

Field map:
{field_map_json}
"""


# ---------------------------------------------------------------------------
# Core parser class
# ---------------------------------------------------------------------------

class EDISpecParser:
    """Parse EDI partner specs and generate workflow integration artefacts.

    Uses OpenAI to extract a structured FieldMap from a raw spec, then
    generates a JavaScript transform script, validator rules, and an
    optional workflow export JSON.
    """

    def __init__(self, model: str = MODEL):
        self.model = model
        if not _OPENAI_AVAILABLE:
            raise RuntimeError("OpenAI package not available. Run: pip install openai")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def parse_spec(
        self,
        spec_text: str,
        doc_type: str,
        partner: str,
        direction: str = "inbound",
        target_system: str = _TARGET_SYSTEM,
        target_api_endpoint: str = _TARGET_API_ENDPOINT,
        build_workflow_export: bool = True,
    ) -> ParseResult:
        """Full pipeline: spec text → ParseResult with JS, rules, and workflow JSON."""
        warnings: List[str] = []

        logger.info("Parsing %s %s %s spec…", partner, doc_type, direction)
        field_map = self._parse_field_map(spec_text, doc_type, partner, direction)

        logger.info("Generating JavaScript transform…")
        js_script = self._generate_js_transform(
            field_map, target_system, target_api_endpoint
        )

        logger.info("Generating validator rules…")
        validator_rules = self._generate_validator_rules(field_map)

        template_key = f"{partner.lower()}_{doc_type}_{direction}"
        workflow_json = ""
        if build_workflow_export:
            workflow_json = self._build_workflow_export(
                field_map, js_script, template_key, warnings
            )

        return ParseResult(
            field_map=field_map,
            js_transform_script=js_script,
            validator_rules=validator_rules,
            workflow_export_json=workflow_json,
            template_key=template_key,
            warnings=warnings,
        )

    def parse_spec_file(
        self,
        spec_path: Path,
        doc_type: str,
        partner: str,
        direction: str = "inbound",
        **kwargs: Any,
    ) -> ParseResult:
        """Load spec from file (txt/json/pdf) then call parse_spec()."""
        spec_path = Path(spec_path)
        if not spec_path.exists():
            raise FileNotFoundError(f"Spec file not found: {spec_path}")

        suffix = spec_path.suffix.lower()

        if suffix == ".json":
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
            spec_text = json.dumps(raw, indent=2)
        elif suffix == ".pdf":
            spec_text = self._extract_pdf_text(spec_path)
        else:
            spec_text = spec_path.read_text(encoding="utf-8")

        return self.parse_spec(spec_text, doc_type, partner, direction, **kwargs)

    # ------------------------------------------------------------------
    # Step 1: Field map extraction
    # ------------------------------------------------------------------

    def _parse_field_map(
        self, spec_text: str, doc_type: str, partner: str, direction: str
    ) -> EDIFieldMap:
        prompt = _FIELD_MAP_PROMPT.format(
            partner=partner,
            doc_type=doc_type,
            direction=direction,
            spec_text=spec_text[:12000],  # cap to avoid token overflow
        )
        raw = self._llm_call(prompt)
        try:
            data = _extract_json(raw)
        except ValueError as exc:
            logger.warning("Field map JSON parse failed (%s) — using empty map", exc)
            data = {}

        segments: List[SegmentField] = []
        for s in data.get("segments", []):
            segments.append(
                SegmentField(
                    segment=s.get("segment", ""),
                    element_pos=int(s.get("element_pos", 0)),
                    element_id=s.get("element_id", ""),
                    name=s.get("name", ""),
                    requirement=s.get("requirement", "O"),
                    data_type=s.get("data_type", "AN"),
                    min_length=int(s.get("min_length", 1)),
                    max_length=int(s.get("max_length", 255)),
                    qualifier_code=s.get("qualifier_code"),
                    valid_values=s.get("valid_values", []),
                    notes=s.get("notes", ""),
                    orderful_path=s.get("orderful_path", ""),
                    erp_field=s.get("erp_field", s.get("sage_field", "")),
                )
            )

        return EDIFieldMap(
            partner=partner,
            doc_type=doc_type,
            direction=direction,
            description=data.get("description", f"{partner} {doc_type} {direction}"),
            segments=segments,
            loop_structure=data.get("loop_structure", []),
            qualifiers=data.get("qualifiers", {}),
            max_line_items=int(data.get("max_line_items", 999999)),
            notes=data.get("notes", ""),
        )

    # ------------------------------------------------------------------
    # Step 2: JavaScript transform script
    # ------------------------------------------------------------------

    def _generate_js_transform(
        self,
        field_map: EDIFieldMap,
        target_system: str,
        target_api_endpoint: str,
    ) -> str:
        mappings = [
            {
                "element_id": s.element_id,
                "name": s.name,
                "requirement": s.requirement,
                "orderful_path": s.orderful_path,
                "erp_field": s.erp_field,
                "data_type": s.data_type,
            }
            for s in field_map.segments
        ]

        prompt = _JS_TRANSFORM_PROMPT.format(
            partner=field_map.partner,
            doc_type=field_map.doc_type,
            direction=field_map.direction,
            target_system=target_system,
            target_api_endpoint=target_api_endpoint,
            field_mappings_json=json.dumps(mappings, indent=2),
        )
        js = self._llm_call(prompt)
        js = _strip_code_fences(js)

        if "exports.step" not in js:
            js = f"exports.step = function(input, fileInput) {{\n{js}\n}};"

        return js

    # ------------------------------------------------------------------
    # Step 3: Validator rules
    # ------------------------------------------------------------------

    def _generate_validator_rules(self, field_map: EDIFieldMap) -> List[Dict[str, Any]]:
        fm_json = json.dumps(
            {
                "partner": field_map.partner,
                "doc_type": field_map.doc_type,
                "segments": [
                    {
                        "element_id": s.element_id,
                        "name": s.name,
                        "requirement": s.requirement,
                        "data_type": s.data_type,
                        "min_length": s.min_length,
                        "max_length": s.max_length,
                        "valid_values": s.valid_values,
                        "notes": s.notes,
                    }
                    for s in field_map.segments
                ],
                "qualifiers": field_map.qualifiers,
                "notes": field_map.notes,
            },
            indent=2,
        )

        prompt = _VALIDATOR_RULES_PROMPT.format(
            partner=field_map.partner,
            doc_type=field_map.doc_type,
            field_map_json=fm_json[:8000],
        )
        raw = self._llm_call(prompt)
        try:
            rules = _extract_json(raw)
            if not isinstance(rules, list):
                rules = []
        except ValueError:
            rules = []

        return rules

    # ------------------------------------------------------------------
    # Step 4: Workflow export JSON
    # ------------------------------------------------------------------

    def _build_workflow_export(
        self,
        field_map: EDIFieldMap,
        js_script: str,
        template_key: str,
        warnings: List[str],
    ) -> str:
        """Build a workflow export JSON using the tray_workflow_schema builder
        (optional dependency; gracefully skipped if unavailable).
        """
        try:
            from tray_workflow_schema import TrayWorkflowBuilder, TrayExport  # type: ignore
        except ImportError as exc:
            warnings.append(
                f"tray_workflow_schema not available: {exc}. "
                "Skipping workflow export. Install it or provide a custom builder."
            )
            return ""

        doc_type = field_map.doc_type
        direction = field_map.direction
        partner = field_map.partner.lower()

        b = TrayWorkflowBuilder(
            title=f"{field_map.partner} {doc_type} {direction.title()} — {template_key}",
            description=field_map.description,
        )

        sftp_host = "${env.SFTP_HOST}"
        orderful_base = "${env.ORDERFUL_BASE}"
        orderful_key = "${env.ORDERFUL_API_KEY}"
        erp_base = "${env.ERP_BASE_URL}"
        erp_key = "${env.ERP_API_KEY}"
        teams_webhook = "${env.TEAMS_WEBHOOK}"

        if direction == "inbound":
            b.add_cron_trigger("*/15 * * * *")
            sftp_list = b.add_sftp_list(
                host=sftp_host,
                path=f"/edi/inbox/{partner}/{doc_type}/",
                pattern="*.edi",
                step_name="list_sftp_files",
            )
            loop_start = b.add_loop_start(
                array_ref=b.step_ref(sftp_list, "files"),
                step_name="loop_files",
            )
            sftp_get = b.add_sftp_get(
                host=sftp_host,
                remote_path=b.step_ref("$.loop.value", "path"),
                step_name="get_edi_file",
            )
            orderful_submit = b.add_http(
                method="POST",
                url=f"{orderful_base}/transactions",
                headers=[
                    ("Authorization", f"Bearer {orderful_key}"),
                    ("Content-Type", "application/edi-x12"),
                ],
                body=b.step_ref(sftp_get, "file_content"),
                step_name="submit_to_orderful",
            )
            transform = b.add_script(
                js_code=js_script,
                input_data={"orderful_response": b.step_ref(orderful_submit, "response.body")},
                step_name="transform_to_erp",
            )
            b.add_http(
                method="POST",
                url=f"{erp_base}/api/orders",
                headers=[
                    ("Authorization", f"Bearer {erp_key}"),
                    ("Content-Type", "application/json"),
                ],
                body=b.step_ref(transform, "result"),
                step_name="create_erp_order",
            )
            b.add_sftp_move(
                host=sftp_host,
                source_path=b.step_ref("$.loop.value", "path"),
                dest_path=b.step_ref("$.loop.value", "path").replace(
                    f"/inbox/{partner}/{doc_type}/",
                    f"/processed/{partner}/{doc_type}/",
                ),
                step_name="archive_edi_file",
            )
            b.add_loop_end(loop_start, step_name="end_loop_files")

        else:  # outbound (856/810/846/860/997)
            b.add_webhook_trigger()
            transform = b.add_script(
                js_code=js_script,
                input_data={"erp_payload": b.step_ref("$.trigger", "body")},
                step_name="transform_to_edi",
            )
            orderful_submit = b.add_http(
                method="POST",
                url=f"{orderful_base}/transactions",
                headers=[
                    ("Authorization", f"Bearer {orderful_key}"),
                    ("Content-Type", "application/json"),
                ],
                body=b.step_ref(transform, "result"),
                step_name="submit_to_orderful",
            )
            b.add_sftp_move(
                host=sftp_host,
                source_path=f"/tmp/{template_key}_out.edi",
                dest_path=f"/edi/outbox/{partner}/{doc_type}/",
                step_name="deposit_to_sftp",
            )
            b.add_http(
                method="POST",
                url=teams_webhook,
                headers=[("Content-Type", "application/json")],
                body={"text": f"✅ {partner.title()} {doc_type} submitted to Orderful"},
                step_name="notify_webhook",
            )

        export = TrayExport([b.build()])
        return export.to_json()

    # ------------------------------------------------------------------
    # PDF extraction (optional dependency)
    # ------------------------------------------------------------------

    def _extract_pdf_text(self, pdf_path: Path) -> str:
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf_path)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages)
        except ImportError:
            pass

        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(pdf_path))
            return "\n".join(page.get_text() for page in doc)
        except ImportError:
            pass

        raise RuntimeError(
            "PDF extraction requires pdfplumber or PyMuPDF. "
            "Run: pip install pdfplumber  OR  pip install pymupdf"
        )

    # ------------------------------------------------------------------
    # LLM wrapper
    # ------------------------------------------------------------------

    def _llm_call(self, prompt: str) -> str:
        if not _OPENAI_AVAILABLE or _openai_client is None:
            raise RuntimeError("OpenAI client not initialized")

        response = _openai_client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Rule persistence helpers
# ---------------------------------------------------------------------------

def save_validator_rules(
    rules: List[Dict[str, Any]],
    partner: str,
    doc_type: str,
    out_path: Optional[Path] = None,
) -> Path:
    """Save generated validator rules to a JSON file for EDIValidator to load."""
    if out_path is None:
        out_path = _DEFAULT_OUT_DIR / "rules" / f"{partner}_{doc_type}_rules.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "partner": partner,
        "doc_type": doc_type,
        "generated_by": "firstpass.validators.spec_parser",
        "rules": rules,
    }
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Saved %d validator rules → %s", len(rules), out_path)
    return out_path


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Any:
    """Extract JSON from LLM response, stripping code fences if present."""
    text = _strip_code_fences(text).strip()
    match = re.search(r"[\[{]", text)
    if match:
        text = text[match.start():]
    return json.loads(text)


def _strip_code_fences(text: str) -> str:
    """Remove ```json / ```javascript / ``` fences from LLM output."""
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    ap = argparse.ArgumentParser(
        description="FirstPass EDI: parse a partner spec and generate integration artefacts."
    )
    ap.add_argument("--spec", required=True, help="Path to spec file (txt, json, pdf)")
    ap.add_argument("--doc-type", required=True, help="EDI doc type e.g. 850, 856, 810")
    ap.add_argument("--partner", required=True, help="Trading partner name e.g. acme_retail")
    ap.add_argument(
        "--direction", default="inbound", choices=["inbound", "outbound"],
        help="Transaction direction (default: inbound)"
    )
    ap.add_argument(
        "--out-dir", default=str(_DEFAULT_OUT_DIR),
        help="Output directory for generated files"
    )
    ap.add_argument(
        "--no-workflow", action="store_true",
        help="Skip workflow export generation (field map + rules only)"
    )
    args = ap.parse_args()

    parser = EDISpecParser()
    result = parser.parse_spec_file(
        spec_path=Path(args.spec),
        doc_type=args.doc_type,
        partner=args.partner,
        direction=args.direction,
        build_workflow_export=not args.no_workflow,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base = f"{args.partner.lower()}_{args.doc_type}_{args.direction}"

    # Save field map
    fm_path = out_dir / f"{base}_field_map.json"
    fm_data = {
        "partner": result.field_map.partner,
        "doc_type": result.field_map.doc_type,
        "direction": result.field_map.direction,
        "description": result.field_map.description,
        "segments": [s.__dict__ for s in result.field_map.segments],
        "loop_structure": result.field_map.loop_structure,
        "qualifiers": result.field_map.qualifiers,
        "max_line_items": result.field_map.max_line_items,
        "notes": result.field_map.notes,
    }
    fm_path.write_text(json.dumps(fm_data, indent=2), encoding="utf-8")
    print(f"Field map        → {fm_path}")

    # Save JS transform
    js_path = out_dir / f"{base}_transform.js"
    js_path.write_text(result.js_transform_script, encoding="utf-8")
    print(f"JS transform     → {js_path}")

    # Save validator rules
    rules_path = save_validator_rules(
        result.validator_rules, args.partner, args.doc_type,
        out_path=out_dir / f"{base}_rules.json"
    )
    print(f"Validator rules  → {rules_path}")

    # Save workflow export
    if result.workflow_export_json:
        wf_path = out_dir / f"{base}_workflow_export.json"
        wf_path.write_text(result.workflow_export_json, encoding="utf-8")
        print(f"Workflow export  → {wf_path}")
    else:
        for w in result.warnings:
            print(f"WARNING: {w}")

    print(f"\nDone — template key: {result.template_key}")
    if result.warnings:
        print("Warnings:")
        for w in result.warnings:
            print(f"  ! {w}")


if __name__ == "__main__":
    main()
