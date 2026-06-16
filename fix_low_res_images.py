"""
fix_low_res_images.py
=====================
Repariert eBay-Listings mit eBay Error 25002 (Bild-Auflösung zu gering)
und verhindert Falschbilder (Set-Fotos für Einzelartikel etc.).

Bild-Quellen (Priorität):
  1. Icecat      — Hersteller-Originalbilder, höchste Qualität & Genauigkeit
  2. artikeldaten.csv (images_xl) — Kosatec CDN, gute Qualität
  3. Kein Bild   — DDG deaktiviert (zu unzuverlässig, liefert Falschbilder)

Validierung jedes Bildes:
  - URL darf keine Set/Bundle/Kit-Keywords enthalten
  - Wenn Titel "single", "einzeln", "1x", "1 stück" enthält →
    nur Bilder ohne Set-Indikatoren akzeptieren
  - Wenn URL "set", "4er", "bundle", "kit", "pack", "combo", "multipack"
    enthält → Bild ablehnen

Aufruf:
  python3 fix_low_res_images.py                  # alle SKUs
  python3 fix_low_res_images.py --limit 50       # nur 50 auf einmal
  python3 fix_low_res_images.py --dry-run        # nur anzeigen
  python3 fix_low_res_images.py --sku PPSE0102   # einzelne SKU
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient

log = logging.getLogger("fix_images")

IMAGE_FIX_FILE   = "image_fix_needed.json"
ARTIKELDATEN_CSV = "artikeldaten.csv"
INVENTORY_PATH   = "/sell/inventory/v1/inventory_item"
DELAY            = 0.8

# Icecat-Zugangsdaten
ICECAT_USER  = "neovogen"
ICECAT_TOKEN = "a923fe60-04bd-4f83-ae2e-a1e1a8427c98"
ICECAT_LANG  = "de"

# Keywords die auf Set/Bundle/Falschbild hindeuten (in URL oder Dateiname)
SET_KEYWORDS_URL = [
    "set", "bundle", "kit", "pack", "combo", "multipack",
    "4er", "3er", "2er", "5er", "6er", "10er",
    "4pack", "3pack", "2pack", "5pack",
    "4-pack", "3-pack", "2-pack",
    "collection", "serie",
]

# Keywords die auf Einzelartikel im Titel hindeuten
SINGLE_KEYWORDS_TITLE = [
    "single", "einzeln", "1x ", "1 stück", "1 stuck",
    "1 piece", "1pc", "one piece",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
})


# ─── Bild-Validierung ─────────────────────────────────────────────────────────

def is_valid_image(url: str, title: str = "") -> bool:
    """
    Prüft ob ein Bild-URL für dieses Produkt geeignet ist.
    Lehnt Set/Bundle-Bilder ab wenn das Produkt ein Einzelartikel ist.
    """
    if not url or not url.startswith("http"):
        return False

    url_lower = url.lower()

    # URL-basierte Set-Erkennung
    for kw in SET_KEYWORDS_URL:
        # Nur im Dateinamen/Pfad suchen, nicht in der Domain
        path_part = url_lower.split("//", 1)[-1].split("/", 1)[-1] if "//" in url_lower else url_lower
        if kw in path_part:
            log.debug(f"  Bild abgelehnt (Set-Keyword '{kw}' in URL): {url[:70]}")
            return False

    # Titel-basierte Verschärfung: Einzelartikel → strenger prüfen
    title_lower = title.lower()
    is_single_product = any(kw in title_lower for kw in SINGLE_KEYWORDS_TITLE)
    if is_single_product:
        # Zusätzlich: Zahlen vor "er" im Dateinamen ablehnen (2er, 3er, ...)
        import re
        path_part = url_lower.split("//", 1)[-1].split("/", 1)[-1] if "//" in url_lower else url_lower
        if re.search(r'\d+er', path_part):
            log.debug(f"  Bild abgelehnt (Einzelartikel-Titel + Mengen-Pattern in URL): {url[:70]}")
            return False

    return True


# ─── Icecat ───────────────────────────────────────────────────────────────────

def fetch_icecat_images(ean: str, title: str = "") -> list[str]:
    """Holt Hersteller-Originalbilder von Icecat per EAN."""
    url = (
        f"https://live.icecat.biz/api"
        f"?UserName={ICECAT_USER}&Language={ICECAT_LANG}"
        f"&GTIN={ean}&Token={ICECAT_TOKEN}"
    )
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        if data.get("msg") != "OK" or not data.get("data"):
            return []

        img_data = data["data"].get("Image", {}) or {}
        high_pic = img_data.get("HighPic", "") or ""
        pics     = img_data.get("Pics", []) or []
        extra    = [p.get("Pic", "") for p in pics if p.get("Pic")]

        all_urls = [u for u in ([high_pic] + extra) if u.startswith("http")]
        valid    = [u for u in all_urls if is_valid_image(u, title)]

        if valid:
            log.info(f"  ✓ Icecat: {len(valid)} Bilder gefunden")
        return valid

    except Exception as e:
        log.debug(f"  Icecat-Fehler EAN {ean}: {e}")
        return []


# ─── artikeldaten.csv ─────────────────────────────────────────────────────────

def load_artikeldaten_index() -> dict:
    """Lädt artikeldaten.csv → {ean: [url1, url2, ...]} (images_xl)."""
    p = Path(ARTIKELDATEN_CSV)
    if not p.exists():
        log.warning(f"{ARTIKELDATEN_CSV} nicht gefunden")
        return {}
    index = {}
    with open(p, encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f, delimiter=";"):
            ean = (row.get("ean") or "").strip()
            raw = (row.get("images_xl") or row.get("images_l") or "").strip()
            if not ean or not raw:
                continue
            urls = [u.strip() for u in raw.split("|") if u.strip()]
            if urls:
                index[ean] = urls
    log.info(f"Artikeldaten-Index: {len(index)} EANs mit Bildern geladen")
    return index


# ─── eBay Inventory Item aktualisieren ────────────────────────────────────────

def update_inventory_images(client: EbayClient, sku: str, image_urls: list[str]) -> bool:
    """Ersetzt imageUrls im bestehenden Inventory Item."""
    try:
        existing = client._request("GET", f"{INVENTORY_PATH}/{sku}")
    except Exception as e:
        log.error(f"  GET Inventory Item {sku}: {e}")
        return False

    if not existing:
        log.warning(f"  Kein Inventory Item für SKU {sku}")
        return False

    existing.setdefault("product", {})
    existing["product"]["imageUrls"] = image_urls[:12]

    # Felder die beim PUT nicht erlaubt sind
    for field in ["sku", "locale", "packageWeightAndSize"]:
        existing.pop(field, None)

    try:
        client._request("PUT", f"{INVENTORY_PATH}/{sku}", json_body=existing)
        return True
    except Exception as e:
        log.error(f"  PUT Inventory Item {sku}: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",  default="config_shop2.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit",   type=int, default=0)
    parser.add_argument("--sku",     help="Nur eine bestimmte SKU fixen")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    load_dotenv()
    cfg    = yaml.safe_load(open(args.config, encoding="utf-8"))
    client = EbayClient.from_env(cfg["ebay"])

    # Fix-Liste laden
    fix_file = Path(IMAGE_FIX_FILE)
    if not fix_file.exists():
        log.info(f"Keine {IMAGE_FIX_FILE} — nichts zu tun.")
        return

    image_fix: dict = json.loads(fix_file.read_text(encoding="utf-8"))
    if not image_fix:
        log.info("image_fix_needed.json ist leer — nichts zu tun.")
        return

    # Gesperrte SKUs (Abmahnung / Markenrecht) niemals anfassen
    banned_file = Path("banned_skus.json")
    if banned_file.exists():
        try:
            banned_skus: dict = json.loads(banned_file.read_text(encoding="utf-8"))
            before = len(image_fix)
            image_fix = {k: v for k, v in image_fix.items() if k not in banned_skus}
            removed = before - len(image_fix)
            if removed:
                log.warning(f"⛔ {removed} gesperrte SKUs aus image_fix entfernt (banned_skus.json)")
        except Exception:
            pass

    if args.sku:
        if args.sku not in image_fix:
            # SKU nicht in Liste → trotzdem versuchen (manueller Aufruf)
            image_fix = {args.sku: {"ean": "", "title": args.sku}}
        else:
            image_fix = {args.sku: image_fix[args.sku]}

    if args.limit:
        image_fix = dict(list(image_fix.items())[:args.limit])

    log.info(f"Zu fixen: {len(image_fix)} SKUs | Dry-Run: {args.dry_run}")
    log.info("Quellen: Icecat → artikeldaten.csv (DDG deaktiviert)")

    # artikeldaten.csv einmal laden
    artdata = load_artikeldaten_index()

    stats   = {"ok": 0, "icecat": 0, "artdata": 0, "no_image": 0, "error": 0}
    fixed   = []

    # Original-Fix-Liste für späteres Speichern laden
    full_fix: dict = json.loads(fix_file.read_text(encoding="utf-8"))

    for i, (sku, meta) in enumerate(image_fix.items(), 1):
        ean   = meta.get("ean", "")
        title = meta.get("title", sku)
        log.info(f"[{i}/{len(image_fix)}] {sku}  {title[:60]}")

        image_urls = []
        source     = ""

        # ── 1. Icecat (Hersteller-Originalbilder) ────────────────────────
        if ean:
            image_urls = fetch_icecat_images(ean, title)
            if image_urls:
                source = "icecat"
            time.sleep(0.5)

        # ── 2. artikeldaten.csv (Kosatec CDN) ────────────────────────────
        if not image_urls and ean:
            raw_urls = artdata.get(ean, [])
            valid    = [u for u in raw_urls if is_valid_image(u, title)]
            if valid:
                image_urls = valid
                source     = "artikeldaten"
                log.info(f"  ✓ artikeldaten.csv: {len(image_urls)} Bilder")
            elif raw_urls:
                log.warning(
                    f"  artikeldaten.csv hat {len(raw_urls)} Bilder, "
                    f"aber alle durch Validierung abgelehnt — manuell prüfen!"
                )

        # ── 3. Kein Bild gefunden ─────────────────────────────────────────
        if not image_urls:
            log.warning(f"  ✗ Kein geeignetes Bild gefunden → übersprungen (manuell prüfen)")
            stats["no_image"] += 1
            continue

        log.info(f"  Quelle: {source} | {len(image_urls)} Bilder | {image_urls[0][:70]}")

        if args.dry_run:
            stats["ok"] += 1
            stats[source] = stats.get(source, 0) + 1
            continue

        ok = update_inventory_images(client, sku, image_urls)
        if ok:
            log.info(f"  ✓ Bilder aktualisiert ({source})")
            stats["ok"] += 1
            stats[source] = stats.get(source, 0) + 1
            fixed.append(sku)
        else:
            stats["error"] += 1

        time.sleep(DELAY)

    # Erfolgreich gefixter aus Fix-Liste entfernen
    if fixed and not args.dry_run:
        for sku in fixed:
            full_fix.pop(sku, None)
        fix_file.write_text(
            json.dumps(full_fix, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"\n{len(fixed)} SKUs gefixt und aus {IMAGE_FIX_FILE} entfernt")
        log.info(f"{len(full_fix)} SKUs verbleiben")

    log.info(f"\n{'─'*55}")
    log.info(f"  Erfolgreich:              {stats['ok']}")
    log.info(f"  davon Icecat:             {stats.get('icecat', 0)}")
    log.info(f"  davon artikeldaten.csv:   {stats.get('artikeldaten', 0)}")
    log.info(f"  Kein Bild (manuell):      {stats['no_image']}")
    log.info(f"  API-Fehler:               {stats['error']}")
    if args.dry_run:
        log.info("  [DRY-RUN — keine Änderungen]")
    if stats["no_image"] > 0:
        log.info(f"\n  ⚠ {stats['no_image']} SKUs ohne Bild — bitte manuell in eBay Seller Hub prüfen!")


if __name__ == "__main__":
    main()
