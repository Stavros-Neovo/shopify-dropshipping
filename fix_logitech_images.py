"""
fix_logitech_images.py
======================
Ersetzt alle ipcstore-Bilder und fehlende Bilder bei Logitech-Produkten
durch echte Produktfotos von assets.logitech.com.

Aufruf:
  cd ~/Documents/Dropshipping
  python3 fix_logitech_images.py

Danach committen:
  git add enrichment_index.csv
  git commit -m "fix: logitech produktbilder von offiziellem cdn"
  git push
"""
import csv
import re
import time
import json
import requests
from pathlib import Path
from urllib.parse import quote_plus
from bs4 import BeautifulSoup

ENRICHMENT_FILE = "enrichment_index.csv"
DELAY = 1.0  # Sekunden zwischen Requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def clean_title(title: str) -> str:
    """Entfernt generische Bezeichnungen die die Suche verschlechtern."""
    title = re.sub(r'\bLogitech\b', '', title, flags=re.IGNORECASE).strip()
    # Modellnummern wie 910-005771 sind bei Logitech gute Suchbegriffe
    return title.strip()


def get_logitech_image_from_cdn(title: str, ean: str) -> str:
    """
    Strategie 1: Logitech Search API (JSON-Endpunkt).
    Gibt direkt assets.logitech.com-URLs zurück.
    """
    try:
        query = clean_title(title)
        # Logitech Algolia-basierte Suche
        url = f"https://www.logitech.com/de-de/search?q={quote_plus(query)}"
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")

        # 1. Suche nach assets.logitech.com Bildern direkt im HTML
        for img_tag in soup.find_all("img"):
            src = img_tag.get("src") or img_tag.get("data-src") or ""
            if "assets.logitech.com" in src and any(ext in src for ext in [".png", ".jpg", ".webp"]):
                return src if src.startswith("http") else "https:" + src

        # 2. og:image als Fallback
        og = soup.find("meta", property="og:image")
        if og and og.get("content") and "logitech" in og["content"].lower():
            return og["content"]

    except Exception as e:
        print(f"    Strategie 1 Fehler: {e}")
    return ""


def get_logitech_image_via_icecat(ean: str) -> str:
    """
    Strategie 2: Icecat Open-Katalog (kein Login für öffentliche Produkte).
    Logitech-Bilder kommen hier direkt von assets.logitech.com.
    """
    try:
        url = f"https://icecat.us/api/icecat/products?ean={ean}&lang=de&output=dict"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            img = data.get("data", {}).get("GeneralInfo", {}).get("IcecatId")
            # Icecat öffentliche Produktseite
            images = data.get("data", {}).get("Gallery", [])
            if images:
                hi = images[0].get("Pic500", "") or images[0].get("HighPic", "")
                if hi.startswith("http"):
                    return hi
    except Exception:
        pass
    return ""


def get_logitech_image_from_barcodelookup(ean: str) -> str:
    """Strategie 3: barcodelookup.com (hat oft offizielle Bilder)."""
    try:
        url = f"https://www.barcodelookup.com/{ean}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        img = soup.select_one(".product-image img, #productImage img, .main-product-image img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src.startswith("http") and any(ext in src for ext in [".jpg", ".png", ".webp"]):
                if "logitech" in src.lower() or "ipc" not in src.lower():
                    return src
    except Exception:
        pass
    return ""


def get_best_logitech_image(title: str, ean: str) -> str:
    """Probiert alle Strategien nacheinander."""
    # Priorität: offizielle Logitech CDN
    img = get_logitech_image_from_cdn(title, ean)
    if img:
        return img

    time.sleep(0.3)

    # Fallback: barcodelookup
    img = get_logitech_image_from_barcodelookup(ean)
    if img:
        return img

    return ""


def main():
    p = Path(ENRICHMENT_FILE)
    if not p.exists():
        print(f"Datei nicht gefunden: {ENRICHMENT_FILE}")
        return

    with open(p, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    # Alle Logitech-Produkte
    logitech_rows = []
    other_rows = []
    for row in rows:
        brand = row.get("brand", "").lower()
        title = row.get("title_full", "").lower()
        if "logi" in brand or "logitech" in title:
            logitech_rows.append(row)
        else:
            other_rows.append(row)

    print(f"Logitech-Produkte gefunden: {len(logitech_rows)}")
    print(f"Davon ohne Bild:           {sum(1 for r in logitech_rows if not r.get('image_main','').strip())}")
    print(f"Davon mit ipcstore-Bild:   {sum(1 for r in logitech_rows if 'ipcstore' in r.get('image_main',''))}")
    print()

    updated = skipped = not_found = 0

    for i, row in enumerate(logitech_rows, 1):
        ean   = row.get("ean", "").strip()
        title = row.get("title_full", "").strip()
        current_img = row.get("image_main", "").strip()

        # Nur updaten wenn: kein Bild ODER ipcstore-Bild
        if current_img and "ipcstore" not in current_img and "logitech" not in current_img.lower():
            skipped += 1
            print(f"[{i:3}/{len(logitech_rows)}] SKIP  {title[:55]}")
            print(f"         Bild OK: {current_img[:70]}")
            continue

        reason = "kein Bild" if not current_img else "ipcstore ersetzt"
        print(f"[{i:3}/{len(logitech_rows)}] SUCHE {title[:55]}")
        print(f"         Grund: {reason}")

        img = get_best_logitech_image(title, ean)
        time.sleep(DELAY)

        if img:
            row["image_main"] = img
            # images_all aktualisieren: offizielles Bild vorne anhängen
            existing_all = row.get("images_all", "").strip()
            if img not in existing_all:
                row["images_all"] = img + ("|" + existing_all if existing_all else "")
            row["source"] = "logitech_cdn"
            updated += 1
            print(f"         ✓ {img[:80]}")
        else:
            not_found += 1
            print(f"         ✗ Kein Bild gefunden")

    # Alle Rows wieder zusammenführen
    all_rows = other_rows + logitech_rows

    # In der ursprünglichen Reihenfolge speichern (nach EAN-Key)
    index_map = {r.get("ean",""): r for r in rows}
    for r in all_rows:
        index_map[r.get("ean","")] = r

    with open(p, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in index_map.values():
            writer.writerow(row)

    print()
    print(f"═══════════════════════════════════════")
    print(f"Aktualisiert:      {updated}")
    print(f"Übersprungen (OK): {skipped}")
    print(f"Nicht gefunden:    {not_found}")
    print()
    print("Nächste Schritte:")
    print("  git add enrichment_index.csv")
    print("  git commit -m 'fix: logitech produktbilder aktualisiert'")
    print("  git push")


if __name__ == "__main__":
    main()
