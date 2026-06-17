#!/usr/bin/env python3
"""
withdraw_unsafe_images.py
=========================
Deaktiviert eBay-Listings mit Bildern von unzuverlässigen Retailer-Sites.
Nur PUBLISHED Offers werden withdrawn (UNPUBLISHED werden übersprungen).

Aufruf lokal:  python withdraw_unsafe_images.py --config config_shop2.yaml
GitHub Action: secrets EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN_2
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from ebay_client import EbayClient

INVENTORY_PATH = "/sell/inventory/v1/inventory_item"

# Domains von Shopping-Sites / US-Retailern — keine zuverlässigen Produktbilder
UNSAFE_DOMAINS = {
    'pisces.bbystatic.com',          # Best Buy USA
    'www.ebuyer.com',                # UK Retailer
    'www.irvsluggage.com',           # Kofferladen
    'i5.walmartimages.com',          # Walmart
    'c1.neweggimages.com',           # Newegg
    'down-id.img.susercontent.com',  # Shopee Indonesia
    'm.media-amazon.com',            # Amazon (falsche Variante möglich)
    'cdn.idealo.com',                # Idealo Thumbnails
    'www.alternate.de',
    'www.onedirect.de',
    'media.cdn.kaufland.de',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config_shop2.yaml')
    parser.add_argument('--dry-run', action='store_true', help='Nur anzeigen, nicht deaktivieren')
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding='utf-8'))

    # Credentials: GitHub Actions setzt EBAY_CLIENT_ID + EBAY_CLIENT_SECRET direkt
    # (gemappt von Secrets EBAY_CLIENT_ID_2 / EBAY_CLIENT_SECRET_2 im Workflow)
    if not os.environ.get('EBAY_CLIENT_ID'):
        raise ValueError("EBAY_CLIENT_ID nicht gesetzt. Im Workflow als Secret EBAY_CLIENT_ID_2 → env EBAY_CLIENT_ID mappen.")
    if not os.environ.get('EBAY_CLIENT_SECRET'):
        raise ValueError("EBAY_CLIENT_SECRET nicht gesetzt.")
    token_var = cfg['ebay'].get('refresh_token_env_var', 'EBAY_REFRESH_TOKEN_2')
    if not os.environ.get(token_var):
        raise ValueError(f"{token_var} nicht gesetzt.")

    client = EbayClient.from_env(cfg['ebay'])

    # 1) Alle Inventory Items holen
    print("Lade eBay Inventory Items …", flush=True)
    all_items = []
    offset = 0
    while True:
        resp = client._request("GET", INVENTORY_PATH, params={"limit": 100, "offset": offset})
        if not resp:
            break
        batch = resp.get("inventoryItems", [])
        all_items.extend(batch)
        total = resp.get("total", 0)
        print(f"  {len(all_items)}/{total}", end='\r', flush=True)
        if len(all_items) >= total or not batch:
            break
        offset += 100
    print(f"\n{len(all_items)} Items geladen.", flush=True)

    # 2) Items mit unsicheren Bildern identifizieren
    unsafe_skus = []
    for item in all_items:
        imgs = item.get('product', {}).get('imageUrls', [])
        if not imgs:
            continue
        domains = {urlparse(img).netloc for img in imgs}
        if domains & UNSAFE_DOMAINS:
            unsafe_skus.append({
                'sku': item['sku'],
                'domain': (domains & UNSAFE_DOMAINS).pop(),
                'title': item.get('product', {}).get('title', '')[:60],
            })

    print(f"{len(unsafe_skus)} Items mit unsicheren Bildern gefunden.", flush=True)

    if args.dry_run:
        for x in unsafe_skus:
            print(f"  [DRY] {x['sku']} | {x['domain']} | {x['title']}")
        return

    # 3) Offers holen und nur PUBLISHED withdrawn
    withdrawn = []
    skipped = []
    errors = []

    for item in unsafe_skus:
        sku = item['sku']
        try:
            offer = client.get_offer_for_sku(sku)
            if not offer:
                skipped.append(sku + ' (kein Offer)')
                continue
            status = offer.get('status', '')
            offer_id = offer.get('offerId', '')
            if status != 'PUBLISHED':
                skipped.append(f"{sku} ({status})")
                continue
            client.withdraw_offer(offer_id)
            withdrawn.append({'sku': sku, 'offer_id': offer_id, 'domain': item['domain']})
            print(f"  ✅ {sku} deaktiviert ({item['domain']})", flush=True)
        except Exception as e:
            errors.append({'sku': sku, 'error': str(e)})
            print(f"  ❌ {sku}: {e}", flush=True)
        time.sleep(0.4)

    print(f"\n=== Ergebnis ===", flush=True)
    print(f"Deaktiviert: {len(withdrawn)}", flush=True)
    print(f"Übersprungen (bereits inaktiv): {len(skipped)}", flush=True)
    print(f"Fehler: {len(errors)}", flush=True)

    result = {
        'withdrawn': len(withdrawn),
        'skipped': len(skipped),
        'errors': len(errors),
        'withdrawn_details': withdrawn,
        'error_details': errors,
    }
    Path('withdraw_result.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print("Ergebnis → withdraw_result.json", flush=True)


if __name__ == '__main__':
    main()
