"""Holt die echten eBay-Kategorien (Taxonomy-API) pro Produkt-Titel und
speichert SKU -> categoryId nach ebay_sku_category.json. build_matrixify_csv.py
nutzt das als Shopify-'Type' (gleiche Kategorisierung wie eBay).
Inkrementelles Speichern -> Abbruch unkritisch, einfach neu starten."""
import json
from pathlib import Path
import yaml
from dotenv import load_dotenv
from csv_loader import load_supplier_feed
from ebay_client import EbayClient

OUT = Path("ebay_sku_category.json")


def main():
    load_dotenv()
    cfg = yaml.safe_load(open("config.yaml"))
    shop2 = yaml.safe_load(open("config_shop2.yaml"))
    client = EbayClient.from_env(shop2.get("ebay", {}))

    result = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    products = list(load_supplier_feed(cfg))
    total = len(products)
    done = 0
    for p in products:
        sku = p.get("sku", "")
        if not sku or sku in result:
            done += 1
            continue
        title = p.get("title", "")
        if not title:
            continue
        try:
            cid = client.get_category_for_title(title, p.get("category", ""))
        except Exception as e:
            print(f"  Fehler {sku}: {repr(e)[:120]}", flush=True)
            continue
        if cid:
            result[sku] = cid
        done += 1
        if done % 50 == 0:
            OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"… {done}/{total} ({len(result)} kategorisiert)", flush=True)

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"FERTIG: {len(result)} SKUs kategorisiert -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
