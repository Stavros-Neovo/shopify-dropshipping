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
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

from csv_loader import load_supplier_feed
from pricing import calculate_vk

PRICE_OVERRIDES_FILE = "price_overrides.yaml"

def load_price_overrides(path: str) -> Dict[str, float]:
    """Lädt manuelle Preisüberschreibungen aus price_overrides.yaml."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(open(p, encoding="utf-8")) or {}
    return {str(k): float(v) for k, v in data.items()}

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
    "SEO Title",                   # Meta-Title (Suchergebnis-Überschrift)
    "SEO Description",             # Meta-Description (Suchergebnis-Text)
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
    # Google Shopping (Merchant Center) — Namespace mm-google-shopping liest die Google-&-YouTube-App
    "Metafield: mm-google-shopping.condition [single_line_text_field]",     # immer 'new'
    "Metafield: mm-google-shopping.mpn [single_line_text_field]",           # Hersteller-Teilenr. (rettet Produkte ohne GTIN)
    "Metafield: mm-google-shopping.custom_label_0 [single_line_text_field]", # Kampagnen-Segment (Hygiene / Kategorie)
]


_EBAY_CAT_NAMES: Optional[dict] = None
_EBAY_SKU_CAT: Optional[dict] = None


def ebay_category_name(product: dict) -> str:
    """Liefert den deutschen eBay-Kategorie-Namen für ein Produkt — exakt die
    eBay-Logik (Titel-Keywords zuerst, sonst BAB-ItemMainGroup-Map). Wird als
    Shopify-'Type' genutzt, damit Shopify dieselbe Kategorisierung wie eBay hat."""
    global _EBAY_CAT_NAMES, _EBAY_SKU_CAT
    if _EBAY_CAT_NAMES is None:
        try:
            _EBAY_CAT_NAMES = json.loads(Path("ebay_category_names_cache.json").read_text(encoding="utf-8"))
        except Exception:
            _EBAY_CAT_NAMES = {}
        # Per-SKU eBay-Kategorien aus Taxonomy-API (categorize_for_shopify.py) — Vorrang
        try:
            _EBAY_SKU_CAT = json.loads(Path("ebay_sku_category.json").read_text(encoding="utf-8"))
        except Exception:
            _EBAY_SKU_CAT = {}
    fallback = product.get("category", "") or "Sonstiges"
    cid = _EBAY_SKU_CAT.get(product.get("sku", ""))
    if not cid:
        try:
            from ebay_client import get_ebay_category_id, _guess_category_from_title
            cid = _guess_category_from_title(product.get("title", "")) or get_ebay_category_id(product.get("category", ""))
        except Exception:
            return fallback
    name = _EBAY_CAT_NAMES.get(str(cid)) or _EBAY_CAT_NAMES.get(cid)
    return name or fallback


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


def _fallback_description(bab_title: str, brand_stored: str = "") -> str:
    """Wird genutzt wenn Icecat keine Beschreibungstexte hat - liefert mehr als
    nur den nackten Titel (sonst geht das 1:1 so an eBay/Shopify raus)."""
    from title_optimizer import extract_brand_from_title
    brand = extract_brand_from_title(bab_title, brand_stored)
    lines = [f"<p>{bab_title}</p>"]
    if brand:
        lines.append(f"<p>Marke: {brand}</p>")
    return "\n".join(lines)


def build_description_html(bab_title: str, enrichment: Optional[dict]) -> str:
    """Baut HTML-Body für Shopify aus Bab-Titel + Enrichment-Daten."""
    if not enrichment:
        return _fallback_description(bab_title)

    title_norm = bab_title.strip().lower()

    def is_real_text(val: str) -> bool:
        # Mehrere Quellen (ddg_images, manual, scraper) haben den Titel 1:1 in
        # short_summary/long_summary kopiert statt einer echten Beschreibung -
        # das zaehlt NICHT als verwertbarer Text, auch wenn das Feld nicht leer ist
        return bool(val) and val.strip().lower() != title_norm

    lead = ""
    for field in ("marketing_text", "long_summary", "short_summary"):
        candidate = enrichment.get(field) or ""
        if is_real_text(candidate):
            lead = candidate
            break

    # Immer eine substantielle Basis: echter Lead-Text, sonst Titel+Marke-Fallback
    # (nie nur ein nackter Link oder eine Spec-Tabelle ohne Einleitung)
    parts = [f"<p>{lead}</p>" if lead else _fallback_description(bab_title, enrichment.get("brand", ""))]

    specs = enrichment.get("specs_html") or ""
    if specs:
        parts.append(f'<div class="product-specs">{specs}</div>')

    mfr_url = enrichment.get("manufacturer_url") or ""
    if mfr_url and mfr_url.startswith("http"):
        parts.append(f'<p><small><a href="{mfr_url}" target="_blank" rel="noopener">Herstellerinformationen</a></small></p>')

    return "\n".join(parts)


def build_meta_description(body_html: str, fallback_title: str) -> str:
    """SEO Meta-Description: HTML raus, auf ~155 Zeichen am Wortende gekürzt.
    Shopify würde sonst einen rohen Body-Auszug nehmen - das hier ist sauberer."""
    import re, html
    text = re.sub(r"<[^>]+>", " ", body_html or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip() or fallback_title.strip()
    if len(text) <= 155:
        return text
    cut = text[:155].rsplit(" ", 1)[0]
    return cut + "…"


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


# ──────────────────────────────────────────────────────────────────────────
# Bild-Identitäts-Gate: ein Icecat-Bild wird NUR verwendet, wenn beweisbar ist,
# dass es zum BAB-Artikel gehört — d.h. BAB-Titel und Icecat-Titel teilen eine
# echte Modellnummer (MPN). Verhindert Falschbild-Retouren (Stand: 5/11 Retouren
# wegen falscher Bilder). Refurbished ausgeschlossen: deren EAN ist ein
# Refurbisher-Code, den Icecat auf ein anderes Produkt mappt.
_REFURB_SKU = re.compile(r"^(REF|REL|RER|RET|REU)", re.I)
_REFURB_KW  = re.compile(r"refurb|tecxl|upcycle|reteq|\bt1a\b|grade\s+[a-c]\b", re.I)


def _mpn_tokens(text: str) -> set:
    """Distinktive Modellnummern aus einem Titel: alphanumerisch, ≥6 Zeichen,
    mit MINDESTENS einem Buchstaben UND einer Ziffer (z.B. st8000vn004,
    cm8071504821112, 7800x3d). Reine Zahlen/Kapazitäten (16gb, 3200) zählen nicht."""
    out = set()
    for t in re.findall(r"[a-z0-9]{4,}", (text or "").lower()):
        if len(t) >= 6 and any(c.isalpha() for c in t) and any(c.isdigit() for c in t):
            out.add(t)
    return out


def image_identity_ok(bab_title: str, enrichment: Optional[dict], sku: str) -> bool:
    """True wenn das (per GTIN gematchte) Icecat-Bild dem BAB-Artikel vertraut werden darf.

    Haupt-Risiko für Falschbilder = REFURBISHED: deren EAN ist ein Refurbisher-Code,
    den Icecat auf ein anderes Produkt mappt → immer ausschließen.
    Für Neuware ist das Bild per GTIN gematcht (image_audit holt es über die exakte EAN)
    und damit korrekt; als Sanity-Check muss die Marke ODER die Modellnummer zwischen
    BAB- und Icecat-Titel übereinstimmen (fängt grobe Brand-Mismatches ab). Icecat-Titel
    lassen die Teilenummer oft weg, darum reicht der Marken-Match — ein striktes
    MPN-im-Titel-Muss würde ~85% korrekter Bilder fälschlich verwerfen."""
    if _REFURB_SKU.match(sku or "") or _REFURB_KW.search(bab_title or ""):
        return False
    if not enrichment:
        return True                        # Neuware + GTIN-Bild, kein Enrichment zum Abgleich
    bab = (bab_title or "").lower()
    brand = (enrichment.get("brand") or "").lower().split()
    if brand and brand[0] in bab:          # Marke stimmt überein
        return True
    return bool(_mpn_tokens(bab_title) & _mpn_tokens(enrichment.get("title_full") or ""))


# ──────────────────────────────────────────────────────────────────────────
# Hygiene-Preislogik (§312g BGB, versiegelt) — aggressive Volumen-Formel wie eBay:
# kein %-Markup, nur Versand + fester Mindestgewinn, keine Retouren-Rücklage.
# Erkennung identisch zu eBay: is_hygiene_category über die eBay-Kategorie der SKU.
try:
    from ebay_fees import is_hygiene_category
except Exception:                       # ebay_fees nicht ladbar → keine Hygiene-Sonderpreise
    def is_hygiene_category(_cat):
        return False


def load_sku_categories(path: str = "ebay_sku_category.json") -> Dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    return {k: str(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}


def is_hygiene_sku(sku: str, sku_cat: Dict[str, str]) -> bool:
    cat = sku_cat.get(sku)
    return bool(cat) and is_hygiene_category(cat)


def load_sku_mpn(path: str = "bab_preisliste.csv") -> Dict[str, str]:
    """SKU → Hersteller-Teilenummer (ReferenceNumber) für Google-Shopping-MPN."""
    p = Path(path)
    out: Dict[str, str] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            sku = (row.get("ItemNo") or "").strip()
            ref = (row.get("ReferenceNumber") or "").strip()
            if sku and ref:
                out[sku] = ref
    return out


def hygiene_pricing_cfg(pricing_cfg: dict) -> dict:
    """calculate_vk-taugliche Config für Hygiene: VK_netto = EK + Versand + Mindestgewinn."""
    hyg = pricing_cfg.get("hygiene", {}) or {}
    ship = float(hyg.get("shipping_buffer_eur", 5.0))
    profit = float(hyg.get("min_profit_eur", 1.5))
    return {
        "vat_rate": pricing_cfg["vat_rate"],
        "tiers": [{"ek_max": 9999999.0, "markup": float(hyg.get("markup", 0.0))}],
        "min_absolute_margin_eur": ship + profit,
        "shipping_buffer": [{"ek_max": 9999999.0, "buffer_eur": ship}],
        "rounding_strategy": pricing_cfg.get("rounding_strategy", "psychological_99"),
    }


def build_rows(product: dict, pr, cfg: dict,
               enrichment: Optional[dict] = None,
               max_images: int = 5,
               verified_images: Optional[list[str]] = None,
               mpn_image: Optional[str] = None,
               hygiene: bool = False,
               mpn_code: str = "") -> list[dict]:
    """
    Wandelt ein Produkt in eine oder mehrere Matrixify-CSV-Zeilen.

    - Erste Zeile: vollständige Produkt-Daten + erstes Bild
    - Folgezeilen: nur Handle + Image-Felder (für weitere Bilder)
    """
    handle = slugify(product["sku"] + "-" + product.get("title", ""))
    now = datetime.now().isoformat(timespec="seconds")

    # Bilder: NUR auflösungsgeprüfte aus supplier_map.json (≥500×500), exakt wie
    # eBay (sync.py). KEIN Enrichment-Fallback - das wäre das ungeprüfte, oft
    # 200×200 ipcstore-Bild, das eBay ablehnt. Ohne verifiziertes Bild → draft.
    # ZUSÄTZLICH: Identitäts-Gate - Bild nur, wenn Modellnummer in BAB- UND
    # Icecat-Titel übereinstimmt (sonst Falschbild-Retoure). Sonst → draft.
    # mpn_image kommt aus der Icecat-Brand+MPN-Suche und ist bereits per
    # BrandPartCode==MPN verifiziert → Gate bereits bestanden, direkt nutzen.
    if mpn_image:
        all_images: list[str] = [mpn_image]
    elif image_identity_ok(product.get("title", ""), enrichment, product.get("sku", "")):
        all_images = list(verified_images or [])[:max_images]
    else:
        all_images = []
    first_image = all_images[0] if all_images else ""

    title = (enrichment.get("title_full") if enrichment else "") or \
            product.get("title", "Unbenanntes Produkt")
    title = title[:250]

    body_html = build_description_html(product.get("title", ""), enrichment)

    # SEO an der Quelle einbauen (manuelle Shopify-Edits würde der Sync sonst
    # bei jedem Lauf überschreiben). Produkttitel = bester Meta-Title: er trägt
    # Marke + Modell (= die Suchbegriffe), auf ~70 Z. fürs SERP-Display gekappt.
    # ponytail: title_optimizer (eBay) bewusst NICHT genutzt - der strippt das Modell.
    seo_title = title if len(title) <= 70 else title[:70].rsplit(" ", 1)[0]
    seo_description = build_meta_description(body_html, title)

    tags = list(filter(None, [
        product.get("category", ""),
        product.get("brand", ""),
    ]))
    if not first_image:
        tags.append("import-ohne-bild")
    if hygiene:
        tags.append("Hygiene")            # für Smart-Collection + Landing-Highlight

    google_cat = get_google_category(product)

    rows: list[dict] = []

    # Hauptzeile (volles Produkt)
    rows.append({
        "ID": "",
        "Handle": handle,
        "Command": "MERGE",
        "Title": title,
        "Body HTML": body_html,
        "SEO Title": seo_title,
        "SEO Description": seo_description,
        "Vendor": product.get("brand", "") or "",
        "Type": ebay_category_name(product),
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
        # Google Shopping
        "Metafield: mm-google-shopping.condition [single_line_text_field]": "new",
        "Metafield: mm-google-shopping.mpn [single_line_text_field]": mpn_code or "",
        "Metafield: mm-google-shopping.custom_label_0 [single_line_text_field]":
            "Hygiene" if hygiene else "Technik",
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

    # supplier_map.json: auflösungsgeprüfte Bilder (≥500×500) pro SKU, gleiche
    # Quelle wie eBay (sync.py). Hat Vorrang vor dem ungeprüften Enrichment-Bild.
    smap_path = Path("supplier_map.json")
    smap: Dict[str, dict] = (
        json.loads(smap_path.read_text(encoding="utf-8")) if smap_path.exists() else {}
    )
    verified_count = sum(1 for v in smap.values() if v.get("image_verified"))
    log.info(f"supplier_map.json: {verified_count} SKUs mit auflösungsgeprüftem Bild")

    # Icecat-MPN-Bilder (Brand+Modellnummer-Suche, bereits per BrandPartCode==MPN
    # verifiziert, icecat.biz/rechtssicher). NUR Shopify-Bildquelle — supplier_map
    # und damit eBay bleiben unangetastet.
    mpn_images_path = Path("icecat_mpn_images.json")
    mpn_images: Dict[str, str] = (
        json.loads(mpn_images_path.read_text(encoding="utf-8")) if mpn_images_path.exists() else {}
    )
    if mpn_images:
        log.info(f"Icecat-MPN-Bilder (zusätzliche Shopify-Quelle): {len(mpn_images)} SKUs")

    # Hygiene: eBay-Kategorien pro SKU + aggressive Preis-Config (wie eBay)
    sku_cat = load_sku_categories()
    hyg_cfg = hygiene_pricing_cfg(cfg["pricing"])
    log.info(f"Hygiene-Erkennung: {len(sku_cat)} SKU→eBay-Kategorie geladen")
    sku_mpn = load_sku_mpn()   # SKU→ReferenceNumber für Google-Shopping-MPN
    if sku_mpn:
        log.info(f"Google-Shopping-MPN: {len(sku_mpn)} SKU→Teilenummer geladen")

    # Preisüberschreibungen laden
    price_overrides = load_price_overrides(PRICE_OVERRIDES_FILE)
    if price_overrides:
        log.info(f"Preisüberschreibungen geladen: {len(price_overrides)} SKUs")

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

            sku = product["sku"]
            # Hygiene-Artikel → aggressive Volumen-Formel (wie eBay), sonst Standard
            hyg = is_hygiene_sku(sku, sku_cat)
            try:
                pr = calculate_vk(product["purchase_price"], hyg_cfg if hyg else cfg["pricing"])
            except Exception as e:
                log.error(f"Pricing-Fehler für SKU {product.get('sku')}: {e}")
                stats["errors"] += 1
                continue

            ean = (product.get("ean") or "").strip()
            enrichment = enrichment_idx.get(ean) if ean else None
            if enrichment:
                stats["enriched"] += 1
            else:
                stats["without_image"] += 1

            # Preisüberschreibung anwenden falls vorhanden
            if sku in price_overrides:
                pr.vk_gross = price_overrides[sku]
                log.info(f"  ↑ Preisüberschreibung: {sku} → {price_overrides[sku]:.2f}€")

            # Auflösungsgeprüfte Bilder aus supplier_map.json (wie eBay)
            verified_images = None
            v = smap.get(sku, {})
            if v.get("image_verified"):
                verified_images = v.get("images") or (
                    [v["image_url"]] if v.get("image_url") else None
                )

            rows = build_rows(product, pr, cfg, enrichment=enrichment,
                              verified_images=verified_images,
                              mpn_image=mpn_images.get(sku),
                              hygiene=hyg,
                              mpn_code=sku_mpn.get(sku, ""))
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
