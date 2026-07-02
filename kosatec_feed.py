#!/usr/bin/env python3
"""
kosatec_feed.py
===============
Kosatec als zweiten Lieferanten für die Shopify-Pipeline (build_matrixify_csv.py).

Liefert normalisierte Produkt-Dicts im SELBEN Format wie der BAB-Feed
(csv_loader.normalize_row), plus Kosatec-eigenes Bild + Enrichment, damit
build_rows() sie 1:1 wie BAB-Produkte verarbeitet (Preis, SEO, Google-Feed).

Filter (Nutzer-Vorgaben 2026-07-01):
  - nur konkurrenzfähig  (would_win aus kosatec_scan_results.json)
  - EK zwischen min_ek (Kleinteile raus) und max_ek (300 €)
  - netto-neu           (EAN nicht schon bei BAB)
  - auf Lager           (menge > 0)

Quellen:
  kosatec_scan_results.json  – Konkurrenz-Scan (would_win, hek, menge, kat1)
  artikeldaten.csv (222 MB)  – Bilder (images_m) + Beschreibungen + specs
"""
from __future__ import annotations
import csv, json, re
from pathlib import Path
from typing import Dict, Iterator

csv.field_size_limit(10_000_000)

SCAN_RESULTS = "kosatec_scan_results.json"
ARTIKELDATEN = "artikeldaten.csv"
SLIM_FILE    = "kosatec_products.json"   # schlanker, committbarer Export (Action liest den)
SKU_PREFIX   = "KOS-"

# Kosatec kat1 → (Shopify-Type / eBay-Kategoriename, Google-Produktkategorie).
# Type matcht wo möglich bestehende Smart-Collections, damit Kosatec-Produkte
# automatisch in die richtigen Kategorien fallen.
KAT_MAP: Dict[str, tuple[str, str]] = {
    "Netzwerk":              ("Netzwerk-Switches", "Electronics > Networking"),
    "Tablets & Smartphones": ("Tablets & Smartphones", "Electronics > Communications > Telephony > Mobile Phones"),
    "Home & Living":         ("Haushalt & Wohnen", "Home & Garden"),
    "Audio & Video":         ("Audio & Video", "Electronics > Audio"),
    "Kabel & Adapter":       ("Kabel & Adapter", "Electronics > Electronics Accessories > Cables"),
    "Eingabegeräte":         ("Mäuse, Trackballs & Touchpads", "Electronics > Computers > Input Devices"),
    "Flash-Speicher":        ("USB-Sticks & Flash-Speicher", "Electronics > Computers > Computer Components > Storage Devices"),
    "Gehäuse":               ("PC-Gehäuse", "Electronics > Computers > Computer Components"),
    "Arbeitsspeicher":       ("Arbeitsspeicher (RAM)", "Electronics > Computers > Computer Components > Computer Memory"),
    "Grafikkarten":          ("Grafikkarten", "Electronics > Computers > Computer Components"),
    "Verbrauchsmaterialien": ("Verbrauchsmaterial", "Electronics"),
    "Baumarkt & Garten":     ("Baumarkt & Garten", "Hardware"),
    "Notebooks & Zubehör":   ("Notebook-Zubehör", "Electronics > Computers"),
}
_DEFAULT = ("Sonstiges", "Electronics")


def _norm_ean(e: str) -> str:
    return (e or "").strip().lstrip("0")


def _first_image(images_field: str) -> str:
    """Pipe-getrennt — nimm das erste (Kosatec-Produktfoto, ipcstore/Icecat-CDN)."""
    return (images_field or "").split("|")[0].strip()


# Kosatec-Artname beginnt oft mit Port-Anzahl statt Marke: "3P …", "4+2P …", "48P …".
_PORT_PREFIX = re.compile(r"^\s*\d[\dP+]*P\s+", re.I)


def _clean_title(raw: str, brand: str = "") -> str:
    """Roher Kosatec-Artname → verkäufer-/SEO-tauglicher Titel: Port-Präfix weg,
    Marke nach vorne (= Suchbegriff). Sonst bleibt der Titel wie er ist."""
    t = re.sub(r"\s+", " ", _PORT_PREFIX.sub("", (raw or "").strip())).strip()
    b = (brand or "").strip()
    if b and not t.lower().startswith(b.lower()):
        t = f"{b} {t}"
    return t


def load_kosatec_products(bab_eans: set[str], *, min_ek: float = 10.0,
                          max_ek: float = 300.0, competitive_only: bool = True) -> Iterator[dict]:
    """Yield normalisierte Kosatec-Produkte, dedupliziert gegen BAB (EAN).

    Bevorzugt den schlanken, committbaren Export (SLIM_FILE); nur wenn der fehlt,
    wird die 222-MB-artikeldaten.csv gescannt (lokaler Dev/Export)."""
    if Path(SLIM_FILE).exists():
        for p in json.loads(Path(SLIM_FILE).read_text(encoding="utf-8")):
            e = _norm_ean(p.get("ean", ""))
            if e and e in bab_eans:
                continue
            if not (min_ek <= float(p.get("purchase_price", 0)) <= max_ek):
                continue
            yield p
        return
    yield from _build_from_artikeldaten(bab_eans, min_ek=min_ek, max_ek=max_ek,
                                        competitive_only=competitive_only)


def _build_from_artikeldaten(bab_eans: set[str], *, min_ek: float, max_ek: float,
                             competitive_only: bool) -> Iterator[dict]:
    """Baut die Produkte aus scan_results + artikeldaten.csv (lokal, 222 MB)."""
    scan = json.loads(Path(SCAN_RESULTS).read_text(encoding="utf-8"))

    # 1. Filter auf Scan-Ebene → Ziel-Artikelnummern sammeln
    targets: dict[str, dict] = {}
    for x in scan:
        if competitive_only and not x.get("would_win"):
            continue
        hek = float(x.get("hek") or 0)
        if not (min_ek <= hek <= max_ek):
            continue
        if int(x.get("menge") or 0) <= 0:
            continue
        if _norm_ean(x.get("ean", "")) in bab_eans:
            continue                       # Dedup: schon bei BAB → skip
        targets[str(x["artnr"])] = x
    if not targets:
        return

    # 2. artikeldaten.csv EINMAL scannen, nur Ziel-Zeilen behalten (222 MB → speichersparsam)
    with open(ARTIKELDATEN, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            artnr = str(row.get("artnr") or "")
            if artnr not in targets:
                continue
            t = targets[artnr]
            ean = _norm_ean(row.get("ean", ""))
            brand = (row.get("hersteller") or "").strip()
            title = _clean_title(row.get("artname") or row.get("title") or "", brand)
            kat1 = (row.get("kat1") or t.get("kat1") or "").strip()
            type_name, google_cat = KAT_MAP.get(kat1, _DEFAULT)
            image = _first_image(row.get("images_xl", ""))   # Full-Res (/img/<id>.jpg), NICHT images_m (=115px!)
            enrichment = {
                "title_full": title,
                "short_summary": (row.get("short_summary") or "").strip(),
                "long_summary": (row.get("long_summary") or "").strip(),
                "marketing_text": (row.get("marketing_text") or "").strip(),
                "specs_html": (row.get("specs") or "").strip(),
                "brand": (row.get("hersteller") or "").strip(),
                "manufacturer_url": (row.get("hersturl") or "").strip(),
                "image_main": image,
            }
            yield {
                "sku": SKU_PREFIX + artnr,
                "title": title,
                "ean": row.get("ean", "").strip(),
                "purchase_price": float(t["hek"]),
                "stock": int(t.get("menge") or 0),
                "weight_kg": _to_float(row.get("gewicht")),
                "brand": (row.get("hersteller") or "").strip(),
                "category": kat1,
                "type_override": type_name,      # ebay_category_name nutzt das
                "google_category": google_cat,   # get_google_category nutzt das
                "mpn_code": (row.get("herstnr") or "").strip(),
                "kosatec_image": image,          # Bildquelle (bypasst Icecat-Gate)
                "kosatec_enrichment": enrichment,
            }


def _to_float(s) -> float:
    try:
        return round(float(str(s).replace(",", ".")), 3)
    except (ValueError, TypeError):
        return 0.0


# ── Self-Check (ponytail) ───────────────────────────────────────────────────
def _selftest():
    assert _norm_ean("0065030836548") == "65030836548"
    assert _first_image("https://a/1.jpg|https://a/2.jpg") == "https://a/1.jpg"
    assert _clean_title("3P ALLNET ALL-SGI8003P POE industrial", "ALLNET") == "ALLNET ALL-SGI8003P POE industrial"
    assert _clean_title("4+2P ALLNET X POE", "ALLNET") == "ALLNET X POE"
    assert _clean_title("Xerox Toner 006R03841", "Xerox") == "Xerox Toner 006R03841"   # kein Port-Präfix, unverändert
    assert KAT_MAP["Netzwerk"][0] == "Netzwerk-Switches"
    assert KAT_MAP.get("gibtsnicht", _DEFAULT) == _DEFAULT
    print("selftest OK")


def _verify_images(prods: list, *, min_px: int = 500, workers: int = 20) -> int:
    """Pixel-Check gegen den CDN: kosatec_image < min_px oder unerreichbar → leeren
    (Produkt geht dann auf draft, statt ein unscharfes Bild zu veröffentlichen).
    Läuft NUR hier beim lokalen --export; die Action liest den geprüften Cache."""
    import io, urllib.request
    from PIL import Image
    from concurrent.futures import ThreadPoolExecutor

    def ok(u: str) -> bool:
        if not u:
            return False
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read()
            return min(Image.open(io.BytesIO(raw)).size) >= min_px
        except Exception:
            return False

    good = list(ThreadPoolExecutor(max_workers=workers).map(
        lambda p: ok(p.get("kosatec_image", "")), prods))
    dropped = 0
    for p, g in zip(prods, good):
        if p.get("kosatec_image") and not g:
            p["kosatec_image"] = ""
            if isinstance(p.get("kosatec_enrichment"), dict):
                p["kosatec_enrichment"]["image_main"] = ""
            dropped += 1
    return dropped


def export_slim(path: str = SLIM_FILE) -> int:
    """Erzeugt den schlanken, committbaren Produkt-Export aus der 222-MB-artikeldaten.csv.
    Verifiziert jedes Bild auf ≥500px — nicht bestandene werden geleert (→ draft)."""
    prods = list(_build_from_artikeldaten(set(), min_ek=10.0, max_ek=300.0, competitive_only=True))
    dropped = _verify_images(prods)
    print(f"Bild-Check: {dropped}/{len(prods)} Bilder <500px entfernt (→ draft)")
    Path(path).write_text(json.dumps(prods, ensure_ascii=False), encoding="utf-8")
    return len(prods)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    elif "--export" in sys.argv:
        n = export_slim()
        print(f"{n} Kosatec-Produkte → {SLIM_FILE} ({Path(SLIM_FILE).stat().st_size/1024:.0f} KB)")
    else:
        eans = set()
        n = sum(1 for _ in load_kosatec_products(eans))
        print(f"Kosatec-Produkte (ohne BAB-Dedup): {n}")
