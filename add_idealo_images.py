"""
add_idealo_images.py
====================
Sucht Produktbilder über idealo.de per EAN-Suche.
Funktioniert für alle Marken ohne API-Key oder Rate-Limit.

Aufruf:
  cd ~/Documents/Dropshipping
  python3 add_idealo_images.py
"""
import csv
import io
import json
import re
import time
import requests
import yaml
from pathlib import Path
from bs4 import BeautifulSoup

ENRICHMENT_FILE = "enrichment_index.csv"
CONFIG_FILE     = "config.yaml"
DELAY           = 1.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_idealo_image(ean: str, title: str) -> str:
    """Sucht auf idealo.de nach EAN und gibt das erste Produktbild zurück."""
    try:
        # Suche per EAN
        search_url = f"https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={ean}"
        r = SESSION.get(search_url, timeout=12)
        if r.status_code != 200:
            return ""

        soup = BeautifulSoup(r.text, "html.parser")

        # Erstes Suchergebnis finden
        # Idealo hat Produktbilder in den Suchergebnissen
        result = soup.select_one(
            "div[class*='offerList'] img, "
            "div[class*='product-tile'] img, "
            "div[class*='resultItem'] img, "
            ".sr-resultList img, "
            "article img"
        )
        if result:
            src = (result.get("src") or result.get("data-src") or
                   result.get("data-lazy") or result.get("srcset", "").split()[0] or "")
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http") and not any(x in src for x in ["logo", "sprite", "icon", "placeholder"]):
                # Größeres Bild anfordern (idealo nutzt URL-Parameter)
                src = re.sub(r'\/\d+x\d+\/', '/800x800/', src)
                return src

        # Fallback: og:image der ersten Produktseite
        first_link = soup.select_one("a[href*='/preisvergleich/Angebote']")
        if first_link:
            product_url = "https://www.idealo.de" + first_link["href"]
            r2 = SESSION.get(product_url, timeout=12)
            if r2.status_code == 200:
                soup2 = BeautifulSoup(r2.text, "html.parser")
                og = soup2.find("meta", property="og:image")
                if og and og.get("content"):
                    return og["content"]

                # JSON-LD Schema suchen
                for script in soup2.find_all("script", type="application/ld+json"):
                    try:
                        data = json.loads(script.string)
                        if isinstance(data, dict):
                            img = data.get("image")
                            if img:
                                return img[0] if isinstance(img, list) else img
                    except Exception:
                        pass

    except Exception as e:
        print(f"    Fehler: {e}")
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

    # Alle Produkte ohne Bild sammeln
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
    print("Starte idealo.de Suche...\n")

    added = updated = not_found = 0

    for i, product in enumerate(missing, 1):
        ean   = product["ean"]
        sku   = product["sku"]
        title = product["title"]
        brand = product["brand"]

        print(f"  [{i}/{len(missing)}] {sku} | {title[:50]}")
        img = get_idealo_image(ean, title)
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
            "source":           "idealo",
        }
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean in index:
            index[ean]["images_all"] = img
            index[ean]["source"] = "idealo"
            updated += 1
        else:
            index[ean] = entry
            added += 1

        # Alle 50 Produkte zwischenspeichern
        if i % 50 == 0:
            with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for row in index.values():
                    writer.writerow(row)
            print(f"\n  💾 Zwischenstand gespeichert ({i} verarbeitet)\n")

    # Final speichern
    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)

    print(f"\nFertig: {added} hinzugefügt, {updated} aktualisiert, {not_found} nicht gefunden")
    print("\nJetzt pushen:")
    print("  git add enrichment_index.csv && git commit -m 'feat: idealo bilder' && git push")


if __name__ == "__main__":
    main()
