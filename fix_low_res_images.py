"""
fix_low_res_images.py
=====================
Repariert eBay-Listings mit eBay Error 25002 (Bild-Auflösung zu gering).

Ablauf:
  1. Liest image_fix_needed.json (vom Repricer befüllt)
  2. Für jede SKU: sucht hochauflösendes Bild
     a) artikeldaten.csv → images_xl (Kosatec-CDN, höchste Qualität)
     b) DuckDuckGo Bildsuche als Fallback
  3. Aktualisiert eBay Inventory Item mit neuen Bildern (PUT)
  4. Entfernt erfolgreich gefixter SKUs aus image_fix_needed.json

Aufruf:
  python3 fix_low_res_images.py                    # alle SKUs
  python3 fix_low_res_images.py --limit 50         # nur 50 auf einmal
  python3 fix_low_res_images.py --dry-run          # nur anzeigen, nichts ändern
  python3 fix_low_res_images.py --sku CPUA0065     # einzelne SKU
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient

log = logging.getLogger("fix_images")

IMAGE_FIX_FILE   = "image_fix_needed.json"
ARTIKELDATEN_CSV = "artikeldaten.csv"
INVENTORY_PATH   = "/sell/inventory/v1/inventory_item"
DELAY            = 1.0
MIN_DIM          = 500   # eBay verlangt mind. 500px

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
})

BLOCKED_TERMS = ["logo", "placeholder", "sprite", "icon", "banner",
                 "tracking", "pixel", "favicon", "no-image", "noimage"]


# ─── Artikeldaten-Index aufbauen ──────────────────────────────────────────────

def load_artikeldaten_index() -> dict:
    """Lädt artikeldaten.csv → {ean: [url1, url2, ...]} (nur images_xl)."""
    p = Path(ARTIKELDATEN_CSV)
    if not p.exists():
        log.warning(f"{ARTIKELDATEN_CSV} nicht gefunden — nur DDG-Fallback verfügbar")
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
    log.info(f"Artikeldaten-Index: {len(index)} EANs mit Bildern")
    return index


# ─── DuckDuckGo Bildsuche ─────────────────────────────────────────────────────

def get_ddg_images(query: str, min_dim: int = MIN_DIM, max_results: int = 6) -> list[str]:
    """Gibt bis zu max_results hochauflösende Bild-URLs zurück."""
    try:
        r = SESSION.get(
            "https://duckduckgo.com/",
            params={"q": query, "iax": "images", "ia": "images"},
            timeout=10,
        )
        match = re.search(r'vqd=([^&"]+)', r.text)
        if not match:
            return []
        token = match.group(1)

        r2 = SESSION.get(
            "https://duckduckgo.com/i.js",
            params={"q": query, "vqd": token, "f": ",,,,,", "p": "1"},
            timeout=10,
        )
        if r2.status_code != 200:
            return []

        results = []
        for item in r2.json().get("results", [])[:20]:
            url = item.get("image", "")
            if not url:
                continue
            if any(b in url.lower() for b in BLOCKED_TERMS):
                continue
            w = item.get("width", 0)
            h = item.get("height", 0)
            if w >= min_dim and h >= min_dim:
                results.append(url)
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        log.debug(f"DDG-Fehler: {e}")
        return []


# ─── eBay Inventory Item aktualisieren ────────────────────────────────────────

def update_inventory_images(client: EbayClient, sku: str, image_urls: list[str]) -> bool:
    """
    Holt das bestehende Inventory Item und ersetzt nur imageUrls.
    Gibt True zurück wenn erfolgreich.
    """
    try:
        existing = client._request("GET", f"{INVENTORY_PATH}/{sku}")
    except Exception as e:
        log.error(f"  GET Inventory Item {sku} fehlgeschlagen: {e}")
        return False

    if not existing:
        log.warning(f"  Kein Inventory Item für {sku}")
        return False

    # Nur imageUrls ersetzen, alles andere unverändert lassen
    existing.setdefault("product", {})
    existing["product"]["imageUrls"] = image_urls[:12]

    # Felder die beim PUT nicht erlaubt sind entfernen
    for field in ["sku", "locale", "packageWeightAndSize"]:
        existing.pop(field, None)

    try:
        client._request("PUT", f"{INVENTORY_PATH}/{sku}", json_body=existing)
        return True
    except Exception as e:
        log.error(f"  PUT Inventory Item {sku} fehlgeschlagen: {e}")
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
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    client = EbayClient.from_env(cfg["ebay"])

    # Fix-Liste laden
    fix_file = Path(IMAGE_FIX_FILE)
    if not fix_file.exists():
        log.info(f"Keine {IMAGE_FIX_FILE} gefunden — nichts zu tun.")
        return

    image_fix: dict = json.loads(fix_file.read_text(encoding="utf-8"))
    if not image_fix:
        log.info("image_fix_needed.json ist leer — nichts zu tun.")
        return

    if args.sku:
        if args.sku not in image_fix:
            log.error(f"SKU {args.sku} nicht in {IMAGE_FIX_FILE}")
            return
        to_fix = {args.sku: image_fix[args.sku]}
    else:
        to_fix = image_fix

    if args.limit:
        items = list(to_fix.items())[:args.limit]
        to_fix = dict(items)

    log.info(f"Zu fixen: {len(to_fix)} SKUs | Dry-Run: {args.dry_run}")

    # Artikeldaten-Index laden (EAN → hochauflösende Bilder)
    artdata = load_artikeldaten_index()

    stats = {"ok": 0, "ddg_fallback": 0, "no_image": 0, "error": 0}
    fixed_skus = []

    for i, (sku, meta) in enumerate(to_fix.items(), 1):
        ean   = meta.get("ean", "")
        title = meta.get("title", sku)
        log.info(f"[{i}/{len(to_fix)}] {sku}  {title[:60]}")

        # 1. Artikeldaten (beste Qualität)
        image_urls = artdata.get(ean, [])
        source = "artikeldaten"

        # 2. DDG-Fallback
        if not image_urls:
            log.info(f"  Kein Bild in artikeldaten.csv → DDG-Suche für: {title[:50]}")
            image_urls = get_ddg_images(title, min_dim=MIN_DIM)
            source = "ddg"
            time.sleep(1.2)

        if not image_urls:
            log.warning(f"  ✗ Kein Bild gefunden → überspringen")
            stats["no_image"] += 1
            continue

        log.info(f"  Quelle: {source} | {len(image_urls)} Bilder | 1. URL: {image_urls[0][:70]}")

        if args.dry_run:
            log.info(f"  [DRY-RUN] würde {len(image_urls)} Bilder setzen")
            stats["ok"] += 1
            if source == "ddg":
                stats["ddg_fallback"] += 1
            continue

        ok = update_inventory_images(client, sku, image_urls)
        if ok:
            log.info(f"  ✓ Bilder aktualisiert")
            stats["ok"] += 1
            if source == "ddg":
                stats["ddg_fallback"] += 1
            fixed_skus.append(sku)
        else:
            stats["error"] += 1

        time.sleep(DELAY)

    # Erfolgreich gefixter aus der Liste entfernen
    if fixed_skus and not args.dry_run:
        for sku in fixed_skus:
            image_fix.pop(sku, None)
        fix_file.write_text(
            json.dumps(image_fix, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"\n{len(fixed_skus)} SKUs aus {IMAGE_FIX_FILE} entfernt — {len(image_fix)} verbleiben")

    log.info(f"\n{'─'*50}")
    log.info(f"  Erfolgreich:        {stats['ok']}")
    log.info(f"  davon DDG-Fallback: {stats['ddg_fallback']}")
    log.info(f"  Kein Bild gefunden: {stats['no_image']}")
    log.info(f"  Fehler:             {stats['error']}")
    if args.dry_run:
        log.info("  [DRY-RUN — keine Änderungen]")


if __name__ == "__main__":
    main()
