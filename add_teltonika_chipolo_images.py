"""
add_teltonika_chipolo_images.py
================================
Holt Bilder für Teltonika Networks und Chipolo Produkte.

Aufruf:
  cd ~/Documents/Dropshipping
  python3 add_teltonika_chipolo_images.py
"""
import csv
import io
import re
import time
import requests
import yaml
from pathlib import Path
from bs4 import BeautifulSoup

ENRICHMENT_FILE = "enrichment_index.csv"
CONFIG_FILE     = "config.yaml"
DELAY           = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# ---------------------------------------------------------------------------
# Chipolo: feste Bild-URLs pro Produkttyp (alle Farben = gleiche Form)
# ---------------------------------------------------------------------------
CHIPOLO_IMAGES = {
    "pop":  "https://chipolo.net/cdn/shop/files/POP-White_540x.jpg",
    "loop": "https://chipolo.net/cdn/shop/files/LOOP-White_540x.jpg",
    "card": "https://chipolo.net/cdn/shop/files/CARD-White_540x.jpg",
}

def get_chipolo_image(title: str) -> str:
    t = title.lower()
    if "loop" in t:
        return CHIPOLO_IMAGES["loop"]
    if "card" in t:
        return CHIPOLO_IMAGES["card"]
    if "pop" in t:
        return CHIPOLO_IMAGES["pop"]
    return ""

# ---------------------------------------------------------------------------
# Teltonika: Modellnummer aus Titel extrahieren → Produktseite scrapen
# ---------------------------------------------------------------------------
def get_teltonika_image(title: str) -> str:
    # Modellnummer extrahieren z.B. RUT956, TSW212, TRB142, RUTM50
    match = re.search(r'\b([A-Z]{2,5}[0-9]{2,4}[A-Z0-9]*)\b', title)
    if not match:
        return ""
    model = match.group(1).lower()

    # Teltonika Produkt-URL versuchen
    urls = [
        f"https://teltonika-networks.com/products/routers/{model}/",
        f"https://teltonika-networks.com/products/gateways/{model}/",
        f"https://teltonika-networks.com/products/switches/{model}/",
        f"https://teltonika-networks.com/products/modems/{model}/",
        f"https://teltonika-networks.com/products/{model}/",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                og = soup.find("meta", property="og:image")
                if og and og.get("content"):
                    img = og["content"]
                    if img.startswith("//"):
                        img = "https:" + img
                    if "placeholder" not in img and "logo" not in img.lower():
                        return img
                # Fallback: erstes Produktbild
                img_tag = soup.select_one(".product-image img, .hero img, img[src*='teltonika']")
                if img_tag:
                    src = img_tag.get("src") or img_tag.get("data-src") or ""
                    if src.startswith("//"):
                        src = "https:" + src
                    if src.startswith("http"):
                        return src
        except Exception:
            continue
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

    TARGET = {"teltonika networks", "teltonika", "chipolo"}

    missing = []
    for row in feed_rows:
        brand_raw = (row.get("ManufacturerName") or "").strip()
        brand_key = brand_raw.lower()
        if brand_key not in TARGET:
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
            "brand": brand_raw,
            "key":   brand_key,
        })

    print(f"Produkte ohne Bild: {len(missing)}")

    added = updated = not_found = 0

    for product in missing:
        ean   = product["ean"]
        sku   = product["sku"]
        title = product["title"]
        brand = product["brand"]
        key   = product["key"]

        print(f"  [{brand}] {title[:55]}")

        if "chipolo" in key:
            img = get_chipolo_image(title)
        else:
            img = get_teltonika_image(title)
            time.sleep(DELAY)

        if not img:
            print(f"    ✗ Kein Bild")
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
            "source":           f"scraper_{key.replace(' ', '_')}",
        }
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean in index:
            index[ean]["images_all"] = img
            index[ean]["source"] = entry["source"]
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
    print("  git add enrichment_index.csv && git commit -m 'feat: teltonika+chipolo bilder' && git push")


if __name__ == "__main__":
    main()
