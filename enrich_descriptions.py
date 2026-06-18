"""
enrich_descriptions.py — Produktbeschreibungen via Icecat + Claude API
=======================================================================
Liest enrichment_index.csv, findet Einträge ohne long_summary,
fragt Icecat Open Catalog per EAN ab und verbessert die Beschreibung
optional mit Claude claude-haiku-4-5 (Anthropic API).

Ergebnis: enrichment_index.csv mit gefüllten long_summary / specs_html.
Danach: smart_lister --list überträgt die Beschreibungen an eBay.

Aufruf:
  python enrich_descriptions.py              # Alle ohne Beschreibung
  python enrich_descriptions.py --all        # Alle (auch vorhandene verbessern)
  python enrich_descriptions.py --dry-run    # Nur anzeigen, nicht speichern
  python enrich_descriptions.py --limit 50   # Nur 50 Artikel pro Run

Benötigte Secrets (GitHub Actions / .env):
  ICECAT_USER     — Icecat Open Catalog Username (kostenlos: icecat.us)
  ICECAT_PASS     — Icecat Passwort
  ANTHROPIC_API_KEY — optional, für KI-Verbesserung (claude-haiku-4-5)
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
SUPPLIER_MAP = "supplier_map.json"

ICECAT_API   = "https://icecat.us/api/products"
CLAUDE_API   = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Icecat
# ---------------------------------------------------------------------------

def fetch_icecat(ean: str, user: str, password: str, lang: str = "DE") -> dict | None:
    """
    Fragt Icecat Open Catalog per EAN ab.
    Gibt dict mit {long_summary, short_summary, specs_html, marketing_text, brand, title} oder None zurück.
    """
    try:
        resp = requests.get(
            ICECAT_API,
            params={
                "UserName": user,
                "Password": password,
                "Content":  "Full",
                "GTIN":     ean,
                "lang":     lang,
                "output":   "json",
            },
            timeout=15,
        )
        if resp.status_code == 401:
            log.error("Icecat: Ungültige Zugangsdaten (401)")
            return None
        if resp.status_code == 404:
            log.debug(f"Icecat: EAN {ean} nicht gefunden (404)")
            return None
        if not resp.ok:
            log.warning(f"Icecat: HTTP {resp.status_code} für EAN {ean}")
            return None

        data = resp.json()
        # Icecat gibt {"data": {...}} oder direkt das Produkt
        product = data.get("data") or data

        if not product or product.get("Code") == -1:
            return None

        # Beschreibungen extrahieren
        long_desc  = ""
        short_desc = ""
        marketing  = ""

        for desc in product.get("ProductDescription", []):
            if desc.get("langid") == "9":  # DE
                long_desc  = desc.get("LongDesc",    "").strip()
                short_desc = desc.get("ShortDesc",   "").strip()
                marketing  = desc.get("ShortSummary","").strip()
                break
        # Fallback Englisch
        if not long_desc:
            for desc in product.get("ProductDescription", []):
                long_desc  = desc.get("LongDesc",    "").strip()
                short_desc = desc.get("ShortDesc",   "").strip()
                if long_desc:
                    break

        # Specs als HTML-Tabelle
        specs_html = _build_specs_html(product.get("FeaturesGroups", []))

        brand = (product.get("Supplier") or {}).get("name", "")
        title = product.get("Title", "")

        return {
            "long_summary":   long_desc,
            "short_summary":  short_desc,
            "marketing_text": marketing,
            "specs_html":     specs_html,
            "brand":          brand,
            "title":          title,
        }

    except Exception as e:
        log.warning(f"Icecat Fehler für EAN {ean}: {e}")
        return None


def _build_specs_html(features_groups: list) -> str:
    """Konvertiert Icecat FeaturesGroups in eine HTML-Tabelle."""
    if not features_groups:
        return ""

    rows = []
    for group in features_groups:
        group_name = group.get("FeatureGroup", {}).get("Name", {})
        if isinstance(group_name, dict):
            group_name = group_name.get("Value", "")

        for feat in group.get("Features", []):
            name  = feat.get("Feature", {}).get("Name", {})
            value = feat.get("LocalValue", feat.get("Value", ""))
            unit  = feat.get("Feature", {}).get("Measure", {}).get("Signs", {})

            if isinstance(name,  dict): name  = name.get("Value",  "")
            if isinstance(value, list): value = ", ".join(str(v) for v in value)
            if isinstance(unit,  dict): unit  = unit.get("_", "")

            if name and value:
                display_value = f"{value} {unit}".strip() if unit else str(value)
                rows.append(f"<tr><td><strong>{name}</strong></td><td>{display_value}</td></tr>")

    if not rows:
        return ""

    return (
        "<table style='border-collapse:collapse;width:100%'>"
        + "".join(rows)
        + "</table>"
    )


# ---------------------------------------------------------------------------
# Claude API — Beschreibung verbessern
# ---------------------------------------------------------------------------

def improve_with_claude(title: str, long_desc: str, specs_html: str, api_key: str) -> str:
    """
    Verbessert eine Produktbeschreibung mit claude-haiku-4-5.
    Gibt verbesserten deutschen HTML-Text zurück.
    """
    specs_text = re.sub(r"<[^>]+>", " ", specs_html)[:800] if specs_html else ""

    prompt = f"""Du bist ein eBay-Texter. Schreibe eine deutsche Produktbeschreibung für eBay.

Produkt: {title}
Hersteller-Beschreibung: {long_desc[:600]}
Technische Daten: {specs_text}

Schreibe 2-3 Absätze (HTML <p>-Tags), die:
- Die wichtigsten Vorteile und Anwendungsfälle nennen
- Auf Deutsch formuliert sind
- Kaufmotivierend sind (keine Superlative)
- KEIN Preis, KEINE Lieferzeit, KEINE Garantieversprechen

Antworte NUR mit dem HTML (nur <p>-Tags, kein <html>/<body>/<div>)."""

    try:
        resp = requests.post(
            CLAUDE_API,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": 600,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if not resp.ok:
            log.warning(f"Claude API Fehler: {resp.status_code} — {resp.text[:200]}")
            return long_desc

        result = resp.json()
        improved = result.get("content", [{}])[0].get("text", "").strip()
        return improved if improved else long_desc

    except Exception as e:
        log.warning(f"Claude API Fehler: {e}")
        return long_desc


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

def load_enrichment_index(path: str = ENRICHMENT) -> tuple[list[str], list[dict]]:
    """Gibt (fieldnames, rows) zurück."""
    rows = []
    fieldnames = []
    if not Path(path).exists():
        return fieldnames, rows
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    return list(fieldnames), rows


def save_enrichment_index(fieldnames: list[str], rows: list[dict], path: str = ENRICHMENT):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def get_supplier_map_eans() -> set[str]:
    """EANs aus supplier_map.json — nur für diese Artikel lohnt sich die Arbeit."""
    try:
        sm = json.loads(Path(SUPPLIER_MAP).read_text(encoding="utf-8"))
        return {v.get("ean", "") for v in sm.values() if v.get("ean")}
    except Exception:
        return set()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Icecat + Claude Produktbeschreibungen")
    parser.add_argument("--all",     action="store_true", help="Auch vorhandene Beschreibungen erneuern")
    parser.add_argument("--dry-run", action="store_true", help="Nichts speichern, nur anzeigen")
    parser.add_argument("--limit",   type=int, default=200, help="Max Artikel pro Run (Standard: 200)")
    parser.add_argument("--lang",    default="DE", help="Icecat Sprache (Standard: DE)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    icecat_user = os.getenv("ICECAT_USER", "")
    icecat_pass = os.getenv("ICECAT_PASS", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not icecat_user or not icecat_pass:
        log.error("ICECAT_USER / ICECAT_PASS fehlen — Registrierung: https://icecat.us/de/register.html")
        sys.exit(1)

    use_claude = bool(anthropic_key)
    if use_claude:
        log.info(f"Claude KI-Verbesserung: aktiviert (Modell: {CLAUDE_MODEL})")
    else:
        log.info("Claude KI-Verbesserung: deaktiviert (kein ANTHROPIC_API_KEY)")

    # Daten laden
    fieldnames, rows = load_enrichment_index()
    if not rows:
        log.error(f"{ENRICHMENT} nicht gefunden")
        sys.exit(1)

    # Nur Artikel aus supplier_map bearbeiten (aktive Listings)
    active_eans = get_supplier_map_eans()
    log.info(f"Aktive EANs (supplier_map): {len(active_eans)}")

    # Zu bearbeitende Zeilen bestimmen
    to_process = []
    for row in rows:
        ean = row.get("ean", "").strip()
        if not ean or ean not in active_eans:
            continue
        has_desc = bool(row.get("long_summary", "").strip())
        if has_desc and not args.all:
            continue
        to_process.append(row)

    to_process = to_process[:args.limit]
    log.info(f"Zu bearbeiten: {len(to_process)} Artikel (Limit: {args.limit})")

    if not to_process:
        log.info("Alle aktiven Artikel haben bereits Beschreibungen ✓")
        sys.exit(0)

    # EAN → row-Index Mapping
    ean_to_idx = {r.get("ean", ""): i for i, r in enumerate(rows)}

    updated = 0
    improved = 0
    not_found = 0

    for i, row in enumerate(to_process, 1):
        ean   = row.get("ean", "").strip()
        title = row.get("title_seo") or row.get("title_full") or ean

        log.info(f"[{i}/{len(to_process)}] EAN {ean} | {title[:50]}")

        icecat_data = fetch_icecat(ean, icecat_user, icecat_pass, lang=args.lang)

        if not icecat_data:
            log.warning(f"  Icecat: nicht gefunden")
            not_found += 1
            time.sleep(0.3)
            continue

        long_desc  = icecat_data.get("long_summary",   "")
        short_desc = icecat_data.get("short_summary",  "")
        marketing  = icecat_data.get("marketing_text", "")
        specs_html = icecat_data.get("specs_html",     "")
        brand      = icecat_data.get("brand",          "")

        log.info(f"  Icecat: {len(long_desc)} Zeichen Beschreibung, {len(specs_html)} Zeichen Specs")

        # Claude-Verbesserung
        if use_claude and long_desc:
            log.info(f"  Claude: Beschreibung verbessern...")
            long_desc = improve_with_claude(title, long_desc, specs_html, anthropic_key)
            improved += 1
            time.sleep(0.5)  # Rate-limit

        if args.dry_run:
            log.warning(f"  DRY-RUN — würde schreiben: {len(long_desc)} Zeichen Beschreibung")
            continue

        # Zeile aktualisieren
        idx = ean_to_idx.get(ean)
        if idx is not None:
            if long_desc:  rows[idx]["long_summary"]   = long_desc
            if short_desc: rows[idx]["short_summary"]  = short_desc
            if marketing:  rows[idx]["marketing_text"] = marketing
            if specs_html: rows[idx]["specs_html"]     = specs_html
            if brand and not rows[idx].get("brand"): rows[idx]["brand"] = brand
            updated += 1

        time.sleep(0.3)

    # Speichern
    if not args.dry_run and updated > 0:
        save_enrichment_index(fieldnames, rows)
        log.info(f"enrichment_index.csv gespeichert: {updated} Artikel aktualisiert")

    log.info(
        f"=== Fertig: {updated} aktualisiert | "
        f"{improved} KI-verbessert | "
        f"{not_found} nicht in Icecat ==="
    )


if __name__ == "__main__":
    main()
