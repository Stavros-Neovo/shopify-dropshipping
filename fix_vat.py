"""
fix_vat.py
==========
Einmalig-Script: Setzt MwSt 19% auf alle bestehenden eBay-Angebote.

Aufruf:
  python fix_vat.py
  python fix_vat.py --dry-run
"""
import argparse
import logging
import os
import sys
import time

import yaml
from dotenv import load_dotenv
from ebay_client import EbayClient, OFFER_PATH

log = logging.getLogger("fix_vat")


def fetch_all_offers(client: EbayClient) -> list[dict]:
    """Holt alle Angebote paginiert."""
    offers = []
    limit = 100
    offset = 0
    while True:
        try:
            data = client._request("GET", OFFER_PATH, params={
                "marketplace_id": client.marketplace_id,
                "limit": limit,
                "offset": offset,
            })
        except Exception as e:
            log.error(f"Fehler beim Abrufen der Angebote: {e}")
            break

        batch = (data or {}).get("offers", [])
        if not batch:
            break
        offers.extend(batch)
        total = (data or {}).get("total", 0)
        offset += limit
        log.info(f"Geladen: {len(offers)}/{total} Angebote...")
        if offset >= total:
            break

    return offers


def fix_offer_vat(client: EbayClient, offer: dict, dry_run: bool = False) -> bool:
    """Fügt MwSt 19% zum Angebot hinzu."""
    offer_id = offer.get("offerId", "")
    sku = offer.get("sku", "")
    tax = offer.get("tax", {})

    # Bereits korrekt gesetzt?
    if tax.get("vatPercentage") == 19.0 and tax.get("applyTax"):
        return True  # nichts zu tun

    pricing = offer.get("pricingSummary", {})
    price_val = pricing.get("price", {}).get("value", "0")
    cat_id = offer.get("categoryId", "")

    payload = {
        "sku": sku,
        "marketplaceId": client.marketplace_id,
        "format": "FIXED_PRICE",
        "listingDuration": offer.get("listingDuration", "GTC"),
        "includeCatalogProductDetails": False,
        "pricingSummary": {
            "price": {"value": price_val, "currency": "EUR"}
        },
        "tax": {
            "vatPercentage": 19.0,
            "applyTax": True,
        },
    }
    if cat_id:
        payload["categoryId"] = cat_id
    if offer.get("listingDescription"):
        payload["listingDescription"] = offer["listingDescription"]
    if offer.get("merchantLocationKey"):
        payload["merchantLocationKey"] = offer["merchantLocationKey"]

    # Business Policies übernehmen
    policies = offer.get("listingPolicies", {})
    if policies:
        payload["listingPolicies"] = {
            k: v for k, v in policies.items()
            if k in ("fulfillmentPolicyId", "paymentPolicyId", "returnPolicyId")
        }

    if dry_run:
        log.info(f"  DRY-RUN SKU {sku} ({offer_id}): würde MwSt 19% setzen")
        return True

    try:
        client._request("PUT", f"{OFFER_PATH}/{offer_id}", json_body=payload)
        return True
    except Exception as e:
        log.warning(f"  ✗ SKU {sku} ({offer_id}): {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="config_shop2.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    client = EbayClient.from_env(cfg["ebay"])

    log.info("=== Alle eBay-Angebote abrufen ===")
    offers = fetch_all_offers(client)
    log.info(f"Gesamt: {len(offers)} Angebote")

    ok = skip = error = 0
    for i, offer in enumerate(offers, 1):
        tax = offer.get("tax", {})
        if tax.get("vatPercentage") == 19.0 and tax.get("applyTax"):
            skip += 1
            continue
        log.info(f"[{i}/{len(offers)}] SKU {offer.get('sku')} → MwSt setzen")
        if fix_offer_vat(client, offer, dry_run=args.dry_run):
            ok += 1
        else:
            error += 1
        time.sleep(0.2)

    log.info(f"=== Fertig: {ok} aktualisiert | {skip} bereits korrekt | {error} Fehler ===")


if __name__ == "__main__":
    main()
