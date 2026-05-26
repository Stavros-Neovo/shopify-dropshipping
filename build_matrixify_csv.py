"""
build_matrixify_csv.py
======================
Generiert eine Matrixify-kompatible Shopify-CSV aus dem Lieferanten-Feed.

Pipeline:
  1. Lieferanten-CSV laden (csv_loader.py)
  2. Filter anwenden (Gewicht > 2kg raus, EK > max raus, Marken-Filter, ...)
  3. VK berechnen (pricing.py)
  4. Matrixify-Format-CSV schreiben — alle Spalten, die Matrixify versteht
  5. Diff gegen letzten Lauf: Produkte, die NICHT MEHR im Feed sind → Status "draft"

Matrixify-Doku (Spalten):
  https://matrixify.app/tutorials/matrixify-csv-headers-products/

Aufruf:
  python3 build_matrixify_csv.py
  python3 build_matrixify_csv.py --config config.yaml --output public/shopify_products.csv
"""
from __future__ import annotations
import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

from csv_loader import load_supplier_feed
from pricing import calculate_vk

# CSVs mit langen Feldern (Specs-HTML kann sehr lang sein)
csv.field_size_limit(sys.maxsize)

log = logging.getLogger("matrixify")

# ---------------------------------------------------------------------------
# Google Product Category Mapping (BAB ItemMainGroup → Google Taxonomy)
# ---------------------------------------------------------------------------
GOOGLE_CATEGORY_MAP: Dict[str, str] = {
    "AMD":        "Electronics > Computers > Computer Components > CPUs & Processors",
    "INTEL":      "Electronics > Computers > Computer Components > CPUs & Processors",
    "DDR3":       "Electronics > Computers > Computer Components > Computer Memory",
    "DDR4":       "Electronics > Computers > Computer Components > Computer Memory",
    "DDR5":       "Electronics > Computers > Computer Components > Computer Memory",
    "SSD":        "Electronics > Computers > Computer Components > Storage Devices",
    "INTERN":     "Electronics > Computers > Computer Components > Storage Devices",
    "INTERN G":   "Electronics > Computers > Computer Components > Storage Devices",
    "EXTERN":     "Electronics > Computers > Computer Components > Storage Devices",
    "SATA":       "Electronics > Computers > Computer Components > Storage Devices",
    "FLASH":      "Electronics > Computers > Computer Components > Storage Devices",
    "VERBATIM":   "Electronics > Computers > Computer Components > Storage Devices",
    "MONITOR":    "Electronics > Video > Video Components > Monitors",
    "DOCKING":    "Electronics > Computers > Laptop Accessories > Laptop Docking Stations",
    "ROUTER":     "Electronics > Networking",
    "SWITCH":     "Electronics > Networking > Network Switches",
    "NETZTEIL":   "Electronics > Computers > Computer Components > Power Supplies",
    "LOGITECH":   "Electronics > Computers > Input Devices",
    "CHERRY":     "Electronics > Computers > Input Devices",
    "MICROSOFT":  "Electronics > Computers > Input Devices",
    "SECURITY":   "Electronics > Security",
    "ZUBEHÖR":    "Electronics > Electronics Accessories",
    "GA":         "Electronics",
    "CE":         "Electronics",
    "HW":         "Electronics > Computers > Computer Components",
    "DV":         "Electronics",
    "PPS":        "Electronics",
    "NEW OPEN BOXED": "Electronics",
    "GRADE A":    "Electronics",
    "GRADE B":    "Electronics",
}

DEFAULT_GOOGLE_CATEGORY = "Electronics"


def get_google_category(product: dict) -> str:
    """Gibt die passende Google Produktkategorie für ein Produkt zurück."""
    cat = (product.get("category") or "").strip().upper()
    return GOOGLE_CATEGORY_MAP.get(cat, DEFAULT_GOOGLE_CATEGORY)


# ---------------------------------------------------------------------------
# Matrixify-Spalten — vollständige Liste der relevanten Felder
# ---------------------------------------------------------------------------
MATRIXIFY_HEADERS = [
    "ID",                          # Shopify product ID (leer = neu)
    "Handle",                      # URL-Slug (eindeutig)
    "Command",                     # MERGE | UPDATE | NEW | REPLACE
    "Title",                       # Produkt-Titel
    "Body HTML",                   # Produkt-Beschreibung
    "Vendor",                      # Hersteller / Marke
    "Type",                        # Produkt-Typ / Kategorie
    "Tags",                        # Tags (Komma-separiert)
    "Tags Command",                # MERGE | REPLACE
    "Status",                      # active | draft | archived
    "Published",                   # TRUE / FALSE
    "Published Scope",             # global | web
    "Google Product Category",     # Google Shopping Kategorie
    "Variant SKU",
    "Variant Price",               # VK Brutto
    "Variant Compare At Price",    # optional Streichpreis
    "Variant Cost",                # EK (für Margen-Reports im Shopify-Admin)
    "Variant Inventory Tracker",   # shopify
    "Variant Inventory Policy",    # deny | continue
    "Variant Inventory Qty",       # Lagerbestand
    "Variant Requires Shipping",   # TRUE
    "Variant Taxable",             # TRUE
    "Variant Weight",
    "Variant Weight Unit",         # kg
    "Variant Barcode",             # EAN
    "Image Src",                   # Bild-URL
    "Image Position",              # 1 = Haupt-Bild
    "Image Alt Text",
    "Metafield: custom.ek_price [number_decimal]",  # zur Diagnose
    "Metafield: custom.margin_eur [number_decimal]",
    "Metafield: custom.last_sync [date_time]",
]


def load_enrichment_index(path: str) -> Dict[str, dict]:
    """Lädt enrichment_index.csv → Dict EAN → Enrichment-Daten."""
    p = Path(path)
    if not p.exists():
        log.warning(f"Enrichment-Index nicht gefunden ({path}) – Produkte werden ohne Bilder/Beschreibung importiert.")
        return {}
    index: Dict[str, dict] = {}
    with open(p, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ean = (row.get("ean") or "").strip()
            if ean:
                index[ean] = row
    log.info(f"Enrichment-Index geladen: {len(index):,} EAN-Einträge ({p.stat().st_size/1e6:.2f} MB)")
    return index


def build_description_html(bab_title: str, enrichment: Optional[dict]) -> str:
    """Baut HTML-Body für Shopify aus Bab-Titel + Enrichment-Daten."""
    if not enrichment:
        return f"<p>{bab_title}</p>"

    parts = []
    lead = enrichment.get("marketing_text") or enrichment.get("long_summary") or enrichment.get("short_summary") or ""
    if lead:
        parts.append(f"<p>{lead}</p>")

    specs = enrichment.get("specs_html") or ""
    if specs:
        parts.append(f'<div class="product-specs">{specs}</div>')

    mfr_url = enrichment.get("manufacturer_url") or ""
    if mfr_url and mfr_url.startswith("http"):
        parts.append(f'<p><small><a href="{mfr_url}" target="_blank" rel="noopener">Herstellerinformationen</a></small></p>')

    return "\n".join(parts) if parts else f"<p>{bab_title}</p>"


def slugify(text: str) -> str:
    """Erzeugt aus einem Titel einen URL-freundlichen Handle."""
    import re
    text = text.lower()
    replacements = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                    "Ä": "ae", "Ö": "oe", "Ü": "ue"}
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:80] or "produkt"


def should_keep(product: dict, filters_cfg: dict) -> tuple[bool, str]:
    """Entscheidet, ob ein Produkt importiert werden soll."""
    if product.get("purchase_price", 0) <= 0:
        return False, "EK ist 0 oder fehlt"
    if product.get("purchase_price", 0) > filters_cfg.get("max_purchase_price_eur", 1e9):
        return False, f"EK > {filters_cfg['max_purchase_price_eur']}€"
    if product.get("weight_kg", 999) > filters_cfg.get("max_weight_kg", 999):
        return False, f"Gewicht > {filters_cfg['max_weight_kg']}kg"
    if product.get("stock", 0) < filters_cfg.get("min_stock", 0):
        return False, f"Stock < {filters_cfg['min_stock']}"

    allowed = filters_cfg.get("allowed_categories") or []
    if allowed and product.get("category", "") not in allowed:
        return False, "Kategorie nicht erlaubt"

    blob = " ".join([
        product.get("title", ""),
        product.get("brand", ""),
        product.get("description", ""),
    ]).lower()
    for kw in filters_cfg.get("excluded_keywords") or []:
        if kw.lower() in blob:
            return False, f"Excluded-Keyword '{kw}'"

    return True, ""


def build_rows(product: dict, pr, cfg: dict,
               enrichment: Optional[dict] = None,
               max_images: int = 5) -> list[dict]:
    """
    Wandelt ein Produkt in eine oder mehrere Matrixify-CSV-Zeilen.

    - Erste Zeile: vollständige Produkt-Daten + erstes Bild
    - Folgezeilen: nur Handle + Image-Felder (für weitere Bilder)
    """
    handle = slugify(product["sku"] + "-" + product.get("title", ""))
    now = datetime.now().isoformat(timespec="seconds")

    # Bilder aus Enrichment-Index (Pipe-separiert)
    all_images: list[str] = []
    if enrichment:
        images_field = enrichment.get("images_all", "")
        all_images = [u.strip() for u in images_field.split("|") if u.strip().startswith("http")]
    all_images = all_images[:max_images]
    first_image = all_images[0] if all_images else ""

    title = (enrichment.get("title_full") if enrichment else "") or \
            product.get("title", "Unbenanntes Produkt")
    title = title[:250]

    body_html = build_description_html(product.get("title", ""), enrichment)

    tags = list(filter(None, [
        product.get("category", ""),
        product.get("brand", ""),
    ]))
    if not first_image:
        tags.append("import-ohne-bild")

    google_cat = get_google_category(product)

    rows: list[dict] = []

    # Hauptzeile (volles Produkt)
    rows.append({
        "ID": "",
        "Handle": handle,
        "Command": "MERGE",
        "Title": title,
        "Body HTML": body_html,
        "Vendor": product.get("brand", "") or "",
        "Type": product.get("category", "") or "",
        "Tags": ", ".join(tags),
        "Tags Command": "MERGE",
        "Status": "active" if first_image else "draft",
        "Published": "TRUE" if first_image else "FALSE",
        "Published Scope": "global",
        "Google Product Category": google_cat,
        "Variant SKU": product["sku"],
        "Variant Price": f"{pr.vk_gross:.2f}",
        "Variant Compare At Price": "",
        "Variant Cost": f"{pr.purchase_price_net:.2f}",
        "Variant Inventory Tracker": "shopify",
        "Variant Inventory Policy": "deny",
        "Variant Inventory Qty": str(int(product.get("stock", 0))),
        "Variant Requires Shipping": "TRUE",
        "Variant Taxable": "TRUE",
        "Variant Weight": f"{product.get('weight_kg', 0.0):.3f}",
        "Variant Weight Unit": "kg",
        "Variant Barcode": product.get("ean", "") or "",
        "Image Src": first_image,
        "Image Position": "1" if first_image else "",
        "Image Alt Text": title if first_image else "",
        "Metafield: custom.ek_price [number_decimal]": f"{pr.purchase_price_net:.2f}",
        "Metafield: custom.margin_eur [number_decimal]": f"{pr.margin_eur:.2f}",
        "Metafield: custom.last_sync [date_time]": now,
    })

    # Zusätzliche Bilder als eigene Zeilen (Matrixify-Konvention)
    for pos, img_url in enumerate(all_images[1:], start=2):
        rows.append({
            "ID": "",
            "Handle": handle,
            "Command": "MERGE",
            "Title": "",
            "Body HTML": "",
            "Vendor": "",
            "Type": "",
            "Tags": "",
            "Tags Command": "",
            "Status": "",
            "Published": "",
            "Published Scope": "",
            "Google Product Category": "",
            "Variant SKU": "",
            "Variant Price": "",
            "Variant Compare At Price": "",
            "Variant Cost": "",
            "Variant Inventory Tracker": "",
            "Variant Inventory Policy": "",
            "Variant Inventory Qty": "",
            "Variant Requires Shipping": "",
            "Variant Taxable": "",
            "Variant Weight": "",
            "Variant Weight Unit": "",
            "Variant Barcode": "",
            "Image Src": img_url,
            "Image Position": str(pos),
            "Image Alt Text": f"{title} – Bild {pos}",
            "Metafield: custom.ek_price [number_decimal]": "",
            "Metafield: custom.margin_eur [number_decimal]": "",
            "Metafield: custom.last_sync [date_time]": "",
        })

    return rows


def build_deactivate_row(sku: str, last_known: dict) -> dict:
    """Erzeugt eine Zeile, die ein verschwundenes Produkt auf 'draft' setzt."""
    return {
        "ID": "",
        "Handle": last_known.get("handle", ""),
        "Command": "MERGE",
        "Title": last_known.get("title", ""),
        "Body HTML": "",
        "Vendor": "",
        "Type": "",
        "Tags": "auto-deactivated",
        "Tags Command": "MERGE",
        "Status": "draft",
        "Published": "FALSE",
        "Published Scope": "",
        "Google Product Category": "",
        "Variant SKU": sku,
        "Variant Price": last_known.get("vk", ""),
        "Variant Compare At Price": "",
        "Variant Cost": "",
        "Variant Inventory Tracker": "shopify",
        "Variant Inventory Policy": "deny",
        "Variant Inventory Qty": "0",
        "Variant Requires Shipping": "TRUE",
        "Variant Taxable": "TRUE",
        "Variant Weight": "",
        "Variant Weight Unit": "kg",
        "Variant Barcode": "",
        "Image Src": "",
        "Image Position": "",
        "Image Alt Text": "",
        "Metafield: custom.ek_price [number_decimal]": "",
        "Metafield: custom.margin_eur [number_decimal]": "",
        "Metafield: custom.last_sync [date_time]": datetime.now().isoformat(timespec="seconds"),
    }


def setup_logging(log_dir: str):
    Path(log_dir).mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = Path(log_dir) / f"matrixify_{ts}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="public/shopify_products.csv",
                        help="Pfad zur generierten Matrixify-CSV")
    parser.add_argument("--state", default="state.json",
                        help="State-Datei für Diff-Erkennung (verschwundene SKUs)")
    parser.add_argument("--enrichment", default="enrichment_index.csv",
                        help="Kleiner Enrichment-Index mit Bildern + Beschreibungen")
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    load_dotenv()

    log_file = setup_logging(cfg["runtime"]["log_dir"])
    log.info(f"=== Matrixify-Generierung gestartet, Log: {log_file} ===")

    # State laden (für Diff)
    state_path = Path(args.state)
    state: Dict[str, dict] = (
        json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    )

    # Enrichment-Index laden (für Bilder + Beschreibungen)
    enrichment_idx = load_enrichment_index(args.enrichment)

    # Counters
    stats = {"total": 0, "filtered": 0, "kept": 0,
             "enriched": 0, "without_image": 0,
             "image_rows": 0,
             "deactivated": 0, "errors": 0}
    filter_reasons: Dict[str, int] = {}

    # Output-Datei vorbereiten
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen_skus: set[str] = set()
    new_state: Dict[str, dict] = {}

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MATRIXIFY_HEADERS, extrasaction="ignore")
        writer.writeheader()

        # ----- aktiver Bestand aus Lieferanten-Feed -----
        for product in load_supplier_feed(cfg):
            stats["total"] += 1
            keep, reason = should_keep(product, cfg["filters"])
            if not keep:
                stats["filtered"] += 1
                filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
                continue

            try:
                pr = calculate_vk(product["purchase_price"], cfg["pricing"])
            except Exception as e:
                log.error(f"Pricing-Fehler für SKU {product.get('sku')}: {e}")
                stats["errors"] += 1
                continue

            sku = product["sku"]
            ean = (product.get("ean") or "").strip()
            enrichment = enrichment_idx.get(ean) if ean else None
            if enrichment:
                stats["enriched"] += 1
            else:
                stats["without_image"] += 1

            rows = build_rows(product, pr, cfg, enrichment=enrichment)
            for row in rows:
                writer.writerow(row)
            stats["image_rows"] += len(rows) - 1

            seen_skus.add(sku)
            new_state[sku] = {
                "handle": rows[0]["Handle"],
                "title": rows[0]["Title"],
                "vk": rows[0]["Variant Price"],
                "last_seen": datetime.now().isoformat(timespec="seconds"),
            }
            stats["kept"] += 1

            if stats["kept"] % 100 == 0:
                log.info(f"… {stats['kept']} Produkte verarbeitet  "
                         f"({stats['enriched']} mit Bildern, {stats['without_image']} ohne)")

        # ----- verschwundene SKUs auf 'draft' setzen -----
        gone_skus = set(state.keys()) - seen_skus
        for sku in sorted(gone_skus):
            row = build_deactivate_row(sku, state[sku])
            writer.writerow(row)
            stats["deactivated"] += 1

    # State speichern
    state_path.write_text(json.dumps(new_state, indent=2, ensure_ascii=False), encoding="utf-8")

    # Schlussreport
    log.info("=" * 60)
    log.info(f"GENERIERTE DATEI: {out_path}  ({out_path.stat().st_size:,} Bytes)")
    log.info("ZUSAMMENFASSUNG")
    for k, v in stats.items():
        log.info(f"  {k:>14}: {v}")
    if stats["enriched"]:
        pct = stats["enriched"] / max(stats["kept"], 1) * 100
        log.info(f"  → Anreicherungs-Quote: {pct:.1f}%")
    if filter_reasons:
        log.info("  Filter-Gründe:")
        for r, c in sorted(filter_reasons.items(), key=lambda x: -x[1]):
            log.info(f"    {c:>4}x  {r}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
