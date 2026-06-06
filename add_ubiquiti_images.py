"""
add_ubiquiti_images.py
======================
Holt Bilder für Ubiquiti-Produkte von store.ui.com (Shopify JSON API).

Aufruf:
  cd ~/Documents/Dropshipping
  python3 add_ubiquiti_images.py
"""
import csv
import io
import re
import time
import requests
import yaml
from pathlib import Path

ENRICHMENT_FILE = "enrichment_index.csv"
CONFIG_FILE     = "config.yaml"
DELAY           = 0.8
UI_BASE         = "https://eu.store.ui.com/products"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
}

# Manuelle Mappings SKU → Handle (falls Auto-Erkennung fehlschlägt)
MANUAL_HANDLES = {
    "NETU0010": "u6-mesh",
    "NETU0015": "usw-24-poe",
    "NETU0016": "usw-48-poe",
    "NETU0017": "usw-pro-24-poe",
    "NETU0023": "usw-pro-48-poe",
    "NETU0024": "udm-se",
    "NETU0028": "udm-pro",
    "NETU0034": "usw-flex-mini",
    "NETU0076": "u6-plus",
    "NETU0084": "uvc-g5-bullet",
    "NETU0122": "uxg-max",
    "NETU0133": "usw-pro-max-16-poe",
    "NETU0143": "uisp-s-pro",
    "NETU0148": "u7-lite",
    "NETU0151": "u7-pro-max",
    "NETU0152": "unas-pro",
    "NETU0154": "ux7",
    "NETU0157": "usw-flex-2-5g-5",
    "NETU0158": "u7-in-wall",
    "NETU0173": "uvc-g6-turret",
    "NETU0181": "u7-pro-xg-wall",
}


def fetch_ui_image(handle: str) -> str:
    """Scrapt eu.store.ui.com und gibt das og:image zurück."""
    from bs4 import BeautifulSoup
    url = f"{UI_BASE}/{handle}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                img = og["content"]
                # Kein generisches og-image zurückgeben
                if "og-image" not in img and "placeholder" not in img:
                    return img
    except Exception as e:
        print(f"    Fehler bei {handle}: {e}")
    return ""


def extract_model(title: str) -> str:
    """Extrahiert Modellnummer aus Produkttitel (z.B. USW-24-POE)."""
    # Suche nach Mustern wie USW-24-POE, U6-PLUS, UVC-G5-BULLET etc.
    match = re.search(r'\b(U[A-Z0-9][-A-Z0-9]+)\b', title)
    if match:
        return match.group(1).lower()
    return ""


def load_enrichment(path: str):
    p = Path(path)
    if not p.exists():
        return {}, []
    with open(p, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    index = {r.get("ean", "").strip(): r for r in rows if r.get("ean", "").strip()}
    return index, fieldnames


def main():
    cfg = yaml.safe_load(open(CONFIG_FILE, encoding="utf-8"))
    csv_cfg = cfg["csv"]
    cols = csv_cfg["columns"]

    print("Lade BAB-Feed...")
    r = requests.get(csv_cfg["url"], timeout=60)
    r.encoding = csv_cfg.get("encoding", "utf-8-sig")
    feed_rows = list(csv.DictReader(io.StringIO(r.text), delimiter=csv_cfg["delimiter"]))

    index, fieldnames = load_enrichment(ENRICHMENT_FILE)

    required_fields = ["ean", "title_full", "short_summary", "long_summary",
                       "marketing_text", "images_all", "specs_html",
                       "manufacturer_url", "brand", "source"]
    for f in required_fields:
        if f not in fieldnames:
            fieldnames.append(f)

    missing = []
    for row in feed_rows:
        brand = (row.get("ManufacturerName") or "").strip().lower()
        if brand != "ubiquiti":
            continue
        ean = row.get(cols.get("ean", "GTIN"), "").strip()
        sku = row.get(cols.get("sku", "ItemNo"), "").strip()
        if not ean:
            continue
        if ean in index and index[ean].get("images_all", "").strip():
            continue
        missing.append({
            "ean":   ean,
            "sku":   sku,
            "title": row.get("Description", "").strip(),
        })

    print(f"Ubiquiti Produkte ohne Bild: {len(missing)}")

    handle_cache = {}
    added = updated = not_found = 0

    for product in missing:
        ean   = product["ean"]
        sku   = product["sku"]
        title = product["title"]

        # Handle bestimmen: manuell > auto
        handle = MANUAL_HANDLES.get(sku) or extract_model(title)
        if not handle:
            print(f"  ✗ Kein Handle: {sku} | {title[:50]}")
            not_found += 1
            continue

        if handle not in handle_cache:
            print(f"  Fetche {handle} ...")
            handle_cache[handle] = fetch_ui_image(handle)
            time.sleep(DELAY)

        img = handle_cache[handle]
        if not img:
            print(f"  ✗ Kein Bild: {sku} | {handle}")
            not_found += 1
            continue

        print(f"  ✓ {sku} | {handle} | {img[:60]}")

        entry = {
            "ean":              ean,
            "title_full":       title,
            "short_summary":    title,
            "long_summary":     title,
            "marketing_text":   "",
            "images_all":       img,
            "specs_html":       "",
            "manufacturer_url": f"https://store.ui.com/products/{handle}",
            "brand":            "Ubiquiti",
            "source":           "ubiquiti_shopify",
        }
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean in index:
            index[ean]["images_all"] = img
            index[ean]["source"] = "ubiquiti_shopify"
            updated += 1
        else:
            index[ean] = entry
            added += 1

    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)

    print(f"\nFertig: {added} hinzugefügt, {updated} aktualisiert, {not_found} nicht gefunden")
    print("\nJetzt pushen:")
    print("  git add enrichment_index.csv && git commit -m 'feat: ubiquiti bilder' && git push")


if __name__ == "__main__":
    main()
