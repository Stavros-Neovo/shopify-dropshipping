"""
add_upc_images.py
=================
Holt Produktbilder über die UPC ItemDB API (kostenlos, EAN-basiert).
Ideal für Marken die nicht bei Icecat gelistet sind (Logitech, Kärcher, etc.)

Aufruf:
  cd ~/Documents/Dropshipping
  python3 add_upc_images.py
"""
import csv
import io
import time
import requests
import yaml
from pathlib import Path
from collections import Counter

ENRICHMENT_FILE = "enrichment_index.csv"
CONFIG_FILE     = "config.yaml"
UPC_API_URL     = "https://api.upcitemdb.com/prod/trial/lookup"
DELAY_SECONDS   = 1.5   # UPC ItemDB Trial: max 100 Anfragen/Tag, daher langsam

# Marken die wir mit UPC ItemDB suchen wollen (Kleinschreibung)
TARGET_BRANDS = {
    "logitech", "kärcher", "karcher", "petkit", "teltonika", "teltonika networks",
    "ubiquiti", "chipolo", "toshiba", "makita", "g.skill", "razer",
    "western digital", "ghd", "lexar", "babyliss", "kioxia", "melitta",
    "intel", "hookii", "needit", "ecoflow"
}


def fetch_upc_image(ean: str) -> str:
    """Fragt UPC ItemDB ab und gibt die beste Bild-URL zurück."""
    try:
        r = requests.get(UPC_API_URL, params={"upc": ean}, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", [])
            if items:
                # Erstes Item mit Bild nehmen
                for item in items:
                    images = item.get("images", [])
                    if images:
                        return images[0]
        elif r.status_code == 429:
            print("  ⚠ Rate-Limit erreicht — warte 60 Sekunden...")
            time.sleep(60)
        return ""
    except Exception as e:
        print(f"  Fehler bei {ean}: {e}")
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
    return index, fieldnames, rows


def main():
    # Config laden
    cfg = yaml.safe_load(open(CONFIG_FILE, encoding="utf-8"))
    csv_cfg = cfg["csv"]
    cols = csv_cfg["columns"]

    # BAB-Feed laden
    print("Lade BAB-Feed...")
    r = requests.get(csv_cfg["url"], timeout=60)
    r.encoding = csv_cfg.get("encoding", "utf-8-sig")
    reader = csv.DictReader(io.StringIO(r.text), delimiter=csv_cfg["delimiter"])

    # Enrichment laden
    index, fieldnames, all_rows = load_enrichment(ENRICHMENT_FILE)
    print(f"Enrichment geladen: {len(index)} Einträge")

    required_fields = ["ean", "title_full", "short_summary", "long_summary",
                       "marketing_text", "images_all", "specs_html",
                       "manufacturer_url", "brand", "source"]
    for f in required_fields:
        if f not in fieldnames:
            fieldnames.append(f)

    # Produkte sammeln die Bilder brauchen
    missing = []
    for row in reader:
        brand = (row.get("ManufacturerName") or "").strip().lower()
        if brand not in TARGET_BRANDS:
            continue
        ean = row.get(cols.get("ean", "GTIN"), "").strip()
        if not ean:
            continue
        if ean in index and index[ean].get("images_all", "").strip():
            continue  # Bild existiert bereits
        missing.append({
            "ean":   ean,
            "sku":   row.get(cols.get("sku", "ItemNo"), "").strip(),
            "title": row.get("Description", "").strip(),
            "brand": row.get("ManufacturerName", "").strip(),
        })

    print(f"Produkte ohne Bild in Zielmarken: {len(missing)}")
    if not missing:
        print("Nichts zu tun.")
        return

    added = updated = not_found = 0

    for product in missing:
        ean   = product["ean"]
        sku   = product["sku"]
        title = product["title"]
        brand = product["brand"]

        print(f"  Suche: {sku} | {title[:50]}")
        img = fetch_upc_image(ean)
        time.sleep(DELAY_SECONDS)

        if not img:
            not_found += 1
            print(f"    ✗ Kein Bild gefunden")
            continue

        print(f"    ✓ {img[:70]}")

        entry = {
            "ean":              ean,
            "title_full":       title,
            "short_summary":    title,
            "long_summary":     title,
            "marketing_text":   "",
            "images_all":       img,
            "specs_html":       "",
            "manufacturer_url": "",
            "brand":            brand,
            "source":           "upc_itemdb",
        }
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean in index:
            index[ean]["images_all"] = img
            index[ean]["source"] = "upc_itemdb"
            updated += 1
        else:
            index[ean] = entry
            added += 1

    # CSV neu schreiben
    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)

    print(f"\nFertig: {added} hinzugefügt, {updated} aktualisiert, {not_found} nicht gefunden")
    print(f"\nJetzt pushen:")
    print(f"  git add enrichment_index.csv && git commit -m 'feat: upc itemdb bilder' && git push")


if __name__ == "__main__":
    main()
