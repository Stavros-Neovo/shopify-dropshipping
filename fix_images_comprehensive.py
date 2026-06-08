"""
fix_images_comprehensive.py
===========================
Behebt zwei Bildprobleme in enrichment_index.csv:

1. Produkte mit images_all aber ohne image_main
   → Erste URL aus images_all wird zu image_main

2. Produkte mit ipcstore-Bildern (403 Forbidden / Hotlink-Blockiert)
   → Ersetze durch Bild via EAN-Lookup (icecat → DDG-Images → Open Product Data)

Aufruf:
  python fix_images_comprehensive.py              # Alles fixen
  python fix_images_comprehensive.py --dry-run    # Nur Analyse, keine Änderungen
  python fix_images_comprehensive.py --ipcstore-only  # Nur ipcstore ersetzen
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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("fix_images")

ENRICHMENT_FILE = "enrichment_index.csv"
BACKUP_FILE = "enrichment_index_backup.csv"

# ─── HTTP-Helper ─────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


def url_is_image(url: str, timeout: int = 6) -> bool:
    """Prüft ob eine URL ein gültiges Bild liefert."""
    if not url or not url.startswith("http"):
        return False
    try:
        req = urllib.request.Request(url, headers={**HEADERS, "Range": "bytes=0-511"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            code = r.status
            if code == 206:  # Partial Content
                return "image/" in ct
            return code == 200 and "image/" in ct
    except Exception:
        # Manche Server lehnen Range-Requests ab → HEAD
        try:
            req2 = urllib.request.Request(url, method="HEAD", headers=HEADERS)
            with urllib.request.urlopen(req2, timeout=timeout) as r:
                ct = r.headers.get("Content-Type", "")
                return r.status == 200 and "image/" in ct
        except Exception:
            return False


# ─── Bildsuche via DuckDuckGo ─────────────────────────────────────────────────
def ddg_image_search(query: str, timeout: int = 8) -> Optional[str]:
    """Liefert die erste Bild-URL aus DuckDuckGo Image Search."""
    try:
        params = urllib.parse.urlencode({"q": query, "format": "json", "pretty": 1})
        vqd_url = f"https://duckduckgo.com/?{params}"
        req = urllib.request.Request(vqd_url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html,*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="replace")
        m = re.search(r'vqd=([^&"]+)', html)
        if not m:
            return None
        vqd = m.group(1)

        img_params = urllib.parse.urlencode({
            "l": "de-de", "o": "json", "q": query,
            "vqd": vqd, "f": ",,,,,", "p": "1",
        })
        img_url = f"https://duckduckgo.com/i.js?{img_params}"
        req2 = urllib.request.Request(img_url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://duckduckgo.com/",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req2, timeout=timeout) as r:
            data = json.loads(r.read())
        results = data.get("results", [])
        for r in results:
            u = r.get("image", "")
            if u and url_is_image(u, timeout=5):
                return u
    except Exception:
        pass
    return None


# ─── icecat EAN Lookup ────────────────────────────────────────────────────────
def icecat_image_by_ean(ean: str, timeout: int = 8) -> Optional[str]:
    """Sucht ein Produktbild via icecat.us XML API (keine Auth nötig für public)."""
    try:
        url = f"https://icecat.us/api/products?EAN={ean}&content=medium&lang=de"
        req = urllib.request.Request(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        # Icecat gibt zurück: data['msg']['GeneralInfo']['IcecatId'] usw.
        product = data.get("msg", {})
        img = (product.get("Image", {}) or {}).get("HighPic", "")
        if img and url_is_image(img, timeout=5):
            return img
        # Fallback: Gallery
        gallery = product.get("Gallery", []) or []
        for g in gallery:
            u = (g.get("Pic750") or g.get("Pic500") or g.get("Pic", "")).strip()
            if u and url_is_image(u, timeout=5):
                return u
    except Exception:
        pass
    return None


# ─── Open Product Data (Barcode Lookup) ──────────────────────────────────────
def opd_image_by_ean(ean: str, timeout: int = 8) -> Optional[str]:
    """Sucht Bild via Open Product Data / barcodelookup.com scrape."""
    try:
        url = f"https://www.barcodelookup.com/{ean}"
        req = urllib.request.Request(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="replace")
        # Suche nach product-image
        m = re.search(r'<img[^>]+class="[^"]*product[^"]*"[^>]+src="([^"]+)"', html, re.I)
        if m:
            img = m.group(1)
            if img.startswith("//"):
                img = "https:" + img
            if url_is_image(img, timeout=5):
                return img
    except Exception:
        pass
    return None


# ─── Haupt-Logik ──────────────────────────────────────────────────────────────
def load_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def save_csv(path: str, fieldnames: list[str], rows: list[dict]):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fix_images(dry_run: bool = False, ipcstore_only: bool = False,
               max_ipcstore: int = 0, validate: bool = True):
    fieldnames, rows = load_csv(ENRICHMENT_FILE)

    if not dry_run:
        import shutil
        shutil.copy2(ENRICHMENT_FILE, BACKUP_FILE)
        log.info(f"Backup erstellt: {BACKUP_FILE}")

    changed = 0
    skipped_ipc = 0
    fixed_promote = 0
    fixed_ipcstore = 0
    failed_ipcstore = 0

    ipc_count = 0

    for i, row in enumerate(rows):
        img_main = (row.get("image_main") or "").strip()
        imgs_all = (row.get("images_all") or "").strip()
        ean = (row.get("ean") or "").strip()
        title = (row.get("title_full") or "").strip()[:60]
        brand = (row.get("brand") or "").strip()

        # ── Fix 1: Kein image_main aber images_all vorhanden ──────────────────
        if not ipcstore_only and not img_main and imgs_all:
            first = imgs_all.split("|")[0].strip()
            if first:
                if not dry_run:
                    row["image_main"] = first
                log.info(f"[PROMOTE] EAN={ean}  {first[:60]}")
                fixed_promote += 1
                changed += 1
            continue

        # ── Fix 2: ipcstore-Bild ersetzen ─────────────────────────────────────
        if "ipcstore" in img_main.lower():
            if max_ipcstore > 0 and ipc_count >= max_ipcstore:
                skipped_ipc += 1
                continue
            ipc_count += 1

            # Prüfe ob ipcstore-URL noch funktioniert
            if validate and url_is_image(img_main, timeout=5):
                log.debug(f"[IPCSTORE OK] EAN={ean}  Bild funktioniert")
                continue

            # URL funktioniert nicht → suche Ersatz
            new_url = None
            search_query = f"{brand} {title} product image" if brand else f"{title} product image"

            # 1. Versuch: icecat via EAN
            if ean:
                log.info(f"[IPCSTORE] EAN={ean}  versuche icecat...")
                new_url = icecat_image_by_ean(ean)
                time.sleep(0.3)

            # 2. Versuch: DDG
            if not new_url:
                log.info(f"[IPCSTORE] EAN={ean}  versuche DDG: {search_query}")
                new_url = ddg_image_search(search_query)
                time.sleep(0.5)

            if new_url:
                log.info(f"[IPCSTORE FIXED] EAN={ean}  →  {new_url[:70]}")
                if not dry_run:
                    row["image_main"] = new_url
                    # Alte ipcstore URL zu images_all hinzufügen (als Backup)
                    existing_all = [x.strip() for x in imgs_all.split("|") if x.strip()]
                    if img_main not in existing_all:
                        existing_all.insert(0, new_url)
                    row["images_all"] = "|".join(existing_all)
                fixed_ipcstore += 1
                changed += 1
            else:
                log.warning(f"[IPCSTORE FAILED] EAN={ean}  kein Ersatz gefunden")
                failed_ipcstore += 1

    log.info(f"\n{'─'*60}")
    log.info(f"Ergebnis:")
    log.info(f"  images_all→image_main promoted: {fixed_promote}")
    log.info(f"  ipcstore ersetzt:               {fixed_ipcstore}")
    log.info(f"  ipcstore nicht gefunden:        {failed_ipcstore}")
    log.info(f"  ipcstore übersprungen (limit):  {skipped_ipc}")
    log.info(f"  Gesamt geändert:                {changed}")

    if not dry_run and changed > 0:
        save_csv(ENRICHMENT_FILE, fieldnames, rows)
        log.info(f"✓ {ENRICHMENT_FILE} gespeichert ({changed} Änderungen)")
    elif dry_run:
        log.info("DRY-RUN: Keine Änderungen gespeichert")

    return changed


def main():
    parser = argparse.ArgumentParser(description="Bilder in enrichment_index.csv fixen")
    parser.add_argument("--dry-run", action="store_true", help="Nur analysieren, nichts speichern")
    parser.add_argument("--ipcstore-only", action="store_true", help="Nur ipcstore-Bilder ersetzen")
    parser.add_argument("--no-validate", action="store_true", help="ipcstore-URLs nicht prüfen (immer ersetzen)")
    parser.add_argument("--max-ipcstore", type=int, default=0,
                        help="Maximal N ipcstore-Einträge ersetzen (0 = alle)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    fix_images(
        dry_run=args.dry_run,
        ipcstore_only=args.ipcstore_only,
        max_ipcstore=args.max_ipcstore,
        validate=not args.no_validate,
    )


if __name__ == "__main__":
    main()
