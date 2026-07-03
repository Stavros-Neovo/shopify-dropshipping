#!/usr/bin/env python3
"""Wendet kosatec_content.json auf die Shopify-Kosatec-Produkte an: sauberer Titel,
ausführliche Beschreibung, SEO, Full-Res-Bild (ersetzt das 115px-Bild).

Läuft in der Action fix_kosatec.yml mit secrets.SHOPIFY_ADMIN_TOKEN.
    python3 fix_kosatec_products.py                 # DRY-RUN (nichts schreiben)
    python3 fix_kosatec_products.py --apply          # live schreiben
    python3 fix_kosatec_products.py --apply --limit=5
"""
import os, sys, json, time, yaml, requests

APPLY = "--apply" in sys.argv
LIMIT = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--limit=")), "0"))

cfg = yaml.safe_load(open("config_shop2.yaml"))
# shop_domain ist in config bewusst leer (sync.py überspringt Shopify) → hardcoded Fallback
DOMAIN = os.environ.get("SHOPIFY_SHOP_DOMAIN") or cfg["shopify"].get("shop_domain") or "ijadcz-hp"
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
if not TOKEN:
    print("FEHLER: SHOPIFY_ADMIN_TOKEN fehlt (Secret nicht gesetzt?)"); sys.exit(1)
BASE = f"https://{DOMAIN}.myshopify.com/admin/api/2024-10"
H = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

content = json.load(open("kosatec_content.json", encoding="utf-8"))


def all_kos_products():
    """SKU (KOS-*) → product_id, per Pagination."""
    url = f"{BASE}/products.json?limit=250&fields=id,variants"
    m = {}
    while url:
        r = requests.get(url, headers=H, timeout=30)
        if r.status_code == 401:
            print("FEHLER 401: Token ungültig."); sys.exit(1)
        r.raise_for_status()
        for p in r.json().get("products", []):
            for v in p.get("variants", []):
                if str(v.get("sku", "")).startswith("KOS-"):
                    m[v["sku"]] = p["id"]
        nxt = None
        for part in r.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                nxt = part[part.find("<") + 1:part.find(">")]
        url = nxt
        time.sleep(0.5)
    return m


def update(pid, c):
    body = {"product": {
        "id": pid,
        "title": c["title"],
        "body_html": c["description_html"],
        "metafields_global_title_tag": c["seo_title"],
        "metafields_global_description_tag": c["seo_description"],
    }}
    img = c.get("image", "")
    if img and "/img/m/" not in img:      # nur Full-Res, nie das 115px-Medium
        body["product"]["images"] = [{"src": img}]
    if not APPLY:
        return "DRY"
    r = requests.put(f"{BASE}/products/{pid}.json", headers=H, json=body, timeout=40)
    time.sleep(0.55)                       # ~2 req/s Rate-Limit
    return r.status_code


def main():
    skumap = all_kos_products()
    skus = [s for s in content if s in skumap]
    if LIMIT:
        skus = skus[:LIMIT]
    print(f"KOS in Shopify: {len(skumap)} | Content: {len(content)} | verarbeite: {len(skus)} "
          f"({'LIVE-APPLY' if APPLY else 'DRY-RUN'})", flush=True)
    ok = err = 0
    for i, sku in enumerate(skus, 1):
        try:
            st = update(skumap[sku], content[sku])
            if st in ("DRY", 200):
                ok += 1
            else:
                err += 1; print(f"  {sku}: HTTP {st}")
        except Exception as e:
            err += 1; print(f"  {sku}: {type(e).__name__} {str(e)[:60]}")
        if i % 25 == 0:
            print(f"  {i}/{len(skus)} … ok {ok} err {err}", flush=True)
    print(f"Fertig: {ok} ok, {err} Fehler" + ("" if APPLY else "  (DRY-RUN, nichts geschrieben)"))


if __name__ == "__main__":
    main()
