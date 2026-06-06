"""
add_brand_images.py
===================
Scrapt Produktbilder direkt von Hersteller-Websites.
Kein API-Key, kein Rate-Limit.

Unterstützte Marken:
  - Logitech (logitech.com)
  - Kärcher (kaercher.com)
  - Makita (makita.de)
  - Razer (razer.com)
  - Western Digital (westerndigital.com)
  - Toshiba (toshiba.com)
  - G.Skill (gskill.com)
  - Kioxia (kioxia.com)
  - Ubiquiti (ui.com)

Aufruf:
  cd ~/Documents/Dropshipping
  python3 add_brand_images.py
"""
import csv
import io
import re
import time
import json
import requests
import yaml
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

ENRICHMENT_FILE = "enrichment_index.csv"
CONFIG_FILE     = "config.yaml"
DELAY           = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# ---------------------------------------------------------------------------
# Brand Scrapers
# ---------------------------------------------------------------------------

def get_logitech_image(title: str, ean: str) -> str:
    """Sucht auf logitech.com nach dem Produkt und gibt die erste Bild-URL zurück."""
    try:
        # Logitech hat eine JSON-Suchantwort
        query = title.replace("Logitech", "").strip()
        url = f"https://www.logitech.com/de-de/search?q={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        # Produktbilder aus og:image oder ersten Produkt-Thumbnails
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]

        # Produktkarten
        img = soup.select_one(".product-card img, .search-result img, article img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http") and any(ext in src for ext in [".jpg", ".png", ".webp"]):
                return src
    except Exception as e:
        print(f"    Logitech-Fehler: {e}")
    return ""


def get_kaercher_image(title: str, ean: str) -> str:
    """Sucht auf kaercher.com nach dem Produkt."""
    try:
        query = title.replace("Kärcher", "").replace("Karcher", "").strip()
        url = f"https://www.kaercher.com/de/search.html?query={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        # Produktbilder
        img = soup.select_one(".product-tile__image img, .product-image img, .search-result__image img")
        if img:
            src = img.get("src") or img.get("data-src") or img.get("data-lazy") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src

        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]
    except Exception as e:
        print(f"    Kärcher-Fehler: {e}")
    return ""


def get_makita_image(title: str, ean: str) -> str:
    """Sucht auf makita.de nach dem Produkt."""
    try:
        query = title.replace("Makita", "").strip()
        url = f"https://www.makita.de/suche/?q={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        img = soup.select_one(".product-item img, .product__image img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src
    except Exception as e:
        print(f"    Makita-Fehler: {e}")
    return ""


def get_razer_image(title: str, ean: str) -> str:
    """Sucht auf razer.com nach dem Produkt."""
    try:
        query = title.replace("Razer", "").strip()
        url = f"https://www.razer.com/de-de/search#q={quote_plus(query)}&t=Products"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]

        img = soup.select_one(".product-card img, .search-result img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src
    except Exception as e:
        print(f"    Razer-Fehler: {e}")
    return ""


def get_wd_image(title: str, ean: str) -> str:
    """Western Digital - shop.westerndigital.com."""
    try:
        query = title.replace("Western Digital", "WD").strip()
        url = f"https://shop.westerndigital.com/de-de/search#q={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        img = soup.select_one("img.product-image, img[data-product-image]")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src

        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]
    except Exception as e:
        print(f"    WD-Fehler: {e}")
    return ""


def get_gskill_image(title: str, ean: str) -> str:
    """G.Skill - gskill.com."""
    try:
        query = title.replace("G.Skill", "").strip()
        url = f"https://www.gskill.com/search?q={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        img = soup.select_one(".product img, .search-result img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src
    except Exception as e:
        print(f"    G.Skill-Fehler: {e}")
    return ""


def get_kioxia_image(title: str, ean: str) -> str:
    """Kioxia - europe.kioxia.com."""
    try:
        query = title.replace("Kioxia", "").strip()
        url = f"https://europe.kioxia.com/de-de/search.html?query={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        img = soup.select_one(".product-item img, .result-item img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src

        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]
    except Exception as e:
        print(f"    Kioxia-Fehler: {e}")
    return ""


def get_ubiquiti_image(title: str, ean: str) -> str:
    """Ubiquiti - store.ui.com."""
    try:
        query = title.strip()
        url = f"https://store.ui.com/de/en/search?q={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        img = soup.select_one(".product-card img, [data-testid='product-image'] img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src
    except Exception as e:
        print(f"    Ubiquiti-Fehler: {e}")
    return ""


# Dispatcher: Marke → Scraper-Funktion
BRAND_SCRAPERS = {
    "logitech":         get_logitech_image,
    "kärcher":          get_kaercher_image,
    "karcher":          get_kaercher_image,
    "makita":           get_makita_image,
    "razer":            get_razer_image,
    "western digital":  get_wd_image,
    "g.skill":          get_gskill_image,
    "kioxia":           get_kioxia_image,
    "ubiquiti":         get_ubiquiti_image,
}


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
    # Config laden
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

    # Produkte sammeln
    missing = []
    for row in feed_rows:
        brand_raw = (row.get("ManufacturerName") or "").strip()
        brand_key = brand_raw.lower()
        if brand_key not in BRAND_SCRAPERS:
            continue
        ean = row.get(cols.get("ean", "GTIN"), "").strip()
        if not ean:
            continue
        if ean in index and index[ean].get("images_all", "").strip():
            continue
        missing.append({
            "ean":       ean,
            "sku":       row.get(cols.get("sku", "ItemNo"), "").strip(),
            "title":     row.get("Description", "").strip(),
            "brand":     brand_raw,
            "brand_key": brand_key,
        })

    print(f"Produkte ohne Bild: {len(missing)}")

    added = updated = not_found = 0

    for product in missing:
        ean       = product["ean"]
        title     = product["title"]
        brand     = product["brand"]
        brand_key = product["brand_key"]
        scraper   = BRAND_SCRAPERS[brand_key]

        print(f"  [{brand}] {title[:55]}")
        img = scraper(title, ean)
        time.sleep(DELAY)

        if not img:
            not_found += 1
            print(f"    ✗ Kein Bild")
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
            "source":           f"brand_scraper_{brand_key}",
        }
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean in index:
            index[ean]["images_all"] = img
            index[ean]["source"] = f"brand_scraper_{brand_key}"
            updated += 1
        else:
            index[ean] = entry
            added += 1

    # CSV speichern
    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)

    print(f"\nFertig: {added} hinzugefügt, {updated} aktualisiert, {not_found} nicht gefunden")
    print("\nJetzt pushen:")
    print("  git add enrichment_index.csv && git commit -m 'feat: brand scraper bilder' && git push")


if __name__ == "__main__":
    main()
