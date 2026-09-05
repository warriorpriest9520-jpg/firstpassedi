"""
FirstPass EDI — Diagnostic Agent (Tree-of-Thought Beam Search)
================================================================
Performs root-cause analysis on a failed ValidationResult using the
**Tree-of-Thought (ToT)** reasoning pattern with beam search.

Tree-of-Thought Beam Search Algorithm
──────────────────────────────────────
  Depth 0 (seed):
    → Generate `initial_hypotheses` (3) root-cause hypotheses from the
      validation errors.

  For each depth in [1 … max_depth]:
    1. Score each hypothesis on 3 evidence dimensions:
         evidence_match      — Does evidence in the 856 itself support this?
         historical_precedent — Does it match a known failure pattern?
         consistency         — Is the hypothesis internally consistent?
       Combined score = Σ(weight_i × score_i)

    2. Prune: keep the top `beam_width` (2) hypotheses.
       Discard the rest.

    3. Expand: generate child hypotheses from each survivor by
       refining the root cause one level deeper.
       (At final depth: skip expand, return survivors as final diagnoses.)

  Escalate if top hypothesis score < `confidence_threshold` (0.7).

In DEMO_MODE, hypothesis generation and expansion use pre-scripted logic
tied to known error codes.  In production, each step calls GPT-4o.

Scoring Dimensions
──────────────────
  evidence_match       (weight 0.50) — Evidence found in 856 segment data
  historical_precedent (weight 0.30) — Match against vector store failure logs
  consistency          (weight 0.20) — Logical coherence of the hypothesis
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from config import config
from agents.retrieval_agent import ContextPackage
from agents.validation_agent import ValidationError, ValidationResult
from tools.vector_store import VectorStore

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class Hypothesis:
    """
    A single root-cause hypothesis in the ToT beam.

    Attributes
    ----------
    id:       Unique identifier for this node in the tree.
    parent_id: ID of the hypothesis this was expanded from (empty = root).
    depth:    Depth in the tree (0 = initial hypothesis).
    description: Short description of the hypothesised root cause.
    root_cause: More detailed root cause statement.
    fix_recommendation: Concrete action to resolve the issue.
    affected_segments: Which EDI segments are implicated.
    evidence_match: Score in [0, 1] for evidence found in the 856.
    historical_precedent: Score in [0, 1] for match against failure history.
    consistency: Score in [0, 1] for internal logical coherence.
    score: Weighted composite of the three dimension scores.
    """

    id: str
    parent_id: str
    depth: int
    description: str
    root_cause: str
    fix_recommendation: str
    affected_segments: list[str]
    evidence_match: float = 0.0
    historical_precedent: float = 0.0
    consistency: float = 0.0
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "depth": self.depth,
            "description": self.description,
            "root_cause": self.root_cause,
            "fix_recommendation": self.fix_recommendation,
            "affected_segments": self.affected_segments,
            "scores": {
                "evidence_match": round(self.evidence_match, 3),
                "historical_precedent": round(self.historical_precedent, 3),
                "consistency": round(self.consistency, 3),
                "composite": round(self.score, 3),
            },
        }


@dataclass
class BeamIteration:
    """Records one depth-level of the beam search for audit/explainability."""

    depth: int
    candidates: list[dict]          # All hypotheses evaluated at this depth
    beam: list[str]                 # IDs of the surviving beam after pruning
    pruned: list[str]               # IDs of hypotheses that were cut


@dataclass
class DiagnosticResult:
    """
    The outcome of the DiagnosticAgent's ToT beam search.

    Attributes
    ----------
    top_diagnoses:
        Final beam — ranked list of surviving hypotheses (best first).
    confidence:
        Score of the top-ranked hypothesis (0–1).
    should_escalate:
        True if confidence < ``DiagnosticConfig.confidence_threshold`` (0.7).
    beam_iterations:
        Full record of each depth level for explainability.
    summary:
        One-line human-readable diagnosis verdict.
    """

    top_diagnoses: list[Hypothesis]
    confidence: float
    should_escalate: bool
    beam_iterations: list[BeamIteration]
    summary: str


# ── Diagnostic Agent ──────────────────────────────────────────────────────────


class DiagnosticAgent:
    """
    Performs root-cause analysis on a failed 856 using Tree-of-Thought beam search.

    Parameters
    ----------
    vector_store:
        VectorStore used to look up historical failure patterns.
        Defaults to a new VectorStore pointing at ``config.chroma_db_path``.
    """

    def __init__(self, vector_store: Optional[VectorStore] = None) -> None:
        self._store = vector_store or VectorStore()
        self._cfg = config.diagnostic
        self._demo_mode = config.demo_mode

        if not self._demo_mode:
            try:
                import openai
                self._llm = openai.OpenAI(api_key=config.openai_api_key)
            except ImportError:
                logger.warning("openai package not found; DiagnosticAgent using demo mode.")
                self._demo_mode = True

    def diagnose(
        self,
        validation_result: ValidationResult,
        context: ContextPackage,
    ) -> DiagnosticResult:
        """
        Run ToT beam search to identify the root cause of validation failures.

        Parameters
        ----------
        validation_result:
            The failed ValidationResult from the ValidationAgent.
        context:
            ContextPackage from the RetrievalAgent (used for spec lookup).

        Returns
        -------
        DiagnosticResult
            Ranked diagnoses, confidence, escalation flag, and beam trace.
        """
        logger.info(
            "DiagnosticAgent starting ToT beam search (depth=%d, width=%d, demo=%s).",
            self._cfg.max_depth,
            self._cfg.beam_width,
            self._demo_mode,
        )

        # ── Depth 0: Generate initial hypotheses ──────────────────────────────
        hypotheses = self._generate_initial_hypotheses(validation_result, context)
        logger.info("Generated %d initial hypotheses.", len(hypotheses))

        beam_iterations: list[BeamIteration] = []

        for depth in range(self._cfg.max_depth):

            # ── Score all current hypotheses ──────────────────────────────────
            scored = self._score_hypotheses(hypotheses, validation_result, context)

            sorted_scored = sorted(scored, key=lambda h: h.score, reverse=True)
            beam = sorted_scored[: self._cfg.beam_width]
            pruned = sorted_scored[self._cfg.beam_width :]

            beam_iterations.append(BeamIteration(
                depth=depth,
                candidates=[h.to_dict() for h in sorted_scored],
                beam=[h.id for h in beam],
                pruned=[h.id for h in pruned],
            ))

            logger.info(
                "Depth %d: %d candidates scored. Beam: %s (pruned %d).",
                depth,
                len(sorted_scored),
                [h.id[:8] for h in beam],
                len(pruned),
            )

            # If we're at max depth, stop here and return the current beam.
            if depth == self._cfg.max_depth - 1:
                break

            # Early exit: if beam leader already has high confidence, stop searching.
            if beam and beam[0].score >= 0.92:
                logger.info("Early exit: top hypothesis score %.3f ≥ 0.92.", beam[0].score)
                break

            # ── Expand: generate child hypotheses from each beam survivor ─────
            next_hypotheses: list[Hypothesis] = []
            for parent in beam:
                children = self._expand_hypothesis(parent, validation_result, context, depth + 1)
                next_hypotheses.extend(children)

            hypotheses = next_hypotheses

        # ── Final result ──────────────────────────────────────────────────────
        top_confidence = beam[0].score if beam else 0.0
        should_escalate = top_confidence < self._cfg.confidence_threshold

        summary = self._format_summary(beam, top_confidence, should_escalate)

        logger.info(
            "DiagnosticAgent complete: top_confidence=%.3f, escalate=%s.",
            top_confidence,
            should_escalate,
        )

        return DiagnosticResult(
            top_diagnoses=beam,
            confidence=top_confidence,
            should_escalate=should_escalate,
            beam_iterations=beam_iterations,
            summary=summary,
        )

    # ── Hypothesis generation ─────────────────────────────────────────────────

    def _generate_initial_hypotheses(
        self,
        result: ValidationResult,
        context: ContextPackage,
    ) -> list[Hypothesis]:
        """
        Generate `initial_hypotheses` (3) root-cause candidates from error patterns.

        In demo mode: map error codes to known hypothesis templates.
        In production: send error list to GPT-4o and parse structured JSON output.
        """
        if self._demo_mode:
            return self._demo_generate_hypotheses(result)

        # ── Production: LLM-generated hypotheses ──────────────────────────────
        error_summary = json.dumps(
            [{"code": e.code, "segment": e.segment_type, "severity": e.severity, "desc": e.description}
             for e in result.errors],
            indent=2,
        )
        prompt = f"""You are an EDI root-cause analyst.

Trading partner: {context.partner_name}
Spec version: {context.spec_version}

Validation errors found:
{error_summary}

Generate exactly 3 distinct root-cause hypotheses for these errors.
Each hypothesis must have a unique angle (e.g. upstream data quality, mapping config, spec change).

Respond ONLY with a JSON array of 3 objects, each with these keys:
  description        (str, ≤15 words)
  root_cause         (str, 1–2 sentences)
  fix_recommendation (str, 1–2 sentences)
  affected_segments  (list of str)

No extra text or markdown."""

        response = self._llm.chat.completions.create(
            model=config.openai_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = json.loads(response.choices[0].message.content)
        items = raw if isinstance(raw, list) else raw.get("hypotheses", [])

        hypotheses: list[Hypothesis] = []
        for item in items[: self._cfg.initial_hypotheses]:
            hypotheses.append(Hypothesis(
                id=str(uuid.uuid4()),
                parent_id="",
                depth=0,
                description=item.get("description", ""),
                root_cause=item.get("root_cause", ""),
                fix_recommendation=item.get("fix_recommendation", ""),
                affected_segments=item.get("affected_segments", []),
            ))
        return hypotheses

    # ── Hypothesis expansion ──────────────────────────────────────────────────

    def _expand_hypothesis(
        self,
        parent: Hypothesis,
        result: ValidationResult,
        context: ContextPackage,
        depth: int,
    ) -> list[Hypothesis]:
        """
        Expand a surviving hypothesis into more specific child hypotheses.

        Each parent generates 2 children (one per beam slot remaining).
        Children refine the parent's root cause with more specific evidence.
        """
        if self._demo_mode:
            return self._demo_expand(parent, depth)

        # ── Production: LLM expansion ──────────────────────────────────────────
        prompt = f"""You are an EDI root-cause analyst refining a diagnosis.

Parent hypothesis:
  Description: {parent.description}
  Root cause: {parent.root_cause}

Generate exactly 2 more specific child hypotheses that refine this root cause.
Respond ONLY with a JSON array of 2 objects with keys:
  description, root_cause, fix_recommendation, affected_segments
No extra text."""

        response = self._llm.chat.completions.create(
            model=config.openai_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = json.loads(response.choices[0].message.content)
        items = raw if isinstance(raw, list) else raw.get("hypotheses", [])

        children: list[Hypothesis] = []
        for item in items[:2]:
            children.append(Hypothesis(
                id=str(uuid.uuid4()),
                parent_id=parent.id,
                depth=depth,
                description=item.get("description", ""),
                root_cause=item.get("root_cause", ""),
                fix_recommendation=item.get("fix_recommendation", ""),
                affected_segments=item.get("affected_segments", parent.affected_segments),
            ))
        return children

    # ── Hypothesis scoring ────────────────────────────────────────────────────

    def _score_hypotheses(
        self,
        hypotheses: list[Hypothesis],
        result: ValidationResult,
        context: ContextPackage,
    ) -> list[Hypothesis]:
        """
        Score each hypothesis on three evidence dimensions, then compute the
        weighted composite score.

        Returns the same list with ``evidence_match``, ``historical_precedent``,
        ``consistency``, and ``score`` populated.
        """
        weights = self._cfg.scoring_weights

        for h in hypotheses:
            h.evidence_match = self._score_evidence_match(h, result)
            h.historical_precedent = self._score_historical_precedent(h, context)
            h.consistency = self._score_consistency(h, result)

            h.score = round(
                weights["evidence_match"] * h.evidence_match
                + weights["historical_precedent"] * h.historical_precedent
                + weights["consistency"] * h.consistency,
                4,
            )

        return hypotheses

    def _score_evidence_match(
        self, h: Hypothesis, result: ValidationResult
    ) -> float:
        """
        Score how well this hypothesis explains the actual validation errors.

        Simple overlap: what fraction of the hypothesis's affected_segments
        appear in the actual error list?
        """
        if not h.affected_segments:
            return 0.3  # No segment claims — weak evidence

        actual_error_segs = {e.segment_type for e in result.errors}
        matched = sum(1 for seg in h.affected_segments if seg in actual_error_segs)
        return round(matched / len(h.affected_segments), 3)

    def _score_historical_precedent(
        self, h: Hypothesis, context: ContextPackage
    ) -> float:
        """
        Score based on similarity to historical failure patterns in the vector store.

        Searches the validation_history collection for the hypothesis description
        and uses the top result's similarity as the precedent score.
        """
        try:
            results = self._store.search(
                query=h.description + " " + h.root_cause,
                collection="validation_history",
                top_k=1,
                where={"partner_name": context.partner_name},
            )
            if results:
                return round(1.0 - results[0].distance, 3)
        except Exception as exc:
            logger.warning("Historical precedent lookup failed: %s", exc)
        return 0.2  # No precedent found — neutral-low score

    @staticmethod
    def _score_consistency(h: Hypothesis, result: ValidationResult) -> float:
        """
        Score internal consistency of the hypothesis.

        Heuristics used:
        - Hypothesis has a non-empty fix recommendation → +0.3
        - fix_recommendation mentions at least one affected segment → +0.3
        - Description is specific (> 5 words) → +0.2
        - Root cause provides causal language ("because", "due to", "caused by") → +0.2
        """
        score = 0.0
        if h.fix_recommendation:
            score += 0.3
            if any(seg.lower() in h.fix_recommendation.lower() for seg in h.affected_segments):
                score += 0.3
        if len(h.description.split()) > 5:
            score += 0.2
        causal_terms = ("because", "due to", "caused by", "result of", "missing", "invalid")
        if any(term in h.root_cause.lower() for term in causal_terms):
            score += 0.2
        return round(min(1.0, score), 3)

    # ── Demo hypothesis templates ─────────────────────────────────────────────

    def _demo_generate_hypotheses(self, result: ValidationResult) -> list[Hypothesis]:
        """
        Return pre-scripted hypotheses based on error codes found in the
        validation result.  Used when DEMO_MODE is active.
        """
        error_codes = {e.code for e in result.errors}
        error_segs = list({e.segment_type for e in result.errors})

        # Template pool — pick the most relevant 3 based on what errors fired.
        templates = []

        if "TD5_SCAC_MISSING" in error_codes or "TD5_SCAC_NONSTANDARD" in error_codes:
            templates.append(Hypothesis(
                id=str(uuid.uuid4()), parent_id="", depth=0,
                description="Carrier SCAC missing or null in shipping system carrier table",
                root_cause=(
                    "The carrier SCAC code is absent in TD5-03 because the carrier "
                    "record in the shipping system's carrier mapping table has a null or "
                    "blank SCAC field for the carrier assigned to this shipment."
                ),
                fix_recommendation=(
                    "Locate the carrier in the shipping system's carrier/SCAC mapping table "
                    "and populate TD5-03 with the correct 2–4 character SCAC code."
                ),
                affected_segments=["TD5"],
            ))

        if "SN1_QTY_ZERO" in error_codes or "SN1_QTY_MISSING" in error_codes:
            templates.append(Hypothesis(
                id=str(uuid.uuid4()), parent_id="", depth=0,
                description="Unfulfilled line items exported with zero shipped quantity",
                root_cause=(
                    "The inventory / WMS system exported all order lines to EDI regardless "
                    "of fulfillment status.  Lines that were not picked or packed have a "
                    "shipped quantity of 0, which RetailerA's spec disallows."
                ),
                fix_recommendation=(
                    "Add a pre-export filter in the WMS EDI outbound workflow to exclude "
                    "any line item where shipped_qty = 0.  Only send lines with qty > 0."
                ),
                affected_segments=["SN1", "HL"],
            ))

        if "LIN_UPC_FORMAT" in error_codes or "LIN_QUALIFIER" in error_codes:
            templates.append(Hypothesis(
                id=str(uuid.uuid4()), parent_id="", depth=0,
                description="Invalid or non-numeric UPC barcode in item master",
                root_cause=(
                    "The item master record contains a UPC value that is either non-numeric, "
                    "fewer than 12 digits, or uses an EAN-13 format.  The EDI mapper passes "
                    "this value directly to LIN-03 without validation."
                ),
                fix_recommendation=(
                    "Clean item master UPC field: ensure all UPCs are exactly 12 numeric "
                    "digits.  Add a pre-EDI UPC validation step using GS1 check-digit algorithm."
                ),
                affected_segments=["LIN"],
            ))

        if "TD1_WEIGHT_MISSING" in error_codes or "TD1_WEIGHT_ZERO" in error_codes:
            templates.append(Hypothesis(
                id=str(uuid.uuid4()), parent_id="", depth=0,
                description="Shipment weight not calculated before EDI generation",
                root_cause=(
                    "The gross shipment weight in TD1-08 is 0 or missing because the "
                    "scale/weigh station data was not captured before the ASN was generated, "
                    "or the weight field is not mapped in the EDI translation template."
                ),
                fix_recommendation=(
                    "Ensure the ASN is generated after pack/weigh confirmation in the WMS.  "
                    "Verify TD1-08 mapping in the EDI translation template points to the "
                    "gross_weight field from the shipment record."
                ),
                affected_segments=["TD1"],
            ))

        if "PRF_PO_MISSING" in error_codes:
            templates.append(Hypothesis(
                id=str(uuid.uuid4()), parent_id="", depth=0,
                description="Purchase order reference missing at Order HL level",
                root_cause=(
                    "PRF-01 (PO number) is empty because the order-to-shipment linkage "
                    "was broken — either the shipment was created without an associated "
                    "order, or the EDI mapper is reading from the wrong database field."
                ),
                fix_recommendation=(
                    "Confirm the PO number is recorded on the shipment record in the WMS. "
                    "Check the EDI translation map to ensure PRF-01 reads from the correct "
                    "order reference field."
                ),
                affected_segments=["PRF", "HL"],
            ))

        # Generic fallback hypothesis
        if len(templates) < self._cfg.initial_hypotheses:
            templates.append(Hypothesis(
                id=str(uuid.uuid4()), parent_id="", depth=0,
                description="EDI translation map outdated relative to current partner spec",
                root_cause=(
                    "One or more validation errors may result from the EDI translation "
                    "map referencing an older version of the partner routing guide.  "
                    "Mandatory segments or elements added in recent spec updates are "
                    "absent because the map was never updated."
                ),
                fix_recommendation=(
                    "Review the partner's current routing guide (latest version) against "
                    "the active EDI translation map.  Update any segment/element mappings "
                    "that differ from the current spec."
                ),
                affected_segments=error_segs or ["BSN", "TD5", "LIN"],
            ))

        return templates[: self._cfg.initial_hypotheses]

    def _demo_expand(self, parent: Hypothesis, depth: int) -> list[Hypothesis]:
        """
        Generate 2 child hypotheses refining a parent (demo mode).

        Child hypotheses add one more layer of specificity to the parent's
        root cause, simulating the tree expansion step.
        """
        child_a = Hypothesis(
            id=str(uuid.uuid4()),
            parent_id=parent.id,
            depth=depth,
            description=f"[Refined] {parent.description} — data source level",
            root_cause=(
                parent.root_cause
                + "  At the data source level: the upstream system's export query "
                "does not join to the carrier/weight table before EDI generation."
            ),
            fix_recommendation=(
                parent.fix_recommendation
                + "  Specifically, update the export query or stored procedure to "
                "JOIN the carrier_master table and include scac_code and gross_weight."
            ),
            affected_segments=parent.affected_segments,
        )
        child_b = Hypothesis(
            id=str(uuid.uuid4()),
            parent_id=parent.id,
            depth=depth,
            description=f"[Refined] {parent.description} — mapping config level",
            root_cause=(
                parent.root_cause
                + "  At the mapping config level: the EDI translator's field map "
                "points to a column that does not exist in the current schema version."
            ),
            fix_recommendation=(
                parent.fix_recommendation
                + "  Audit the EDI translation template (map file) for any field "
                "references to renamed or removed columns in the source database."
            ),
            affected_segments=parent.affected_segments,
        )
        return [child_a, child_b]

    # ── Result formatting ─────────────────────────────────────────────────────

    @staticmethod
    def _format_summary(
        beam: list[Hypothesis],
        confidence: float,
        should_escalate: bool,
    ) -> str:
        if not beam:
            return "⚠️  No diagnosis could be formed — insufficient error data. Escalating."

        top = beam[0]
        if should_escalate:
            return (
                f"⚠️  ESCALATE — Top hypothesis confidence {confidence:.0%} is below "
                f"threshold 70%. Best candidate: \"{top.description}\". "
                "Human review required."
            )
        return (
            f"🔍  DIAGNOSIS — Root cause identified with {confidence:.0%} confidence: "
            f"\"{top.description}\". "
            f"Recommended fix: {top.fix_recommendation}"
        )
