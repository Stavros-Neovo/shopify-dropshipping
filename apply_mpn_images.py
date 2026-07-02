#!/usr/bin/env python3
"""Wendet die per Marke+MPN gefundenen Icecat-Bilder (icecat_mpn_search.py) auf
supplier_map.json an — der bisher fehlende Integrationsschritt.

Zwei Fixes gegenüber der Rohausgabe:
  1. URL-Upgrade gallery_mediums -> gallery (Volloriginal). Die MPN-Suche speichert
     die Medium-Variante, die oft <500px ist; das Original ist groß genug.
  2. Echter Pixel-Check ≥500×500 (Pillow, via Icecat-CDN lokal erreichbar). Nur
     Treffer die bestehen bekommen image_verified=True und fließen so nach Shopify.

Kein eBay-Push (bewusst — apply_image_updates.py macht das, ist aber eBay-gekoppelt).
    python3 apply_mpn_images.py            # anwenden
    python3 apply_mpn_images.py --dry-run  # nur prüfen, nichts schreiben
"""
import argparse, io, json, urllib.request
from datetime import datetime
from pathlib import Path
from PIL import Image

MIN_PX = 500
SRC = "icecat_mpn_images.json"
SMAP = "supplier_map.json"


def to_fullres(url: str) -> str:
    """gallery_mediums -> gallery (Icecat-Volloriginal). Andere URLs unverändert."""
    return url.replace("/gallery_mediums/", "/gallery/")


def resolution(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=20).read()
    return Image.open(io.BytesIO(raw)).size


def best_variant(raw_url: str):
    """Beste ≥500px-Variante: erst gallery (Volloriginal), sonst Original-URL.
    Returns (url, (w,h)) oder (None, None) wenn nichts erreichbar/zu klein."""
    candidates = [to_fullres(raw_url)]
    if raw_url not in candidates:
        candidates.append(raw_url)
    best = (None, None)
    for u in candidates:
        try:
            w, h = resolution(u)
        except Exception:
            continue
        if min(w, h) >= MIN_PX:
            return u, (w, h)
        if best[0] is None:              # kleinste-nicht-leere merken für Report
            best = (u, (w, h))
    return best if best[0] and min(best[1]) < MIN_PX else (None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mpn = json.load(open(SRC, encoding="utf-8"))
    sm = json.load(open(SMAP, encoding="utf-8"))

    applied, too_small, errors, missing = [], [], [], []
    for sku, raw_url in mpn.items():
        if sku not in sm:
            # supplier_map ist der aus dem BAB-Feed gebaute Master. Ein MPN-Bild für
            # eine dort unbekannte SKU würde nur einen ean/title-losen Junk-Eintrag
            # anlegen → überspringen; das Bild greift, sobald die SKU im Master ist.
            missing.append(sku)
            continue
        url, wh = best_variant(raw_url)
        if url is None:
            errors.append(sku)
            print(f"  {sku}: keine erreichbare ≥{MIN_PX}px-Variante")
            continue
        w, h = wh
        if min(w, h) < MIN_PX:
            too_small.append(sku)
            print(f"  {sku}: {w}x{h} zu klein — übersprungen")
            continue
        print(f"  {sku}: {w}x{h} ✓")
        applied.append(sku)
        if not args.dry_run:
            v = sm.get(sku, {})
            v["image_url"] = url
            v["images"] = [url]
            v["image_verified"] = True
            v.pop("image_too_small", None)
            v["image_source"] = "icecat_mpn"
            sm[sku] = v

    if not args.dry_run and applied:
        bak = f"{SMAP}.bak_{datetime.now():%Y%m%d_%H%M%S}"
        Path(bak).write_text(json.dumps(json.load(open(SMAP, encoding="utf-8")),
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        json.dump(sm, open(SMAP, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n✅ {len(applied)} Bilder angewandt, Backup: {bak}")
    print(f"\nErgebnis: {len(applied)} angewandt | {len(too_small)} zu klein | {len(errors)} Fehler"
          f" | {len(missing)} nicht in supplier_map (übersprungen)"
          + (" (DRY-RUN, nichts geschrieben)" if args.dry_run else ""))


# ponytail: URL-Transform ist die einzige nicht-triviale Pure-Logic → Mini-Check
assert to_fullres("https://x/img/gallery_mediums/a.jpg") == "https://x/img/gallery/a.jpg"
assert to_fullres("https://x/img/gallery/a.jpg") == "https://x/img/gallery/a.jpg"

if __name__ == "__main__":
    main()
