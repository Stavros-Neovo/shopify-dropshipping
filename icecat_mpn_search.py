#!/usr/bin/env python3
"""
icecat_mpn_search.py
====================
Bild-Fallback per Marke + Modellnummer (MPN), wenn der GTIN-Lookup nichts findet.

Warum: Die BAB-GTIN ist nicht immer in Icecat — das Produkt aber oft unter
Brand + ManufacturerCode (MPN). Diese Suche schließt die Lücke und liefert das
ORIGINAL in voller Auflösung (kein Upscaling nötig).

SICHERHEIT (gegen Falschbild-Retouren):
  - Treffer wird NUR akzeptiert, wenn die von Icecat zurückgegebene
    BrandPartCode == gesuchte MPN (bzw. die MPN exakt im Icecat-Titel steht).
  - Refurbished (REF*/tecXL/T1A/Upcycle) ausgeschlossen: deren MPN ist unzuverlässig.
  - REVIEW-ONLY: schreibt nur icecat_mpn_review.json. Schaltet NICHTS live.
    Übernahme nach Sichtung in einem separaten Schritt.

Aufruf:
  python icecat_mpn_search.py [--limit N] [--only-missing] [--check-size]
"""
from __future__ import annotations
import argparse, csv, json, os, re, sys, time
from pathlib import Path

import requests

ICECAT_API     = "https://live.icecat.biz/api"
ICECAT_USER    = os.environ.get("ICECAT_USER",  "neovogen")
ICECAT_TOKEN   = os.environ.get("ICECAT_TOKEN", "a923fe60-04bd-4f83-ae2e-a1e1a8427c98")
# Optionaler Full-Icecat app_key (schaltet die „Full-Icecat"-Produkte frei, z.B. CPUs,
# die mit dem Open-Token 403 geben). Wenn gesetzt, wird er an die Anfrage gehängt.
ICECAT_APP_KEY = os.environ.get("ICECAT_APP_KEY", "").strip()
BAB_CSV      = Path("bab_preisliste.csv")
SUPPLIER_MAP = Path("supplier_map.json")
REVIEW_FILE  = Path("icecat_mpn_review.json")
MIN_PX       = 500

_REFURB = re.compile(r"^(REF|REL|RER|RET|REU)|refurb|tecxl|upcycle|reteq|\bt1a\b|grade\s+[a-c]\b", re.I)


def norm_mpn(s: str) -> str:
    """Vergleichsform einer Modellnummer: nur alphanumerisch, lowercase."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def identity_ok(searched_mpn: str, part_code: str, title: str) -> bool:
    """Bild gehört zum Produkt, wenn die gesuchte MPN exakt dem von Icecat
    gelieferten BrandPartCode entspricht ODER unverkennbar im Titel steht."""
    s = norm_mpn(searched_mpn)
    if len(s) < 4:
        return False                       # zu kurz = nicht eindeutig
    if s == norm_mpn(part_code):
        return True
    return s in norm_mpn(title)


def icecat_by_mpn(brand: str, mpn: str) -> dict | None:
    """Sucht Icecat per Brand + ProductCode. Gibt data-Objekt oder None."""
    params = {"UserName": ICECAT_USER, "Language": "de", "Brand": brand, "ProductCode": mpn}
    if ICECAT_APP_KEY:
        params["app_key"] = ICECAT_APP_KEY      # schaltet Full-Icecat frei, falls gesetzt
    try:
        r = requests.get(
            ICECAT_API, params=params,
            headers={"Authorization": f"Bearer {ICECAT_TOKEN}"},
            timeout=20,
        )
    except Exception:
        return None
    if r.status_code in (403, 404):
        return None                        # 403 = Full-Icecat (nur in Action), 404 = nicht da
    if not r.ok:
        return None
    try:
        d = r.json()
    except Exception:
        return None
    return d.get("data") if d.get("msg") == "OK" else None


def pick_image(data: dict) -> str:
    img = data.get("Image") or {}
    return img.get("Pic500x500") or img.get("HighPic") or img.get("LowPic") or ""


def size_ok(url: str) -> bool | None:
    """True/False ob ≥500x500; None wenn nicht prüfbar. Nur mit --check-size."""
    try:
        from PIL import Image
        import io
        r = requests.get(url, timeout=15)
        if not r.ok:
            return None
        im = Image.open(io.BytesIO(r.content))
        return im.size[0] >= MIN_PX and im.size[1] >= MIN_PX
    except Exception:
        return None


def title_mpns(title: str) -> list[str]:
    """Modellnummer-Kandidaten aus dem Titel: alphanum-Tokens mit Buchstabe UND
    Ziffer, ≥5 Zeichen, längste zuerst (= distinktivste MPN)."""
    toks = {t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-/]{4,}", title or "")
            if any(c.isdigit() for c in t) and any(c.isalpha() for c in t)}
    return sorted(toks, key=len, reverse=True)


def load_candidates(only_missing: bool) -> list[dict]:
    """BAB-Produkte mit Marke + Modellnummer. Brand = ManufacturerCode (Icecat-Markencode,
    z.B. AMD/SEAGATE), MPN = ReferenceNumber (echte Teilenummer); Titel-MPNs als Fallback.
    only_missing = nur Produkte ohne bereits verifiziertes Bild."""
    verified = set()
    if only_missing and SUPPLIER_MAP.exists():
        smap = json.loads(SUPPLIER_MAP.read_text(encoding="utf-8"))
        verified = {k for k, v in smap.items() if v.get("image_verified")}
    out = []
    with BAB_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            sku   = (row.get("ItemNo") or "").strip()
            brand = (row.get("ManufacturerCode") or "").strip()
            title = (row.get("Description") or "").strip()
            ref   = (row.get("ReferenceNumber") or "").strip()
            # MPN-Kandidaten: erst die echte Teilenummer, dann Titel-Tokens
            mpns = ([ref] if ref else []) + [m for m in title_mpns(title) if m != ref]
            if not (sku and brand and mpns):
                continue
            if _REFURB.search(sku) or _REFURB.search(title):
                continue
            if only_missing and sku in verified:
                continue
            out.append({"sku": sku, "brand": brand, "mpns": mpns, "title": title,
                        "ean": (row.get("GTIN") or "").strip()})
    return out


IMAGES_FILE = Path("icecat_mpn_images.json")  # Apply-Ziel: {sku: image_url} (nur Shopify, eBay unberührt)


def apply_review():
    """Übernimmt die identitätsbestätigten Review-Treffer in icecat_mpn_images.json.
    Diese Datei liest build_matrixify_csv.py als Shopify-Bildquelle (supplier_map/eBay
    bleibt unangetastet). Bilder sind alle icecat.biz (rechtssicher) + MPN-verifiziert."""
    if not REVIEW_FILE.exists():
        print("Keine Review-Datei — erst Suche laufen lassen.", file=sys.stderr); return
    hits = json.loads(REVIEW_FILE.read_text(encoding="utf-8"))
    out = {h["sku"]: h["image"] for h in hits if "icecat.biz" in h.get("image", "")}
    IMAGES_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ {len(out)} bestätigte Bilder → {IMAGES_FILE}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="nur N Produkte (Test)")
    ap.add_argument("--only-missing", action="store_true", help="nur Produkte ohne verifiziertes Bild")
    ap.add_argument("--check-size", action="store_true", help="Bildgröße per Download prüfen (langsam)")
    ap.add_argument("--apply", action="store_true", help="Review → icecat_mpn_images.json (kein Suchlauf)")
    args = ap.parse_args()

    if args.apply:
        apply_review(); return

    cands = load_candidates(args.only_missing)
    if args.limit:
        cands = cands[:args.limit]
    print(f"Kandidaten: {len(cands)}", file=sys.stderr)

    hits, found = [], 0
    for i, c in enumerate(cands, 1):
        rec = None
        for mpn in c["mpns"][:4]:          # erst echte Teilenummer, dann Titel-MPNs
            data = icecat_by_mpn(c["brand"], mpn)
            time.sleep(0.3)                # ponytail: höflich, kein Hammer auf Icecat
            if not data:
                continue
            gi = data.get("GeneralInfo") or {}
            part = gi.get("BrandPartCode") or gi.get("ProductCode") or ""
            title = gi.get("Title") or ""
            if not identity_ok(mpn, part, title):
                continue                   # Identität NICHT bestätigt → verwerfen (kein Falschbild)
            img = pick_image(data)
            if not img:
                continue
            rec = {"sku": c["sku"], "brand": c["brand"], "mpn": mpn,
                   "bab_title": c["title"], "icecat_title": title,
                   "icecat_partcode": part, "image": img, "ean": c["ean"]}
            if args.check_size:
                rec["size_ok"] = size_ok(img)
            break                          # erster bestätigter Treffer reicht
        if rec:
            hits.append(rec)
            found += 1
        if i % 50 == 0:
            print(f"  {i}/{len(cands)} … bestätigte Treffer: {found}", file=sys.stderr)

    REVIEW_FILE.write_text(json.dumps(hits, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ Identitätsbestätigte Bild-Treffer: {found}", file=sys.stderr)
    print(f"   → Review-Datei: {REVIEW_FILE} (schaltet NICHTS live)", file=sys.stderr)


# ── Self-Check (ponytail: eine runnable Prüfung der Kernlogik) ──────────────
def _selftest():
    assert identity_ok("ST8000VN004", "ST8000VN004", "Seagate IronWolf ST8000VN004 8TB")
    assert identity_ok("ST8000VN004", "", "Seagate IronWolf ST8000VN004 Festplatte")  # MPN im Titel
    assert not identity_ok("ST8000VN004", "ST4000VN006", "Seagate IronWolf ST4000VN006")  # falsches Modell
    assert not identity_ok("ABC", "ABC", "kurz")          # zu kurz
    assert norm_mpn("ST-8000 VN004") == "st8000vn004"
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
