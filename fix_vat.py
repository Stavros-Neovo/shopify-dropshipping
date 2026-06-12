"""
fix_vat.py
==========
Einmalig-Script: Setzt MwSt 19% auf alle bestehenden eBay-Angebote.
Liest SKUs aus supplier_map.json, ruft je Angebot per SKU ab und aktualisiert.

Aufruf:
  python fix_vat.py
  python fix_vat.py --dry-run
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from ebay_client import EbayClient, OFFER_PATH

log = logging.getLogger("fix_vat")


def get_offer_by_sku(client: EbayClient, sku: str) -> dict | None:
    """Holt das Angebot für eine SKU."""
    try:
        data = client._request("GET", OFFER_PATH, params={
            "sku": sku,
            "marketplace_id": client.marketplace_id,
        })
        offers = (data or {}).get("offers", [])
        return offers[0] if offers else None
    except Exception as e:
        err = str(e)
        if "25001" in err or "25002" in err:
            return None  # Kein Angebot für diese SKU
        log.warning(f"  SKU {sku}: {e}")
        return None


def update_offer_vat(client: EbayClient, offer: dict, dry_run: bool = False) -> bool:
    """Setzt MwSt 19% auf ein bestehendes Angebot."""
    offer_id = offer.get("offerId", "")
    sku = offer.get("sku", "")

    # Bereits korrekt?
    tax = offer.get("tax", {})
    if tax.get("vatPercentage") == 19.0 and tax.get("applyTax"):
        return None  # Skip-Signal

    price_val = offer.get("pricingSummary", {}).get("price", {}).get("value", "0")

    payload = {
        "sku":           sku,
        "marketplaceId": client.marketplace_id,
        "format":        offer.get("format", "FIXED_PRICE"),
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
    if offer.get("categoryId"):
        payload["categoryId"] = offer["categoryId"]
    if offer.get("listingDescription"):
        payload["listingDescription"] = offer["listingDescription"]
    if offer.get("merchantLocationKey"):
        payload["merchantLocationKey"] = offer["merchantLocationKey"]

    policies = {
        k: v for k, v in offer.get("listingPolicies", {}).items()
        if k in ("fulfillmentPolicyId", "paymentPolicyId", "returnPolicyId")
    }
    if policies:
        payload["listingPolicies"] = policies

    if dry_run:
        log.info(f"  DRY-RUN {sku} ({offer_id}): würde MwSt 19% setzen (aktuell: {tax})")
        return True

    try:
        client._request("PUT", f"{OFFER_PATH}/{offer_id}", json_body=payload)
        return True
    except Exception as e:
        log.warning(f"  ✗ {sku} ({offer_id}): {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="config_shop2.yaml")
    parser.add_argument("--supplier-map", default="supplier_map.json")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    client = EbayClient.from_env(cfg["ebay"])

    smap_path = Path(args.supplier_map)
    if not smap_path.exists():
        log.error("supplier_map.json nicht gefunden")
        sys.exit(1)

    smap = json.loads(smap_path.read_text(encoding="utf-8"))
    skus = list(smap.keys())
    log.info(f"SKUs aus supplier_map.json: {len(skus)}")

    ok = skip = error = no_offer = 0

    for i, sku in enumerate(skus, 1):
        offer = get_offer_by_sku(client, sku)
        if not offer:
            no_offer += 1
            continue

        result = update_offer_vat(client, offer, dry_run=args.dry_run)
        if result is None:
            skip += 1
        elif result:
            ok += 1
            if i % 50 == 0:
                log.info(f"[{i}/{len(skus)}] {ok} aktualisiert, {skip} übersprungen...")
        else:
            error += 1

        time.sleep(0.2)

    log.info(
        f"=== Fertig: {ok} aktualisiert | {skip} bereits korrekt | "
        f"{no_offer} kein Angebot | {error} Fehler ==="
    )


if __name__ == "__main__":
    main()
