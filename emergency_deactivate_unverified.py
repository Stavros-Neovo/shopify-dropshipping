"""
emergency_deactivate_unverified.py
===================================
NOTFALL: Alle Listings mit unverifizierten DDG-Bildern SOFORT auf eBay deaktivieren.
Kein falsches Bild mehr sichtbar → keine Fehlkäufe, keine Retouren.

Aufruf:
  python emergency_deactivate_unverified.py
  python emergency_deactivate_unverified.py --dry-run   # nur anzeigen
"""
from __future__ import annotations
import json, os, sys, time, logging, argparse
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("emergency")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

SUPPLIER_MAP = Path("supplier_map.json")


def get_token() -> str:
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        auth=(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]),
        data={"grant_type": "refresh_token",
              "refresh_token": os.environ.get("EBAY_REFRESH_TOKEN_2") or os.environ["EBAY_REFRESH_TOKEN"],
              "scope": ("https://api.ebay.com/oauth/api_scope/sell.inventory "
                        "https://api.ebay.com/oauth/api_scope/sell.account "
                        "https://api.ebay.com/oauth/api_scope/sell.fulfillment")},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def withdraw(token: str, sku: str) -> str:
    """Gibt 'withdrawn', 'already_unpublished', 'not_found', 'error' zurück."""
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.get(f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}", headers=h, timeout=10)
    if not r.ok:
        return "not_found"
    offers = r.json().get("offers", [])
    if not offers:
        return "not_found"
    for offer in offers:
        status = offer.get("status", "")
        if status == "PUBLISHED":
            oid = offer["offerId"]
            wd = requests.post(f"https://api.ebay.com/sell/inventory/v1/offer/{oid}/withdraw",
                               headers=h, timeout=10)
            return "withdrawn" if wd.ok else "error"
        if status == "UNPUBLISHED":
            return "already_unpublished"
    return "not_found"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sm = json.loads(SUPPLIER_MAP.read_text())

    # Alle SKUs mit unverifizierten DDG-Bildern
    targets = [(sku, v) for sku, v in sm.items()
               if v.get("image_unverified_ddg") and not v.get("image_url")]

    log.info(f"{'[DRY-RUN] ' if args.dry_run else ''}NOTFALL-Deaktivierung: {len(targets)} Listings mit unsicheren Bildern")

    if args.dry_run:
        for sku, v in targets:
            log.info(f"  WÜRDE deaktivieren: {sku} | {v.get('title','')[:50]}")
        log.info(f"Gesamt: {len(targets)} Listings würden deaktiviert")
        return

    log.info("Token holen …")
    token = get_token()
    log.info("✓ Token OK")

    stats = {"withdrawn": 0, "already_unpublished": 0, "not_found": 0, "error": 0}

    for i, (sku, v) in enumerate(targets, 1):
        title = v.get("title", "")[:45]
        result = withdraw(token, sku)
        stats[result] += 1
        icon = {"withdrawn": "📴", "already_unpublished": "✓", "not_found": "–", "error": "✗"}.get(result, "?")
        log.info(f"[{i}/{len(targets)}] {icon} {sku} | {title} → {result}")
        time.sleep(0.15)

    log.info("=" * 55)
    log.info(f"📴 Deaktiviert:          {stats['withdrawn']}")
    log.info(f"✓  Bereits inaktiv:      {stats['already_unpublished']}")
    log.info(f"–  Nicht auf eBay:       {stats['not_found']}")
    log.info(f"✗  Fehler:               {stats['error']}")
    log.info(f"→  Sichere Listings noch aktiv: alle mit image_verified=True")
    log.info("=" * 55)


if __name__ == "__main__":
    main()
