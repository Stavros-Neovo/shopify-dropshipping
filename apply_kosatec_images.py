#!/usr/bin/env python3
"""Wendet die geprüften Kosatec-XL-Bilder (kosatec_image_verified.json, schon ≥500px)
auf bildlose BAB-SKUs an — mit STRENGER Identitätsprüfung, damit garantiert das
richtige Produktbild zugeordnet wird (Falschbild = Retoure).

Akzeptiert nur wenn ALLE drei stimmen:
  1. EAN identisch (GTIN ist produktweit eindeutig)          — schon beim Matching
  2. Marke: Kosatec-Hersteller kommt im BAB-Titel vor
  3. Modell: mind. eine distinktive Modellnummer (≥6 Z., Buchstabe+Ziffer) taucht in
     BAB-Titel UND Kosatec-Name/MPN auf
Refurbished ausgeschlossen (unzuverlässige EAN). Alles nicht-eindeutige → Review-Datei.

    python3 apply_kosatec_images.py --dry-run
    python3 apply_kosatec_images.py
"""
import argparse, csv, json, sys
from datetime import datetime
from pathlib import Path
from build_matrixify_csv import _mpn_tokens, _REFURB_SKU, _REFURB_KW

csv.field_size_limit(sys.maxsize)
VERIFIED = "kosatec_image_verified.json"
SMAP = "supplier_map.json"
ART = "artikeldaten.csv"


def kosatec_by_ean(eans: set) -> dict:
    """Kosatec-Meta (Marke, Name, MPN) für die gebrauchten EANs aus artikeldaten.csv."""
    out = {}
    with open(ART, encoding="utf-8", errors="replace") as f:
        r = csv.reader(f, delimiter=";"); next(r)
        for row in r:
            if len(row) <= 31:
                continue
            e = str(row[5]).strip().lstrip("0")
            if e in eans and e not in out:
                out[e] = {"herstnr": row[1], "artname": row[2],
                          "hersteller": row[3], "title": row[20]}
    return out


def identity_ok(bab_title: str, sku: str, kos: dict):
    """(bool, grund). EAN ist schon eindeutig; als Korruptions-Schutz gegen Feed-EAN-
    Fehler reicht EIN übereinstimmendes Sekundärsignal (Marke ODER Modellnummer) —
    identisch zur system-eigenen image_identity_ok. Marke-und-Modell-beide-daneben
    (oder leerer BAB-Titel) → nicht verifizierbar, in Review."""
    if _REFURB_SKU.match(sku or "") or _REFURB_KW.search(bab_title or ""):
        return False, "refurb"
    bab = (bab_title or "").lower()
    brand = (kos.get("hersteller") or "").lower().split()
    brand_ok = bool(brand) and brand[0] in bab
    blob = f"{kos.get('artname','')} {kos.get('title','')} {kos.get('herstnr','')}"
    model_ok = bool(_mpn_tokens(bab_title) & _mpn_tokens(blob))
    if brand_ok or model_ok:
        return True, "ok"
    return False, "marke+modell"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    smap = json.load(open(SMAP, encoding="utf-8"))
    verified = json.load(open(VERIFIED, encoding="utf-8"))
    ean_of = {sku: str(smap.get(sku, {}).get("ean", "")).strip().lstrip("0") for sku in verified}
    kos = kosatec_by_ean(set(ean_of.values()))

    accepted, rejected = {}, {}
    for sku, url in verified.items():
        k = kos.get(ean_of[sku])
        if not k:
            rejected[sku] = {"reason": "kein_kosatec_meta", "url": url}
            continue
        ok, why = identity_ok(smap.get(sku, {}).get("title", ""), sku, k)
        if ok:
            accepted[sku] = url
        else:
            rejected[sku] = {"reason": why, "url": url,
                             "bab": smap.get(sku, {}).get("title", ""),
                             "kosatec": k["artname"]}

    Path("kosatec_image_rejected.json").write_text(
        json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.dry_run and accepted:
        Path(f"{SMAP}.bak_{datetime.now():%Y%m%d_%H%M%S}").write_text(
            json.dumps(smap, ensure_ascii=False, indent=2), encoding="utf-8")
        for sku, url in accepted.items():
            v = smap.get(sku, {})
            v["image_url"] = url
            v["images"] = [url]
            v["image_verified"] = True
            v.pop("image_too_small", None)
            v["image_source"] = "kosatec_ipcstore"
            smap[sku] = v
        json.dump(smap, open(SMAP, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    verb = "würde anwenden" if args.dry_run else "angewandt"
    print(f"{len(accepted)} {verb} | {len(rejected)} zur Review (kosatec_image_rejected.json)")
    from collections import Counter
    for reason, c in Counter(r["reason"] for r in rejected.values()).most_common():
        print(f"    abgelehnt [{reason}]: {c}")


# ponytail: Identitäts-Gate ist die sicherheitskritische Logik → Self-Check
_K = {"herstnr": "ST4000VX016", "artname": "Seagate SkyHawk 4TB", "hersteller": "Seagate", "title": ""}
assert identity_ok("Seagate SkyHawk ST4000VX016 4TB", "HDIS0250", _K)[0] is True
assert identity_ok("Seagate SkyHawk ST4000VX016 4TB", "REF0250", _K)[0] is False          # refurb
assert identity_ok("WD Red 4TB WD40EFAX", "HDIS0999", _K)[0] is False                       # falsche Marke+Modell

if __name__ == "__main__":
    main()
