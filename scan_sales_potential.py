"""
scan_sales_potential.py
========================
Reine Lese-Analyse: für jede aktuell PUBLISHED eBay-SKU wird unser Preis
gegen echte Mitbewerber-Preise (Browse API) verglichen und in
"günstigster Anbieter / knapp dran / Verlustbringer / keine Konkurrenz"
einsortiert. Macht KEINE Preisänderungen (kein PUT) - nur Report.

Nutzt dieselben, bereits in repricer.py getesteten Funktionen
(get_offer_data, get_competitor_prices, get_margin_tier, calc_vk), damit
hier keine zweite, abweichende Preislogik entsteht.
"""
from __future__ import annotations
import json
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient
from repricer import (
    get_offer_data, get_competitor_prices, get_margin_tier, calc_vk,
    psychological_round, load_bab_feed, DELAY,
)

load_dotenv()
cfg = yaml.safe_load(Path("config_shop2.yaml").read_text(encoding="utf-8"))
client = EbayClient.from_env(cfg["ebay"])
app_token = client._get_app_token()
base_url = client.base

bab_feed = load_bab_feed(cfg)  # {ean: {sku, ek, title, ...}}
banned_skus = json.loads(Path("banned_skus.json").read_text(encoding="utf-8")) if Path("banned_skus.json").exists() else {}

buckets = {"cheapest": [], "close": [], "loser": [], "no_competition": []}
checked = 0

for ean, feed_data in bab_feed.items():
    sku = feed_data["sku"]
    ek = feed_data["ek"]
    if sku in banned_skus or ek <= 0:
        continue

    offer = get_offer_data(client, sku)
    if not offer or offer.get("status") != "PUBLISHED" or offer["currentPrice"] <= 0:
        continue
    checked += 1
    current_price = offer["currentPrice"]
    category_id = offer.get("categoryId", "")

    floor_m, target_m = get_margin_tier(ek, cfg)
    floor_price = psychological_round(calc_vk(ek, floor_m, cfg, category_id))

    comp_prices = get_competitor_prices(ean, app_token, base_url, current_price, floor_price)
    time.sleep(DELAY)

    row = {"sku": sku, "ek": round(ek, 2), "price": current_price,
           "floor": floor_price, "title": feed_data.get("title", "")[:50]}

    if len(comp_prices) < 2:
        buckets["no_competition"].append(row)
        continue

    lowest = comp_prices[0]
    row["competitor"] = round(lowest, 2)
    row["gap"] = round(lowest - current_price, 2)

    if current_price < lowest:
        buckets["cheapest"].append(row)
    elif lowest >= floor_price * 0.90:
        buckets["close"].append(row)
    else:
        buckets["loser"].append(row)

    if checked % 50 == 0:
        print(f"... {checked} geprüft")

print()
print(f"{checked} aktive, gebannte-freie SKUs mit >=2 Mitbewerbern/Daten geprüft\n")
for name, label in [("cheapest", "Günstigster Anbieter"), ("close", "Knapp dran (<10% unter Floor)"),
                     ("loser", "Verlustbringer (Mitbewerber deutlich unter Floor)"),
                     ("no_competition", "Zu wenig/keine Konkurrenz gefunden")]:
    print(f"{label}: {len(buckets[name])}")

Path("sales_potential_report.json").write_text(
    json.dumps(buckets, indent=2, ensure_ascii=False), encoding="utf-8"
)
print("\nDetails in sales_potential_report.json")
