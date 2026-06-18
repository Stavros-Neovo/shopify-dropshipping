#!/usr/bin/env python3
"""
apply_image_updates.py
======================
Liest image_updates.json (vom Dashboard geschrieben), aktualisiert:
  1. supplier_map.json  → image_url + image_verified=True
  2. enrichment_index.csv → image_main
  3. eBay Inventory Item → imageUrls (PUT)
  4. eBay Offer → publish (falls UNPUBLISHED)

Aufruf: python apply_image_updates.py --config config_shop2.yaml
"""
from __future__ import annotations
import json, os, csv, io, sys, time, argparse
from pathlib import Path
import requests, yaml

sys.path.insert(0, str(Path(__file__).parent))
from ebay_client import EbayClient

INVENTORY_PATH = "/sell/inventory/v1/inventory_item"
OFFER_PATH     = "/sell/inventory/v1/offer"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config_shop2.yaml')
    args = parser.parse_args()

    updates_path = Path('image_updates.json')
    if not updates_path.exists():
        print("image_updates.json nicht gefunden — nichts zu tun.")
        return

    data = json.loads(updates_path.read_text(encoding='utf-8'))
    items = data.get('items', [])
    if not items:
        print("Keine Updates in image_updates.json.")
        return

    print(f"{len(items)} Image-Updates zu verarbeiten …")

    cfg = yaml.safe_load(open(args.config, encoding='utf-8'))
    client = EbayClient.from_env(cfg['ebay'])

    # supplier_map laden
    sm_path = Path('supplier_map.json')
    with open(sm_path, encoding='utf-8') as f:
        sm = json.load(f)

    # enrichment_index laden
    ei_path = Path('enrichment_index.csv')
    ei_rows = []
    ei_fields = []
    ean_to_row_idx = {}
    with open(ei_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        ei_fields = reader.fieldnames
        for i, row in enumerate(reader):
            ei_rows.append(row)
            ean_to_row_idx[row.get('ean','').strip()] = i

    results = []

    for upd in items:
        sku       = upd.get('sku', '')
        image_url = upd.get('image_url', '').strip()
        if not sku or not image_url:
            continue

        print(f"\n  → {sku} : {image_url[:70]}")

        # 1) supplier_map
        v = sm.get(sku, {})
        v['image_url']      = image_url
        v['images']         = [image_url]
        v['image_verified'] = True
        v['image_source']   = 'dashboard_manual'
        sm[sku] = v
        ean = v.get('ean', '')

        # 2) enrichment_index
        if ean and ean in ean_to_row_idx:
            idx = ean_to_row_idx[ean]
            ei_rows[idx]['image_main'] = image_url
            ei_rows[idx]['source']     = 'dashboard_manual'

        # 3) eBay Inventory Item aktualisieren
        try:
            resp = client._request("GET", f"{INVENTORY_PATH}/{sku}")
            if resp:
                product = resp.get('product', {})
                product['imageUrls'] = [image_url]
                payload = {
                    'availability': resp.get('availability', {'shipToLocationAvailability': {'quantity': 1}}),
                    'condition':    resp.get('condition', 'NEW'),
                    'product':      product,
                }
                if 'packageWeightAndSize' in resp:
                    payload['packageWeightAndSize'] = resp['packageWeightAndSize']
                client._request("PUT", f"{INVENTORY_PATH}/{sku}", json_body=payload)
                print(f"    ✅ eBay Inventory Item aktualisiert")
        except Exception as e:
            print(f"    ❌ Inventory Update fehlgeschlagen: {e}")

        time.sleep(0.5)

        # 4) Offer reaktivieren falls UNPUBLISHED
        try:
            offer = client.get_offer_for_sku(sku)
            if offer:
                status   = offer.get('status', '')
                offer_id = offer.get('offerId', '')
                if status == 'UNPUBLISHED' and offer_id:
                    client.publish_offer(offer_id)
                    print(f"    ✅ Offer {offer_id} reaktiviert (PUBLISHED)")
                    results.append({'sku': sku, 'status': 'republished', 'offer_id': offer_id})
                elif status == 'PUBLISHED':
                    print(f"    ℹ️  Offer bereits PUBLISHED")
                    results.append({'sku': sku, 'status': 'already_published'})
            else:
                print(f"    ⚠️  Kein Offer für {sku}")
        except Exception as e:
            print(f"    ❌ Publish fehlgeschlagen: {e}")

        time.sleep(0.5)

    # supplier_map speichern
    with open(sm_path, 'w', encoding='utf-8') as f:
        json.dump(sm, f, ensure_ascii=False, indent=2)
    print("\n✅ supplier_map.json gespeichert")

    # enrichment_index speichern
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=ei_fields)
    w.writeheader(); w.writerows(ei_rows)
    ei_path.write_text(out.getvalue(), encoding='utf-8')
    print("✅ enrichment_index.csv gespeichert")

    # Ergebnis
    result_data = {'processed': len(items), 'results': results, 'updated_at': __import__('datetime').datetime.utcnow().isoformat()}
    Path('apply_image_result.json').write_text(json.dumps(result_data, ensure_ascii=False, indent=2))
    print(f"✅ Fertig: {len(items)} verarbeitet")


if __name__ == '__main__':
    main()
