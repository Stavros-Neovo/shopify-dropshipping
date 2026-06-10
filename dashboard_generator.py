"""
dashboard_generator.py
======================
Liest repricer_report.json + eBay Orders API und schreibt docs/dashboard_data.js.
Das JS-Format funktioniert sowohl lokal (file://) als auch auf GitHub Pages.

Aufruf:
  python dashboard_generator.py           # live eBay-Daten
  python dashboard_generator.py --mock    # Demo-Daten (kein eBay API nötig)
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yaml

REPORT_FILE = "repricer_report.json"
OUTPUT_FILE = "docs/dashboard_data.js"
CONFIG_FILE = "config_shop2.yaml"
ORDERS_PATH = "/sell/fulfillment/v1/order"


# ─── OAuth ────────────────────────────────────────────────────────────────────

def get_user_token(client_id, client_secret, refresh_token, sandbox=False):
    base = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
    r = requests.post(
        f"{base}/identity/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ─── eBay Orders ──────────────────────────────────────────────────────────────

def fetch_orders(base_url, token, days=30):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    orders = []
    offset = 0
    while True:
        r = requests.get(
            f"{base_url}{ORDERS_PATH}",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "filter": f"creationdate:[{since}..]",
                "limit": 50,
                "offset": offset,
            },
            timeout=30,
        )
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("orders", [])
        orders.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    return orders


def calc_profit(line_item: dict, ek_map: dict) -> float:
    sku        = line_item.get("sku", "")
    qty        = line_item.get("quantity", 1)
    unit_price = float(line_item.get("lineItemCost", {}).get("value", 0))
    ek = ek_map.get(sku, 0)
    if not ek or not unit_price:
        return 0.0
    vk_netto = unit_price * 0.87 / 1.19
    profit   = (vk_netto - 5.0 - ek) * qty
    return round(profit, 2)


def process_orders(orders: list, ek_map: dict) -> dict:
    today         = datetime.now(timezone.utc).date()
    total_rev     = total_profit = 0.0
    today_rev     = today_profit = 0.0
    today_count   = total_count = 0
    product_stats: dict = {}
    recent_sales: list  = []
    bab_deadlines: list = []

    for order in orders:
        created_str = order.get("creationDate", "")
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except Exception:
            continue

        order_date  = created.date()
        is_today    = order_date == today
        order_total = float(
            order.get("pricingSummary", {}).get("total", {}).get("value", 0)
        )
        order_profit = 0.0

        for item in order.get("lineItems", []):
            sku   = item.get("sku", "")
            title = item.get("title", sku)
            qty   = item.get("quantity", 1)
            p     = calc_profit(item, ek_map)
            order_profit += p
            if sku not in product_stats:
                product_stats[sku] = {
                    "sku": sku, "title": title,
                    "sold": 0, "revenue": 0.0, "profit": 0.0,
                }
            product_stats[sku]["sold"]    += qty
            product_stats[sku]["revenue"] += float(
                item.get("lineItemCost", {}).get("value", 0)
            ) * qty
            product_stats[sku]["profit"]  += p

        total_rev    += order_total
        total_profit += order_profit
        total_count  += 1
        if is_today:
            today_rev    += order_total
            today_profit += order_profit
            today_count  += 1

        deadline  = order_date + timedelta(days=14)
        days_left = (deadline - today).days
        if 0 <= days_left <= 14:
            bab_deadlines.append({
                "order_id":  order.get("orderId", ""),
                "date":      order_date.isoformat(),
                "deadline":  deadline.isoformat(),
                "days_left": days_left,
                "amount_ek": round(sum(
                    ek_map.get(i.get("sku",""), 0) * i.get("quantity", 1)
                    for i in order.get("lineItems", [])
                ), 2),
                "items": [i.get("title", i.get("sku","")) for i in order.get("lineItems", [])],
                "urgent": days_left <= 3,
            })

        if len(recent_sales) < 20:
            recent_sales.append({
                "date":    created.strftime("%d.%m %H:%M"),
                "title":   order.get("lineItems", [{}])[0].get("title", "")[:50],
                "revenue": round(order_total, 2),
                "profit":  round(order_profit, 2),
            })

    top_products = sorted(
        product_stats.values(), key=lambda x: x["profit"], reverse=True
    )[:10]
    for p in top_products:
        p["revenue"] = round(p["revenue"], 2)
        p["profit"]  = round(p["profit"], 2)

    bab_deadlines.sort(key=lambda x: x["days_left"])

    return {
        "stats": {
            "today_sales":       today_count,
            "today_revenue":     round(today_rev, 2),
            "today_profit":      round(today_profit, 2),
            "total_sales_30d":   total_count,
            "total_revenue_30d": round(total_rev, 2),
            "total_profit_30d":  round(total_profit, 2),
        },
        "top_products":  top_products,
        "recent_sales":  recent_sales,
        "bab_deadlines": bab_deadlines,
    }


# ─── Mock-Daten ───────────────────────────────────────────────────────────────

def mock_orders_data() -> dict:
    return {
        "stats": {
            "today_sales":       4,
            "today_revenue":     612.50,
            "today_profit":      142.30,
            "total_sales_30d":   87,
            "total_revenue_30d": 14320.00,
            "total_profit_30d":  3240.80,
        },
        "top_products": [
            {"sku": "ENAB0011", "title": "Enabot EBO Air2 Pink",
             "sold": 8,  "revenue": 1863.92, "profit": 432.00},
            {"sku": "SAN0042", "title": "SanDisk Extreme SSD 1TB",
             "sold": 14, "revenue": 1260.00, "profit": 280.00},
            {"sku": "LOG0099", "title": "Logitech MX Keys Mini",
             "sold": 11, "revenue":  990.00, "profit": 198.00},
            {"sku": "TP0023",  "title": "TP-Link 8-Port Gigabit Switch",
             "sold": 19, "revenue":  760.00, "profit": 152.00},
            {"sku": "KIN0031", "title": "Kingston A400 SSD 960GB",
             "sold": 12, "revenue":  720.00, "profit": 120.00},
        ],
        "recent_sales": [
            {"date": "10.06 14:32", "title": "Logitech M330 Silent Maus",       "revenue":  32.99, "profit":  8.20},
            {"date": "10.06 13:15", "title": "SanDisk Ultra USB-Stick 128GB",   "revenue":  18.99, "profit":  4.10},
            {"date": "10.06 11:44", "title": "TP-Link Netzwerk Switch 5-Port",  "revenue":  28.99, "profit":  6.80},
            {"date": "10.06 09:02", "title": "Kingston DDR4 3200MHz 16GB RAM",  "revenue":  54.99, "profit": 12.40},
            {"date": "09.06 21:18", "title": "Ubiquiti UAP-AC-PRO Access Point","revenue": 148.99, "profit": 38.50},
            {"date": "09.06 18:33", "title": "G.Skill Ripjaws V 32GB DDR4 RAM","revenue":  89.99, "profit": 18.20},
        ],
        "bab_deadlines": [
            {"order_id": "28-12345", "date": "2026-05-28", "deadline": "2026-06-11",
             "days_left": 1, "amount_ek": 131.95, "items": ["Enabot EBO Air2"],         "urgent": True},
            {"order_id": "28-12289", "date": "2026-05-30", "deadline": "2026-06-13",
             "days_left": 3, "amount_ek":  84.50, "items": ["SanDisk SSD 1TB", "USB-Stick 64GB"], "urgent": True},
            {"order_id": "28-12201", "date": "2026-06-01", "deadline": "2026-06-15",
             "days_left": 5, "amount_ek":  42.20, "items": ["TP-Link Switch"],           "urgent": False},
        ],
    }


# ─── Repricing ────────────────────────────────────────────────────────────────

def load_repricer_data() -> dict:
    try:
        with open(REPORT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    Path("docs").mkdir(exist_ok=True)

    repricer      = load_repricer_data()
    last_run      = repricer.get("last_run", {})
    history       = repricer.get("history", [])
    changes       = repricer.get("last_changes", [])
    active_listings = last_run.get("checked", 0)

    recent_changes = [
        {
            "sku":    c["sku"],
            "title":  c["title"][:45],
            "old":    c["old_price"],
            "new":    c["new_price"],
            "diff":   round(c["new_price"] - c["old_price"], 2),
            "action": c["action"],
        }
        for c in (changes or [])[:10]
    ]

    chart_history = [
        {
            "date":    h.get("timestamp", "")[:10],
            "lowered": h.get("lowered", 0),
            "raised":  h.get("raised", 0),
            "floor":   h.get("skipped_floor", 0),
        }
        for h in (history or [])[-14:]
    ]

    if args.mock:
        orders_data = mock_orders_data()
    else:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            sandbox   = cfg["ebay"].get("sandbox", False)
            base_url  = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
            client_id     = os.getenv("EBAY_CLIENT_ID", "")
            client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
            refresh_token = os.getenv(cfg["ebay"]["refresh_token_env_var"], "")
            token      = get_user_token(client_id, client_secret, refresh_token, sandbox)
            raw_orders = fetch_orders(base_url, token, days=30)
            orders_data = process_orders(raw_orders, ek_map={})
            print(f"  eBay Orders geladen: {len(raw_orders)}")
        except Exception as e:
            print(f"  eBay Orders nicht verfügbar ({e}) → Demo-Daten")
            orders_data = mock_orders_data()

    dashboard = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "active_listings": active_listings,
        "repricing": {
            "last_run":       last_run,
            "recent_changes": recent_changes,
            "chart_history":  chart_history,
        },
        "orders": orders_data,
    }

    # Als JS-Datei schreiben → funktioniert mit file:// und https://
    js_content = "window.DASHBOARD_DATA = " + json.dumps(
        dashboard, ensure_ascii=False, indent=2
    ) + ";\n"
    Path(OUTPUT_FILE).write_text(js_content, encoding="utf-8")

    print(f"✓ Dashboard: {OUTPUT_FILE}")
    print(f"  Aktive Listings:  {active_listings}")
    print(f"  Preisänderungen:  {len(recent_changes)}")
    print(f"  Heute Verkäufe:   {orders_data['stats']['today_sales']}")
    print(f"  BAB Fristen:      {len(orders_data['bab_deadlines'])}")


if __name__ == "__main__":
    main()
