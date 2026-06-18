"""
enrich_descriptions.py — Produktbeschreibungen + Bilder via Icecat
===================================================================
Workflow:
  1. BAB Preisliste CSV → alle EANs mit Lagerbestand > 0
  2. supplier_map.json → alle gelisteten EANs (mit vk-Preis)
  3. enrichment_index.csv → fehlende Beschreibungen + Bilder via Icecat
  4. Optional: Claude Haiku verbessert Beschreibungen

Aufruf:
  python enrich_descriptions.py              # Fehlende Beschreibungen
  python enrich_descriptions.py --all        # Alle neu laden
  python enrich_descriptions.py --dry-run    # Nur anzeigen
  python enrich_descriptions.py --limit 50   # Max 50 Artikel
  python enrich_descriptions.py --images-only # Nur Bilder ergänzen
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

log = logging.getLogger("enrich_desc")

ENRICHMENT   = "enrichment_index.csv"
BAB_CSV      = "bab_preisliste.csv"
SUPPLIER_MAP = "supplier_map.json"

ICECAT_API   = "https://live.icecat.biz/api"
ICECAT_USER  = "neovogen"
ICECAT_TOKEN = "a923fe60-04bd-4f83-ae2e-a1e1a8427c98"

CLAUDE_API   = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# EANs ermitteln
# ---------------------------------------------------------------------------

def get_listed_eans() -> set[str]:
    """
    Alle EANs die aktiv gelistet sind = in supplier_map MIT vk-Preis.
    """
    try:
        sm = json.loads(Path(SUPPLIER_MAP).read_text(encoding="utf-8"))
        return {v.get("ean", "") for v in sm.values()
                if v.get("ean") and v.get("vk")}
    except Exception as e:
        log.warning(f"supplier_map.json Fehler: {e}")
        return set()


def get_bab_eans() -> set[str]:
    """
    EANs aus der BAB Preisliste mit Stock > 0.
    """
    try:
        eans = set()
        with open(BAB_CSV, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                gtin = (row.get("GTIN") or "").strip()
                stock = (row.get("Stock") or "0").strip()
                try:
                    stock_int = int(stock)
                except ValueError:
                    stock_int = 0
                if gtin and stock_int > 0:
                    eans.add(gtin)
        return eans
    except Exception as e:
        log.warning(f"BAB CSV Fehler: {e}")
        return set()


# ---------------------------------------------------------------------------
# Icecat
# ---------------------------------------------------------------------------

def fetch_icecat(ean: str, lang: str = "de") -> dict | None:
    """
    Fragt live.icecat.biz per EAN (GTIN) ab.
    Gibt dict mit Beschreibungen, Specs und Bildern zurück.
    """
    try:
        resp = requests.get(
            ICECAT_API,
            params={
                "UserName": ICECAT_USER,
                "Token":    ICECAT_TOKEN,
                "Language": lang,
                "GTIN":     ean,
            },
            timeout=20,
        )
        if not resp.ok:
            log.debug(f"Icecat HTTP {resp.status_code} für EAN {ean}")
            return None

        data = resp.json()
        if data.get("msg") != "OK" or not data.get("data"):
            return None

        product = data["data"]
        gi  = product.get("GeneralInfo", {}) or {}
        img = product.get("Image", {}) or {}

        # Beschreibungen
        desc   = gi.get("Description", {}) or {}
        long_d = (desc.get("LongDesc", "") or "").strip()
        short_d = (desc.get("ShortDesc", "") or "").strip()
        summ   = gi.get("SummaryDescription", {}) or {}
        marketing = (summ.get("LongSummaryDescription", "") or "").strip()

        # Specs
        specs_html = _build_specs_html(product.get("FeaturesGroups", []))

        # Bilder
        image_main = (img.get("HighPic") or img.get("Pic") or "").strip()
        images_all_raw = product.get("Gallery", []) or []
        images_all = "|".join(
            g.get("Pic", "") for g in images_all_raw if g.get("Pic")
        )
        if image_main and not images_all:
            images_all = image_main

        brand = (gi.get("Brand") or gi.get("BrandInfo", {}).get("BrandName") or "").strip()
        title = (gi.get("Title") or gi.get("ProductName") or "").strip()

        return {
            "long_summary":   long_d,
            "short_summary":  short_d,
            "marketing_text": marketing,
            "specs_html":     specs_html,
            "image_main":     image_main,
            "images_all":     images_all,
            "brand":          brand,
            "title":          title,
        }

    except Exception as e:
        log.warning(f"Icecat Fehler für EAN {ean}: {e}")
        return None


def _build_specs_html(features_groups: list) -> str:
    if not features_groups:
        return ""
    rows = []
    for group in features_groups:
        for feat in group.get("Features", []):
            name  = feat.get("Feature", {}).get("Name", {})
            value = feat.get("LocalValue", feat.get("Value", ""))
            unit  = feat.get("Feature", {}).get("Measure", {}).get("Signs", {})
            if isinstance(name,  dict): name  = name.get("Value", "")
            if isinstance(value, list): value = ", ".join(str(v) for v in value)
            if isinstance(unit,  dict): unit  = unit.get("_", "")
            if name and value:
                display = f"{value} {unit}".strip() if unit else str(value)
                rows.append(f"<tr><td><strong>{name}</strong></td><td>{display}</td></tr>")
    if not rows:
        return ""
    return "<table style='border-collapse:collapse;width:100%'>" + "".join(rows) + "</table>"


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def improve_with_claude(title: str, long_desc: str, specs_html: str, api_key: str) -> str:
    specs_text = re.sub(r"<[^>]+>", " ", specs_html)[:800] if specs_html else ""
    prompt = f"""Du bist ein eBay-Texter. Schreibe eine professionelle deutsche Produktbeschreibung für eBay.

Produkt: {title}
Hersteller-Beschreibung: {long_desc[:600]}
Technische Daten: {specs_text}

Schreibe 2-3 Absätze (HTML <p>-Tags):
- Wichtigste Vorteile und Anwendungsfälle
- Auf Deutsch, kaufmotivierend (keine Superlative)
- KEIN Preis, KEINE Lieferzeit, KEINE Garantieversprechen, KEINE Links

Antworte NUR mit dem HTML (nur <p>-Tags)."""

    try:
        resp = requests.post(
            CLAUDE_API,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if resp.ok:
            result = resp.json().get("content", [{}])[0].get("text", "").strip()
            return result if result else long_desc
    except Exception as e:
        log.warning(f"Claude Fehler: {e}")
    return long_desc


# ---------------------------------------------------------------------------
# enrichment_index I/O
# ---------------------------------------------------------------------------

def load_enrichment(path: str = ENRICHMENT) -> tuple[list[str], list[dict]]:
    if not Path(path).exists():
        return [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def save_enrichment(fieldnames: list[str], rows: list[dict], path: str = ENRICHMENT):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",         action="store_true", help="Auch vorhandene Einträge neu laden")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--limit",       type=int, default=200)
    parser.add_argument("--lang",        default="de")
    parser.add_argument("--images-only", action="store_true", help="Nur Bilder ergänzen")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    use_claude = bool(anthropic_key) and not args.images_only
    if use_claude:
        log.info(f"Claude KI: aktiviert ({CLAUDE_MODEL})")

    # EANs aus BAB + supplier_map
    bab_eans    = get_bab_eans()
    listed_eans = get_listed_eans()
    target_eans = listed_eans  # Alle mit aktivem Listing
    if bab_eans:
        # Priorisiere EANs die AUCH im BAB-Feed mit Stock sind
        priority = listed_eans & bab_eans
        rest     = listed_eans - bab_eans
        target_eans_ordered = list(priority) + list(rest)
    else:
        target_eans_ordered = list(listed_eans)

    log.info(f"Gelistete EANs (supplier_map): {len(listed_eans)}")
    log.info(f"BAB EANs mit Stock > 0:        {len(bab_eans)}")
    log.info(f"Prio-EANs (beide):             {len(listed_eans & bab_eans) if bab_eans else 'n/a'}")

    # enrichment_index laden
    fieldnames, rows = load_enrichment()
    if not rows:
        log.error(f"{ENRICHMENT} nicht gefunden")
        sys.exit(1)

    ean_to_idx = {r.get("ean", ""): i for i, r in enumerate(rows)}

    # Zu bearbeitende EANs bestimmen
    to_process = []
    for ean in target_eans_ordered:
        if ean not in ean_to_idx:
            # EAN noch nicht in enrichment_index → überspringen (kein Titel bekannt)
            continue
        idx = ean_to_idx[ean]
        row = rows[idx]
        has_desc  = bool(row.get("long_summary", "").strip())
        has_image = bool(row.get("image_main", "").strip())

        if args.all:
            to_process.append(ean)
        elif args.images_only:
            if not has_image:
                to_process.append(ean)
        else:
            if not has_desc or not has_image:
                to_process.append(ean)

    to_process = to_process[:args.limit]
    log.info(f"Zu bearbeiten: {len(to_process)} Artikel (Limit: {args.limit})")

    if not to_process:
        log.info("✓ Alle gelisteten Artikel haben Beschreibungen und Bilder")
        sys.exit(0)

    updated = improved = not_found = 0

    for i, ean in enumerate(to_process, 1):
        idx   = ean_to_idx[ean]
        row   = rows[idx]
        title = (row.get("title_seo") or row.get("title_full") or ean)
        log.info(f"[{i}/{len(to_process)}] EAN {ean} | {title[:50]}")

        data = fetch_icecat(ean, lang=args.lang)
        if not data:
            log.warning(f"  Icecat: nicht gefunden")
            not_found += 1
            time.sleep(0.3)
            continue

        long_d  = data.get("long_summary", "")
        short_d = data.get("short_summary", "")
        marketing = data.get("marketing_text", "")
        specs   = data.get("specs_html", "")
        img_main = data.get("image_main", "")
        imgs_all = data.get("images_all", "")
        brand   = data.get("brand", "")

        log.info(f"  Icecat: desc={len(long_d)}Z, specs={len(specs)}Z, img={'✓' if img_main else '✗'}")

        if use_claude and long_d:
            log.info(f"  Claude: verbessern...")
            long_d = improve_with_claude(title, long_d, specs, anthropic_key)
            improved += 1
            time.sleep(0.5)

        if args.dry_run:
            log.warning(f"  DRY-RUN")
            continue

        if long_d:  row["long_summary"]   = long_d
        if short_d: row["short_summary"]  = short_d
        if marketing: row["marketing_text"] = marketing
        if specs:   row["specs_html"]     = specs
        if img_main: row["image_main"]    = img_main
        if imgs_all: row["images_all"]    = imgs_all
        if brand and not row.get("brand"): row["brand"] = brand
        updated += 1

        time.sleep(0.3)

    if not args.dry_run and updated > 0:
        save_enrichment(fieldnames, rows)

    log.info(
        f"=== Fertig: {updated} aktualisiert | "
        f"{improved} KI-verbessert | "
        f"{not_found} nicht in Icecat ==="
    )


if __name__ == "__main__":
    main()
