"""
build_enrichment_index.py
=========================
Wird EINMAL lokal ausgeführt.

Liest die große artikeldaten.csv (~222 MB, 75K Produkte) und das BAB-Feed,
extrahiert nur die Produkte, die in BEIDEN vorkommen (Match via EAN/GTIN),
und schreibt einen kleinen Enrichment-Index (~5 MB), der dann nach GitHub
gepusht wird.

Aufruf:
  python3 build_enrichment_index.py
  python3 build_enrichment_index.py --artikeldaten artikeldaten.csv \
                                    --output enrichment_index.csv

Nach Lauf:
  - enrichment_index.csv → das ist die kleine Datei für GitHub
  - artikeldaten.csv → bleibt lokal, NICHT nach GitHub pushen
"""
from __future__ import annotations
import argparse
import csv
import logging
import sys
from pathlib import Path
from typing import Dict
import yaml
from dotenv import load_dotenv

from csv_loader import load_supplier_feed

# CSVs mit langen Feldern brauchen erhöhtes Limit
csv.field_size_limit(sys.maxsize)

log = logging.getLogger("enrich-index")


# Spalten, die wir aus artikeldaten.csv übernehmen (auf das Wichtige reduziert)
INDEX_COLUMNS = [
    "ean",            # Match-Key
    "title_full",     # Hersteller-Titel (oft besser als BAB-Titel)
    "short_summary",  # 1-Zeiler
    "long_summary",   # 3-5 Sätze
    "marketing_text", # ausführlicher Text
    "specs_html",     # technische Specs in HTML
    "image_main",     # Haupt-Bild (Large)
    "images_all",     # alle Bilder (|-getrennt)
    "manufacturer_url",
]


def clean_html(text: str) -> str:
    """Sanftes Cleanup für HTML-Specs aus artikeldaten."""
    if not text:
        return ""
    # entferne sehr lange leere Stellen
    text = text.replace("\r", "").strip()
    return text


def pick_first(pipe_separated: str) -> str:
    """Erste URL aus einer |-getrennten Liste."""
    if not pipe_separated:
        return ""
    parts = [p.strip() for p in pipe_separated.split("|") if p.strip()]
    return parts[0] if parts else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--artikeldaten",
        default="artikeldaten.csv",
        help="Pfad zur großen artikeldaten.csv (lokal)",
    )
    parser.add_argument(
        "--output",
        default="enrichment_index.csv",
        help="Kleiner Index für GitHub",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    load_dotenv()

    art_path = Path(args.artikeldaten)
    if not art_path.exists():
        log.error(f"Datei nicht gefunden: {art_path}")
        log.error("Bitte artikeldaten.csv in den dropshipping-Ordner legen.")
        sys.exit(1)

    # --- 1) BAB-EANs sammeln ---
    log.info("Lade BAB-Feed um relevante EANs zu sammeln ...")
    bab_eans: set[str] = set()
    bab_count = 0
    for product in load_supplier_feed(cfg):
        ean = (product.get("ean") or "").strip()
        if ean:
            bab_eans.add(ean)
        bab_count += 1
    log.info(f"BAB-Produkte gesamt: {bab_count:,}  | davon mit EAN: {len(bab_eans):,}")

    if not bab_eans:
        log.error("Keine EANs im BAB-Feed gefunden – Match unmöglich!")
        sys.exit(1)

    # --- 2) artikeldaten.csv streamen und nur passende EANs übernehmen ---
    log.info(f"Stream artikeldaten.csv ({art_path.stat().st_size/1e6:.0f} MB) ...")
    matched: Dict[str, dict] = {}
    seen = 0
    with open(art_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            seen += 1
            ean = (row.get("ean") or "").strip()
            if not ean or ean not in bab_eans:
                continue
            # Mehrfach-Matches → ersten nehmen (artikeldaten kann Duplikate haben)
            if ean in matched:
                continue
            matched[ean] = {
                "ean": ean,
                "title_full": (row.get("title") or row.get("artname") or "").strip(),
                "short_summary": (row.get("short_summary") or "").strip(),
                "long_summary": (row.get("long_summary") or "").strip(),
                "marketing_text": (row.get("marketing_text") or "").strip(),
                "specs_html": clean_html(row.get("specs", "")),
                "image_main": pick_first(row.get("images_l", "")) or pick_first(row.get("images_xl", "")),
                "images_all": (row.get("images_l") or row.get("images_xl") or "").strip(),
                "manufacturer_url": (row.get("hersturl") or "").strip(),
            }
            if len(matched) % 100 == 0:
                log.info(f"  … {len(matched):,} Matches gefunden (Zeile {seen:,})")

    log.info(f"Gescannt: {seen:,} Zeilen | Matches: {len(matched):,}")

    # --- 3) Index als CSV speichern ---
    out_path = Path(args.output)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for v in matched.values():
            writer.writerow(v)

    size_mb = out_path.stat().st_size / 1e6
    log.info("=" * 60)
    log.info(f"Index erstellt: {out_path}  ({size_mb:.2f} MB)")
    log.info(f"Enthaltene Produkte: {len(matched):,}")
    log.info(f"Match-Rate: {len(matched)/len(bab_eans)*100:.1f}% der BAB-EANs")
    log.info("=" * 60)
    log.info("→ Diese Datei kannst du jetzt mit nach GitHub pushen.")
    log.info("→ Die große artikeldaten.csv bleibt LOKAL.")


if __name__ == "__main__":
    main()
