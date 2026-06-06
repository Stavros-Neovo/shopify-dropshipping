"""
add_noco_images.py
==================
Trägt alle NOCO-Produkte mit Bildern in enrichment_index.csv ein.

Aufruf:
  cd ~/Documents/dropshipping
  python3 add_noco_images.py
"""
import csv
from pathlib import Path

ENRICHMENT_FILE = "enrichment_index.csv"

# NOCO-Produkte: (EAN, SKU, Modell, Beschreibung)
NOCO_PRODUCTS = [
    ("1210000624260", "NOCO0001", "al5",       "NOCO AL5 5A Lithium Air Inflator"),
    ("1210000615015", "NOCO0002", "gb20",      "NOCO GB20 Boost 12V 500A Jump Starter"),
    ("1210000615022", "NOCO0003", "gb40",      "NOCO GB40 Boost 12V 1000A Jump Starter"),
    ("1210000615060", "NOCO0006", "gb150",     "NOCO GB150 Boost 12V 3000A Jump Starter"),
    ("1210000620064", "NOCO0007", "gbx45",     "NOCO GBX45 Boost X 12V 1250A Jump Starter"),
    ("1210000619259", "NOCO0009", "genius2",   "NOCO GENIUS2 2A Battery Charger"),
    ("1210000619419", "NOCO0012", "gb251",     "NOCO GB251 Boost Max 24V 3000A Jump Starter"),
    ("1210000619402", "NOCO0013", "gb250",     "NOCO GB250 Boost Max 12V 5250A Jump Starter"),
    ("1210000620071", "NOCO0014", "gbx55",     "NOCO GBX55 Boost X 12V 1750A Jump Starter"),
    ("1210000620088", "NOCO0015", "gbx75",     "NOCO GBX75 Boost X 12V 2500A Jump Starter"),
    ("1210000624253", "NOCO0016", "ax65",      "NOCO AX65 Boost Air 12V 2000A Jump Starter und Air Compressor"),
    ("1210000620095", "NOCO0017", "gbx155",    "NOCO GBX155 Boost X 12V 4250A Jump Starter"),
    ("1210000620057", "NOCO0018", "geniusu65", "NOCO GENIUS U65 USB-C Charger"),
    ("1210000617309", "NOCO0019", "xgc4",      "NOCO XGC4 56W XGC Power Adapter"),
    ("1210000617279", "NOCO0020", "gbc013",    "NOCO GBC013 GB20/40 EVA Schutzcase"),
    ("1210000617286", "NOCO0021", "gbc014",    "NOCO GBC014 GB70 EVA Schutzcase"),
    ("1210000621047", "NOCO0024", "gbc101",    "NOCO GBC101 GBX45 EVA Schutzcase"),
    ("1210000621054", "NOCO0025", "gbc102",    "NOCO GBC102 GBX55 EVA Schutzcase"),
    ("1210000618146", "NOCO0028", "genius2",   "NOCO GENIUS2DEU 2A Direct-Mount Battery Charger"),
]

def image_url(model: str) -> str:
    """Konstruiert die NOCO-Katalog-Bild-URL aus dem Modellnamen."""
    m = model.lower()
    return f"https://no.co/media/catalog/product/{m[0]}/{m[1]}/{m}.png"

def load_enrichment(path: str):
    p = Path(path)
    if not p.exists():
        return {}, []
    rows = []
    index = {}
    with open(p, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            rows.append(row)
            ean = (row.get("ean") or "").strip()
            if ean:
                index[ean] = row
    return index, fieldnames

def main():
    index, fieldnames = load_enrichment(ENRICHMENT_FILE)

    # Alle Felder sicherstellen
    required_fields = ["ean", "title_full", "short_summary", "long_summary",
                       "marketing_text", "images_all", "specs_html",
                       "manufacturer_url", "brand", "source"]
    for f in required_fields:
        if f not in fieldnames:
            fieldnames.append(f)

    added = 0
    updated = 0

    for ean, sku, model, description in NOCO_PRODUCTS:
        img = image_url(model)
        entry = {
            "ean":             ean,
            "title_full":      description,
            "short_summary":   description,
            "long_summary":    description,
            "marketing_text":  "",
            "images_all":      img,
            "specs_html":      "",
            "manufacturer_url": f"https://no.co/{model}",
            "brand":           "NOCO",
            "source":          "noco_manual",
        }
        # Fehlende Felder aus bestehenden Einträgen auffüllen
        for f in fieldnames:
            if f not in entry:
                entry[f] = ""

        if ean in index:
            # Nur Bilder updaten wenn noch keins vorhanden
            old = index[ean]
            if not old.get("images_all"):
                old["images_all"] = img
                old["source"] = "noco_manual"
                old["brand"] = "NOCO"
                index[ean] = old
                updated += 1
                print(f"✓ Updated:  {sku} | {ean} | {description[:50]}")
            else:
                # Bild immer mit NOCO-Bild überschreiben (höhere Qualität)
                old["images_all"] = img
                old["source"] = "noco_manual"
                index[ean] = old
                updated += 1
                print(f"↑ Replaced: {sku} | {ean} | {description[:50]}")
        else:
            index[ean] = entry
            added += 1
            print(f"+ Added:    {sku} | {ean} | {description[:50]}")

    # CSV neu schreiben
    all_fields = list(fieldnames)
    for f in required_fields:
        if f not in all_fields:
            all_fields.append(f)

    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)

    print(f"\nFertig: {added} hinzugefügt, {updated} aktualisiert")
    print(f"Jetzt pushen:")
    print(f"  git add enrichment_index.csv && git commit -m 'feat: noco bilder' && git push")

if __name__ == "__main__":
    main()
