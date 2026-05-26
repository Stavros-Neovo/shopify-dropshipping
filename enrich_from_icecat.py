"""
enrich_from_icecat.py
=====================
Fragt Icecat für alle Produkte ohne Bild ab und erweitert enrichment_index.csv.

Aufruf:
  cd ~/Documents/dropshipping
  python3 enrich_from_icecat.py

Voraussetzungen:
  pip install requests pyyaml python-dotenv --break-system-packages
"""
from __future__ import annotations
import csv
import json
import logging
import sys
import time
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
ICECAT_USER     = "neovogen"
ICECAT_TOKEN    = "a923fe60-04bd-4f83-ae2e-a1e1a8427c98"
ICECAT_LANG     = "de"
CONFIG_FILE     = "config.yaml"
ENRICHMENT_FILE = "enrichment_index.csv"
CACHE_FILE      = "icecat_cache.json"   # speichert bereits abgefragte EANs
DELAY_SECONDS   = 1.0                   # Pause zwischen Anfragen (Rate-Limit)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("icecat")

# ---------------------------------------------------------------------------
# Icecat-Lookup
# ---------------------------------------------------------------------------

def fetch_icecat(ean: str) -> dict | None:
    """Fragt Icecat per EAN (GTIN) ab. Gibt None zurück wenn kein Treffer."""
    url = (
        f"https://live.icecat.biz/api"
        f"?UserName={ICECAT_USER}&Language={ICECAT_LANG}"
        f"&GTIN={ean}&Token={ICECAT_TOKEN}"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            # Icecat gibt {"msg": "OK", "data": {...}} zurück
            if data.get("msg") == "OK" and data.get("data"):
                return data["data"]
        return None
    except Exception as e:
        log.warning(f"Icecat-Fehler für EAN {ean}: {e}")
        return None


def extract_enrichment(ean: str, data: dict) -> dict:
    """Extrahiert relevante Felder aus Icecat-Response."""
    gi = data.get("GeneralInfo", {})
    img = data.get("Image", {})

    # Titel
    title = gi.get("Title", "") or ""

    # Beschreibungen
    desc = gi.get("Description", {}) or {}
    long_desc  = desc.get("LongDesc", "")  or ""
    short_desc = desc.get("ShortDesc", "") or ""

    # Bilder
    high_pic = img.get("HighPic", "") or ""
    pics = img.get("Pics", []) or []
    extra_pics = [p.get("Pic", "") for p in pics if p.get("Pic")]
    all_images = [u for u in ([high_pic] + extra_pics) if u.startswith("http")]
    images_all = "|".join(all_images[:5])

    # Marke
    brand = gi.get("BrandName", "") or ""

    # Specs als einfache HTML-Tabelle
    specs_html = ""
    for group in data.get("FeaturesGroups", []) or []:
        features = group.get("Features", []) or []
        rows = []
        for feat in features:
            name  = (feat.get("Feature", {}) or {}).get("Name", {})
            name  = name.get("Value", "") if isinstance(name, dict) else str(name)
            value = (feat.get("LocalValue", "") or feat.get("Value", "") or "")
            if name and value:
                rows.append(f"<tr><td>{name}</td><td>{value}</td></tr>")
        if rows:
            specs_html += "<table>" + "".join(rows) + "</table>"

    return {
        "ean":           ean,
        "title_full":    title,
        "short_summary": short_desc[:300],
        "long_summary":  long_desc[:1000],
        "marketing_text": "",
        "images_all":    images_all,
        "specs_html":    specs_html[:5000],
        "manufacturer_url": "",
        "brand":         brand,
        "source":        "icecat",
    }


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

def load_enrichment(path: str) -> dict[str, dict]:
    """Lädt enrichment_index.csv → Dict EAN → Zeile."""
    p = Path(path)
    if not p.exists():
        return {}
    result = {}
    with open(p, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            ean = (row.get("ean") or "").strip()
            if ean:
                result[ean] = row
    log.info(f"Enrichment geladen: {len(result):,} Einträge")
    return result


def load_bab_eans(config_path: str) -> list[tuple[str, str]]:
    """Lädt BAB-Feed und gibt [(ean, sku), ...] zurück."""
    cfg = yaml.safe_load(open(config_path, encoding="utf-8"))
    csv_cfg = cfg["csv"]
    import io
    r = requests.get(csv_cfg["url"], timeout=60)
    r.encoding = csv_cfg.get("encoding", "utf-8-sig")
    reader = csv.DictReader(io.StringIO(r.text), delimiter=csv_cfg["delimiter"])
    cols = csv_cfg["columns"]
    ean_col = cols.get("ean", "GTIN")
    sku_col = cols.get("sku", "ItemNo")
    results = []
    for row in reader:
        ean = (row.get(ean_col) or "").strip()
        sku = (row.get(sku_col) or "").strip()
        if ean and len(ean) >= 8:
            results.append((ean, sku))
    log.info(f"BAB-Feed: {len(results):,} Produkte mit EAN")
    return results


def save_enrichment(path: str, index: dict[str, dict]):
    """Schreibt enrichment_index.csv neu."""
    if not index:
        return
    # Alle Felder aus ALLEN Einträgen sammeln (alte + neue Icecat-Felder)
    all_fields: list[str] = []
    seen: set[str] = set()
    for row in index.values():
        for k in row.keys():
            if k not in seen:
                all_fields.append(k)
                seen.add(k)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        for row in index.values():
            writer.writerow(row)
    size_mb = Path(path).stat().st_size / 1e6
    log.info(f"Enrichment gespeichert: {len(index):,} Einträge ({size_mb:.2f} MB) → {path}")


def main():
    # Cache laden (bereits abgefragte EANs)
    cache_path = Path(CACHE_FILE)
    cache: dict = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    # Bestehenden Enrichment-Index laden
    enrichment = load_enrichment(ENRICHMENT_FILE)
    original_fields = list(next(iter(enrichment.values())).keys()) if enrichment else None

    # BAB-Feed holen
    log.info("Lade BAB-Feed …")
    bab_eans = load_bab_eans(CONFIG_FILE)

    # ALLE Produkte mit Icecat prüfen — Icecat-Bilder sind immer besser als BAB-Bilder
    missing = [(ean, sku) for ean, sku in bab_eans]
    log.info(f"Produkte für Icecat-Lookup: {len(missing):,} (alle)")

    if not missing:
        log.info("Alle Produkte haben bereits Enrichment — nichts zu tun.")
        return

    # Icecat abfragen
    found = 0
    not_found = 0
    for i, (ean, sku) in enumerate(missing, 1):
        log.info(f"[{i}/{len(missing)}] EAN {ean} (SKU {sku}) …")

        # Cache-Treffer?
        if ean in cache:
            if cache[ean]:
                enrichment[ean] = cache[ean]
                found += 1
            else:
                not_found += 1
            continue

        # Icecat anfragen
        data = fetch_icecat(ean)
        if data:
            entry = extract_enrichment(ean, data)
            # Bestehende Felder aus altem Eintrag behalten (z.B. specs), nur Bilder überschreiben
            if ean in enrichment:
                old = enrichment[ean].copy()
                # Nur Bilder + Titel von Icecat übernehmen wenn vorhanden
                if entry.get("images_all"):
                    old["images_all"] = entry["images_all"]
                if entry.get("title_full"):
                    old["title_full"] = entry["title_full"]
                if entry.get("long_summary"):
                    old["long_summary"] = entry["long_summary"]
                old["source"] = "icecat+bab"
                entry = old
            # Fehlende Felder mit Leerstrings auffüllen
            if original_fields:
                for field in original_fields:
                    if field not in entry:
                        entry[field] = ""
            enrichment[ean] = entry
            cache[ean] = entry
            found += 1
            log.info(f"  ✓ Gefunden: {entry.get('title_full', '')[:60]}")
        else:
            cache[ean] = None
            not_found += 1
            log.info(f"  ✗ Nicht gefunden")

        # Cache alle 10 Einträge speichern
        if i % 10 == 0:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            save_enrichment(ENRICHMENT_FILE, enrichment)

        time.sleep(DELAY_SECONDS)

    # Finales Speichern
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    save_enrichment(ENRICHMENT_FILE, enrichment)

    log.info("=" * 50)
    log.info(f"ERGEBNIS: {found} gefunden, {not_found} nicht gefunden")
    log.info(f"Neue enrichment_index.csv enthält {len(enrichment):,} Einträge")
    log.info("Nächster Schritt: enrichment_index.csv auf GitHub pushen")
    log.info("  cd ~/Documents/dropshipping")
    log.info("  git add enrichment_index.csv && git commit -m 'feat: icecat enrichment' && git push")


if __name__ == "__main__":
    main()
