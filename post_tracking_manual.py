"""
post_tracking_manual.py
========================
Liest tracking_manual.csv (ean, tracking) und:
  1. Löst EANs -> BAB-SKUs auf (aus dem Lieferanten-Feed)
  2. Sucht passende eBay-Bestellungen via Fulfillment API
  3. Postet Tracking + markiert Bestellung als versendet

Aufruf:
  python post_tracking_manual.py                        # Shop 2 (default)
  python post_tracking_manual.py --config config.yaml  # Shop 1
  python post_tracking_manual.py --dry-run             # Test
"""
from __future__ import annotations
import argparse, base64, csv, io, json, logging, os, sys
from datetime import datetime, timezone
from pathlib import Path
import yaml
from dotenv import load_dotenv
from ebay_client import EbayClient

log = logging.getLogger("post_tracking_manual")
FULFILLMENT_PATH = "/sell/fulfillment/v1/order"
TRACKING_FILE    = "tracking_manual.csv"

def detect_carrier(t):
    if t.startswith("1Z"): return "UPS"
    if t.startswith("JD") or t.startswith("00"): return "DHL"
    if len(t)==14 and t.isdigit(): return "DPD"
    return "DHL"

def load_ean_to_sku(cfg):
    import urllib.request
    c = cfg.get("csv", {})
    url = c.get("url","")
    user = os.environ.get("CSV_HTTP_USER","")
    pw   = os.environ.get("CSV_HTTP_PASSWORD","")
    enc  = c.get("encoding","utf-8-sig")
    delim= c.get("delimiter",";")
    cols = c.get("columns",{})
    sku_col = cols.get("sku","ItemNo")
    ean_col = cols.get("ean","GTIN")
    log.info(f"Lade BAB-Feed …")
    req = urllib.request.Request(url)
    if user and pw:
        req.add_header("Authorization","Basic "+base64.b64encode(f"{user}:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode(enc, errors="replace")
    mapping = {}
    for row in csv.DictReader(io.StringIO(raw), delimiter=delim):
        ean = (row.get(ean_col) or "").strip()
        sku = (row.get(sku_col) or "").strip()
        if ean and sku:
            mapping[ean] = sku
    log.info(f"Feed: {len(mapping)} EANs")
    return mapping

def fetch_all_orders(client):
    orders, offset, limit = [], 0, 50
    while True:
        try:
            r = client._request("GET", FULFILLMENT_PATH, params={"limit":limit,"offset":offset})
        except Exception as e:
            log.error(f"eBay API: {e}"); break
        batch = (r or {}).get("orders",[])
        if not batch: break
        orders.extend(batch)
        offset += limit
        if offset >= (r or {}).get("total",0): break
    log.info(f"{len(orders)} eBay-Bestellungen geladen")
    return orders

def find_order(orders, sku):
    """Gibt (order_id, line_item_ids) zurück oder (None, [])."""
    for o in orders:
        for item in o.get("lineItems", []):
            if (item.get("sku") or "").strip().upper() == sku.upper():
                # Alle lineItemIds dieser Bestellung sammeln
                line_item_ids = [
                    {"lineItemId": i.get("lineItemId"), "quantity": i.get("quantity", 1)}
                    for i in o.get("lineItems", [])
                    if i.get("lineItemId")
                ]
                return o.get("orderId"), line_item_ids
    return None, []

def post_tracking(client, order_id, line_item_ids, tracking, dry_run=False):
    carrier = detect_carrier(tracking)
    payload = {
        "lineItems": line_item_ids,  # echte lineItemIds aus der Bestellung
        "shippedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "shippingCarrierCode": carrier,
        "trackingNumber": tracking,
    }
    log.info(f"  POST tracking: {order_id} | {carrier} {tracking}")
    if dry_run:
        log.warning(f"  DRY-RUN: {json.dumps(payload)}")
        return True
    try:
        client._request("POST", f"{FULFILLMENT_PATH}/{order_id}/shipping_fulfillment", json_body=payload)
        log.info(f"  OK: Bestellung {order_id} als versendet markiert")
        return True
    except RuntimeError as e:
        log.error(f"  FEHLER {order_id}: {e}")
        return False

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config_shop2.yaml")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--file", default=TRACKING_FILE)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)])
    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    if not cfg.get("ebay",{}).get("enabled", False):
        log.error(f"eBay nicht aktiviert in {args.config}"); sys.exit(1)

    entries = []
    with open(args.file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ean = (row.get("ean") or "").strip()
            trk = (row.get("tracking") or "").strip()
            if ean and trk: entries.append({"ean":ean,"tracking":trk})
    log.info(f"{len(entries)} Eintraege in {args.file}")

    ean_to_sku = load_ean_to_sku(cfg)
    client = EbayClient.from_env(cfg.get("ebay",{}))
    orders = fetch_all_orders(client)

    ok = fail = 0
    for e in entries:
        ean, trk = e["ean"], e["tracking"]
        sku = ean_to_sku.get(ean,"")
        if not sku:
            log.warning(f"EAN {ean} nicht im Feed — suche direkt in eBay-Bestellungen")
            # Fallback: suche Bestellung nach EAN direkt in lineItems
            order_id, line_item_ids = None, []
            for o in orders:
                for item in o.get("lineItems", []):
                    item_ean = (item.get("sku") or "")
                    # eBay speichert manchmal EAN als SKU oder im Titel
                    if ean in str(item):
                        line_item_ids = [
                            {"lineItemId": i.get("lineItemId"), "quantity": i.get("quantity",1)}
                            for i in o.get("lineItems",[]) if i.get("lineItemId")
                        ]
                        order_id = o.get("orderId")
                        log.info(f"  Bestellung via EAN-Suche gefunden: {order_id}")
                        break
                if order_id:
                    break
            if not order_id:
                log.warning(f"EAN {ean} auch in eBay-Bestellungen nicht gefunden"); fail+=1; continue
            if post_tracking(client, order_id, line_item_ids, trk, dry_run=args.dry_run): ok+=1
            else: fail+=1
            continue
        log.info(f"EAN {ean} -> SKU {sku}")
        order_id, line_item_ids = find_order(orders, sku)
        if not order_id:
            log.warning(f"Keine eBay-Bestellung fuer SKU {sku}"); fail+=1; continue
        if post_tracking(client, order_id, line_item_ids, trk, dry_run=args.dry_run): ok+=1
        else: fail+=1

    log.info(f"\n{'─'*50}")
    log.info(f"Versendet: {ok}  Fehler: {fail}")

if __name__ == "__main__":
    main()
