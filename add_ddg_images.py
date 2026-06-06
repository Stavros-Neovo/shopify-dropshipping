"""
add_ddg_images.py
=================
Sucht Produktbilder über DuckDuckGo Image Search (kein API-Key nötig).

Aufruf:
  cd ~/Documents/Dropshipping
  python3 add_ddg_images.py
"""
import csv
import io
import json
import re
import time
import requests
import yaml
from pathlib import Path

ENRICHMENT_FILE = "enrichment_index.csv"
CONFIG_FILE     = "config.yaml"
DELAY           = 1.2

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
})

# Domains die wir bevorzugen (Hersteller-CDNs)
PREFERRED_DOMAINS = [
    "cdn.shopify.com", "images.logitech.com", "resource.logitech.com",
    "makita.de", "razer.com", "westerndigital.com", "wd.com",
    "toshiba.com", "gskill.com", "lexar.com", "intel.com",
    "ghdhair.com", "babyliss.de", "melitta.de", "sony.de",
    "ecoflow.com", "cdn.ecomm.ui.com", "teltonika",
]

# Domains die wir meiden
BLOCKED_DOMAINS = [
    "logo", "placeholder", "sprite", "icon", "banner", "ads",
    "tracking", "pixel", "avatar", "favicon"
]


def get_ddg_token(query: str) -> str:
    """Holt den VQD-Token von DuckDuckGo für die Bildersuche."""
    try:
        r = SESSION.get(
            "https://duckduckgo.com/",
            params={"q": query, "iax": "images", "ia": "images"},
            timeout=10
        )
        match = re.search(r'vqd=([^&"]+)', r.text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


def search_ddg_images(query: str) -> str:
    """Sucht Bilder via DuckDuckGo und gibt die beste URL zurück."""
    token = get_ddg_token(query)
    if not token:
        return ""

    try:
        r = SESSION.get(
            "https://duckduckgo.com/i.js",
            params={
                "q": query,
                "vqd": token,
                "f": ",,,,,",
                "p": "1",
                "v7exp": "a",
            },
            timeout=10
        )
        if r.status_code != 200:
            return ""

        data = r.json()
        results = data.get("results", [])

        # Bevorzugte Domains zuerst
        for result in results[:10]:
            url = result.get("image", "")
            if not url:
                continue
            if any(bad in url.lower() for bad in BLOCKED_DOMAINS):
                continue
            if any(good in url.lower() for good in PREFERRED_DOMAINS):
                return url

        # Fallback: erstes brauchbares Bild
        for result in results[:5]:
            url = result.get("image", "")
            if url and not any(bad in url.lower() for bad in BLOCKED_DOMAINS):
                # Mindestgröße prüfen
                w = result.get("width", 0)
                h = result.get("height", 0)
                if w >= 300 and h >= 300:
                    return url

    except Exception as e:
        print(f"    DDG-Fehler: {e}")
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
    print(f"Enrichment geladen: {len(index)} Einträge")

    required_fields = ["ean", "title_full", "short_summary", "long_summary",
                       "marketing_text", "images_all", "specs_html",
                       "manufacturer_url", "brand", "source"]
    for f in required_fields:
        if f not in fieldnames:
            fieldnames.append(f)

    # Alle Produkte ohne Bild
    missing = []
    for row in feed_rows:
        ean = row.get(cols.get("ean", "GTIN"), "").strip()
        if not ean:
            continue
        if ean in index and index[ean].get("images_all", "").strip():
            continue
        missing.append({
            "ean":   ean,
            "sku":   row.get(cols.get("sku", "ItemNo"), "").strip(),
            "title": row.get("Description", "").strip(),
            "brand": row.get("ManufacturerName", "").strip(),
        })

    print(f"Produkte ohne Bild: {len(missing)}")
    print("Starte DuckDuckGo Bildsuche...\n")

    added = updated = not_found = 0

    for i, product in enumerate(missing, 1):
        ean   = product["ean"]
        sku   = product["sku"]
        title = product["title"]
        brand = product["brand"]

        # Suchquery: Produktname + Marke
        query = f"{title} product image"
        print(f"  [{i}/{len(missing)}] {sku} | {title[:50]}")

        img = search_ddg_images(query)
        time.sleep(DELAY)

        if not img:
            # Fallback: nur EAN suchen
            img = search_ddg_images(f"{ean} product")
            time.sleep(DELAY)

        if not img:
            print(f"    ✗ Nicht gefunden")
            not_found += 1
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
            "source":           "ddg_images",
        }
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean in index:
            index[ean]["images_all"] = img
            index[ean]["source"] = "ddg_images"
            updated += 1
        else:
            index[ean] = entry
            added += 1

        # Alle 30 Produkte zwischenspeichern
        if i % 30 == 0:
            with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for row in index.values():
                    writer.writerow(row)
            print(f"\n  Zwischenstand gespeichert ({i}/{len(missing)})\n")

    # Final speichern
    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)

    print(f"\nFertig: {added} hinzugefügt, {updated} aktualisiert, {not_found} nicht gefunden")
    print("\nJetzt pushen:")
    print("  git add enrichment_index.csv && git commit -m 'feat: ddg bilder' && git push")


if __name__ == "__main__":
    main()
