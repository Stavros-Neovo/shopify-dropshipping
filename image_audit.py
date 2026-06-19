"""
image_audit.py — 100% sichere Bild-Verifizierung via Icecat EAN-Match
=======================================================================
Strategie:
  1. Jedes SKU mit image_unverified_ddg → Icecat per EAN abfragen
  2. Icecat hat Bild → image_url setzen, image_verified = True, DDG entfernen
  3. Icecat hat kein Bild → DDG entfernen, image_verified = False (kein Bild)
  4. Listings ohne verifiziertes Bild → auf eBay deaktivieren

Aufruf:
  python image_audit.py              # alle unverifizierten SKUs
  python image_audit.py --limit 50   # max 50 pro Run
  python image_audit.py --dry-run    # nur anzeigen, nichts schreiben
"""
from __future__ import annotations

import argparse
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

SUPPLIER_MAP = Path("supplier_map.json")

ICECAT_API   = "https://live.icecat.biz/api"
ICECAT_USER  = os.environ.get("ICECAT_USER", "neovogen")
ICECAT_TOKEN = os.environ.get("ICECAT_TOKEN", "a923fe60-04bd-4f83-ae2e-a1e1a8427c98")


# ---------------------------------------------------------------------------
# Icecat lookup by EAN
# ---------------------------------------------------------------------------

def icecat_by_ean(ean: str) -> dict | None:
    """Gibt Icecat-Produkt-Dict zurück oder None wenn nicht gefunden."""
    try:
        url = f"{ICECAT_API}?UserName={ICECAT_USER}&Language=de&GTIN={ean}&output=productid"
        r = requests.get(url, headers={"Authorization": f"Bearer {ICECAT_TOKEN}"},
                         timeout=15)
        if not r.ok:
            return None
        data = r.json()
        msg = data.get("Message", {})
        if msg.get("Code") != 0:
            return None
        return data.get("data", {})
    except Exception as e:
        log.debug(f"Icecat Fehler für EAN {ean}: {e}")
        return None


def extract_image(data: dict) -> str | None:
    """Holt bestes Bild aus Icecat-Response."""
    if not data:
        return None
    # Haupt-Bild
    img = (data.get("GeneralInfo", {})
               .get("IcecatId") and
           data.get("Image", {}).get("HighUrl") or
           data.get("Image", {}).get("LowUrl"))
    if not img:
        # Aus GeneralInfo
        img = data.get("GeneralInfo", {}).get("Image", {}).get("HighUrl")
    if not img:
        # Erster Multimedia-Eintrag
        for m in data.get("Multimedia", []):
            if m.get("Type", "").lower() in ("image", "pic", "photo"):
                img = m.get("Pic") or m.get("URL")
                break
    return img or None


def icecat_images_all(data: dict) -> list[str]:
    """Holt alle Bilder aus Icecat-Response."""
    imgs = []
    # Haupt-Bild
    main = extract_image(data)
    if main:
        imgs.append(main)
    # Galerie
    for m in data.get("Multimedia", []):
        url = m.get("Pic") or m.get("URL", "")
        if url and url not in imgs:
            imgs.append(url)
    return imgs[:8]


# ---------------------------------------------------------------------------
# eBay: Angebot zurückziehen (deaktivieren)
# ---------------------------------------------------------------------------

def get_ebay_token() -> str | None:
    """Holt eBay Access Token via Refresh Token."""
    client_id     = os.environ.get("EBAY_CLIENT_ID")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET")
    refresh_token = os.environ.get("EBAY_REFRESH_TOKEN_2") or os.environ.get("EBAY_REFRESH_TOKEN", "")
    if not (client_id and client_secret and refresh_token):
        log.warning("eBay Credentials fehlen — eBay-Deaktivierung übersprungen")
        return None
    try:
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            auth=(client_id, client_secret),
            data={"grant_type": "refresh_token", "refresh_token": refresh_token,
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


def ebay_withdraw_offer(token: str, sku: str) -> bool:
    """Zieht ein eBay-Offer zurück (deaktiviert Listing)."""
    try:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        # Offer per SKU finden
        r = requests.get(
            f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}",
            headers=headers, timeout=10
        )
        if not r.ok:
            return False
        offers = r.json().get("offers", [])
        for offer in offers:
            if offer.get("status") == "PUBLISHED":
                offer_id = offer["offerId"]
                wd = requests.post(
                    f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/withdraw",
                    headers=headers, timeout=10
                )
                if wd.ok:
                    log.info(f"    eBay deaktiviert: {sku} (Offer {offer_id})")
                    return True
        return False
    except Exception as e:
        log.debug(f"eBay withdraw Fehler {sku}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",   type=int, default=100,  help="Max SKUs pro Run")
    parser.add_argument("--dry-run", action="store_true",    help="Nur anzeigen")
    parser.add_argument("--deactivate", action="store_true", help="Listings ohne Bild auf eBay deaktivieren")
    args = parser.parse_args()

    sm = json.loads(SUPPLIER_MAP.read_text())

    # Kandidaten: DDG-Bild vorhanden (nicht verifiziert)
    candidates = [
        (sku, v) for sku, v in sm.items()
        if v.get("image_unverified_ddg") and not v.get("image_url")
    ]
    log.info(f"Unverifizierte DDG-Bilder: {len(candidates)}")
    log.info(f"Limit: {args.limit} | dry-run: {args.dry_run}")

    ebay_token = get_ebay_token() if args.deactivate and not args.dry_run else None

    stats = {"icecat_hit": 0, "removed_ddg": 0, "deactivated": 0, "errors": 0}
    changed = False

    for i, (sku, v) in enumerate(candidates[:args.limit], 1):
        ean = v.get("ean", "")
        title = v.get("title", "")[:45]
        log.info(f"[{i}/{min(len(candidates), args.limit)}] {sku} | EAN {ean} | {title}")

        if not ean:
            log.info("    Kein EAN → DDG entfernen")
            if not args.dry_run:
                sm[sku].pop("image_unverified_ddg", None)
                sm[sku]["image_verified"] = False
                changed = True
            stats["removed_ddg"] += 1
            continue

        # Icecat abfragen
        data = icecat_by_ean(ean)
        img_url = extract_image(data) if data else None

        if img_url:
            log.info(f"    ✅ Icecat-Bild gefunden: {img_url[:60]}")
            stats["icecat_hit"] += 1
            if not args.dry_run:
                sm[sku]["image_url"]           = img_url
                sm[sku]["images"]              = icecat_images_all(data)
                sm[sku]["image_verified"]      = True
                sm[sku].pop("image_unverified_ddg", None)
                changed = True
        else:
            log.info("    ❌ Kein Icecat-Bild → DDG entfernen, Listing deaktivieren")
            stats["removed_ddg"] += 1
            if not args.dry_run:
                sm[sku].pop("image_unverified_ddg", None)
                sm[sku]["image_verified"] = False
                changed = True
            # eBay deaktivieren wenn kein Bild
            if args.deactivate and ebay_token and not args.dry_run:
                if ebay_withdraw_offer(ebay_token, sku):
                    stats["deactivated"] += 1

        time.sleep(0.3)  # Icecat Rate-Limit

    if changed:
        SUPPLIER_MAP.write_text(json.dumps(sm, ensure_ascii=False, indent=2))
        log.info(f"✓ supplier_map.json gespeichert")

    log.info("=" * 50)
    log.info(f"Ergebnis:")
    log.info(f"  ✅ Icecat-Bilder gefunden:    {stats['icecat_hit']}")
    log.info(f"  🗑️  DDG-Bilder entfernt:       {stats['removed_ddg']}")
    log.info(f"  📴 eBay deaktiviert:           {stats['deactivated']}")
    remaining = len([v for v in sm.values() if v.get("image_unverified_ddg")])
    log.info(f"  ⚠️  Noch unverifiziert:         {remaining}")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
