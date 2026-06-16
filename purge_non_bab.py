#!/usr/bin/env python3
"""
purge_non_bab.py
================
Löscht alle eBay-Listings die NICHT in der BAB-Lieferantenliste sind.
Nutzt GET /inventory_item (statt /offer) um den 25707-Fehler bei
Kosatec-SKUs mit Sonderzeichen zu umgehen.
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

# BAB-SKUs aus supplier_map laden
smap = json.loads(Path("supplier_map.json").read_text())
bab_skus = {k for k, v in smap.items() if v.get("supplier") == "BAB"}
log.info(f"BAB-SKUs (geschützt): {len(bab_skus)}")

INV_PATH  = "/sell/inventory/v1/inventory_item"
OFFER_PATH = "/sell/inventory/v1/offer"
LIMIT     = 25   # kleiner Batch → stabiler bei Sonderzeichen-SKUs

stats = {"total": 0, "bab_kept": 0, "deleted": 0, "not_found": 0, "errors": 0}
to_delete: list[str] = []

# Schritt 1: Alle Inventory Items einsammeln
log.info("Scanne alle eBay Inventory Items ...")
offset = 0
while True:
    try:
        r = client._request("GET", INV_PATH, params={"limit": LIMIT, "offset": offset})
    except Exception as e:
        err = str(e)
        log.warning(f"Offset {offset}: Fehler übersprungen → {err[:120]}")
        offset += LIMIT
        if offset > 20000:
            break
        time.sleep(1)
        continue

    items = r.get("inventoryItems", []) if isinstance(r, dict) else []
    if not items:
        break

    for item in items:
        sku = item.get("sku", "")
        stats["total"] += 1
        if sku in bab_skus:
            stats["bab_kept"] += 1
        else:
            to_delete.append(sku)

    total_in_response = r.get("total", 0) if isinstance(r, dict) else 0
    if offset % 200 == 0:
        log.info(f"  Offset {offset}/{total_in_response}: {stats['total']} gescannt | {len(to_delete)} zu löschen")

    if offset + LIMIT >= total_in_response or len(items) < LIMIT:
        break
    offset += LIMIT
    time.sleep(0.3)

log.info(f"\nScan: {stats['total']} Items | {stats['bab_kept']} BAB | {len(to_delete)} zu löschen")

if not to_delete:
    log.info("Nichts zu löschen — eBay ist sauber!")
    sys.exit(0)

log.info(f"\nErste 20 zu löschende SKUs:")
for sku in to_delete[:20]:
    log.info(f"  {sku}")

# Schritt 2: Offers zurückziehen + Inventory Item löschen
log.info(f"\nStarte Bereinigung von {len(to_delete)} Items ...")
for i, sku in enumerate(to_delete, 1):
    # Zuerst aktiven Offer zurückziehen (damit Listing offline geht)
    try:
        offers = client._request(
            "GET", OFFER_PATH,
            params={"sku": sku, "marketplace_id": cfg["ebay"]["marketplace_id"]}
        )
        for offer in (offers or {}).get("offers", []):
            if offer.get("status") == "PUBLISHED":
                client._request("POST", f"{OFFER_PATH}/{offer['offerId']}/withdraw")
    except Exception:
        pass  # kein Offer vorhanden oder schon offline

    # Dann Inventory Item löschen
    try:
        client._request("DELETE", f"{INV_PATH}/{sku}")
        stats["deleted"] += 1
    except RuntimeError as e:
        err = str(e)
        if "404" in err or "25001" in err:
            stats["not_found"] += 1
        else:
            stats["errors"] += 1
            if stats["errors"] <= 10:
                log.warning(f"  ✗ {sku}: {err[:100]}")
    time.sleep(0.2)

    if i % 50 == 0:
        log.info(f"[{i}/{len(to_delete)}] Gelöscht: {stats['deleted']} | Fehler: {stats['errors']}")

log.info(f"\n{'='*55}")
log.info(f"ERGEBNIS purge_non_bab")
log.info(f"{'='*55}")
log.info(f"Gesamt gescannt:  {stats['total']}")
log.info(f"BAB behalten:     {stats['bab_kept']}")
log.info(f"Geloescht:        {stats['deleted']}")
log.info(f"Nicht gefunden:   {stats['not_found']}")
log.info(f"Fehler:           {stats['errors']}")
log.info(f"Nur noch BAB-Artikel auf eBay aktiv")
