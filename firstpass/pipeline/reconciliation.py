"""
reconciliation.py — EDI Order Reconciliation Engine for FirstPass EDI.

Cross-references three sources of truth to build a per-order status view:

  1. ERP (e.g. Sage 100)  — open SO headers + closed history
  2. ShipStation           — has the order shipped? when? tracking?
  3. Orderful              — was 856 ASN sent? was 810 invoice sent? any errors?

Join key: ShipStation orderNumber == ERP SalesOrderNo

Reconcile stages per order:
  open        — in ERP open orders, not yet shipped in SS
  shipped     — SS orderStatus=shipped, has trackingNumber
  asn_sent    — Orderful 856 transaction exists for this PO
  asn_pending — shipped in SS but no 856 found in Orderful yet
  invoiced    — Orderful 810 transaction exists OR ERP AR invoice
  inv_pending — ASN sent but no 810 found
  closed      — in ERP order history (fully closed)
  stuck       — any stage overdue per SLA thresholds (flagged for alert)

Middleware routing by customer number → partner config:
  See partner_customer_map.json (editable without code changes).

Usage:
  python -m firstpass.pipeline.reconciliation --dry-run    # print results, no push
  python -m firstpass.pipeline.reconciliation --days 7

Source lineage: edi_reconcile.py (ceo-bot / MorningTaskBot)
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("firstpass.pipeline.reconciliation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_DIR   = Path(__file__).parent.parent.parent
CACHE_FILE = Path(os.getenv("RECONCILE_CACHE_FILE",
                             str(BASE_DIR / "edi_reconcile_cache.json")))
MAP_FILE   = Path(os.getenv("PARTNER_MAP_FILE",
                             str(BASE_DIR / "partner_customer_map.json")))

# ── SLA thresholds (hours) ────────────────────────────────────────────────────
SLA_SHIP_HOURS    = float(os.getenv("RECONCILE_SLA_SHIP_HOURS",    "48"))   # open → shipped
SLA_ASN_HOURS     = float(os.getenv("RECONCILE_SLA_ASN_HOURS",     "6"))    # shipped → 856
SLA_INVOICE_HOURS = float(os.getenv("RECONCILE_SLA_INVOICE_HOURS", "24"))   # ASN → 810

# ── Credentials (inherit from config if available, fall back to env vars) ─────
try:
    from ..config import config as _fp_config
    SS_KEY       = _fp_config.SHIPSTATION_API_KEY    or os.getenv("SHIPSTATION_API_KEY",    "")
    SS_SECRET    = _fp_config.SHIPSTATION_API_SECRET or os.getenv("SHIPSTATION_API_SECRET", "")
    ORDERFUL_KEY = _fp_config.ORDERFUL_API_KEY       or os.getenv("ORDERFUL_API_KEY",       "")
    SUPABASE_URL = _fp_config.SUPABASE_URL           or os.getenv("SUPABASE_URL",           "")
    SUPABASE_KEY = _fp_config.SUPABASE_KEY           or os.getenv("SUPABASE_KEY",           "")
except ImportError:
    SS_KEY       = os.getenv("SHIPSTATION_API_KEY",    "")
    SS_SECRET    = os.getenv("SHIPSTATION_API_SECRET", "")
    ORDERFUL_KEY = os.getenv("ORDERFUL_API_KEY",       "")
    SUPABASE_URL = os.getenv("SUPABASE_URL",           "")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY",           "")

# ERP database connection
ERP_DB_HOST = os.getenv("ERP_DB_HOST", os.getenv("DB_HOST", ""))
ERP_DB_NAME = os.getenv("ERP_DB_NAME", os.getenv("DB_NAME", "ERP_DB"))
ERP_DB_USER = os.getenv("ERP_DB_USER", os.getenv("DB_USER", ""))
ERP_DB_PASS = os.getenv("ERP_DB_PASS", os.getenv("DB_PASSWORD", ""))


# ─────────────────────────────────────────────────────────────────────────────
# Partner / customer map
# ─────────────────────────────────────────────────────────────────────────────

def _load_partner_map() -> Dict:
    """Load CustomerNo → partner config from partner_customer_map.json."""
    if MAP_FILE.exists():
        try:
            return json.loads(MAP_FILE.read_text())
        except Exception as e:
            logger.warning("Could not load partner map: %s", e)
    return {}


def _partner_for_customer(customer_no: str, partner_map: Dict) -> Dict:
    """Return partner config for a CustomerNo, or a default unknown entry."""
    return partner_map.get(customer_no.strip(), {
        "name":       f"Unknown ({customer_no.strip()})",
        "middleware": "unknown",
        "channel":    None,
    })


# ─────────────────────────────────────────────────────────────────────────────
# ERP SQL
# ─────────────────────────────────────────────────────────────────────────────

def _erp_conn():
    import pyodbc
    if ERP_DB_USER:
        cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={ERP_DB_HOST};"
              f"DATABASE={ERP_DB_NAME};UID={ERP_DB_USER};PWD={ERP_DB_PASS};"
              "TrustServerCertificate=yes")
    else:
        cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={ERP_DB_HOST};"
              f"DATABASE={ERP_DB_NAME};Trusted_Connection=yes;TrustServerCertificate=yes")
    return pyodbc.connect(cs, timeout=15)


def _fetch_open_orders(customer_no: Optional[str] = None) -> List[Dict]:
    """Pull open orders from SO_SalesOrderHeader, optionally filtered by CustomerNo."""
    where  = "WHERE h.CustomerNo = ?" if customer_no else ""
    params = [customer_no] if customer_no else []
    sql = f"""
        SELECT
            h.SalesOrderNo,
            h.CustomerPONo,
            h.CustomerNo,
            CONVERT(varchar, h.OrderDate, 23)   AS OrderDate,
            h.OrderStatus,
            h.OrderType,
            h.ShipToName,
            h.ShipToCity,
            h.ShipToState,
            ISNULL(h.TaxableAmt, 0) + ISNULL(h.NonTaxableAmt, 0) AS OrderTotal
        FROM SO_SalesOrderHeader h
        {where}
        ORDER BY h.OrderDate DESC
    """
    try:
        conn = _erp_conn()
        cur  = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        conn.close()
        logger.info("ERP open orders: %d", len(rows))
        return rows
    except Exception as e:
        logger.error("ERP open orders query failed: %s", e)
        return []


def _fetch_recently_closed(days: int = 14, customer_no: Optional[str] = None) -> List[Dict]:
    """Pull orders that closed in the last N days."""
    cust_filter = "AND h.CustomerNo = ?" if customer_no else ""
    params      = [-days, customer_no] if customer_no else [-days]
    sql = f"""
        SELECT
            h.SalesOrderNo,
            h.CustomerPONo,
            h.CustomerNo,
            CONVERT(varchar, h.OrderDate, 23)    AS OrderDate,
            h.OrderStatus,
            h.ShipToName,
            ISNULL(h.TaxableAmt, 0) + ISNULL(h.NonTaxableAmt, 0) AS OrderTotal
        FROM SO_SalesOrderHistoryHeader h
        WHERE h.OrderDate >= DATEADD(day, ?, GETDATE()) {cust_filter}
        ORDER BY h.OrderDate DESC
    """
    try:
        conn = _erp_conn()
        cur  = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        conn.close()
        logger.info("ERP recently closed orders (last %dd): %d", days, len(rows))
        return rows
    except Exception as e:
        logger.error("ERP closed orders query failed: %s", e)
        return []


def _fetch_invoiced_orders(sales_order_nos: List[str]) -> Set[str]:
    """Return set of SalesOrderNo that have an invoice in ERP AR history."""
    if not sales_order_nos:
        return set()
    chunk_size = 200
    invoiced: Set[str] = set()
    try:
        conn = _erp_conn()
        cur  = conn.cursor()
        for i in range(0, len(sales_order_nos), chunk_size):
            chunk = sales_order_nos[i:i + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            cur.execute(
                f"SELECT SalesOrderNo FROM AR_InvoiceHistoryHeader "
                f"WHERE SalesOrderNo IN ({placeholders})",
                chunk,
            )
            for row in cur.fetchall():
                invoiced.add(row[0].strip())
        conn.close()
    except Exception as e:
        logger.error("Invoice history query failed: %s", e)
    return invoiced


# ─────────────────────────────────────────────────────────────────────────────
# ShipStation
# ─────────────────────────────────────────────────────────────────────────────

def _ss_headers() -> Dict:
    auth = base64.b64encode(f"{SS_KEY}:{SS_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}


def _ss_get(path: str, params: Optional[Dict] = None) -> Dict:
    url = f"https://ssapi.shipstation.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_ss_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = int(e.headers.get("Retry-After", 30))
            logger.warning("SS rate limit — sleeping %ds", retry)
            time.sleep(retry)
            return _ss_get(path, params)
        logger.error("SS request failed %s: %s", path, e)
        return {}
    except Exception as e:
        logger.error("SS request error %s: %s", path, e)
        return {}


def _fetch_ss_shipments(lookback_days: int = 30) -> Dict[str, Dict]:
    """Pull all shipments from ShipStation in the lookback window.

    Returns a dict keyed by orderNumber → most-recent shipment info.
    """
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    shipments: Dict[str, Dict] = {}
    page = 1

    while True:
        data  = _ss_get("/shipments", {"shipDateStart": start, "pageSize": 500, "page": page,
                                        "includeShipmentItems": False})
        batch = data.get("shipments", [])
        if not batch:
            break

        for s in batch:
            order_no = (s.get("orderNumber") or "").strip()
            if not order_no:
                continue
            existing = shipments.get(order_no)
            if not existing or (s.get("shipDate") or "") > (existing.get("ss_ship_date") or ""):
                shipments[order_no] = {
                    "ss_shipment_id":  s.get("shipmentId"),
                    "ss_order_id":     s.get("orderId"),
                    "ss_order_status": "shipped",
                    "ss_ship_date":    s.get("shipDate"),
                    "ss_tracking":     s.get("trackingNumber"),
                    "ss_carrier":      s.get("carrierCode"),
                    "ss_service":      s.get("serviceCode"),
                    "ss_voided":       s.get("voided", False),
                }

        total    = data.get("total", 0)
        per_page = data.get("pageSize") or 500
        if page * per_page >= total:
            break
        page += 1

    # Drop voided shipments
    shipments = {k: v for k, v in shipments.items() if not v.get("ss_voided")}
    logger.info("ShipStation shipments loaded (last %dd): %d", lookback_days, len(shipments))
    return shipments


def _fetch_ss_open_orders(sales_order_nos: List[str]) -> Dict[str, str]:
    """Check ShipStation for awaiting_shipment status for open ERP orders."""
    if not sales_order_nos:
        return {}
    sage_set = {n.strip() for n in sales_order_nos}
    found: Dict[str, str] = {}
    page = 1

    while True:
        data   = _ss_get("/orders", {"orderStatus": "awaiting_shipment", "pageSize": 500, "page": page})
        orders = data.get("orders", [])
        if not orders:
            break
        for o in orders:
            order_no = (o.get("orderNumber") or "").strip()
            if order_no in sage_set:
                found[order_no] = o.get("orderStatus", "awaiting_shipment")
        total    = data.get("total", 0)
        per_page = data.get("pageSize") or 500
        if page * per_page >= total:
            break
        page += 1

    return found


# ─────────────────────────────────────────────────────────────────────────────
# Orderful
# ─────────────────────────────────────────────────────────────────────────────

def _orderful_headers() -> Dict:
    return {
        "Authorization": f"Bearer {ORDERFUL_KEY}",
        "Content-Type":  "application/json",
    }


def _orderful_get(path: str, params: Optional[Dict] = None) -> Dict:
    url = f"https://api.orderful.com/v3{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_orderful_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.debug("Orderful %s: %s", path, e)
        return {}


def _fetch_orderful_sent_docs(lookback_days: int = 30) -> Dict[str, Dict]:
    """Pull recent outbound transactions from Orderful (856 ASN + 810 Invoice).

    Returns dict keyed by PO/reference number:
      { asn_sent, asn_ts, invoice_sent, invoice_ts, orderful_errors }
    """
    since  = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00Z")
    result: Dict[str, Dict] = {}

    for doc_type in ["856", "810"]:
        page = 1
        while True:
            data = _orderful_get("/transactions", {
                "documentType": doc_type,
                "direction":    "outbound",
                "createdAfter": since,
                "limit":        200,
                "page":         page,
            })
            txs = data.get("data") or data.get("transactions") or []
            if not txs:
                break

            for tx in txs:
                po = _extract_po_from_orderful_tx(tx)
                if not po:
                    continue
                po    = po.strip()
                entry = result.setdefault(po, {
                    "asn_sent":        False,
                    "asn_ts":          None,
                    "invoice_sent":    False,
                    "invoice_ts":      None,
                    "orderful_errors": [],
                })
                status = (tx.get("status") or "").lower()
                ts     = tx.get("createdAt") or tx.get("updatedAt")

                if doc_type == "856":
                    entry["asn_sent"] = True
                    entry["asn_ts"]   = ts
                    if status in ("error", "rejected", "failed"):
                        entry["orderful_errors"].append(f"856 {status}")
                elif doc_type == "810":
                    entry["invoice_sent"] = True
                    entry["invoice_ts"]   = ts
                    if status in ("error", "rejected", "failed"):
                        entry["orderful_errors"].append(f"810 {status}")

            meta  = data.get("meta") or {}
            total = meta.get("total") or data.get("total") or 0
            if not total or page * 200 >= total:
                break
            page += 1

    logger.info("Orderful sent docs indexed: %d POs", len(result))
    return result


def _extract_po_from_orderful_tx(tx: Dict) -> Optional[str]:
    """Try to extract the customer PO number from an Orderful transaction."""
    parsed = tx.get("parsedPayload") or tx.get("payload") or {}
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except Exception:
            parsed = {}

    for key in ["purchaseOrderNumber", "purchase_order_number", "poNumber",
                "customerPoNumber", "referenceNumber", "orderNumber"]:
        val = parsed.get(key) or tx.get(key)
        if val:
            return str(val)

    refs = parsed.get("references") or []
    for ref in refs:
        if isinstance(ref, dict) and ref.get("type") in ("PO", "BM", "CR"):
            return ref.get("value") or ref.get("number")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Supabase push
# ─────────────────────────────────────────────────────────────────────────────

def _supabase_upsert(table: str, rows: List[Dict]) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase not configured — skipping push")
        return False
    if not rows:
        return True
    url  = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(rows).encode()
    req  = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            logger.info("Supabase upsert %s: %d rows → %s", table, len(rows), r.status)
            return True
    except Exception as e:
        logger.error("Supabase upsert failed: %s", e)
        return False


def _supabase_set_snapshot(snapshot: Dict) -> bool:
    """Store the full reconcile snapshot as a shared_context row."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    row = {
        "key":        "edi_reconcile_snapshot",
        "value":      json.dumps(snapshot),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return _supabase_upsert("shared_context", [row])


# ─────────────────────────────────────────────────────────────────────────────
# Core reconcile logic
# ─────────────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hours_since(ts_str: Optional[str]) -> Optional[float]:
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now_utc() - ts).total_seconds() / 3600
    except Exception:
        return None


def _hours_since_date(date_str: Optional[str]) -> Optional[float]:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (_now_utc() - d).total_seconds() / 3600
    except Exception:
        return None


def _classify_stage(
    order:               Dict,
    ss_shipped:          Optional[Dict],
    orderful_docs:       Optional[Dict],
    is_invoiced_in_erp:  bool,
    is_closed:           bool,
) -> tuple:
    """Determine the reconcile stage and any stuck flags for one order.

    Returns: (stage: str, flags: list[str])
    """
    flags: List[str] = []

    if is_closed:
        return "closed", []

    order_date = order.get("OrderDate") or order.get("order_date")
    age_h      = _hours_since_date(order_date)

    if not ss_shipped:
        stage      = "open"
        middleware = order.get("_middleware", "unknown")
        if age_h and age_h > SLA_SHIP_HOURS and middleware != "unknown":
            flags.append(f"open {age_h:.0f}h (SLA {SLA_SHIP_HOURS:.0f}h)")
        return stage, flags

    ship_ts  = ss_shipped.get("ss_ship_date")
    ship_age = _hours_since(ship_ts) if ship_ts else age_h

    if not orderful_docs:
        middleware = order.get("_middleware", "unknown")
        if middleware == "orderful":
            stage = "asn_pending"
            if ship_age and ship_age > SLA_ASN_HOURS:
                flags.append(f"shipped {ship_age:.0f}h, no 856 yet (SLA {SLA_ASN_HOURS:.0f}h)")
        else:
            stage = "shipped"
        return stage, flags

    asn_sent     = orderful_docs.get("asn_sent", False)
    invoice_sent = orderful_docs.get("invoice_sent", False)
    asn_ts       = orderful_docs.get("asn_ts")
    asn_age      = _hours_since(asn_ts) if asn_ts else None
    errors       = orderful_docs.get("orderful_errors", [])

    if errors:
        flags.extend([f"Orderful error: {e}" for e in errors])

    if not asn_sent:
        stage = "asn_pending"
        if ship_age and ship_age > SLA_ASN_HOURS:
            flags.append(f"shipped {ship_age:.0f}h, no 856 sent (SLA {SLA_ASN_HOURS:.0f}h)")
        return stage, flags

    if not invoice_sent and not is_invoiced_in_erp:
        stage = "inv_pending"
        if asn_age and asn_age > SLA_INVOICE_HOURS:
            flags.append(f"856 sent {asn_age:.0f}h ago, no 810 yet (SLA {SLA_INVOICE_HOURS:.0f}h)")
        return stage, flags

    if invoice_sent or is_invoiced_in_erp:
        return "invoiced", flags

    return "asn_sent", flags


def run_reconcile(
    lookback_days: int = 30,
    dry_run:       bool = False,
    customer_no:   Optional[str] = None,
) -> Dict:
    """Main reconcile run. Returns full snapshot dict."""
    logger.info(
        "Starting EDI reconcile (lookback=%dd, dry_run=%s, customer=%s)",
        lookback_days, dry_run, customer_no or "ALL",
    )
    partner_map = _load_partner_map()
    run_ts      = _now_utc().isoformat()

    # Pull data from all sources
    open_orders   = _fetch_open_orders(customer_no=customer_no)
    closed_orders = _fetch_recently_closed(days=lookback_days, customer_no=customer_no)

    open_order_nos  = [o["SalesOrderNo"].strip() for o in open_orders]
    closed_order_nos = {o["SalesOrderNo"].strip() for o in closed_orders}

    ss_shipments  = _fetch_ss_shipments(lookback_days=lookback_days)
    orderful_docs = _fetch_orderful_sent_docs(lookback_days=lookback_days)
    invoiced_erp  = _fetch_invoiced_orders(open_order_nos)

    # Build per-order records
    records: List[Dict] = []

    for o in open_orders:
        so_no      = o["SalesOrderNo"].strip()
        cust_no    = (o.get("CustomerNo") or "").strip()
        po_no      = (o.get("CustomerPONo") or "").strip()
        partner    = _partner_for_customer(cust_no, partner_map)
        middleware = partner.get("middleware", "unknown")

        ss_data   = ss_shipments.get(so_no) or ss_shipments.get(po_no)
        oful_key  = po_no or so_no
        oful_docs = orderful_docs.get(oful_key) if middleware == "orderful" else None

        stage, flags = _classify_stage(
            order={**o, "_middleware": middleware},
            ss_shipped=ss_data,
            orderful_docs=oful_docs,
            is_invoiced_in_erp=(so_no in invoiced_erp),
            is_closed=False,
        )

        records.append({
            "sales_order_no":    so_no,
            "customer_po_no":    po_no,
            "customer_no":       cust_no,
            "partner":           partner.get("name", cust_no),
            "middleware":        middleware,
            "order_date":        o.get("OrderDate"),
            "ship_to":           (o.get("ShipToName") or "").strip(),
            "order_total":       float(o.get("OrderTotal") or 0),
            "erp_status":        "open",
            "stage":             stage,
            "flags":             flags,
            "is_stuck":          len(flags) > 0,
            "ss_shipped":        bool(ss_data),
            "ss_ship_date":      (ss_data or {}).get("ss_ship_date"),
            "ss_tracking":       (ss_data or {}).get("ss_tracking"),
            "asn_sent":          bool(oful_docs and oful_docs.get("asn_sent")),
            "asn_ts":            (oful_docs or {}).get("asn_ts"),
            "invoice_sent":      bool(oful_docs and oful_docs.get("invoice_sent")),
            "invoice_ts":        (oful_docs or {}).get("invoice_ts"),
            "invoiced_in_erp":   so_no in invoiced_erp,
            "updated_at":        run_ts,
        })

    for o in closed_orders:
        so_no   = o["SalesOrderNo"].strip()
        cust_no = (o.get("CustomerNo") or "").strip()
        partner = _partner_for_customer(cust_no, partner_map)
        records.append({
            "sales_order_no":  so_no,
            "customer_po_no":  (o.get("CustomerPONo") or "").strip(),
            "customer_no":     cust_no,
            "partner":         partner.get("name", cust_no),
            "middleware":      partner.get("middleware", "unknown"),
            "order_date":      o.get("OrderDate"),
            "ship_to":         (o.get("ShipToName") or "").strip(),
            "order_total":     float(o.get("OrderTotal") or 0),
            "erp_status":      "closed",
            "stage":           "closed",
            "flags":           [],
            "is_stuck":        False,
            "ss_shipped":      None,
            "ss_ship_date":    None,
            "ss_tracking":     None,
            "asn_sent":        None,
            "asn_ts":          None,
            "invoice_sent":    None,
            "invoice_ts":      None,
            "invoiced_in_erp": True,
            "updated_at":      run_ts,
        })

    # Summaries
    stage_counts:   Dict[str, int] = {}
    partner_summary: Dict[str, Dict] = {}
    stuck:          List[Dict] = []

    for r in records:
        s  = r["stage"]
        p  = r["partner"]
        stage_counts[s] = stage_counts.get(s, 0) + 1
        if p not in partner_summary:
            partner_summary[p] = {
                "open": 0, "shipped": 0, "asn_sent": 0,
                "asn_pending": 0, "invoiced": 0, "inv_pending": 0,
                "closed": 0, "stuck": 0,
            }
        partner_summary[p][s] = partner_summary[p].get(s, 0) + 1
        if r["is_stuck"]:
            partner_summary[p]["stuck"] += 1
            stuck.append({
                "sales_order_no": r["sales_order_no"],
                "partner":        r["partner"],
                "stage":          r["stage"],
                "flags":          r["flags"],
                "order_date":     r["order_date"],
            })

    snapshot = {
        "run_ts":          run_ts,
        "lookback_days":   lookback_days,
        "total_orders":    len(records),
        "stage_counts":    stage_counts,
        "stuck_count":     len(stuck),
        "stuck_orders":    stuck,
        "partner_summary": partner_summary,
        "orders":          records,
    }

    if dry_run:
        print(json.dumps({k: v for k, v in snapshot.items() if k != "orders"}, indent=2))
        print(f"\n[dry-run] {len(records)} orders, {len(stuck)} stuck")
        for r in stuck:
            print(f"  ⚠️  {r['sales_order_no']} ({r['partner']}) — {r['stage']} — {r['flags']}")
    else:
        CACHE_FILE.write_text(json.dumps(snapshot, indent=2))
        _supabase_set_snapshot(snapshot)
        logger.info("Reconcile complete: %d orders, %d stuck", len(records), len(stuck))

    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI route registration
# ─────────────────────────────────────────────────────────────────────────────

def register_reconcile_routes(app: Any) -> None:
    """Register /api/edi/reconcile/* routes on a FastAPI app."""
    try:
        from fastapi import HTTPException
        from fastapi.responses import JSONResponse
    except ImportError:
        logger.error("FastAPI not installed — reconcile routes not registered")
        return

    @app.get("/api/edi/reconcile", tags=["EDI"])
    def get_reconcile():
        """Return the latest reconcile snapshot from local cache."""
        if CACHE_FILE.exists():
            try:
                data = json.loads(CACHE_FILE.read_text())
                return JSONResponse(content=data)
            except Exception as e:
                raise HTTPException(500, f"Cache read error: {e}")
        raise HTTPException(
            404,
            "No reconcile snapshot yet — trigger POST /api/edi/reconcile/run first",
        )

    @app.get("/api/edi/reconcile/order/{sales_order_no}", tags=["EDI"])
    def get_order_reconcile(sales_order_no: str):
        """Return reconcile status for a single sales order."""
        if not CACHE_FILE.exists():
            raise HTTPException(404, "No snapshot yet")
        data = json.loads(CACHE_FILE.read_text())
        for order in data.get("orders", []):
            if order.get("sales_order_no", "").strip().upper() == sales_order_no.upper():
                return JSONResponse(content=order)
        raise HTTPException(404, f"Order {sales_order_no} not in snapshot")

    @app.post("/api/edi/reconcile/run", tags=["EDI"])
    def trigger_reconcile(days: int = 30):
        """Trigger a fresh reconcile run (runs inline — consider async for large datasets)."""
        snapshot = run_reconcile(lookback_days=days, dry_run=False)
        return JSONResponse(content={
            "status":       "ok",
            "run_ts":       snapshot["run_ts"],
            "total_orders": snapshot["total_orders"],
            "stuck_count":  snapshot["stuck_count"],
        })


# ─────────────────────────────────────────────────────────────────────────────
# Public facade class
# ─────────────────────────────────────────────────────────────────────────────

class Reconciler:
    """Facade class for the FirstPass EDI reconciliation engine.

    Wraps module-level functions into a convenient class interface::

        from firstpass.pipeline.reconciliation import Reconciler

        # Run a full reconcile and push to Supabase
        snapshot = Reconciler.run(lookback_days=30)

        # Get the cached snapshot (from last run)
        snapshot = Reconciler.get_snapshot()

        # Register dashboard FastAPI routes
        Reconciler.register_routes(app)
    """

    @staticmethod
    def run(
        lookback_days: int = 30,
        dry_run:       bool = False,
        customer_no:   Optional[str] = None,
    ) -> Dict:
        """Run a full cross-system reconcile. Returns the snapshot dict."""
        return run_reconcile(lookback_days=lookback_days, dry_run=dry_run,
                             customer_no=customer_no)

    @staticmethod
    def get_snapshot() -> Optional[Dict]:
        """Return the most recent cached reconcile snapshot, or None."""
        if CACHE_FILE.exists():
            try:
                return json.loads(CACHE_FILE.read_text())
            except Exception:
                pass
        return None

    @staticmethod
    def load_partner_map() -> Dict:
        """Load and return the customer-number → partner config map."""
        return _load_partner_map()

    @staticmethod
    def register_routes(app: Any) -> None:
        """Register /api/edi/reconcile/* routes on a FastAPI app."""
        register_reconcile_routes(app)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FirstPass EDI Order Reconciliation Engine")
    parser.add_argument("--dry-run",     action="store_true", help="Print results, do not push")
    parser.add_argument("--days",        type=int, default=30, help="Lookback days (default 30)")
    parser.add_argument("--customer-no", type=str, default=None,
                        help="Scope to one ERP CustomerNo (much faster)")
    args = parser.parse_args()

    run_reconcile(
        lookback_days=args.days,
        dry_run=args.dry_run,
        customer_no=args.customer_no,
    )
