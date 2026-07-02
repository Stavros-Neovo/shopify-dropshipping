#!/usr/bin/env python3
"""Findet aktive Produkte im Build mit unscharfem (<500px) oder totem Bild und
repariert sie: erst Kosatec-XL-Rescue (≥500px + Identität), sonst image_verified=False
+ image_too_small=True → nächster Build stellt sie auf draft (kein schlechtes Bild live).

    python3 fix_bad_active_images.py --dry-run
    python3 fix_bad_active_images.py
"""
import argparse, csv, io, json, sys, urllib.request
from datetime import datetime
from pathlib import Path
from PIL import Image
from concurrent.futures import ThreadPoolExecutor

sys.argv_backup = sys.argv; sys.argv = ["x"]
from apply_kosatec_images import identity_ok
sys.argv = sys.argv_backup
csv.field_size_limit(sys.maxsize)

BUILD = "public/shopify_products.csv"
SMAP = "supplier_map.json"
ART = "artikeldaten.csv"
MIN_PX = 500


def resolution(u):
    raw = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read()
    return min(Image.open(io.BytesIO(raw)).size)


def kosatec_lookup(eans: set) -> dict:
    """EAN -> {xl_url, herstnr, artname, hersteller} aus artikeldaten.csv."""
    out = {}
    with open(ART, encoding="utf-8", errors="replace") as f:
        r = csv.reader(f, delimiter=";"); next(r)
        for row in r:
            if len(row) <= 31:
                continue
            e = str(row[5]).strip().lstrip("0")
            if e in eans and e not in out:
                out[e] = {"xl": row[31].strip().split("|")[0], "herstnr": row[1],
                          "artname": row[2], "hersteller": row[3], "title": row[20]}
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = [r for r in csv.DictReader(open(BUILD)) if r.get("Title")
            and r.get("Status") == "active" and r.get("Image Src")]

    def chk(r):
        try:
            return r["Variant SKU"], resolution(r["Image Src"])
        except Exception:
            return r["Variant SKU"], -1

    checked = list(ThreadPoolExecutor(max_workers=24).map(chk, rows))
    bad = [s for s, m in checked if m < MIN_PX and not s.startswith("KOS-")]
    print(f"aktive mit schlechtem Bild (BAB): {len(bad)}")
    if not bad:
        return

    smap = json.load(open(SMAP, encoding="utf-8"))
    ean_of = {s: str(smap.get(s, {}).get("ean", "")).strip().lstrip("0") for s in bad}
    kos = kosatec_lookup(set(e for e in ean_of.values() if e))

    rescued, demoted = [], []
    for s in bad:
        k = kos.get(ean_of[s])
        ok = False
        if k and k["xl"] and identity_ok(smap.get(s, {}).get("title", ""), s, k)[0]:
            try:
                if resolution(k["xl"]) >= MIN_PX:
                    if not args.dry_run:
                        smap[s].update(image_url=k["xl"], images=[k["xl"]],
                                       image_verified=True, image_source="kosatec_ipcstore")
                        smap[s].pop("image_too_small", None)
                    rescued.append(s); ok = True
            except Exception:
                pass
        if not ok:
            if not args.dry_run and s in smap:
                smap[s]["image_verified"] = False
                smap[s]["image_too_small"] = True
            demoted.append(s)

    if not args.dry_run:
        Path(f"{SMAP}.bak_{datetime.now():%Y%m%d_%H%M%S}").write_text(
            json.dumps(json.load(open(SMAP, encoding="utf-8")), ensure_ascii=False, indent=2), encoding="utf-8")
        json.dump(smap, open(SMAP, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"{'(DRY) ' if args.dry_run else ''}Kosatec-XL gerettet: {len(rescued)} | auf draft demoted: {len(demoted)}")
    print("  gerettet:", rescued[:20])
    print("  demoted :", demoted[:20])


if __name__ == "__main__":
    main()
