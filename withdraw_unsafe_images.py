#!/usr/bin/env python3
"""
withdraw_unsafe_images.py
=========================
Deaktiviert eBay-Listings mit unsicheren Bildern (Retailer-Sites statt Hersteller-CDN).
Läuft als GitHub Action (kein Timeout-Problem).

Aufruf: python withdraw_unsafe_images.py --config config_shop2.yaml
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from ebay_client import EbayClient

# Domains, die KEINE zuverlässigen Produktbilder liefern
UNSAFE_DOMAINS = {
    'pisces.bbystatic.com',
    'www.ebuyer.com',
    'www.irvsluggage.com',
    'i5.walmartimages.com',
    'c1.neweggimages.com',
    'down-id.img.susercontent.com',
    'm.media-amazon.com',
    'cdn.idealo.com',
    'www.alternate.de',
    'www.onedirect.de',
    'media.cdn.kaufland.de',
    'media.cdn.bauhaus',
    'pisces.bbystatic.com',
    'www.shopprice.com.au',
}

def load_ddg_eans(path: str = "enrichment_index.csv") -> set[str]:
    """Liest EANs, deren Bilder aus DDG-Quellen stammen."""
    import csv, io
    DDG_SOURCES = {'ddg_images', 'ddg_fixed', 'ddg_fixed2'}
    eans = set()
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('source','') in DDG_SOURCES:
                e = row.get('ean','').strip()
                if e:
                    eans.add(e)
    return eans


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config_shop2.yaml')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config))
    client = EbayClient.from_env(cfg['ebay'])

    # Alle eBay Inventory Items holen
    print("Lade alle eBay Inventory Items …")
    INVENTORY_PATH = "/sell/inventory/v1/inventory_item"
    all_items = []
    offset = 0
    while True:
        resp = client._request("GET", INVENTORY_PATH, params={"limit": 100, "offset": offset})
        if not resp:
            break
        items = resp.get("inventoryItems", [])
        all_items.extend(items)
        total = resp.get("total", 0)
        print(f"  {len(all_items)}/{total}", end='\r')
        if len(all_items) >= total or not items:
            break
        offset += 100
    print(f"\n{len(all_items)} Items geladen.")

    # DDG-EANs laden
    enrichment_path = Path(args.config).parent / "enrichment_index.csv"
    ddg_eans = load_ddg_eans(str(enrichment_path))
    print(f"{len(ddg_eans)} DDG-EANs bekannt.")

    # Finde Items mit unsicheren Bildern
    to_withdraw = []
    for item in all_items:
        sku = item.get('sku', '')
        product = item.get('product', {})
        image_urls = product.get('imageUrls', [])
        ean_list = product.get('ean', [])
        ean = ean_list[0] if ean_list else ''

        if not image_urls:
            continue

        domains = {urlparse(img).netloc for img in image_urls}
        has_unsafe = bool(domains & UNSAFE_DOMAINS)

        if has_unsafe:
            to_withdraw.append({'sku': sku, 'ean': ean, 'title': product.get('title','')[:60], 'images': image_urls})

    print(f"\n{len(to_withdraw)} Listings mit unsicheren Bildern gefunden.")
    if args.dry_run:
        for x in to_withdraw:
            print(f"  [DRY] {x['sku']} | {x['images'][0][:70]}")
        return

    # Offers holen und deaktivieren
    withdrawn = []; errors = []
    for item in to_withdraw:
        sku = item['sku']
        try:
            offer = client.get_offer_for_sku(sku)
            if not offer:
                print(f"  {sku}: kein Offer")
                continue
            offer_id = offer.get('offerId', '')
            if not offer_id:
                continue
            client.withdraw_offer(offer_id)
            withdrawn.append({'sku': sku, 'offer_id': offer_id})
            print(f"  ✓ {sku} deaktiviert")
        except Exception as e:
            errors.append({'sku': sku, 'error': str(e)})
            print(f"  ✗ {sku}: {e}")
        time.sleep(0.5)

    result = {'withdrawn': len(withdrawn), 'errors': len(errors), 'details': withdrawn, 'error_details': errors}
    Path('withdraw_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n✅ Deaktiviert: {len(withdrawn)} | ❌ Fehler: {len(errors)}")


if __name__ == '__main__':
    main()
