"""
apply_manual_images.py
======================
Liest manual_images.yaml und trägt die Bild-URLs in enrichment_index.csv ein.

Aufruf:
  cd ~/Documents/Dropshipping
  python3 apply_manual_images.py
"""
import csv
import yaml
from pathlib import Path

ENRICHMENT_FILE = "enrichment_index.csv"
MANUAL_FILE     = "manual_images.yaml"


def main():
    # Manuelle Bilder laden
    data = yaml.safe_load(open(MANUAL_FILE, encoding="utf-8")) or {}
    manual = {str(k): str(v).strip() for k, v in data.items() if v and str(v).strip()}
    print(f"Manuelle Bild-URLs geladen: {len(manual)}")

    if not manual:
        print("Keine URLs eingetragen — bitte manual_images.yaml befüllen.")
        return

    # Enrichment CSV laden
    p = Path(ENRICHMENT_FILE)
    rows = []
    fieldnames = []
    with open(p, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    # Index aufbauen
    index = {r.get("ean", "").strip(): i for i, r in enumerate(rows) if r.get("ean", "").strip()}

    updated = 0
    added   = 0
    skipped = 0

    for ean, img_url in manual.items():
        if ean in index:
            rows[index[ean]]["images_all"] = img_url
            rows[index[ean]]["source"] = "manual"
            updated += 1
            print(f"  ↑ Updated: {ean}")
        else:
            # Neue Zeile anlegen
            new_row = {f: "" for f in fieldnames}
            new_row["ean"] = ean
            new_row["images_all"] = img_url
            new_row["source"] = "manual"
            rows.append(new_row)
            added += 1
            print(f"  + Added:   {ean}")

    # CSV neu schreiben
    with open(p, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"\nFertig: {updated} aktualisiert, {added} hinzugefügt, {skipped} übersprungen")
    print("\nJetzt pushen:")
    print("  git add enrichment_index.csv manual_images.yaml && git commit -m 'feat: manuelle bilder' && git push")


if __name__ == "__main__":
    main()
