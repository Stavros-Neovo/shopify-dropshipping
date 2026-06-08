"""
fix_ipcstore_images.py
======================
Ersetzt ipcstore-Bilder (403 Forbidden) durch echte Produktbilder via:
  1. Icecat API (IT-Produkte → sehr hohe Erfolgsrate)
  2. DuckDuckGo Images (Fallback)

Aufruf:
  python fix_ipcstore_images.py              # alle ipcstore-Produkte
  python fix_ipcstore_images.py --limit 50   # nur die ersten 50 testen
  python fix_ipcstore_images.py --dry-run    # nur Analyse
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import requests
    USE_REQUESTS = True
except ImportError:
    USE_REQUESTS = False

log = logging.getLogger("fix_ipcstore")

ENRICHMENT_FILE = "enrichment_index.csv"
ICECAT_USER  = "neovogen"
ICECAT_TOKEN = "a923fe60-04bd-4f83-ae2e-a1e1a8427c98"
ICECAT_LANG  = "de"
CACHE_FILE   = "icecat_cache.json"
DELAY        = 0.8  # Sekunden zwischen Icecat-Requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ─── Cache ────────────────────────────────────────────────────────────────────
def load_cache(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(path: str, cache: dict):
    Path(path).write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Icecat ───────────────────────────────────────────────────────────────────
def fetch_icecat(ean: str) -> Optional[dict]:
    url = (f"https://live.icecat.biz/api"
           f"?UserName={ICECAT_USER}&Language={ICECAT_LANG}"
           f"&GTIN={ean}&Token={ICECAT_TOKEN}")
    try:
        if USE_REQUESTS:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("msg") == "OK" and data.get("data"):
                    return data["data"]
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
                if data.get("msg") == "OK" and data.get("data"):
                    return data["data"]
    except Exception as e:
        log.debug(f"Icecat EAN={ean}: {e}")
    return None


def icecat_images(ean: str) -> tuple[str, str, str]:
    """Gibt (image_main, images_all, brand) zurück. Leer wenn kein Treffer."""
    data = fetch_icecat(ean)
    if not data:
        return "", "", ""
    img = data.get("Image", {}) or {}
    gi  = data.get("GeneralInfo", {}) or {}
    high = (img.get("HighPic") or "").strip()
    pics = img.get("Pics", []) or []
    extra = [p.get("Pic", "") for p in pics if p.get("Pic", "").startswith("http")]
    all_imgs = [u for u in ([high] + extra) if u.startswith("http")]
    brand = (gi.get("BrandName") or "").strip()
    return (all_imgs[0] if all_imgs else ""), "|".join(all_imgs[:5]), brand


# ─── DDG Fallback ─────────────────────────────────────────────────────────────
def ddg_image(query: str) -> Optional[str]:
    """Erste passende Bild-URL aus DuckDuckGo Image Search."""
    try:
        params = urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(
            f"https://duckduckgo.com/?{params}",
            headers={"User-Agent": UA, "Accept": "text/html,*/*"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
        m = re.search(r'vqd=([^&"]+)', html)
        if not m:
            return None
        vqd = m.group(1)

        img_params = urllib.parse.urlencode({
            "l": "de-de", "o": "json", "q": query,
            "vqd": vqd, "f": ",,,,,", "p": "1",
        })
        req2 = urllib.request.Request(
            f"https://duckduckgo.com/i.js?{img_params}",
            headers={"User-Agent": UA, "Referer": "https://duckduckgo.com/",
                     "Accept": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=10) as r:
            data = json.loads(r.read())
        results = data.get("results", [])
        for res in results:
            url = res.get("image", "")
            if url and not any(bad in url for bad in ["ipcstore", "placeholder", "noimage"]):
                return url
    except Exception:
        pass
    return None


# ─── Hauptlogik ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max N Produkte bearbeiten")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Einlesen
    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    cache = load_cache(CACHE_FILE)

    # Identifiziere ipcstore-Zeilen
    ipc_rows = [(i, r) for i, r in enumerate(rows)
                if "ipcstore" in (r.get("image_main") or "").lower()]
    log.info(f"ipcstore-Bilder gefunden: {len(ipc_rows)}")

    if args.limit:
        ipc_rows = ipc_rows[:args.limit]
        log.info(f"Limitiert auf: {args.limit}")

    if not args.dry_run:
        shutil.copy2(ENRICHMENT_FILE, "enrichment_index_backup_ipcstore.csv")

    fixed_icecat = 0
    fixed_ddg    = 0
    failed       = 0

    for n, (idx, row) in enumerate(ipc_rows, 1):
        ean   = (row.get("ean") or "").strip()
        title = (row.get("title_full") or "").strip()[:70]
        old_img = (row.get("image_main") or "").strip()

        log.info(f"[{n}/{len(ipc_rows)}] EAN={ean}  {title[:50]}")

        new_img = ""
        new_all = ""
        new_brand = ""

        # 1. Icecat (bevorzugt für IT-Produkte)
        if ean:
            if ean in cache and cache[ean] and cache[ean].get("image_main"):
                new_img   = cache[ean]["image_main"]
                new_all   = cache[ean].get("images_all", "")
                new_brand = cache[ean].get("brand", "")
                log.info(f"  → Cache-Hit: {new_img[:60]}")
            else:
                main_img, all_imgs, brand = icecat_images(ean)
                cache[ean] = {"image_main": main_img, "images_all": all_imgs, "brand": brand}
                if main_img:
                    new_img   = main_img
                    new_all   = all_imgs
                    new_brand = brand
                    log.info(f"  → Icecat: {new_img[:60]}")
                time.sleep(DELAY)

        # 2. DDG Fallback
        if not new_img:
            query = f"{title} product"
            new_img = ddg_image(query) or ""
            if new_img:
                log.info(f"  → DDG: {new_img[:60]}")
                time.sleep(0.5)

        if new_img:
            if not args.dry_run:
                rows[idx]["image_main"] = new_img
                if new_all:
                    rows[idx]["images_all"] = new_all
                if new_brand and (not rows[idx].get("brand") or rows[idx].get("brand") == "Unbekannt"):
                    rows[idx]["brand"] = new_brand
                if new_brand:
                    rows[idx]["source"] = "icecat+ipcstore_fix"
                else:
                    rows[idx]["source"] = "ddg_ipcstore_fix"
            if new_brand:
                fixed_icecat += 1
            else:
                fixed_ddg += 1
        else:
            log.warning(f"  → KEIN Bild gefunden für EAN={ean}")
            failed += 1

        # Cache alle 20 speichern
        if n % 20 == 0:
            save_cache(CACHE_FILE, cache)
            log.info(f"  Cache gespeichert ({len(cache)} Einträge)")

    save_cache(CACHE_FILE, cache)

    if not args.dry_run and (fixed_icecat + fixed_ddg) > 0:
        with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        log.info(f"\n✓ {ENRICHMENT_FILE} gespeichert")

    log.info(f"\n{'─'*60}")
    log.info(f"  Icecat fixes:  {fixed_icecat}")
    log.info(f"  DDG fixes:     {fixed_ddg}")
    log.info(f"  Nicht gefunden:{failed}")
    log.info(f"  Gesamt:        {fixed_icecat + fixed_ddg + failed}/{len(ipc_rows)}")


if __name__ == "__main__":
    main()
