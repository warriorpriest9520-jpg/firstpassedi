"""
order_pipeline.py — Universal EDI Order Pipeline Monitor for FirstPass EDI.

Tracks all active orders end-to-end through the EDI pipeline:
  850 received → ERP SO created → Shipped → 856 ASN sent → 997 ACK received

Auto-discovers orders from multiple sources:
  - ERP SQL (open sales orders with EDI flag — via pyodbc / ODBC Driver 17)
  - EDI_Log table (platform-logged transactions)
  - ShipStation (shipment events)
  - edi_watchdog.json (manually-registered watch entries)

Provides FastAPI route registration via OrderPipeline.register_routes(app).

Source lineage: edi_order_pipeline.py (ceo-bot / MorningTaskBot)
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("firstpass.pipeline.order_pipeline")

# ── Config ────────────────────────────────────────────────────────────────────

WATCHDOG_JSON       = Path(os.getenv("WATCHDOG_JSON",
                           str(Path(__file__).parent.parent.parent / "edi_watchdog.json")))
PIPELINE_STATE_FILE = Path(os.getenv("PIPELINE_STATE_FILE",
                           str(Path(__file__).parent.parent.parent / "edi_pipeline_state.json")))

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")

# Thresholds for "stuck" detection (minutes)
STUCK_THRESHOLDS: Dict[str, int] = {
    "850_received→SO_created":    int(os.getenv("STUCK_850_TO_SO",   "30")),   # 30 min
    "SO_created→shipped":         int(os.getenv("STUCK_SO_TO_SHIP", "1440")),  # 24 h
    "shipped→856_sent":           int(os.getenv("STUCK_SHIP_TO_856",  "60")),  # 1 h
    "856_sent→997_received":      int(os.getenv("STUCK_856_TO_997",  "120")),  # 2 h
}

# ERP SQL connection (optional — enables auto-discovery from your ERP database)
try:
    import pyodbc
    _PYODBC_AVAILABLE = True
except ImportError:
    _PYODBC_AVAILABLE = False

ERP_DB_HOST = os.getenv("ERP_DB_HOST", os.getenv("SAGE_DB_HOST", ""))
ERP_DB_NAME = os.getenv("ERP_DB_NAME", os.getenv("SAGE_DB_NAME", "ERP_DB"))
ERP_DB_USER = os.getenv("ERP_DB_USER", os.getenv("SAGE_DB_USER", ""))
ERP_DB_PASS = os.getenv("ERP_DB_PASS", os.getenv("SAGE_DB_PASS", ""))

# ── Pipeline stages ───────────────────────────────────────────────────────────

PIPELINE_STAGES = [
    "850_received",
    "SO_created",
    "shipped",
    "856_sent",
    "997_received",
    "complete",
]

STAGE_LABELS: Dict[str, str] = {
    "850_received": "📥 850 Received",
    "SO_created":   "📋 ERP SO Created",
    "shipped":      "📦 Shipped",
    "856_sent":     "📤 856 ASN Sent",
    "997_received": "✅ 997 ACK",
    "complete":     "✔️  Complete",
    "error":        "❌ Error",
    "stuck":        "⚠️  Stuck",
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class OrderStageEvent:
    stage:     str
    timestamp: str
    source:    str          # "erp_sql" | "edi_log" | "shipstation" | "manual" | "platform"
    details:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineOrder:
    po_number:        str
    customer_name:    str
    trading_partner:  str = ""
    erp_order_no:     str = ""
    current_stage:    str = "850_received"
    status:           str = "in_progress"   # in_progress | stuck | complete | error
    stuck_since:      Optional[str] = None
    stuck_transition: Optional[str] = None
    events:           List[OrderStageEvent] = field(default_factory=list)
    added_at:         str = field(default_factory=lambda: _now_iso())
    last_checked:     Optional[str] = None
    resolved_at:      Optional[str] = None
    notes:            str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "PipelineOrder":
        events = [OrderStageEvent(**e) for e in d.pop("events", [])]
        obj = cls(**{k: v for k, v in d.items() if k != "events"})
        obj.events = events
        return obj

    def add_event(self, stage: str, source: str, details: Optional[Dict] = None) -> None:
        self.events.append(OrderStageEvent(
            stage=stage,
            timestamp=_now_iso(),
            source=source,
            details=details or {},
        ))
        if PIPELINE_STAGES.index(stage) > PIPELINE_STAGES.index(self.current_stage):
            self.current_stage = stage
        if stage in ("complete", "997_received"):
            self.status      = "complete"
            self.resolved_at = _now_iso()
            self.stuck_since = None
        elif stage == "error":
            self.status = "error"

    def check_stuck(self) -> bool:
        if self.status in ("complete", "error"):
            return False
        for event in reversed(self.events):
            if event.stage == self.current_stage:
                event_time = _parse_iso(event.timestamp)
                if event_time is None:
                    continue
                elapsed_minutes = (datetime.now(timezone.utc) - event_time).total_seconds() / 60
                stage_idx = PIPELINE_STAGES.index(self.current_stage)
                if stage_idx < len(PIPELINE_STAGES) - 1:
                    next_stage = PIPELINE_STAGES[stage_idx + 1]
                    threshold  = STUCK_THRESHOLDS.get(f"{self.current_stage}→{next_stage}", 120)
                    if elapsed_minutes > threshold:
                        return True
                break
        return False

    def mark_stuck(self) -> None:
        stage_idx = PIPELINE_STAGES.index(self.current_stage)
        if stage_idx < len(PIPELINE_STAGES) - 1:
            next_stage = PIPELINE_STAGES[stage_idx + 1]
            self.stuck_transition = f"{self.current_stage}→{next_stage}"
        self.status = "stuck"
        if not self.stuck_since:
            self.stuck_since = _now_iso()

    def age_hours(self) -> float:
        t = _parse_iso(self.added_at)
        if t is None:
            return 0.0
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600

    def summary_line(self) -> str:
        stage_label = STAGE_LABELS.get(
            self.status if self.status in STAGE_LABELS else self.current_stage,
            self.current_stage,
        )
        age        = f"{self.age_hours():.1f}h"
        stuck_note = f" STUCK at {self.stuck_transition}" if self.status == "stuck" else ""
        return f"PO#{self.po_number} [{self.customer_name}] → {stage_label}{stuck_note} (age: {age})"


# ── State persistence ─────────────────────────────────────────────────────────

_state_lock = threading.Lock()


def _load_state() -> Dict[str, PipelineOrder]:
    if not PIPELINE_STATE_FILE.exists():
        return {}
    try:
        with open(PIPELINE_STATE_FILE) as f:
            raw = json.load(f)
        return {po: PipelineOrder.from_dict(d) for po, d in raw.items()}
    except Exception as e:
        logger.warning("Failed to load pipeline state: %s", e)
        return {}


def _save_state(orders: Dict[str, PipelineOrder]) -> None:
    try:
        data = {po: o.to_dict() for po, o in orders.items()}
        with open(PIPELINE_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error("Failed to save pipeline state: %s", e)


def _get_or_create_order(
    orders: Dict[str, PipelineOrder],
    po_number: str,
    customer_name: str = "",
    trading_partner: str = "",
) -> PipelineOrder:
    if po_number not in orders:
        orders[po_number] = PipelineOrder(
            po_number=po_number,
            customer_name=customer_name,
            trading_partner=trading_partner,
        )
    else:
        if customer_name and not orders[po_number].customer_name:
            orders[po_number].customer_name = customer_name
        if trading_partner and not orders[po_number].trading_partner:
            orders[po_number].trading_partner = trading_partner
    return orders[po_number]


# ── Auto-discovery sources ────────────────────────────────────────────────────

def _erp_conn_str() -> str:
    if ERP_DB_USER:
        return (
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={ERP_DB_HOST};DATABASE={ERP_DB_NAME};"
            f"UID={ERP_DB_USER};PWD={ERP_DB_PASS};"
            "Connect Timeout=5;"
        )
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={ERP_DB_HOST};DATABASE={ERP_DB_NAME};"
        "Trusted_Connection=yes;Connect Timeout=5;"
    )


def _discover_from_erp_sql() -> List[Dict]:
    """Query the ERP for open EDI orders (requires pyodbc + ERP_DB_HOST)."""
    if not _PYODBC_AVAILABLE or not ERP_DB_HOST:
        return []
    try:
        conn   = pyodbc.connect(_erp_conn_str(), timeout=5)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                h.SalesOrderNo,
                h.CustomerNo,
                h.CustomerPONo,
                h.ShipToName,
                h.OrderDate,
                COALESCE(h.ASN_Sent, 0)    AS ASN_Sent,
                COALESCE(h.EDI_Partner, '') AS EDI_Partner
            FROM SO_SalesOrderHeader h
            WHERE h.OrderStatus NOT IN ('C','X')
              AND (h.EDI_Partner IS NOT NULL AND h.EDI_Partner != '')
            ORDER BY h.OrderDate DESC
        """)
        rows = []
        for row in cursor.fetchall():
            rows.append({
                "erp_order_no":   row[0],
                "customer_no":    row[1],
                "po_number":      row[2] or row[0],
                "ship_to":        row[3],
                "order_date":     str(row[4]) if row[4] else "",
                "asn_sent":       bool(row[5]),
                "trading_partner": row[6],
            })
        conn.close()
        return rows
    except Exception as e:
        logger.debug("ERP SQL discovery failed: %s", e)
        return []


def _discover_from_edi_log() -> List[Dict]:
    """Query the EDI_Log table for recent transactions."""
    if not _PYODBC_AVAILABLE or not ERP_DB_HOST:
        return []
    try:
        conn   = pyodbc.connect(_erp_conn_str(), timeout=5)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DocType, Direction, RefNo, TradingPartner, Status, Timestamp, AckStatus
            FROM EDI_Log
            WHERE Timestamp > DATEADD(day, -7, GETDATE())
            ORDER BY Timestamp DESC
        """)
        rows = []
        for row in cursor.fetchall():
            rows.append({
                "doc_type":       row[0],
                "direction":      row[1],
                "ref_no":         row[2],
                "trading_partner": row[3],
                "status":         row[4],
                "timestamp":      str(row[5]) if row[5] else "",
                "ack_status":     row[6],
            })
        conn.close()
        return rows
    except Exception as e:
        logger.debug("EDI_Log discovery failed: %s", e)
        return []


def _discover_from_watchdog_json() -> List[Dict]:
    """Pull manually-registered watches from edi_watchdog.json."""
    if not WATCHDOG_JSON.exists():
        return []
    try:
        with open(WATCHDOG_JSON) as f:
            data = json.load(f)
        return [
            {
                "po_number":     v["po_number"],
                "customer_name": v.get("customer_name", ""),
                "doc_type":      v.get("doc_type", "850"),
                "status":        v.get("status", "watching"),
                "added_at":      v.get("added_at", ""),
            }
            for v in data.get("watches", {}).values()
        ]
    except Exception as e:
        logger.debug("Watchdog JSON read failed: %s", e)
        return []


# ── Pipeline sync ─────────────────────────────────────────────────────────────

def sync_pipeline() -> Dict[str, PipelineOrder]:
    """Merge all discovery sources into pipeline state. Returns updated orders."""
    with _state_lock:
        orders = _load_state()

        # 1. Manual watches (always authoritative for existence)
        for w in _discover_from_watchdog_json():
            po = w["po_number"]
            o  = _get_or_create_order(orders, po, w.get("customer_name", ""))
            if not o.events:
                o.add_event("850_received", "manual", {"doc_type": w.get("doc_type", "850")})

        # 2. ERP SQL open orders
        for row in _discover_from_erp_sql():
            po = row["po_number"]
            o  = _get_or_create_order(orders, po, row.get("customer_no", ""), row.get("trading_partner", ""))
            o.erp_order_no = row.get("erp_order_no", "")
            if "SO_created" not in [e.stage for e in o.events]:
                o.add_event("SO_created", "erp_sql", {
                    "erp_order_no": row["erp_order_no"],
                    "order_date":   row["order_date"],
                })
            if row.get("asn_sent") and "856_sent" not in [e.stage for e in o.events]:
                o.add_event("856_sent", "erp_sql", {"erp_order_no": row["erp_order_no"]})

        # 3. EDI_Log entries
        for row in _discover_from_edi_log():
            po = row.get("ref_no", "")
            if not po:
                continue
            o         = _get_or_create_order(orders, po, "", row.get("trading_partner", ""))
            doc_type  = row.get("doc_type", "")
            direction = row.get("direction", "")
            ack_status = row.get("ack_status", "")

            if doc_type == "850" and direction == "IN":
                if "850_received" not in [e.stage for e in o.events]:
                    o.add_event("850_received", "edi_log", row)
            elif doc_type in ("856", "ASN") and direction == "OUT":
                if "856_sent" not in [e.stage for e in o.events]:
                    o.add_event("856_sent", "edi_log", row)
            elif doc_type == "997" and direction == "IN":
                if "997_received" not in [e.stage for e in o.events]:
                    details = {**row, "accepted": ack_status not in ("R", "rejected", "E")}
                    o.add_event("997_received", "edi_log", details)
                    if ack_status in ("A", "accepted", ""):
                        o.add_event("complete", "edi_log", {"ack_status": ack_status})

        # 4. Check for stuck orders
        newly_stuck = []
        for po, o in orders.items():
            if o.status == "in_progress" and o.check_stuck():
                o.mark_stuck()
                newly_stuck.append(o)
                logger.warning("Order %s is STUCK: %s", po, o.stuck_transition)
            o.last_checked = _now_iso()

        _save_state(orders)

        if newly_stuck:
            _send_stuck_alerts(newly_stuck)

        return orders


def _send_stuck_alerts(stuck_orders: List[PipelineOrder]) -> None:
    if not DISCORD_WEBHOOK:
        return
    lines = ["⚠️ **EDI Pipeline: Stuck Orders Detected**\n"]
    for o in stuck_orders:
        lines.append(f"• {o.summary_line()}")
        lines.append(f"  → Blocked transition: `{o.stuck_transition}`")
        lines.append(f"  → Been in stage `{o.current_stage}` since {o.stuck_since}")
    message = "\n".join(lines)
    try:
        import urllib.request
        payload = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.error("Discord alert failed: %s", e)


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_pipeline_summary(orders: Optional[Dict[str, PipelineOrder]] = None) -> Dict:
    """Return a dashboard summary of the pipeline."""
    if orders is None:
        with _state_lock:
            orders = _load_state()

    total = len(orders)
    by_stage:  Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    stuck_list        = []
    error_list        = []
    recent_completions = []

    for po, o in orders.items():
        by_stage[o.current_stage]  = by_stage.get(o.current_stage, 0) + 1
        by_status[o.status]        = by_status.get(o.status, 0) + 1

        if o.status == "stuck":
            stuck_list.append({
                "po_number":       po,
                "customer_name":   o.customer_name,
                "current_stage":   o.current_stage,
                "stuck_transition": o.stuck_transition,
                "stuck_since":     o.stuck_since,
                "age_hours":       round(o.age_hours(), 1),
            })
        elif o.status == "error":
            error_list.append({"po_number": po, "customer_name": o.customer_name,
                                "age_hours": round(o.age_hours(), 1)})
        elif o.status == "complete" and o.resolved_at:
            t = _parse_iso(o.resolved_at)
            if t and (datetime.now(timezone.utc) - t).total_seconds() < 86400:
                recent_completions.append({"po_number": po, "resolved_at": o.resolved_at})

    return {
        "total_orders":             total,
        "by_stage":                 by_stage,
        "by_status":                by_status,
        "stuck_count":              len(stuck_list),
        "error_count":              len(error_list),
        "completed_24h":            len(recent_completions),
        "stuck_orders":             stuck_list,
        "error_orders":             error_list,
        "recent_completions":       recent_completions,
        "last_sync":                _now_iso(),
        "stuck_thresholds_minutes": STUCK_THRESHOLDS,
    }


# ── Manual operations ─────────────────────────────────────────────────────────

def add_order_to_pipeline(
    po_number:      str,
    customer_name:  str = "",
    trading_partner: str = "",
    doc_type:       str = "850",
    stage:          str = "850_received",
    notes:          str = "",
) -> Dict:
    """Manually register an order in the pipeline."""
    with _state_lock:
        orders = _load_state()
        o = _get_or_create_order(orders, po_number, customer_name, trading_partner)
        o.notes = notes
        if not any(e.stage == stage for e in o.events):
            o.add_event(stage, "manual", {"doc_type": doc_type})
        _save_state(orders)
    return {"ok": True, "po_number": po_number, "stage": o.current_stage, "status": o.status}


def advance_order_stage(
    po_number: str,
    stage:     str,
    source:    str = "manual",
    details:   Optional[Dict] = None,
) -> Dict:
    """Push an order to a specific stage (e.g. when platform confirms a step)."""
    if stage not in PIPELINE_STAGES:
        return {"ok": False, "error": f"Unknown stage '{stage}'. Valid: {PIPELINE_STAGES}"}
    with _state_lock:
        orders = _load_state()
        if po_number not in orders:
            return {"ok": False, "error": f"Order {po_number} not in pipeline"}
        o = orders[po_number]
        o.add_event(stage, source, details or {})
        if o.status == "stuck":
            o.status          = "in_progress"
            o.stuck_since     = None
            o.stuck_transition = None
        _save_state(orders)
    return {"ok": True, "po_number": po_number, "current_stage": o.current_stage, "status": o.status}


def resolve_order(po_number: str, note: str = "") -> Dict:
    """Manually resolve/close a pipeline order."""
    with _state_lock:
        orders = _load_state()
        if po_number not in orders:
            return {"ok": False, "error": f"Order {po_number} not found"}
        o = orders[po_number]
        o.add_event("complete", "manual", {"note": note})
        o.notes = (o.notes + f" | Resolved: {note}").strip(" |")
        _save_state(orders)
    return {"ok": True, "po_number": po_number, "resolved_at": o.resolved_at}


def get_order_detail(po_number: str) -> Dict:
    """Return full detail for a single order."""
    with _state_lock:
        orders = _load_state()
    if po_number not in orders:
        return {"ok": False, "error": f"Order {po_number} not found"}
    return {"ok": True, "order": orders[po_number].to_dict()}


# ── Background sync worker ────────────────────────────────────────────────────

_sync_running = False


def start_background_sync(interval_seconds: int = 300) -> None:
    """Start a background thread that syncs pipeline state every N seconds."""
    global _sync_running
    if _sync_running:
        return
    _sync_running = True

    def _worker():
        while _sync_running:
            try:
                orders  = sync_pipeline()
                in_prog = sum(1 for o in orders.values() if o.status == "in_progress")
                stuck   = sum(1 for o in orders.values() if o.status == "stuck")
                logger.info("Pipeline sync: %d total, %d in-progress, %d stuck",
                            len(orders), in_prog, stuck)
            except Exception as e:
                logger.error("Pipeline sync error: %s", e)
            time.sleep(interval_seconds)

    t = threading.Thread(target=_worker, daemon=True, name="pipeline-sync")
    t.start()
    logger.info("Pipeline background sync started (every %ds)", interval_seconds)


def stop_background_sync() -> None:
    global _sync_running
    _sync_running = False


# ── FastAPI route registration ────────────────────────────────────────────────

def register_pipeline_routes(app: Any) -> None:
    """Register /api/pipeline/* routes on an existing FastAPI app."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.get("/api/pipeline/summary", tags=["Pipeline"])
    async def pipeline_summary():
        """Pipeline dashboard: totals, stuck orders, recent completions."""
        orders = sync_pipeline()
        return JSONResponse(get_pipeline_summary(orders))

    @app.get("/api/pipeline/orders", tags=["Pipeline"])
    async def pipeline_list_orders(status: Optional[str] = None,
                                   stage:  Optional[str] = None):
        """List pipeline orders with optional status/stage filter."""
        with _state_lock:
            orders = _load_state()
        result = []
        for po, o in orders.items():
            if status and o.status != status:
                continue
            if stage and o.current_stage != stage:
                continue
            result.append({
                "po_number":       po,
                "customer_name":   o.customer_name,
                "trading_partner": o.trading_partner,
                "current_stage":   o.current_stage,
                "status":          o.status,
                "age_hours":       round(o.age_hours(), 1),
                "stuck_transition": o.stuck_transition,
                "added_at":        o.added_at,
            })
        return JSONResponse({"ok": True, "orders": result, "count": len(result)})

    @app.get("/api/pipeline/orders/{po_number}", tags=["Pipeline"])
    async def pipeline_order_detail(po_number: str):
        """Full detail for a single pipeline order."""
        return JSONResponse(get_order_detail(po_number))

    @app.post("/api/pipeline/orders", tags=["Pipeline"])
    async def pipeline_add_order(request: Request):
        """Manually register an order in the pipeline."""
        data = await request.json()
        po   = data.get("po_number", "").strip()
        if not po:
            return JSONResponse({"ok": False, "error": "po_number required"}, status_code=400)
        result = add_order_to_pipeline(
            po_number=po,
            customer_name=data.get("customer_name", ""),
            trading_partner=data.get("trading_partner", ""),
            doc_type=data.get("doc_type", "850"),
            stage=data.get("stage", "850_received"),
            notes=data.get("notes", ""),
        )
        return JSONResponse(result)

    @app.post("/api/pipeline/orders/{po_number}/advance", tags=["Pipeline"])
    async def pipeline_advance_stage(po_number: str, request: Request):
        """Advance a pipeline order to a specific stage."""
        data  = await request.json()
        stage = data.get("stage", "")
        if not stage:
            return JSONResponse({"ok": False, "error": "stage required"}, status_code=400)
        return JSONResponse(advance_order_stage(
            po_number, stage,
            source=data.get("source", "api"),
            details=data.get("details"),
        ))

    @app.post("/api/pipeline/orders/{po_number}/resolve", tags=["Pipeline"])
    async def pipeline_resolve_order(po_number: str, request: Request):
        """Manually resolve / close a pipeline order."""
        data = await request.json()
        return JSONResponse(resolve_order(po_number, data.get("note", "")))

    @app.post("/api/pipeline/sync", tags=["Pipeline"])
    async def pipeline_force_sync():
        """Trigger a manual pipeline discovery sync."""
        orders = sync_pipeline()
        return JSONResponse({**get_pipeline_summary(orders), "ok": True})

    @app.post("/api/pipeline/platform-event", tags=["Pipeline"])
    async def pipeline_platform_event(request: Request):
        """Webhook endpoint for EDI platforms (Orderful, Logicbroker, Tray.io) to
        advance pipeline stages.

        Expected JSON payload::

            {
              "po_number":       "GB25961",
              "stage":           "SO_created",  // or shipped / 856_sent / 997_received / complete
              "trading_partner": "BIGBOX",
              "details":         { ... }
            }
        """
        data  = await request.json()
        po    = data.get("po_number", "").strip()
        stage = data.get("stage", "").strip()
        if not po or not stage:
            return JSONResponse(
                {"ok": False, "error": "po_number and stage required"}, status_code=400
            )
        result = advance_order_stage(po, stage, source="platform", details=data.get("details"))
        logger.info("Platform event: %s → %s (%s)", po, stage, result.get("status"))
        return JSONResponse(result)

    start_background_sync()
    logger.info("Pipeline routes registered at /api/pipeline/*")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ── Public facade class ───────────────────────────────────────────────────────

class OrderPipeline:
    """Facade class for the FirstPass EDI order pipeline.

    Wraps module-level functions into a convenient class interface.
    Import this class for type-hinted usage and FastAPI route registration::

        from firstpass.pipeline.order_pipeline import OrderPipeline

        OrderPipeline.register_routes(app)
        summary = OrderPipeline.sync_and_summarize()
    """

    stages: List[str] = PIPELINE_STAGES
    stage_labels: Dict[str, str] = STAGE_LABELS

    @staticmethod
    def sync() -> Dict[str, PipelineOrder]:
        """Run a discovery sync and return the updated orders dict."""
        return sync_pipeline()

    @staticmethod
    def sync_and_summarize() -> Dict:
        """Sync, then return a summary dashboard dict."""
        orders = sync_pipeline()
        return get_pipeline_summary(orders)

    @staticmethod
    def get_summary(orders: Optional[Dict[str, PipelineOrder]] = None) -> Dict:
        """Return pipeline summary (uses cached state if orders is None)."""
        return get_pipeline_summary(orders)

    @staticmethod
    def add_order(po_number: str, customer_name: str = "", trading_partner: str = "",
                  doc_type: str = "850", stage: str = "850_received",
                  notes: str = "") -> Dict:
        return add_order_to_pipeline(po_number, customer_name, trading_partner,
                                     doc_type, stage, notes)

    @staticmethod
    def advance_stage(po_number: str, stage: str, source: str = "api",
                      details: Optional[Dict] = None) -> Dict:
        return advance_order_stage(po_number, stage, source, details)

    @staticmethod
    def resolve(po_number: str, note: str = "") -> Dict:
        return resolve_order(po_number, note)

    @staticmethod
    def get_order(po_number: str) -> Dict:
        return get_order_detail(po_number)

    @staticmethod
    def start_sync_worker(interval_seconds: int = 300) -> None:
        start_background_sync(interval_seconds)

    @staticmethod
    def stop_sync_worker() -> None:
        stop_background_sync()

    @staticmethod
    def register_routes(app: Any) -> None:
        """Register all /api/pipeline/* routes on a FastAPI app."""
        register_pipeline_routes(app)
