"""
image_audit.py — 100% sichere Bild-Verifizierung
==================================================
Einzige Quelle: Icecat API per EAN (aus BAB CSV)
Kein DDG. Kein Name-Suche. Kein Raten.

Wenn Icecat kein Bild hat → kein Bild. Listing bleibt deaktiviert.
Wenn Icecat Bild hat → verifiziert, Listing reaktiviert.

Aufruf:
  python image_audit.py              # 100 SKUs
  python image_audit.py --all        # alle auf einmal
  python image_audit.py --dry-run    # nur anzeigen
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("image_audit")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

SUPPLIER_MAP  = Path("supplier_map.json")
BAB_CSV       = Path("bab_preisliste.csv")

ICECAT_API    = "https://live.icecat.biz/api"
ICECAT_USER   = os.environ.get("ICECAT_USER",  "neovogen")
ICECAT_TOKEN  = os.environ.get("ICECAT_TOKEN", "a923fe60-04bd-4f83-ae2e-a1e1a8427c98")

EBAY_AUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_INV_URL  = "https://api.ebay.com/sell/inventory/v1"


# ---------------------------------------------------------------------------
# BAB CSV: EAN-Mapping (Quelle der Wahrheit)
# ---------------------------------------------------------------------------

def load_bab_eans() -> dict[str, str]:
    """Gibt {sku: ean} aus BAB CSV zurück."""
    mapping = {}
    if not BAB_CSV.exists():
        log.warning("bab_preisliste.csv nicht gefunden — EANs aus supplier_map")
        return mapping
    with BAB_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            sku = (row.get("ItemNo") or "").strip()
            ean = (row.get("GTIN")   or "").strip()
            if sku and ean and len(ean) >= 8:
                mapping[sku] = ean
    log.info(f"BAB CSV: {len(mapping)} EANs geladen")
    return mapping


# ---------------------------------------------------------------------------
# Icecat: NUR per EAN
# ---------------------------------------------------------------------------

def icecat_fetch(ean: str) -> dict | None:
    """Icecat-Daten per EAN — einzige erlaubte Bildquelle."""
    url = (f"{ICECAT_API}?UserName={ICECAT_USER}"
           f"&Language=de&GTIN={ean}&output=productid")
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {ICECAT_TOKEN}"},
            timeout=15,
        )
        if r.status_code == 404:
            return None  # EAN nicht in Icecat
        if not r.ok:
            log.debug(f"Icecat HTTP {r.status_code} für EAN {ean}")
            return None
        data = r.json()
        code = data.get("Message", {}).get("Code")
        if code != 0:
            return None
        return data.get("data") or None
    except Exception as e:
        log.debug(f"Icecat Fehler EAN {ean}: {e}")
        return None


def extract_images(data: dict) -> tuple[str, list[str]]:
    """Gibt (haupt_bild, alle_bilder) aus Icecat-Daten zurück."""
    if not data:
        return "", []

    img_obj  = data.get("Image") or {}
    main_url = (img_obj.get("HighUrl") or img_obj.get("LowUrl") or
                data.get("GeneralInfo", {}).get("Image", {}).get("HighUrl") or "")

    all_imgs = []
    if main_url:
        all_imgs.append(main_url)

    for m in data.get("Multimedia") or []:
        u = m.get("Pic") or m.get("URL") or ""
        if u and u not in all_imgs:
            ext = u.lower().split("?")[0]
            if any(ext.endswith(x) for x in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                all_imgs.append(u)

    return main_url, all_imgs[:8]


# ---------------------------------------------------------------------------
# eBay: Offer withdraw / publish
# ---------------------------------------------------------------------------

def get_ebay_token() -> str | None:
    cid = os.environ.get("EBAY_CLIENT_ID")
    cs  = os.environ.get("EBAY_CLIENT_SECRET")
    rt  = os.environ.get("EBAY_REFRESH_TOKEN_2") or os.environ.get("EBAY_REFRESH_TOKEN", "")
    if not (cid and cs and rt):
        log.warning("eBay Credentials fehlen")
        return None
    try:
        r = requests.post(
            EBAY_AUTH_URL,
            auth=(cid, cs),
            data={"grant_type": "refresh_token", "refresh_token": rt,
                  "scope": ("https://api.ebay.com/oauth/api_scope/sell.inventory "
                            "https://api.ebay.com/oauth/api_scope/sell.account "
                            "https://api.ebay.com/oauth/api_scope/sell.fulfillment")},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["access_token"]
    except Exception as e:
        log.warning(f"eBay Token Fehler: {e}")
        return None


def ebay_get_offer(token: str, sku: str) -> tuple[str, str]:
    """Gibt (offer_id, status) zurück."""
    h = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(f"{EBAY_INV_URL}/offer?sku={sku}", headers=h, timeout=30)
        if not r.ok:
            return "", ""
        offers = r.json().get("offers", [])
        if not offers:
            return "", ""
        o = offers[0]
        return o.get("offerId", ""), o.get("status", "")
    except Exception as e:
        log.warning(f"ebay_get_offer Fehler ({sku}): {e}")
        return "", ""


def ebay_withdraw(token: str, offer_id: str) -> bool:
    h = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.post(f"{EBAY_INV_URL}/offer/{offer_id}/withdraw", headers=h, timeout=30)
        return r.ok
    except Exception as e:
        log.warning(f"ebay_withdraw Fehler: {e}")
        return False


def ebay_publish(token: str, offer_id: str) -> bool:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(f"{EBAY_INV_URL}/offer/{offer_id}/publish", headers=h, timeout=30)
        return r.ok
    except Exception as e:
        log.warning(f"ebay_publish Fehler: {e}")
        return False


def ebay_update_image(token: str, sku: str, images: list[str]) -> bool:
    """Aktualisiert das Bild im eBay Inventory Item."""
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.get(f"{EBAY_INV_URL}/inventory_item/{sku}", headers=h, timeout=30)
        if not r.ok:
            return False
        item = r.json()
        item.setdefault("product", {})["imageUrls"] = images[:1]
        r2 = requests.put(
            f"{EBAY_INV_URL}/inventory_item/{sku}",
            headers=h, json=item, timeout=30
        )
        return r2.ok
    except Exception as e:
        log.warning(f"ebay_update_image Fehler: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",       action="store_true", help="Alle auf einmal")
    parser.add_argument("--limit",     type=int, default=100)
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--no-ebay",   action="store_true", help="Kein eBay-Update")
    args = parser.parse_args()

    sm       = json.loads(SUPPLIER_MAP.read_text())
    bab_eans = load_bab_eans()

    # Kandidaten: alles ohne image_verified=True (kein Bild, DDG-Rest, unverif. ipcstore)
    candidates = [
        (sku, v) for sku, v in sm.items()
        if not v.get("image_verified")
    ]
    limit = len(candidates) if args.all else args.limit
    log.info(f"Kandidaten gesamt: {len(candidates)} | Limit: {limit}")
    log.info(f"dry-run: {args.dry_run}")

    ebay_token = None
    if not args.no_ebay and not args.dry_run:
        ebay_token = get_ebay_token()
        log.info(f"eBay Token: {'✓' if ebay_token else '✗ (übersprungen)'}")

    stats = {"icecat_hit": 0, "no_icecat": 0, "ebay_updated": 0, "ebay_deactivated": 0}
    changed = False

    for i, (sku, v) in enumerate(candidates[:limit], 1):
        # EAN aus BAB CSV (Quelle der Wahrheit), Fallback: supplier_map
        ean = bab_eans.get(sku) or v.get("ean", "")
        title = v.get("title", "")[:45]

        log.info(f"[{i}/{min(len(candidates), limit)}] {sku} | EAN={ean} | {title}")

        if not ean or len(ean) < 8:
            log.info("    ⚠️  Kein gültiger EAN → überspringe")
            continue

        # Icecat abfragen — einzige erlaubte Quelle
        data = icecat_fetch(ean)
        main_img, all_imgs = extract_images(data)

        if main_img:
            log.info(f"    ✅ Icecat-Bild: {main_img[:70]}")
            stats["icecat_hit"] += 1
            if not args.dry_run:
                sm[sku]["image_url"]          = main_img
                sm[sku]["images"]             = all_imgs
                sm[sku]["image_verified"]     = True
                sm[sku]["ean"]               = ean  # BAB-EAN als Quelle sichern
                sm[sku].pop("image_unverified_ddg", None)
                changed = True

            # eBay: Bild aktualisieren + reaktivieren
            if ebay_token and not args.dry_run:
                ebay_update_image(ebay_token, sku, all_imgs)
                offer_id, status = ebay_get_offer(ebay_token, sku)
                if offer_id and status == "UNPUBLISHED":
                    if ebay_publish(ebay_token, offer_id):
                        log.info(f"    📢 eBay reaktiviert")
                        stats["ebay_updated"] += 1
        else:
            log.info("    ❌ Nicht in Icecat — kein Bild, Listing bleibt deaktiviert")
            stats["no_icecat"] += 1
            if not args.dry_run:
                sm[sku].pop("image_unverified_ddg", None)
                sm[sku]["image_verified"] = False
                sm[sku]["ean"]            = ean
                changed = True

            # eBay deaktivieren falls noch aktiv
            if ebay_token and not args.dry_run:
                offer_id, status = ebay_get_offer(ebay_token, sku)
                if offer_id and status == "PUBLISHED":
                    if ebay_withdraw(ebay_token, offer_id):
                        log.info(f"    📴 eBay deaktiviert")
                        stats["ebay_deactivated"] += 1

        time.sleep(0.4)  # Icecat Rate-Limit

    if changed:
        SUPPLIER_MAP.write_text(json.dumps(sm, ensure_ascii=False, indent=2))
        log.info("✓ supplier_map.json gespeichert")

    # Zusammenfassung
    remaining = sum(1 for v in sm.values() if v.get("image_unverified_ddg"))
    verified  = sum(1 for v in sm.values() if v.get("image_verified"))
    log.info("=" * 55)
    log.info(f"✅ Icecat-Bilder gefunden:     {stats['icecat_hit']}")
    log.info(f"❌ Nicht in Icecat:            {stats['no_icecat']}")
    log.info(f"📢 eBay reaktiviert:           {stats['ebay_updated']}")
    log.info(f"📴 eBay deaktiviert:           {stats['ebay_deactivated']}")
    log.info(f"─────────────────────────────────────────────────────")
    log.info(f"✅ Gesamt verifiziert:          {verified}/{len(sm)}")
    log.info(f"⚠️  Noch DDG-unsicher:          {remaining}")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
