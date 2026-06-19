"""
dashboard_generator.py — Best_Neodeals eBay Shop Dashboard
===========================================================
Schreibt docs/dashboard_data.js mit allen Metriken.

  python dashboard_generator.py           # live eBay-Daten
  python dashboard_generator.py --mock    # Demo-Daten
"""
from __future__ import annotations
import argparse, csv, json, os, random
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import requests, yaml

SUPPLIER_MAP   = "supplier_map.json"
REPORT_FILE    = "repricer_report.json"
OUTPUT_FILE    = "docs/dashboard_data.js"
CONFIG_FILE    = "config_shop2.yaml"
ORDERS_PATH    = "/sell/fulfillment/v1/order"
SHOPIFY_CSV    = "docs/shopify_products.csv"

EBAY_FEE      = 0.13   # 13 % eBay-Grundgebühr
CAMPAIGN_FEE  = 0.08   # 8 % Promoted Listings (cost-per-sale)
TOTAL_FEE     = EBAY_FEE + CAMPAIGN_FEE  # 21 %
VAT_FACTOR  = 1.19   # Brutto → Netto
SHIP_COST   = 5.0    # Pauschale Versandkosten (was wir zahlen)
BUYER_SHIP  = 3.99   # Versandanteil den Käufer zahlt
EST_RATE   = 0.30   # Einkommensteuer-Rücklage
GEWST_RATE = 0.15   # Gewerbesteuer-Rücklage
RETURN_RESERVE_RATE = 0.05  # Retouren-Rücklage auf EK - BAB nimmt nichts zurück (siehe pricing.py)


# ─── EK-Map aus shopify_products.csv ─────────────────────────────────────────

def load_ek_map(csv_path: str = SHOPIFY_CSV) -> dict:
    """Gibt {sku: ek_float} zurück. Nutzt Metafield custom.ek_price falls vorhanden,
    sonst Variant Cost."""
    ek_map: dict = {}
    p = Path(csv_path)
    if not p.exists():
        return ek_map
    with open(p, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = (row.get("Variant SKU") or "").strip()
            if not sku:
                continue
            # Bevorzuge das ek_price Metafield, Fallback auf Variant Cost
            raw = (row.get("Metafield: custom.ek_price [number_decimal]") or
                   row.get("Variant Cost") or "0").strip()
            try:
                ek_map[sku] = float(raw)
            except ValueError:
                pass
    return ek_map


# ─── OAuth ────────────────────────────────────────────────────────────────────

def get_user_token(client_id, client_secret, refresh_token, sandbox=False):
    base = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
    r = requests.post(
        f"{base}/identity/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ─── eBay Orders API ──────────────────────────────────────────────────────────

def fetch_orders(base_url, token, days=90):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    orders, offset = [], 0
    while True:
        r = requests.get(
            f"{base_url}{ORDERS_PATH}",
            headers={"Authorization": f"Bearer {token}"},
            params={"filter": f"creationdate:[{since}..]", "limit": 50, "offset": offset},
            timeout=30,
        )
        if r.status_code != 200:
            break
        batch = r.json().get("orders", [])
        orders.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    return orders


# ─── Gewinn-Berechnung ────────────────────────────────────────────────────────

def calc_profit_item(unit_price: float, ek: float, qty: int = 1) -> dict:
    """Gibt vollständige Gewinnrechnung zurück. profit_u/profit_t sind NETTO nach
    Retouren-Rücklage (BAB nimmt nichts zurück, jede Kundenretoure = voller EK-Verlust)."""
    total_vk    = unit_price + BUYER_SHIP              # Gesamtbetrag inkl. Käufer-Versand
    ebay_fee    = round(total_vk * EBAY_FEE, 2)        # eBay Grundgebühr 13%
    campaign    = round(total_vk * CAMPAIGN_FEE, 2)    # Promoted Listings 8%
    total_fees  = round(total_vk * TOTAL_FEE, 2)       # 21% gesamt
    vat         = round((total_vk - total_fees) * (1 - 1 / VAT_FACTOR), 2)
    netto_vk    = round(total_vk - total_fees - vat, 2)
    return_reserve = round(ek * RETURN_RESERVE_RATE, 2)
    profit_u    = round(netto_vk - SHIP_COST - ek - return_reserve, 2)
    ebay_fee    = total_fees  # für Dashboard-Ausgabe: Gesamtgebühr anzeigen
    profit_t  = round(profit_u * qty, 2)
    return {
        "vk_brutto":  round(total_vk, 2),
        "ebay_fee":   ebay_fee,
        "vat":        vat,
        "netto_vk":   netto_vk,
        "ship":       SHIP_COST,
        "ek":         round(ek, 2),
        "return_reserve": round(return_reserve * qty, 2),
        "profit_u":   profit_u,
        "qty":        qty,
        "profit_t":   profit_t,
    }


# ─── Auswertung ───────────────────────────────────────────────────────────────

def iso_week(d: date) -> tuple:
    return d.isocalendar()[:2]   # (year, week)


def process_orders(orders: list, ek_map: dict) -> dict:
    now   = datetime.now(timezone.utc)
    today = now.date()
    this_week  = iso_week(today)
    prev_week  = iso_week(today - timedelta(weeks=1))
    this_month = (today.year, today.month)

    # Aggregatoren
    agg = {
        "today":     {"sales": 0, "revenue": 0.0, "profit": 0.0, "ek": 0.0, "fee": 0.0, "return_reserve": 0.0},
        "week":      {"sales": 0, "revenue": 0.0, "profit": 0.0, "ek": 0.0, "fee": 0.0, "return_reserve": 0.0},
        "prev_week": {"sales": 0, "revenue": 0.0, "profit": 0.0, "ek": 0.0, "fee": 0.0, "return_reserve": 0.0},
        "month":     {"sales": 0, "revenue": 0.0, "profit": 0.0, "ek": 0.0, "fee": 0.0, "return_reserve": 0.0},
        "d30":       {"sales": 0, "revenue": 0.0, "profit": 0.0, "ek": 0.0, "fee": 0.0, "return_reserve": 0.0},
        "d90":       {"sales": 0, "revenue": 0.0, "profit": 0.0, "ek": 0.0, "fee": 0.0, "return_reserve": 0.0},
    }
    daily: dict[str, dict] = {}   # "YYYY-MM-DD" → {revenue, profit, sales}
    product_stats: dict    = {}
    recent_sales:  list    = []
    bab_deadlines: list    = []
    tracking_missing: list = []

    cutoff_30 = today - timedelta(days=30)
    cutoff_90 = today - timedelta(days=90)

    for order in orders:
        created_str = order.get("creationDate", "")
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except Exception:
            continue

        od = created.date()
        day_key = od.isoformat()

        order_total = float(
            order.get("pricingSummary", {}).get("total", {}).get("value", 0)
        )
        order_profit = 0.0
        order_ek     = 0.0
        order_fee    = 0.0
        order_reserve = 0.0

        for item in order.get("lineItems", []):
            sku   = item.get("sku", "")
            title = item.get("title", sku)
            qty   = item.get("quantity", 1)
            vk    = float(item.get("lineItemCost", {}).get("value", 0))
            ek    = ek_map.get(sku, 0)
            calc  = calc_profit_item(vk, ek, qty)
            order_profit  += calc["profit_t"]
            order_ek      += calc["ek"] * qty
            order_fee     += calc["ebay_fee"] * qty
            order_reserve += calc["return_reserve"]

            # Product stats
            if sku not in product_stats:
                product_stats[sku] = {
                    "sku": sku, "title": title,
                    "sold": 0, "revenue": 0.0, "profit": 0.0,
                    "vk": vk, "ek": ek,
                    "calc": calc_profit_item(vk, ek, 1),   # per unit breakdown
                }
            product_stats[sku]["sold"]    += qty
            product_stats[sku]["revenue"] += vk * qty
            product_stats[sku]["profit"]  += calc["profit_t"]

        # Tracking fehlt?
        fulfillment = order.get("fulfillmentStartInstructions", [{}])[0]
        ship_to     = fulfillment.get("shippingStep", {})
        if order.get("orderFulfillmentStatus") in ("NOT_STARTED", "IN_PROGRESS"):
            shipments = order.get("paymentSummary", {})
            has_track = any(
                s.get("trackingNumber")
                for s in order.get("lineItems", [{}])
            )
            if not has_track:
                tracking_missing.append({
                    "order_id": order.get("orderId", ""),
                    "date":     od.isoformat(),
                    "items":    [i.get("title", "")[:40] for i in order.get("lineItems", [])],
                    "buyer":    order.get("buyer", {}).get("username", ""),
                })

        # Daily chart
        if day_key not in daily:
            daily[day_key] = {"revenue": 0.0, "profit": 0.0, "sales": 0}
        daily[day_key]["revenue"] += order_total
        daily[day_key]["profit"]  += order_profit
        daily[day_key]["sales"]   += 1

        # Aggregatoren
        def add(key):
            agg[key]["sales"]   += 1
            agg[key]["revenue"] += order_total
            agg[key]["profit"]  += order_profit
            agg[key]["ek"]      += order_ek
            agg[key]["fee"]     += order_fee
            agg[key]["return_reserve"] += order_reserve

        if od == today:           add("today")
        if iso_week(od) == this_week:  add("week")
        if iso_week(od) == prev_week:  add("prev_week")
        if (od.year, od.month) == this_month: add("month")
        if od >= cutoff_30:       add("d30")
        if od >= cutoff_90:       add("d90")

        # BAB-Fristen
        deadline  = od + timedelta(days=14)
        days_left = (deadline - today).days
        if 0 <= days_left <= 14:
            bab_deadlines.append({
                "order_id":  order.get("orderId", ""),
                "date":      od.isoformat(),
                "deadline":  deadline.isoformat(),
                "days_left": days_left,
                "items":     [i.get("title", i.get("sku",""))[:40] for i in order.get("lineItems", [])],
                "urgent":    days_left <= 3,
            })

        # Letzte Verkäufe (max 20)
        if len(recent_sales) < 20:
            recent_sales.append({
                "date":    created.strftime("%d.%m %H:%M"),
                "title":   order.get("lineItems", [{}])[0].get("title", "")[:50],
                "revenue": round(order_total, 2),
                "profit":  round(order_profit, 2),
            })

    # Top-Produkte
    top_products = sorted(
        product_stats.values(), key=lambda x: x["profit"], reverse=True
    )[:10]
    for p in top_products:
        p["revenue"] = round(p["revenue"], 2)
        p["profit"]  = round(p["profit"], 2)

    bab_deadlines.sort(key=lambda x: x["days_left"])

    # Chart-Daten aufbereiten
    def build_chart(days):
        result = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            k = d.isoformat()
            v = daily.get(k, {})
            result.append({
                "date":    d.strftime("%d.%m"),
                "revenue": round(v.get("revenue", 0), 2),
                "profit":  round(v.get("profit", 0), 2),
                "sales":   v.get("sales", 0),
            })
        return result

    def rnd(v): return round(v, 2)

    # Steuer-Rücklagen (auf Monatsbasis)
    mp = agg["month"]["profit"]
    mr = agg["month"]["revenue"]
    tax = {
        "ust_month":    rnd(mr * (VAT_FACTOR - 1) / VAT_FACTOR),
        "est_month":    rnd(max(mp, 0) * EST_RATE),
        "gewst_month":  rnd(max(mp, 0) * GEWST_RATE),
        "total_month":  rnd(mr * (VAT_FACTOR - 1) / VAT_FACTOR + max(mp, 0) * (EST_RATE + GEWST_RATE)),
    }

    # Erwartete Auszahlung (eBay Managed Payments: ~wöchentlich)
    payout = rnd(agg["d30"]["revenue"] * (1 - TOTAL_FEE))

    return {
        "stats": {
            "today_sales":       agg["today"]["sales"],
            "today_revenue":     rnd(agg["today"]["revenue"]),
            "today_profit":      rnd(agg["today"]["profit"]),
            "week_sales":        agg["week"]["sales"],
            "week_revenue":      rnd(agg["week"]["revenue"]),
            "week_profit":       rnd(agg["week"]["profit"]),
            "prev_week_sales":   agg["prev_week"]["sales"],
            "prev_week_revenue": rnd(agg["prev_week"]["revenue"]),
            "prev_week_profit":  rnd(agg["prev_week"]["profit"]),
            "month_sales":       agg["month"]["sales"],
            "month_revenue":     rnd(agg["month"]["revenue"]),
            "month_profit":      rnd(agg["month"]["profit"]),
            "month_ek":          rnd(agg["month"]["ek"]),
            "month_ebay_fee":    rnd(agg["month"]["fee"]),
            "month_return_reserve": rnd(agg["month"]["return_reserve"]),
            "total_sales_30d":   agg["d30"]["sales"],
            "total_revenue_30d": rnd(agg["d30"]["revenue"]),
            "total_profit_30d":  rnd(agg["d30"]["profit"]),
        },
        "chart_7d":        build_chart(7),
        "chart_30d":       build_chart(30),
        "chart_90d":       build_chart(90),
        "top_products":    top_products,
        "recent_sales":    recent_sales,
        "bab_deadlines":   bab_deadlines,
        "tracking_missing": tracking_missing,
        "tax_reserves":    tax,
        "expected_payout": payout,
    }


# ─── Mock-Daten ───────────────────────────────────────────────────────────────

def mock_data() -> dict:
    rng = random.Random(42)
    today = date.today()

    def chart(days, base_rev=480, base_prof=110):
        out = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            rev = round(base_rev + rng.gauss(0, 80), 2)
            prf = round(base_prof + rng.gauss(0, 25), 2)
            out.append({"date": d.strftime("%d.%m"), "revenue": max(rev, 0),
                        "profit": max(prf, 0), "sales": rng.randint(1, 6)})
        return out

    top = [
        {"sku":"ENAB0011","title":"Enabot EBO Air2 Pink","sold":8,"revenue":1863.92,"profit":432.00,
         "vk":232.99,"ek":131.95,"calc":{"vk_brutto":232.99,"ebay_fee":30.29,"vat":34.05,"netto_vk":168.65,"ship":5.0,"ek":131.95,"profit_u":31.70,"qty":1,"profit_t":31.70}},
        {"sku":"SAN0042","title":"SanDisk Extreme SSD 1TB","sold":14,"revenue":1260.00,"profit":280.00,
         "vk":90.00,"ek":48.00,"calc":{"vk_brutto":90.00,"ebay_fee":11.70,"vat":13.16,"netto_vk":65.14,"ship":5.0,"ek":48.00,"profit_u":12.14,"qty":1,"profit_t":12.14}},
        {"sku":"LOG0099","title":"Logitech MX Keys Mini DE","sold":11,"revenue":990.00,"profit":198.00,
         "vk":90.00,"ek":52.00,"calc":{"vk_brutto":90.00,"ebay_fee":11.70,"vat":13.16,"netto_vk":65.14,"ship":5.0,"ek":52.00,"profit_u":8.14,"qty":1,"profit_t":8.14}},
        {"sku":"TP0023","title":"TP-Link 8-Port Gigabit Switch","sold":19,"revenue":760.00,"profit":152.00,
         "vk":40.00,"ek":22.00,"calc":{"vk_brutto":40.00,"ebay_fee":5.20,"vat":5.85,"netto_vk":28.95,"ship":5.0,"ek":22.00,"profit_u":1.95,"qty":1,"profit_t":1.95}},
        {"sku":"KIN0031","title":"Kingston A400 SSD 960GB","sold":12,"revenue":720.00,"profit":120.00,
         "vk":60.00,"ek":38.00,"calc":{"vk_brutto":60.00,"ebay_fee":7.80,"vat":8.77,"netto_vk":43.43,"ship":5.0,"ek":38.00,"profit_u":0.43,"qty":1,"profit_t":0.43}},
    ]

    return {
        "stats": {
            "today_sales":4,"today_revenue":612.50,"today_profit":142.30,
            "week_sales":23,"week_revenue":3420.00,"week_profit":784.00,
            "prev_week_sales":19,"prev_week_revenue":2890.00,"prev_week_profit":640.00,
            "month_sales":87,"month_revenue":14320.00,"month_profit":3240.80,
            "total_sales_30d":87,"total_revenue_30d":14320.00,"total_profit_30d":3240.80,
        },
        "chart_7d":  chart(7),
        "chart_30d": chart(30),
        "chart_90d": chart(90),
        "top_products": top,
        "recent_sales": [
            {"date":"10.06 14:32","title":"Logitech M330 Silent Maus","revenue":32.99,"profit":8.20},
            {"date":"10.06 13:15","title":"SanDisk Ultra USB-Stick 128GB","revenue":18.99,"profit":4.10},
            {"date":"10.06 11:44","title":"TP-Link Netzwerk Switch 5-Port","revenue":28.99,"profit":6.80},
            {"date":"10.06 09:02","title":"Kingston DDR4 3200MHz 16GB","revenue":54.99,"profit":12.40},
            {"date":"09.06 21:18","title":"Ubiquiti UAP-AC-PRO","revenue":148.99,"profit":38.50},
        ],
        "bab_deadlines": [
            {"order_id":"28-12345","date":"2026-05-28","deadline":"2026-06-11",
             "days_left":1,"items":["Enabot EBO Air2"],"urgent":True},
            {"order_id":"28-12289","date":"2026-05-30","deadline":"2026-06-13",
             "days_left":3,"items":["SanDisk SSD 1TB","USB-Stick 64GB"],"urgent":True},
            {"order_id":"28-12201","date":"2026-06-01","deadline":"2026-06-15",
             "days_left":5,"items":["TP-Link Switch"],"urgent":False},
        ],
        "tracking_missing": [
            {"order_id":"28-12388","date":"2026-06-09","items":["Logitech MX Keys Mini"],"buyer":"buyer_xy1"},
            {"order_id":"28-12401","date":"2026-06-10","items":["SanDisk Extreme SSD"],"buyer":"buyer_ab2"},
        ],
        "tax_reserves": {
            "ust_month":2164.15,"est_month":972.24,"gewst_month":486.12,"total_month":3622.51
        },
        "expected_payout": 12458.40,
    }


# ─── Repricing ────────────────────────────────────────────────────────────────

def load_repricer_data() -> dict:
    try:
        with open(REPORT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_catalog_stats() -> dict:
    """Katalog-Kennzahlen aus supplier_map.json fuer das Dashboard."""
    try:
        with open(SUPPLIER_MAP, encoding="utf-8") as f:
            smap = json.load(f)
    except Exception:
        return {"total_skus": 0, "images_verified": 0, "images_missing": 0}
    verified = sum(1 for v in smap.values() if v.get("image_verified"))
    return {
        "total_skus":      len(smap),
        "images_verified": verified,
        "images_missing":  len(smap) - verified,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    Path("docs").mkdir(exist_ok=True)

    # Repricing-Daten
    repricer       = load_repricer_data()
    last_run       = repricer.get("last_run", {})
    history        = repricer.get("history", [])
    changes        = repricer.get("last_changes", [])
    active_listings = last_run.get("checked", 0)

    recent_changes = [
        {"sku": c["sku"], "title": c["title"][:45],
         "old": c["old_price"], "new": c["new_price"],
         "diff": round(c["new_price"] - c["old_price"], 2), "action": c["action"]}
        for c in (changes or [])[:10]
    ]
    chart_history = [
        {"date": h.get("timestamp","")[:10],
         "lowered": h.get("lowered",0),
         "raised":  h.get("raised",0),
         "floor":   h.get("skipped_floor",0)}
        for h in (history or [])[-14:]
    ]

    # Order-Daten
    if args.mock:
        orders_data = mock_data()
        if not active_listings:
            active_listings = 1193
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
            raw_orders = fetch_orders(base_url, token, days=90)
            ek_map     = load_ek_map()
            print(f"  EK-Map geladen: {len(ek_map)} SKUs")
            orders_data = process_orders(raw_orders, ek_map=ek_map)
            print(f"  eBay Orders geladen: {len(raw_orders)}")
        except Exception as e:
            print(f"  eBay API nicht verfügbar ({e}) → Demo-Daten")
            orders_data = mock_data()

    dashboard = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "shop_name":       "Best_Neodeals eBay Shop",
        "active_listings": active_listings,
        "catalog":         load_catalog_stats(),
        "repricing": {
            "last_run":       last_run,
            "recent_changes": recent_changes,
            "chart_history":  chart_history,
        },
        "orders": orders_data,
    }

    # Pending & Flagged Orders einlesen
    pending_orders = {}
    flagged_orders = {}
    try:
        p = Path("pending_orders.json")
        if p.exists():
            pending_orders = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        f = Path("flagged_orders.json")
        if f.exists():
            flagged_orders = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    dashboard["pending_orders"] = pending_orders
    dashboard["flagged_orders"] = flagged_orders

    js = "window.DASHBOARD_DATA = " + json.dumps(dashboard, ensure_ascii=False, indent=2) + ";\n"
    Path(OUTPUT_FILE).write_text(js, encoding="utf-8")

    s = orders_data["stats"]
    print(f"✓  {OUTPUT_FILE}")
    print(f"   Heute:    {s['today_sales']} Bestellungen · {s['today_revenue']} € Umsatz · {s['today_profit']} € Gewinn")
    print(f"   Woche:    {s['week_sales']} · {s['week_revenue']} € · {s['week_profit']} €")
    print(f"   Monat:    {s['month_sales']} · {s['month_revenue']} € · {s['month_profit']} €")
    print(f"   BAB:      {len(orders_data['bab_deadlines'])} offene Fristen")
    print(f"   Tracking: {len(orders_data['tracking_missing'])} fehlend")


if __name__ == "__main__":
    main()
