"""
check_images.py
===============
Prüft alle Produktbilder in enrichment_index.csv auf:
  - Erreichbarkeit (HTTP 200)
  - Bildgröße (min. 400x400 empfohlen)
  - Falsches Format (kein echtes Bild)

Ergebnisse werden in image_report.csv gespeichert.

Aufruf:
  cd ~/Documents/Dropshipping
  python3 check_images.py
"""
import csv
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

ENRICHMENT_FILE = "enrichment_index.csv"
REPORT_FILE     = "image_report.csv"
MIN_SIZE        = 400       # Mindestbreite/-höhe in Pixeln
MAX_WORKERS     = 10        # Parallele Downloads
TIMEOUT         = 8         # Sekunden pro Request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


def check_image(row: dict) -> dict:
    ean   = row.get("ean", "").strip()
    url   = row.get("images_all", "").strip()
    brand = row.get("brand", "").strip()
    title = row.get("title_full", "").strip()
    src   = row.get("source", "").strip()

    result = {
        "ean":    ean,
        "brand":  brand,
        "title":  title[:60],
        "source": src,
        "url":    url[:100],
        "status": "",
        "width":  "",
        "height": "",
        "issue":  "",
    }

    if not url:
        result["status"] = "FEHLT"
        result["issue"]  = "Keine URL"
        return result

    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        result["status"] = str(r.status_code)

        if r.status_code != 200:
            result["issue"] = f"HTTP {r.status_code}"
            return result

        # Nur ersten Teil laden für Größencheck
        content = b""
        for chunk in r.iter_content(chunk_size=32768):
            content += chunk
            if len(content) >= 131072:  # max 128KB lesen
                break

        img = Image.open(BytesIO(content))
        w, h = img.size
        result["width"]  = str(w)
        result["height"] = str(h)

        if w < MIN_SIZE or h < MIN_SIZE:
            result["issue"] = f"Zu klein ({w}x{h})"
        else:
            result["issue"] = "OK"

    except Exception as e:
        err = str(e)[:60]
        result["status"] = "FEHLER"
        result["issue"]  = err

    return result


def main():
    # Enrichment laden
    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    rows_with_url = [r for r in rows if r.get("images_all", "").strip()]
    rows_without  = [r for r in rows if not r.get("images_all", "").strip()]

    print(f"Gesamt:        {len(rows)}")
    print(f"Mit URL:       {len(rows_with_url)}")
    print(f"Ohne URL:      {len(rows_without)}")
    print(f"\nPrüfe {len(rows_with_url)} Bilder ({MAX_WORKERS} parallel)...\n")

    results = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_image, row): row for row in rows_with_url}
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append({"issue": str(e), "status": "FEHLER"})
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(rows_with_url)} geprüft...")

    # Statistik
    ok       = [r for r in results if r.get("issue") == "OK"]
    small    = [r for r in results if "Zu klein" in r.get("issue", "")]
    broken   = [r for r in results if r.get("issue") not in ("OK", "") and "Zu klein" not in r.get("issue", "")]

    print(f"\n{'='*50}")
    print(f"ERGEBNIS:")
    print(f"  OK (>={MIN_SIZE}px):  {len(ok)}")
    print(f"  Zu klein:          {len(small)}")
    print(f"  Defekt/Fehler:     {len(broken)}")
    print(f"  Ohne URL:          {len(rows_without)}")
    print(f"{'='*50}\n")

    if small:
        print(f"KLEINE BILDER ({len(small)}):")
        for r in small[:20]:
            print(f"  {r['brand']:15s} | {r['issue']:15s} | {r['title'][:40]}")
        if len(small) > 20:
            print(f"  ... und {len(small)-20} weitere")

    if broken:
        print(f"\nDEFEKTE BILDER ({len(broken)}):")
        for r in broken[:20]:
            print(f"  {r['brand']:15s} | {r['issue']:25s} | {r['title'][:35]}")
        if len(broken) > 20:
            print(f"  ... und {len(broken)-20} weitere")

    # Report speichern
    fieldnames = ["ean", "brand", "title", "source", "status", "width", "height", "issue", "url"]
    with open(REPORT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        # Probleme zuerst sortieren
        results.sort(key=lambda r: (r.get("issue") == "OK", r.get("issue", "")))
        for r in results:
            writer.writerow(r)

    print(f"\nBericht gespeichert: {REPORT_FILE}")
    print(f"Öffne ihn in Excel um alle Details zu sehen.")


if __name__ == "__main__":
    main()
