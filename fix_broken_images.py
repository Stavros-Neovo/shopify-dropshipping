"""
fix_broken_images.py
====================
Liest image_report.csv und ersetzt defekte (404) und zu kleine Bilder
durch neue Suchergebnisse von DuckDuckGo.

Aufruf:
  cd ~/Documents/Dropshipping
  python3 fix_broken_images.py
"""
import csv
import re
import time
import requests
from pathlib import Path

ENRICHMENT_FILE = "enrichment_index.csv"
REPORT_FILE     = "image_report.csv"
DELAY           = 1.2
MIN_SIZE        = 400

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
})

BLOCKED = ["logo", "placeholder", "sprite", "icon", "banner", "tracking", "pixel", "favicon"]


def get_ddg_image(query: str, min_w: int = MIN_SIZE) -> str:
    """DuckDuckGo Bildsuche."""
    try:
        r = SESSION.get("https://duckduckgo.com/",
                        params={"q": query, "iax": "images", "ia": "images"},
                        timeout=10)
        match = re.search(r'vqd=([^&"]+)', r.text)
        if not match:
            return ""
        token = match.group(1)

        r2 = SESSION.get("https://duckduckgo.com/i.js",
                         params={"q": query, "vqd": token, "f": ",,,,,", "p": "1"},
                         timeout=10)
        if r2.status_code != 200:
            return ""

        for result in r2.json().get("results", [])[:10]:
            url = result.get("image", "")
            if not url or any(b in url.lower() for b in BLOCKED):
                continue
            w = result.get("width", 0)
            h = result.get("height", 0)
            if w >= min_w and h >= min_w:
                return url
    except Exception as e:
        print(f"    DDG-Fehler: {e}")
    return ""


def main():
    # Report laden
    report_path = Path(REPORT_FILE)
    if not report_path.exists():
        print(f"Fehler: {REPORT_FILE} nicht gefunden. Bitte zuerst check_images.py ausführen.")
        return

    with open(report_path, encoding="utf-8") as f:
        report_rows = list(csv.DictReader(f))

    # Defekte und kleine Bilder sammeln
    to_fix = []
    for r in report_rows:
        issue = r.get("issue", "")
        if issue == "OK":
            continue
        if "404" in issue or "FEHLER" in issue or "Zu klein" in issue or issue == "FEHLT":
            to_fix.append(r)

    print(f"Zu reparieren: {len(to_fix)} Bilder")
    print(f"  404/Fehler:  {sum(1 for r in to_fix if '404' in r.get('issue','') or 'FEHLER' in r.get('issue',''))}")
    print(f"  Zu klein:    {sum(1 for r in to_fix if 'Zu klein' in r.get('issue',''))}")
    print()

    # EAN → neue URL Mapping aufbauen
    fixes = {}
    for i, row in enumerate(to_fix, 1):
        ean   = row.get("ean", "").strip()
        title = row.get("title", "").strip()
        brand = row.get("brand", "").strip()
        issue = row.get("issue", "")

        if not ean or not title:
            continue

        # Suchquery anpassen
        if "Zu klein" in issue:
            query = f"{brand} {title} product high resolution"
        else:
            query = f"{brand} {title} product image"

        print(f"  [{i}/{len(to_fix)}] {title[:50]}")
        img = get_ddg_image(query, min_w=MIN_SIZE)
        time.sleep(DELAY)

        if not img:
            # Fallback mit kürzerem Query
            short_query = f"{title.split('-')[0].strip()} {brand} official"
            img = get_ddg_image(short_query, min_w=200)
            time.sleep(DELAY)

        if img:
            fixes[ean] = img
            print(f"    ✓ {img[:65]}")
        else:
            print(f"    ✗ Nicht gefunden")

    print(f"\n{len(fixes)} Bilder gefunden, aktualisiere enrichment_index.csv...")

    # Enrichment aktualisieren
    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    updated = 0
    for row in rows:
        ean = row.get("ean", "").strip()
        if ean in fixes:
            row["images_all"] = fixes[ean]
            row["source"] = "ddg_fixed"
            updated += 1

    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Fertig: {updated} Bilder aktualisiert")
    print("\nJetzt pushen:")
    print("  git add enrichment_index.csv && git commit -m 'fix: defekte und kleine bilder ersetzt' && git push")


if __name__ == "__main__":
    main()
