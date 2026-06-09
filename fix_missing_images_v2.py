"""
fix_missing_images_v2.py
========================
Findet Bilder für die verbleibenden 422 Produkte mit ipcstore-URLs.

Quellen (in Reihenfolge):
  1. Icecat API   – GTIN-Lookup mit Auth
  2. Amazon.de    – EAN-Suche → erstes Produktbild
  3. barcodelookup.com – EAN-Datenbank scrapen
  4. DDG Images   – EAN + Titel als Suchanfrage

Einmal gefunden → dauerhaft in enrichment_index.csv + icecat_cache.json gespeichert.
Checkpoint alle 25 Produkte, damit bei Timeout nichts verloren geht.

Aufruf:
  python fix_missing_images_v2.py
  python fix_missing_images_v2.py --limit 50   # nur 50 Produkte testen
  python fix_missing_images_v2.py --dry-run
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
    import requests as _req
    USE_REQUESTS = True
except ImportError:
    USE_REQUESTS = False

log = logging.getLogger("fix_images_v2")

ENRICHMENT_FILE = "enrichment_index.csv"
CACHE_FILE      = "icecat_cache.json"
ICECAT_USER     = "neovogen"
ICECAT_TOKEN    = "a923fe60-04bd-4f83-ae2e-a1e1a8427c98"
ICECAT_LANG     = "de"
SAVE_EVERY      = 25

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

BAD = ["ipcstore", "placeholder", "noimage", "no-image", "notfound",
       "data:image", "blank", "missing"]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_bad(url: str) -> bool:
    if not url or not url.startswith("http"):
        return True
    low = url.lower()
    return any(b in low for b in BAD)


def _get(url: str, headers: dict = None, timeout: int = 10):
    h = {"User-Agent": UA, **(headers or {})}
    if USE_REQUESTS:
        r = _req.get(url, headers=h, timeout=timeout)
        r.raise_for_status()
        return r
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return type("R", (), {
            "text": resp.read().decode("utf-8", errors="replace"),
            "json": lambda self=None: json.loads(self.text if self else ""),
            "status_code": resp.status,
        })()


def load_cache(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(path: str, cache: dict):
    Path(path).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_csv(path: str, fieldnames: list, rows: list):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ─── Source 1: Icecat ────────────────────────────────────────────────────────

def icecat(ean: str) -> tuple[str, str, str]:
    url = (f"https://live.icecat.biz/api"
           f"?UserName={ICECAT_USER}&Language={ICECAT_LANG}"
           f"&GTIN={ean}&Token={ICECAT_TOKEN}")
    try:
        r = _get(url)
        data = json.loads(r.text) if isinstance(r.text, str) else r.json()
        if data.get("msg") != "OK" or not data.get("data"):
            return "", "", ""
        prod = data["data"]
        img = prod.get("Image", {}) or {}
        gi  = prod.get("GeneralInfo", {}) or {}
        high = (img.get("HighPic") or "").strip()
        pics = img.get("Pics", []) or []
        extra = [p.get("Pic", "") for p in pics if p.get("Pic", "").startswith("http")]
        imgs = [u for u in [high] + extra if u.startswith("http") and not is_bad(u)]
        brand = (gi.get("BrandName") or "").strip()
        return (imgs[0] if imgs else ""), "|".join(imgs[:5]), brand
    except Exception as e:
        log.debug(f"Icecat EAN={ean}: {e}")
        return "", "", ""


# ─── Source 2: Amazon.de ─────────────────────────────────────────────────────

def amazon(ean: str) -> str:
    """EAN → Amazon.de Suche → erstes Produktbild."""
    search_url = f"https://www.amazon.de/s?k={ean}&i=electronics"
    try:
        r = _get(search_url, headers={
            "Accept-Language": "de-DE,de;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        }, timeout=12)
        html = r.text

        # Hauptbild des ersten Treffers
        m = re.search(
            r'"hiRes"\s*:\s*"(https://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
            html, re.I
        )
        if m:
            u = m.group(1)
            if not is_bad(u):
                return u

        # Fallback: s-image Klasse
        m = re.search(r'<img[^>]+class="[^"]*s-image[^"]*"[^>]+src="([^"]+)"', html, re.I)
        if m:
            u = m.group(1)
            if u.startswith("http") and not is_bad(u):
                return u

        # Fallback: data-src mit amazon-Bild-CDN
        for m in re.finditer(r'(?:src|data-src)="(https://m\.media-amazon\.com/images/[^"]+\.jpg[^"]*)"', html):
            u = m.group(1)
            if not is_bad(u) and "_UL" not in u and "sprite" not in u:
                return u

    except Exception as e:
        log.debug(f"Amazon EAN={ean}: {e}")
    return ""


# ─── Source 3: barcodelookup.com ─────────────────────────────────────────────

def barcodelookup(ean: str) -> str:
    url = f"https://www.barcodelookup.com/{ean}"
    try:
        r = _get(url, headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de-DE,de;q=0.9",
            "Referer": "https://www.barcodelookup.com/",
        }, timeout=12)
        html = r.text

        # Haupt-Produktbild
        for pattern in [
            r'<div[^>]+class="[^"]*product-image[^"]*"[^>]*>.*?<img[^>]+src="([^"]+)"',
            r'<img[^>]+id="[^"]*product[^"]*"[^>]+src="([^"]+)"',
            r'<img[^>]+class="[^"]*product[^"]*"[^>]+src="([^"]+)"',
            r'"image"\s*:\s*"(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
        ]:
            m = re.search(pattern, html, re.I | re.S)
            if m:
                u = m.group(1)
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith("http") and not is_bad(u):
                    return u
    except Exception as e:
        log.debug(f"barcodelookup EAN={ean}: {e}")
    return ""


# ─── Source 4: DuckDuckGo Images (mit EAN im Query) ──────────────────────────

def ddg(ean: str, title: str, brand: str) -> str:
    # EAN in Query → trifft oft direkt Hersteller-CDN
    query = f"{ean} {brand} {title}".strip()
    if len(query) > 120:
        query = query[:120]
    try:
        params = urllib.parse.urlencode({"q": query})
        r = _get(f"https://duckduckgo.com/?{params}", headers={
            "Accept": "text/html,*/*",
        }, timeout=10)
        html = r.text
        m = re.search(r'vqd=([^&"]+)', html)
        if not m:
            return ""
        vqd = m.group(1)

        img_params = urllib.parse.urlencode({
            "l": "de-de", "o": "json", "q": query,
            "vqd": vqd, "f": ",,,,,", "p": "1",
        })
        r2 = _get(
            f"https://duckduckgo.com/i.js?{img_params}",
            headers={"Referer": "https://duckduckgo.com/", "Accept": "application/json"},
        )
        data = json.loads(r2.text)
        for res in data.get("results", []):
            u = res.get("image", "")
            if u and not is_bad(u):
                return u
    except Exception as e:
        log.debug(f"DDG EAN={ean}: {e}")
    return ""


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
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

    # Nur Zeilen mit ipcstore-image_main
    targets = [
        (i, r) for i, r in enumerate(rows)
        if "ipcstore" in (r.get("image_main") or "").lower()
    ]
    log.info(f"Produkte ohne gültiges Bild: {len(targets)}")

    if args.limit:
        targets = targets[: args.limit]
        log.info(f"Limit: {args.limit}")

    if not args.dry_run and targets:
        shutil.copy2(ENRICHMENT_FILE, "enrichment_index_backup_v2.csv")

    stats = {"icecat": 0, "amazon": 0, "barcode": 0, "ddg": 0, "fail": 0}
    pending = False

    for n, (idx, row) in enumerate(targets, 1):
        ean   = (row.get("ean") or "").strip()
        title = (row.get("title_full") or "").strip()[:80]
        brand = (row.get("brand") or "").strip()
        log.info(f"[{n}/{len(targets)}] EAN={ean}  {title[:55]}")

        new_img = new_all = new_brand = ""
        source  = ""

        # ── 1. Icecat ──────────────────────────────────────────────────────
        if ean:
            img, all_imgs, b = icecat(ean)
            if img and not is_bad(img):
                new_img = img
                new_all = all_imgs
                new_brand = b
                source = "icecat"
                log.info(f"  ✓ Icecat: {img[:70]}")
            time.sleep(0.4)

        # ── 2. Amazon.de ───────────────────────────────────────────────────
        if not new_img and ean:
            img = amazon(ean)
            if img and not is_bad(img):
                new_img = img
                source = "amazon"
                log.info(f"  ✓ Amazon: {img[:70]}")
            time.sleep(0.5)

        # ── 3. barcodelookup.com ───────────────────────────────────────────
        if not new_img and ean:
            img = barcodelookup(ean)
            if img and not is_bad(img):
                new_img = img
                source = "barcodelookup"
                log.info(f"  ✓ Barcodelookup: {img[:70]}")
            time.sleep(0.5)

        # ── 4. DDG mit EAN im Query ────────────────────────────────────────
        if not new_img:
            img = ddg(ean, title, brand)
            if img and not is_bad(img):
                new_img = img
                source = "ddg_ean"
                log.info(f"  ✓ DDG: {img[:70]}")
            time.sleep(0.4)

        # ── Speichern ──────────────────────────────────────────────────────
        if new_img:
            stats[source.split("_")[0] if source else "fail"] = \
                stats.get(source.split("_")[0], 0) + 1
            if not args.dry_run:
                rows[idx]["image_main"] = new_img
                if new_all:
                    rows[idx]["images_all"] = new_all
                if new_brand and rows[idx].get("brand", "") in ("", "Unbekannt"):
                    rows[idx]["brand"] = new_brand
                rows[idx]["source"] = source
            # Cache aktualisieren
            cache[ean] = {
                "image_main": new_img,
                "images_all": new_all,
                "brand": new_brand,
            }
            pending = True
        else:
            log.warning(f"  ✗ Kein Bild für EAN={ean}")
            stats["fail"] = stats.get("fail", 0) + 1

        # Checkpoint
        if n % SAVE_EVERY == 0:
            save_cache(CACHE_FILE, cache)
            if not args.dry_run and pending:
                save_csv(ENRICHMENT_FILE, fieldnames, rows)
                log.info(f"  [Checkpoint {n}/{len(targets)}] gespeichert")
                pending = False

    # Finales Speichern
    save_cache(CACHE_FILE, cache)
    if not args.dry_run:
        save_csv(ENRICHMENT_FILE, fieldnames, rows)
        log.info(f"\n✓ {ENRICHMENT_FILE} gespeichert")

    total_found = sum(v for k, v in stats.items() if k != "fail")
    log.info(f"\n{'─'*60}")
    log.info(f"  Icecat:           {stats.get('icecat', 0)}")
    log.info(f"  Amazon.de:        {stats.get('amazon', 0)}")
    log.info(f"  barcodelookup:    {stats.get('barcode', 0)}")
    log.info(f"  DDG (EAN+Titel):  {stats.get('ddg', 0)}")
    log.info(f"  Nicht gefunden:   {stats.get('fail', 0)}")
    log.info(f"  Gesamt gefunden:  {total_found}/{len(targets)}")


if __name__ == "__main__":
    main()
