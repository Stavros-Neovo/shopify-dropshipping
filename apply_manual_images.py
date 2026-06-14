"""
apply_manual_images.py
======================
Liest manual_images.json (aus dem Browser-Tool exportiert) und setzt
die eingetragenen Bild-URLs direkt auf das eBay Inventory Item.

Aufruf:
  python3 apply_manual_images.py
"""
from __future__ import annotations
import json, logging, sys, time
from pathlib import Path
import yaml
from dotenv import load_dotenv
from ebay_client import EbayClient

MANUAL_FILE    = "manual_images.json"
IMAGE_FIX_FILE = "image_fix_needed.json"
INVENTORY_PATH = "/sell/inventory/v1/inventory_item"

log = logging.getLogger("apply_images")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

load_dotenv()
cfg    = yaml.safe_load(open("config_shop2.yaml", encoding="utf-8"))
client = EbayClient.from_env(cfg["ebay"])

manual = json.loads(Path(MANUAL_FILE).read_text(encoding="utf-8"))
log.info(f"{len(manual)} SKUs aus {MANUAL_FILE}")

fix = json.loads(Path(IMAGE_FIX_FILE).read_text()) if Path(IMAGE_FIX_FILE).exists() else {}
fixed = []

for sku, meta in manual.items():
    url   = meta.get("image_url", "").strip()
    title = meta.get("title", sku)
    if not url:
        continue
    log.info(f"  {sku}  {title[:50]}")
    try:
        existing = client._request("GET", f"{INVENTORY_PATH}/{sku}")
        if not existing:
            log.warning(f"  Kein Inventory Item fuer {sku}")
            continue
        existing.setdefault("product", {})
        existing["product"]["imageUrls"] = [url]
        for f in ["sku", "locale", "packageWeightAndSize"]:
            existing.pop(f, None)
        client._request("PUT", f"{INVENTORY_PATH}/{sku}", json_body=existing)
        log.info(f"  OK gesetzt")
        fixed.append(sku)
        fix.pop(sku, None)
    except Exception as e:
        log.error(f"  Fehler: {e}")
    time.sleep(0.8)

if fixed:
    Path(IMAGE_FIX_FILE).write_text(json.dumps(fix, ensure_ascii=False, indent=2))
    log.info(f"\n{len(fixed)} SKUs gefixt | {len(fix)} verbleiben in image_fix_needed.json")
