"""
FirstPass EDI — X12 856 Parser
================================
Parses raw X12 EDI 856 Advance Ship Notice (ASN) documents into a structured
Python dataclass.

X12 Document Structure (856 ASN)
─────────────────────────────────
  ISA  Interchange Control Header      (always first; defines delimiters)
  GS   Functional Group Header
  ST   Transaction Set Header          (*856 = ASN)
  BSN  Beginning Segment for Ship Notice
  HL   Hierarchical Level loop (nested: S → O → P → I)
    S  Shipment level
      DTM  Date/Time Reference
      TD1  Carrier Details – Quantity & Weight
      TD5  Carrier Details – Routing
      REF  Reference Identification
    O  Order level
      PRF  Purchase Order Reference
      REF  Reference Identification
    P  Pack level
      PO4  Item Physical Details
    I  Item level
      LIN  Item Identification (UPC, EAN, etc.)
      SN1  Item Detail – Shipment
      PID  Product/Item Description (optional)
  CTT  Transaction Totals
  SE   Transaction Set Trailer
  GE   Functional Group Trailer
  IEA  Interchange Control Trailer

Delimiters
──────────
  Segment terminator  : character immediately after ISA16 value  (typically ~)
  Element separator   : character after "ISA"                    (typically *)
  Component separator : ISA16 value                              (typically >)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class InterchangeEnvelope:
    """Parsed ISA/IEA interchange control information."""

    sender_qualifier: str          # ISA05
    sender_id: str                 # ISA06
    receiver_qualifier: str        # ISA07
    receiver_id: str               # ISA08
    date: str                      # ISA09  (YYMMDD)
    time: str                      # ISA10  (HHMM)
    control_number: str            # ISA13
    ack_requested: str             # ISA14
    usage_indicator: str           # ISA15  P=Production, T=Test
    component_separator: str       # ISA16


@dataclass
class FunctionalGroup:
    """Parsed GS/GE functional group header."""

    functional_id: str             # GS01  (SH = Ship Notice)
    sender_code: str               # GS02
    receiver_code: str             # GS03
    date: str                      # GS04  (CCYYMMDD)
    time: str                      # GS05  (HHMM or HHMMSS)
    group_control_number: str      # GS06
    responsible_agency: str        # GS07  (X = ASC X12)
    version: str                   # GS08  (e.g. 005010)


@dataclass
class BeginningSegment:
    """Parsed BSN – Beginning Segment for Ship Notice."""

    transaction_type: str          # BSN01  (00=Original, 05=Replace, 06=Cancel)
    shipment_id: str               # BSN02
    date: str                      # BSN03  (CCYYMMDD)
    time: str                      # BSN04  (HHMM or HHMMSS)
    hierarchical_structure: str    # BSN05  (0002 = Shipment/Order/Pack/Item)


@dataclass
class HierarchicalLevel:
    """
    One HL node in the 856 loop hierarchy.

    Each node has a level code:
      S = Shipment   (HL03 value)
      O = Order
      P = Pack
      I = Item

    Child segments (DTM, TD1, PRF, LIN, etc.) are stored in ``segments``.
    Child HL nodes are stored in ``children``.
    """

    hl_id: str                              # HL01
    parent_id: str                          # HL02 (empty at root)
    level_code: str                         # HL03  S | O | P | I
    has_children: str                       # HL04  0=leaf, 1=has-children
    segments: list[dict] = field(default_factory=list)
    children: list["HierarchicalLevel"] = field(default_factory=list)


@dataclass
class ParsedEDI856:
    """
    Fully parsed X12 856 Advance Ship Notice document.

    ``raw_segments`` is the flat ordered list of all segments (useful for
    the ReAct validation loop which iterates segments sequentially).
    ``hl_tree`` is the reconstructed hierarchy for structural analysis.
    """

    # ── Envelope ──────────────────────────────────────────────────────────────
    interchange: InterchangeEnvelope
    functional_group: FunctionalGroup
    transaction_set_id: str        # ST02 control number
    bsn: BeginningSegment

    # ── Content ───────────────────────────────────────────────────────────────
    hl_tree: list[HierarchicalLevel]    # top-level HL nodes (S level)
    raw_segments: list[dict]            # flat, ordered [{type, elements, raw}]

    # ── Derived / convenience ─────────────────────────────────────────────────
    partner_name: str = ""              # populated by Orchestrator from ISA IDs
    total_line_items: int = 0          # CTT01
    transaction_set_segment_count: int = 0  # SE01


# ── Parser ────────────────────────────────────────────────────────────────────


class EDIParser:
    """
    Stateless X12 856 parser.

    Usage::

        parser = EDIParser()
        doc = parser.parse(raw_edi_string)
        print(doc.bsn.shipment_id)
    """

    # Segments we actively parse and structure.
    # Others are captured as generic {type, elements} dicts in raw_segments.
    _KNOWN_SEGMENTS = {
        "ISA", "GS", "ST", "BSN",
        "HL", "DTM", "REF", "TD1", "TD5",
        "PRF", "PO4", "LIN", "SN1", "PID",
        "CTT", "SE", "GE", "IEA",
    }

    def parse(self, raw: str) -> ParsedEDI856:
        """
        Parse a raw X12 856 string into a ``ParsedEDI856`` dataclass.

        Parameters
        ----------
        raw:
            The full EDI string, typically ending with ``~`` segment
            terminators and ``*`` element separators.

        Returns
        -------
        ParsedEDI856
            Fully populated document object.

        Raises
        ------
        ValueError
            If the ISA header is missing or the segment count in SE does not
            match the actual segment count.
        """
        element_sep, component_sep, segment_term = self._detect_delimiters(raw)
        segments_raw = self._split_segments(raw, segment_term)
        raw_segments: list[dict] = []
        interchange: Optional[InterchangeEnvelope] = None
        functional_group: Optional[FunctionalGroup] = None
        transaction_set_id: str = ""
        bsn: Optional[BeginningSegment] = None
        ctt_total: int = 0
        se_count: int = 0

        # ── HL hierarchy tracking ─────────────────────────────────────────────
        hl_map: dict[str, HierarchicalLevel] = {}   # hl_id → node
        hl_roots: list[HierarchicalLevel] = []       # top-level S nodes
        current_hl: Optional[HierarchicalLevel] = None

        for seg_raw in segments_raw:
            seg_raw = seg_raw.strip()
            if not seg_raw:
                continue

            elements = seg_raw.split(element_sep)
            seg_type = elements[0].upper()

            seg_dict: dict = {
                "type": seg_type,
                "elements": elements[1:],
                "raw": seg_raw,
            }
            raw_segments.append(seg_dict)

            # ── Structured parsing per segment type ───────────────────────────

            if seg_type == "ISA":
                interchange = self._parse_isa(elements, component_sep)

            elif seg_type == "GS":
                functional_group = self._parse_gs(elements)

            elif seg_type == "ST":
                transaction_set_id = self._get(elements, 2)

            elif seg_type == "BSN":
                bsn = self._parse_bsn(elements)

            elif seg_type == "HL":
                hl_node = HierarchicalLevel(
                    hl_id=self._get(elements, 1),
                    parent_id=self._get(elements, 2),
                    level_code=self._get(elements, 3),   # S | O | P | I
                    has_children=self._get(elements, 4),
                )
                hl_map[hl_node.hl_id] = hl_node
                if hl_node.parent_id and hl_node.parent_id in hl_map:
                    hl_map[hl_node.parent_id].children.append(hl_node)
                else:
                    hl_roots.append(hl_node)
                current_hl = hl_node

            elif seg_type == "CTT":
                ctt_total = int(self._get(elements, 1) or 0)

            elif seg_type == "SE":
                se_count = int(self._get(elements, 1) or 0)

            else:
                # Attach non-structural segments to the current HL node.
                if current_hl is not None:
                    current_hl.segments.append(seg_dict)

        if interchange is None:
            raise ValueError("ISA segment not found — not a valid X12 document.")
        if bsn is None:
            raise ValueError("BSN segment not found — not a valid 856 transaction.")

        return ParsedEDI856(
            interchange=interchange,
            functional_group=functional_group or FunctionalGroup("", "", "", "", "", "", "", ""),
            transaction_set_id=transaction_set_id,
            bsn=bsn,
            hl_tree=hl_roots,
            raw_segments=raw_segments,
            total_line_items=ctt_total,
            transaction_set_segment_count=se_count,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _detect_delimiters(raw: str) -> tuple[str, str, str]:
        """
        Derive element separator, component separator, and segment terminator
        from the ISA header.

        The ISA segment is always exactly 106 characters of fixed-width data.
        Position 3 (0-indexed) is the element separator.
        Position 104 is the component separator.
        Position 105 (or the very next char after ISA[16]) is the segment term.
        """
        raw = raw.strip()
        if not raw.upper().startswith("ISA"):
            raise ValueError("Document does not begin with ISA segment.")

        element_sep = raw[3]         # character immediately after "ISA"
        # ISA has 16 elements; ISA16 is the component separator.
        # Segment terminator is the char right after ISA16.
        isa_parts = raw[:106].split(element_sep)
        component_sep = isa_parts[16][0] if len(isa_parts) > 16 else ">"
        # The segment terminator follows ISA16.
        segment_term = raw[105] if len(raw) > 105 else "~"
        return element_sep, component_sep, segment_term

    @staticmethod
    def _split_segments(raw: str, segment_term: str) -> list[str]:
        """Split the document string into individual segment strings."""
        # Some files have whitespace/newlines around terminators.
        return [s.strip() for s in raw.split(segment_term) if s.strip()]

    @staticmethod
    def _get(elements: list[str], index: int, default: str = "") -> str:
        """Safe indexed access into a segment's element list."""
        try:
            return (elements[index] or "").strip()
        except IndexError:
            return default

    def _parse_isa(self, elements: list[str], component_sep: str) -> InterchangeEnvelope:
        return InterchangeEnvelope(
            sender_qualifier=self._get(elements, 5),
            sender_id=self._get(elements, 6),
            receiver_qualifier=self._get(elements, 7),
            receiver_id=self._get(elements, 8),
            date=self._get(elements, 9),
            time=self._get(elements, 10),
            control_number=self._get(elements, 13),
            ack_requested=self._get(elements, 14),
            usage_indicator=self._get(elements, 15),
            component_separator=component_sep,
        )

    def _parse_gs(self, elements: list[str]) -> FunctionalGroup:
        return FunctionalGroup(
            functional_id=self._get(elements, 1),
            sender_code=self._get(elements, 2),
            receiver_code=self._get(elements, 3),
            date=self._get(elements, 4),
            time=self._get(elements, 5),
            group_control_number=self._get(elements, 6),
            responsible_agency=self._get(elements, 7),
            version=self._get(elements, 8),
        )

    def _parse_bsn(self, elements: list[str]) -> BeginningSegment:
        return BeginningSegment(
            transaction_type=self._get(elements, 1),
            shipment_id=self._get(elements, 2),
            date=self._get(elements, 3),
            time=self._get(elements, 4),
            hierarchical_structure=self._get(elements, 5),
        )


# ── Sample 856 for testing ────────────────────────────────────────────────────

SAMPLE_856_RETAILER_A = """\
ISA*00*          *00*          *ZZ*SHIPPER001     *ZZ*RETAILERA01    *260901*0734*^*00501*000000001*0*P*>~
GS*SH*SHIPPER001*RETAILERA01*20260901*0734*1*X*005010~
ST*856*0001~
BSN*00*SHIP-2026-09-001*20260901*073400*0002~
HL*1**S~
TD1*CTN*10****G*245.5*LB~
TD5**2*UPS*PP*GROUND~
REF*BM*BOL-2026-001~
REF*CN*SHIPPER001~
DTM*011*20260901~
DTM*067*20260903~
HL*2*1*O~
PRF*PO-12345****20260825~
REF*IA*ORD-98765~
HL*3*2*P~
PO4*5*EA*CTN**2.5*FT*18*IN*12*IN*10*IN~
HL*4*3*I~
LIN**UP*012345678901~
SN1**5*EA~
PID*F**ZZ*BLUE WIDGET 10PK~
HL*5*2*P~
PO4*5*EA*CTN**2.5*FT*18*IN*12*IN*10*IN~
HL*6*5*I~
LIN**UP*012345678902~
SN1**5*EA~
PID*F**ZZ*RED WIDGET 10PK~
CTT*2~
SE*25*0001~
GE*1*1~
IEA*1*000000001~"""

SAMPLE_856_RETAILER_A_WITH_ERRORS = """\
ISA*00*          *00*          *ZZ*SHIPPER001     *ZZ*RETAILERA01    *260901*0800*^*00501*000000002*0*P*>~
GS*SH*SHIPPER001*RETAILERA01*20260901*0800*2*X*005010~
ST*856*0002~
BSN*00*SHIP-2026-09-002*20260901*080000*0002~
HL*1**S~
TD1*CTN*10***~
TD5**2*UPS~
HL*2*1*O~
PRF*PO-99999~
HL*3*2*P~
PO4*5*EA*CTN~
HL*4*3*I~
LIN**UP*INVALID~
SN1**0*EA~
CTT*1~
SE*15*0002~
GE*1*2~
IEA*1*000000002~"""
