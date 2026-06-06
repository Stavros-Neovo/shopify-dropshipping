"""
add_ecoflow_images.py
=====================
Holt Bilder für EcoFlow-Produkte von der EcoFlow-Website (de.ecoflow.com)
und trägt sie in enrichment_index.csv ein.

Aufruf:
  cd ~/Documents/dropshipping
  python3 add_ecoflow_images.py
"""
import csv
import json
import re
import time
from pathlib import Path
from typing import Optional, Dict

import requests

ENRICHMENT_FILE = "enrichment_index.csv"
ECOFLOW_BASE    = "https://de.ecoflow.com/products"
DELAY_SECONDS   = 0.5

# ---------------------------------------------------------------------------
# Manuelle Mappings: EAN → Bild-URL (für bekannte Produkte)
# ---------------------------------------------------------------------------
MANUAL_IMAGES = {
    # Glacier Classic 35L
    "4895251641623": "https://cdn.shopify.com/s/files/1/0622/6758/8855/files/ecoflow-glacier-classic-portable-fridge-freezer-1168117510.png?v=1757325813",
    # Glacier Classic 55L
    "4895251641807": "https://cdn.shopify.com/s/files/1/0622/6758/8855/files/ecoflow-glacier-classic-portable-fridge-freezer-1168117510.png?v=1757325813",
    # Delta Pro Ultra Inverter
    "4895251608596": "https://cdn.shopify.com/s/files/1/0622/6758/8855/files/ecoflow-delta-pro-ultra-home-diy-power-station-1174283874.png?v=1750410218",
    # 100W Solar Panel
    "4897082668619": "https://cdn.shopify.com/s/files/1/0622/6758/8855/files/ecoflow-100w-flexibles-solarpanel-1147413344.png?v=1742928576",
    # Wave 2 Add-on Battery
    "4895251604758": "https://cdn.shopify.com/s/files/1/0622/6758/8855/files/ecoflow-wave-2-zusatzbatterie-1147510555.png?v=1742934203",
}

# ---------------------------------------------------------------------------
# Handle-Mappings: SKU → EcoFlow Shopify Handle (für automatischen Abruf)
# ---------------------------------------------------------------------------
SKU_TO_HANDLE = {
    "GARE0003":  "camping-light",
    "HHWE1003":  "wave-2-add-on-battery",
    "HHWE1008":  "wave-3-add-on-battery",
    "HHWE1011":  "wave-series-car-vent-seal",
    "HHWE1012":  "shower-kit",
    "HHWE1013":  "wave-series-stabilizing-strap-kit",
    "PPCE0003":  "glacier-plug-in-battery",
    "PPCE0004":  "glacier-protection-bag",
    "PPCE0007":  "glacier-classic-portable-fridge-freezer",
    "PPCE0009":  "glacier-classic-portable-fridge-freezer",
    "PPE0015":   "extra-battery-for-home-backup-power",
    "PPE0016":   "extra-battery-for-home-backup-power",
    "PPE0023":   "river-2-bag",
    "PPE0070":   "rigid-solar-panel-mounting-feet",
    "PPE0142":   "rapid-5000",
    "PPE0143":   "rapid-5000",
    "PPE0144":   "rapid-5000",
    "PPE0148":   "65w-charger",
    "PPE0220":   "rapid-pro-x",
    "PPE0222":   "rapid-pro-20k",
    "PPE0223":   "rapid-25k",
    "PPE0226":   "rapid-25k",
    "PPE0227":   "rapid-25k",
    "PPE0228":   "rapid-25k",
    "PPE0229":   "rapid-pro-x-apple-watch-charger",
    "PPE0230":   "rapid-pro-320w-station",
    "PPE0231":   "140w-charger",
    "PPE0232":   "100w-charger",
    "PPE0234":   "rapid-mag-5k",
    "PPE0235":   "rapid-mag-5k",
    "PPE0236":   "rapid-mag-5k",
    "PPE0237":   "rapid-mag-10k",
    "PPE0239":   "rapid-mag-10k",
    "PPE0242":   "delta-pro-ultra",
    "PPE0259":   "delta-3-bag",
    "PPE0273":   "trolley-for-delta-pro-ultra",
    "PPE0286":   "trail-portable-power-station-waterproof-carrying-case",
    "PPSE0013":  "100w-flexible-solar-panel",
    "PPSE0015":  "single-axis-solar-tracker",
    "PPSE0043":  "get-set-kit",
    "PPSE0044":  "prepared-kit",
    "PPSE0046":  "balcony-hook-kit",
    "PPSE0056":  "adjustable-tilt-mount-bracket",
    "PPSE0072":  "wave-2-nylon-cable-and-support-frame",
    "PPSE0076":  "45w-solar-panel",
    "PPSE0078":  "solar-hat",
    "PPSE0079":  "solar-hat",
    "PPSE0081":  "60w-solar-panel",
    "PPSE0092":  "balcony-bracket-semi-enclosed",
    "PPSE0093":  "balcony-bracket-lattice",
    "PPSE0096":  "adjustable-facade-bracket",
    "PPSE0097":  "facade-bracket",
    "PPSE0098":  "pitched-roof-bracket",
    "PPSE0099":  "lightweight-facade-bracket",
    "PPSE0100":  "lightweight-bracket",
    "PPSE0101":  "single-axis-solar-tracker",
    "PPSE0102":  "smart-plug-2",
    "PPSE0103":  "smart-meter",
    "PPSE0107":  "stream-micro-inverter",
    "PPSE0111":  "400w-portable-solar-panel",
}


def fetch_ecoflow_image(handle: str) -> Optional[str]:
    """Fragt die EcoFlow JSON-API ab und gibt die erste Bild-URL zurück."""
    url = f"{ECOFLOW_BASE}/{handle}.json"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            images = data.get("product", {}).get("images", [])
            if images:
                return images[0]["src"]
        return None
    except Exception as e:
        print(f"  Fehler bei {handle}: {e}")
        return None


def load_enrichment(path: str):
    p = Path(path)
    if not p.exists():
        return {}, []
    rows = []
    index = {}
    with open(p, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            rows.append(row)
            ean = (row.get("ean") or "").strip()
            if ean:
                index[ean] = row
    return index, fieldnames


def main():
    index, fieldnames = load_enrichment(ENRICHMENT_FILE)

    required_fields = ["ean", "title_full", "short_summary", "long_summary",
                       "marketing_text", "images_all", "specs_html",
                       "manufacturer_url", "brand", "source"]
    for f in required_fields:
        if f not in fieldnames:
            fieldnames.append(f)

    # Cache für Handle → Bild-URL (um doppelte Abrufe zu vermeiden)
    handle_cache: Dict[str, Optional[str]] = {}

    added = 0
    updated = 0
    not_found = 0

    # BAB-Feed laden um SKUs zu haben
    import io
    import yaml
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    csv_cfg = cfg["csv"]
    r = requests.get(csv_cfg["url"], timeout=60)
    r.encoding = csv_cfg.get("encoding", "utf-8-sig")
    reader = csv.DictReader(io.StringIO(r.text), delimiter=csv_cfg["delimiter"])
    cols = csv_cfg["columns"]

    bab_products = []
    for row in reader:
        manufacturer = (row.get("ManufacturerName") or "").lower()
        if "ecoflow" in manufacturer:
            bab_products.append({
                "sku": row.get(cols.get("sku", "ItemNo"), "").strip(),
                "ean": row.get(cols.get("ean", "GTIN"), "").strip(),
                "title": row.get("Description", "").strip(),
            })

    print(f"EcoFlow Produkte im BAB-Feed: {len(bab_products)}")

    for product in bab_products:
        sku = product["sku"]
        ean = product["ean"]
        title = product["title"]

        # 1. Manuelle EAN-Mapping prüfen
        img = MANUAL_IMAGES.get(ean)

        # 2. SKU-zu-Handle Mapping prüfen
        if not img and sku in SKU_TO_HANDLE:
            handle = SKU_TO_HANDLE[sku]
            if handle not in handle_cache:
                print(f"  Fetche {handle} ...")
                handle_cache[handle] = fetch_ecoflow_image(handle)
                time.sleep(DELAY_SECONDS)
            img = handle_cache[handle]

        if not img:
            not_found += 1
            print(f"  ✗ Kein Bild: {sku} | {title[:50]}")
            continue

        entry = {
            "ean":              ean,
            "title_full":       title,
            "short_summary":    title,
            "long_summary":     title,
            "marketing_text":   "",
            "images_all":       img,
            "specs_html":       "",
            "manufacturer_url": f"https://de.ecoflow.com/search?q={sku}",
            "brand":            "EcoFlow",
            "source":           "ecoflow_manual",
        }
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean and ean in index:
            old = index[ean]
            old["images_all"] = img
            old["source"] = "ecoflow_manual"
            old["brand"] = "EcoFlow"
            index[ean] = old
            updated += 1
            print(f"  ↑ Updated:  {sku} | {title[:50]}")
        elif ean:
            index[ean] = entry
            added += 1
            print(f"  + Added:    {sku} | {title[:50]}")
        else:
            print(f"  ⚠ Keine EAN: {sku} | {title[:50]}")

    # CSV neu schreiben
    all_fields = list(fieldnames)
    for f in required_fields:
        if f not in all_fields:
            all_fields.append(f)

    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)

    print(f"\nFertig: {added} hinzugefügt, {updated} aktualisiert, {not_found} nicht gefunden")
    print(f"\nJetzt pushen:")
    print(f"  git add enrichment_index.csv && git commit -m 'feat: ecoflow bilder' && git push")


if __name__ == "__main__":
    main()
