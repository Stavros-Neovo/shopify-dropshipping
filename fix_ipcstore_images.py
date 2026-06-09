"""
fix_ipcstore_images.py
======================
Ersetzt ipcstore-Bilder durch echte Produktbilder via:
  1. Icecat API (bevorzugt, frische Abfrage — kein ipcstore aus Cache)
  2. DuckDuckGo Images (Fallback)

Checkpoint-Save alle 30 Produkte, damit bei Timeout nichts verloren geht.

Aufruf:
  python fix_ipcstore_images.py              # alle
  python fix_ipcstore_images.py --limit 50   # nur 50
  python fix_ipcstore_images.py --dry-run    # Analyse
  python fix_ipcstore_images.py --no-validate  # Kompatibilitätsflag
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
    import requests as _requests
    USE_REQUESTS = True
except ImportError:
    USE_REQUESTS = False

log = logging.getLogger("fix_ipcstore")

ENRICHMENT_FILE = "enrichment_index.csv"
ICECAT_USER  = "neovogen"
ICECAT_TOKEN = "a923fe60-04bd-4f83-ae2e-a1e1a8427c98"
ICECAT_LANG  = "de"
CACHE_FILE   = "icecat_cache.json"
DELAY        = 0.4   # Sekunden zwischen Icecat-Requests
DDG_DELAY    = 0.3
SAVE_EVERY   = 30    # CSV-Checkpoint alle N Produkte

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BAD_DOMAINS = ["ipcstore", "placeholder", "noimage", "no-image", "notfound"]


def is_bad_url(url: str) -> bool:
    return not url or not url.startswith("http") or any(b in url.lower() for b in BAD_DOMAINS)


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


def icecat_images(ean: str) -> tuple[str, str, str]:
    """Gibt (image_main, images_all, brand) aus Icecat zurück. Leer = kein Treffer."""
    url = (f"https://live.icecat.biz/api"
           f"?UserName={ICECAT_USER}&Language={ICECAT_LANG}"
           f"&GTIN={ean}&Token={ICECAT_TOKEN}")
    try:
        if USE_REQUESTS:
            r = _requests.get(url, timeout=10)
            if r.status_code != 200:
                return "", "", ""
            data = r.json()
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

        if data.get("msg") != "OK" or not data.get("data"):
            return "", "", ""

        product = data["data"]
        img = product.get("Image", {}) or {}
        gi  = product.get("GeneralInfo", {}) or {}
        high = (img.get("HighPic") or "").strip()
        pics = img.get("Pics", []) or []
        extra = [p.get("Pic", "") for p in pics if p.get("Pic", "").startswith("http")]
        all_imgs = [u for u in ([high] + extra) if u.startswith("http") and not is_bad_url(u)]
        brand = (gi.get("BrandName") or "").strip()
        main  = all_imgs[0] if all_imgs else ""
        return main, "|".join(all_imgs[:5]), brand

    except Exception as e:
        log.debug(f"Icecat EAN={ean}: {e}")
        return "", "", ""


def ddg_image(query: str) -> Optional[str]:
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
        for res in data.get("results", []):
            url = res.get("image", "")
            if url and not is_bad_url(url):
                return url
    except Exception:
        pass
    return None


def save_csv(path: str, fieldnames: list, rows: list):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-validate", action="store_true",
                        help="Kompatibilitätsflag (alle ipcstore ersetzen ohne vorherige URL-Prüfung)")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    cache = load_cache(CACHE_FILE)

    # Alle ipcstore-Zeilen finden
    ipc_rows = [(i, r) for i, r in enumerate(rows)
                if is_bad_url(r.get("image_main") or "")
                and "ipcstore" in (r.get("image_main") or "").lower()]
    log.info(f"ipcstore-Bilder: {len(ipc_rows)}")

    if args.limit:
        ipc_rows = ipc_rows[:args.limit]
        log.info(f"Limit: {args.limit}")

    if not args.dry_run:
        shutil.copy2(ENRICHMENT_FILE, "enrichment_index_backup_ipcstore.csv")

    fixed_icecat = fixed_ddg = failed = 0
    pending_save = False

    for n, (idx, row) in enumerate(ipc_rows, 1):
        ean   = (row.get("ean") or "").strip()
        title = (row.get("title_full") or "").strip()[:70]
        log.info(f"[{n}/{len(ipc_rows)}] EAN={ean}  {title[:50]}")

        new_img = new_all = new_brand = ""

        # 1. Cache — aber nur wenn gecachtes Bild KEIN ipcstore ist
        cached = cache.get(ean) or {}
        cached_img = cached.get("image_main", "")
        if cached_img and not is_bad_url(cached_img):
            new_img   = cached_img
            new_all   = cached.get("images_all", "")
            new_brand = cached.get("brand", "")
            log.info(f"  → Cache: {new_img[:70]}")

        # 2. Icecat (frische Abfrage wenn Cache leer oder ipcstore)
        if not new_img and ean:
            main_img, all_imgs, brand = icecat_images(ean)
            if main_img and not is_bad_url(main_img):
                new_img = main_img
                new_all = all_imgs
                new_brand = brand
                cache[ean] = {"image_main": new_img, "images_all": new_all, "brand": new_brand}
                log.info(f"  → Icecat: {new_img[:70]}")
            else:
                cache[ean] = {"image_main": "", "images_all": "", "brand": ""}
            time.sleep(DELAY)

        # 3. DDG Fallback
        if not new_img:
            ddg = ddg_image(f"{title} product photo")
            if ddg and not is_bad_url(ddg):
                new_img = ddg
                log.info(f"  → DDG: {new_img[:70]}")
                time.sleep(DDG_DELAY)

        if new_img:
            if not args.dry_run:
                rows[idx]["image_main"] = new_img
                if new_all:
                    rows[idx]["images_all"] = new_all
                if new_brand and rows[idx].get("brand", "") in ("", "Unbekannt"):
                    rows[idx]["brand"] = new_brand
                rows[idx]["source"] = "icecat_fixed" if new_brand else "ddg_fixed2"
            if new_brand:
                fixed_icecat += 1
            else:
                fixed_ddg += 1
            pending_save = True
        else:
            log.warning(f"  → KEIN Bild für EAN={ean}")
            failed += 1

        # Checkpoint alle SAVE_EVERY Produkte
        if n % SAVE_EVERY == 0:
            save_cache(CACHE_FILE, cache)
            if not args.dry_run and pending_save:
                save_csv(ENRICHMENT_FILE, fieldnames, rows)
                log.info(f"  [Checkpoint {n}/{len(ipc_rows)}] gespeichert")
                pending_save = False

    # Finales Speichern
    save_cache(CACHE_FILE, cache)
    if not args.dry_run:
        save_csv(ENRICHMENT_FILE, fieldnames, rows)
        log.info(f"\n✓ {ENRICHMENT_FILE} gespeichert")

    log.info(f"\n{'─'*60}")
    log.info(f"  Icecat:         {fixed_icecat}")
    log.info(f"  DDG:            {fixed_ddg}")
    log.info(f"  Nicht gefunden: {failed}")
    log.info(f"  Gesamt:         {fixed_icecat + fixed_ddg + failed}/{len(ipc_rows)}")


if __name__ == "__main__":
    main()
