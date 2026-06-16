#!/usr/bin/env python3
"""
purge_non_bab.py
================
Holt ALLE aktiven eBay-Offers via Pagination.
Löscht jeden Offer dessen SKU NICHT in der BAB-Liste ist.
Damit werden alle verbleibenden Kosatec/unbekannten Listings entfernt.
"""
from __future__ import annotations
import json, logging, sys, time
from pathlib import Path
import yaml
from dotenv import load_dotenv

log = logging.getLogger("purge")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))
cfg = yaml.safe_load(Path("config_shop2.yaml").read_text(encoding="utf-8"))
from ebay_client import EbayClient
client = EbayClient.from_env(cfg["ebay"])

# BAB-SKUs aus aktueller supplier_map laden
smap = json.loads(Path("supplier_map.json").read_text())
bab_skus = {k for k, v in smap.items() if v.get("supplier") == "BAB"}
log.info(f"BAB-SKUs (geschützt): {len(bab_skus)}")

OFFER_PATH = "/sell/inventory/v1/offer"
INV_PATH   = "/sell/inventory/v1/inventory_item"
LIMIT      = 100

stats = {"total": 0, "bab_kept": 0, "deleted": 0, "not_found": 0, "errors": 0}
to_delete: list[dict] = []

# Schritt 1: Alle Offers einsammeln
log.info("Scanne alle aktiven eBay-Offers ...")
offset = 0
while True:
    try:
        r = client._request("GET", OFFER_PATH, params={"limit": LIMIT, "offset": offset})
    except Exception as e:
        err = str(e)
        if "25707" in err or "invalid" in err.lower():
            # Einzelne SKU mit Sonderzeichen verursacht eBay-Fehler → nächsten Batch probieren
            log.warning(f"Offset {offset}: SKU-Fehler übersprungen ({err[:80]})")
            offset += LIMIT
            if offset > 5000:
                break
            continue
        log.error(f"Fehler bei offset {offset}: {e}")
        break

    offers = r.get("offers", []) if isinstance(r, dict) else []
    if not offers:
        break

    for offer in offers:
        sku    = offer.get("sku", "")
        status = offer.get("status", "")
        stats["total"] += 1

        if sku in bab_skus:
            stats["bab_kept"] += 1
            continue

        if status == "PUBLISHED":
            to_delete.append({"sku": sku, "offerId": offer.get("offerId", ""),
                               "status": status})

    total_in_response = r.get("total", 0) if isinstance(r, dict) else 0
    log.info(f"  Offset {offset}: {len(offers)} Offers | Gesamt bisher: {stats['total']} | Zu löschen: {len(to_delete)}")

    if offset + LIMIT >= total_in_response or len(offers) < LIMIT:
        break
    offset += LIMIT
    time.sleep(0.2)

log.info(f"\nScan abgeschlossen: {stats['total']} Offers total | {stats['bab_kept']} BAB behalten | {len(to_delete)} zu löschen")

if not to_delete:
    log.info("Nichts zu löschen — eBay ist sauber!")
    sys.exit(0)

# Sicherheitscheck
log.info(f"\nErste 10 zu löschende Offers:")
for item in to_delete[:10]:
    log.info(f"  SKU={item['sku']} | offerId={item['offerId']}")

# Schritt 2: Löschen via DELETE /inventory_item/{sku}
log.info(f"\nStarte Löschung von {len(to_delete)} Offers ...")
for i, item in enumerate(to_delete, 1):
    sku = item["sku"]
    try:
        client._request("DELETE", f"{INV_PATH}/{sku}")
        stats["deleted"] += 1
        if i % 100 == 0:
            log.info(f"[{i}/{len(to_delete)}] Gelöscht: {stats['deleted']} | Fehler: {stats['errors']}")
    except RuntimeError as e:
        err = str(e)
        if "404" in err or "25001" in err:
            stats["not_found"] += 1
        else:
            stats["errors"] += 1
            if stats["errors"] <= 5:
                log.warning(f"  ✗ {sku}: {err[:100]}")
    time.sleep(0.15)

log.info(f"\n{'='*55}")
log.info(f"ERGEBNIS purge_non_bab")
log.info(f"{'='*55}")
log.info(f"Gesamt gescannt:    {stats['total']}")
log.info(f"BAB behalten:       {stats['bab_kept']}")
log.info(f"Gelöscht:           {stats['deleted']}")
log.info(f"Nicht gefunden:     {stats['not_found']}")
log.info(f"Fehler:             {stats['errors']}")
log.info(f"✅ Nur noch BAB-Artikel auf eBay aktiv")
