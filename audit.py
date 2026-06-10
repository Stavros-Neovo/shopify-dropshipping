"""
audit.py — Shop-Gesundheits-Report
====================================
Prüft jeden Artikel im BAB-Feed gegen eBay und erstellt einen vollständigen
Status-Report.

Kategorien:
  LIVE         — Offer aktiv auf eBay, Preis ok
  INACTIVE     — Offer vorhanden aber nicht aktiv (z.B. ended, out_of_stock)
  NOT_LISTED   — Im Feed, aber kein Offer auf eBay gefunden
  PRICE_RISK   — Aktueller Preis < Floor (Verlustrisiko)
  NO_IMAGE     — Inventory Item hat kein Bild
  NO_STOCK     — Bestand im Feed = 0

Output: audit_report.csv + Zusammenfassung in der Konsole

Aufruf:
  python audit.py                   # alle Produkte
  python audit.py --limit 100       # erste 100 testen
  python audit.py --config config.yaml   # Shop 1
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient

REPORT_FILE = "audit_report.csv"
CONFIG_FILE = "config_shop2.yaml"
DELAY       = 0.3   # Sekunden zwischen API-Calls

# ─── Preisformel (identisch mit repricer.py) ──────────────────────────────────

def calc_vk(ek: float, margin: float, cfg: dict) -> float:
    ep         = cfg["ebay_pricing"]
    carrier    = ep.get("shipping_cost_eur", 5.00)
    buyer_ship = ep.get("buyer_shipping_eur", 0.00)
    fee        = ep.get("ebay_fee_rate", 0.13)
    vat        = ep.get("vat_rate", 0.19)
    return (ek * (1 + margin) + carrier) * (1 + vat) / (1 - fee) - buyer_ship


def psychological_round(price: float) -> float:
    if price <= 0:
        return price
    floored = math.floor(price)
    cent = 0.99 if price < 500 else 0.95
    candidate = floored + cent
    return candidate if candidate >= price else floored + 1 + cent


# ─── BAB-Feed ─────────────────────────────────────────────────────────────────

def load_bab_feed(cfg: dict) -> list[dict]:
    import base64, io, urllib.request
    c     = cfg["csv"]
    url   = c["url"]
    user  = os.environ.get("CSV_HTTP_USER", "")
    pw    = os.environ.get("CSV_HTTP_PASSWORD", "")
    enc   = c.get("encoding", "utf-8-sig")
    delim = c.get("delimiter", ";")
    cols  = c.get("columns", {})

    req = urllib.request.Request(url)
    if user and pw:
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(f"{user}:{pw}".encode()).decode())
    print("Lade BAB-Feed …")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode(enc, errors="replace")

    products = []
    for row in csv.DictReader(io.StringIO(raw), delimiter=delim):
        ean   = (row.get(cols.get("ean",   "GTIN"),         "") or "").strip()
        sku   = (row.get(cols.get("sku",   "ItemNo"),       "") or "").strip()
        ek_s  = (row.get(cols.get("purchase_price","Price_B2B"),"0") or "0").strip().replace(",",".")
        stock_s = (row.get(cols.get("stock","Stock"),       "0") or "0").strip()
        title = (row.get(cols.get("title", "Description"),  "") or "").strip()
        brand = (row.get(cols.get("brand", "ManufacturerName"),"") or "").strip()
        img   = (row.get(cols.get("image_url",""),          "") or "").strip()

        if not sku:
            continue
        try:
            ek    = float(ek_s) if ek_s else 0.0
            stock = int(float(stock_s)) if stock_s else 0
        except ValueError:
            ek, stock = 0.0, 0

        products.append({
            "sku": sku, "ean": ean, "ek": ek, "stock": stock,
            "title": title, "brand": brand, "has_feed_image": bool(img),
        })

    print(f"  {len(products)} Artikel im Feed")
    return products


# ─── eBay API Checks ──────────────────────────────────────────────────────────

def get_offer(client: EbayClient, sku: str) -> dict | None:
    try:
        return client.get_offer_for_sku(sku)
    except Exception:
        return None


def get_inventory_item(client: EbayClient, sku: str) -> dict | None:
    try:
        r = client._request("GET", f"/sell/inventory/v1/inventory_item/{sku}")
        return r
    except Exception:
        return None


# ─── Audit-Logik ──────────────────────────────────────────────────────────────

def audit_product(product: dict, client: EbayClient, cfg: dict) -> dict:
    sku   = product["sku"]
    ek    = product["ek"]
    stock = product["stock"]

    result = {
        "sku":           sku,
        "ean":           product["ean"],
        "title":         product["title"][:70],
        "brand":         product["brand"],
        "ek":            round(ek, 2),
        "stock_feed":    stock,
        "status":        "",
        "issues":        [],
        "ebay_price":    "",
        "floor_price":   "",
        "normal_price":  "",
        "offer_status":  "",
        "has_image":     "",
        "offer_id":      "",
    }

    # ── Kein Bestand im Feed ─────────────────────────────────────────────
    if stock == 0:
        result["issues"].append("NO_STOCK")

    # ── Kein EK ──────────────────────────────────────────────────────────
    if ek <= 0:
        result["status"] = "NOT_LISTED"
        result["issues"].append("NO_EK")
        return result

    # Preise berechnen
    floor  = psychological_round(calc_vk(ek, 0.20, cfg))
    normal = psychological_round(calc_vk(ek, 0.25, cfg))
    result["floor_price"]  = round(floor, 2)
    result["normal_price"] = round(normal, 2)

    # ── Offer prüfen ──────────────────────────────────────────────────────
    offer = get_offer(client, sku)

    if offer is None:
        result["status"] = "NOT_LISTED"
        result["issues"].append("NO_OFFER")

        # Inventory Item trotzdem prüfen (existiert aber kein Offer?)
        inv = get_inventory_item(client, sku)
        if inv:
            result["issues"].append("INV_EXISTS_NO_OFFER")
            img_urls = inv.get("product", {}).get("imageUrls", [])
            result["has_image"] = bool(img_urls)
        return result

    # Offer vorhanden
    offer_status = offer.get("status", "")
    result["offer_status"] = offer_status
    result["offer_id"]     = offer.get("offerId", "")

    try:
        price = float(offer.get("pricingSummary", {}).get("price", {}).get("value", 0))
    except (TypeError, ValueError):
        price = 0.0
    result["ebay_price"] = round(price, 2)

    # Inventory Item für Bild-Check
    inv = get_inventory_item(client, sku)
    if inv:
        img_urls = inv.get("product", {}).get("imageUrls", [])
        result["has_image"] = bool(img_urls)
    else:
        result["has_image"] = False

    # ── Status bestimmen ─────────────────────────────────────────────────
    if offer_status not in ("PUBLISHED", "ACTIVE"):
        result["status"] = "INACTIVE"
        result["issues"].append(f"OFFER_STATUS_{offer_status}")
    elif price <= 0:
        result["status"] = "INACTIVE"
        result["issues"].append("NO_PRICE")
    else:
        result["status"] = "LIVE"

    # ── Weitere Checks ───────────────────────────────────────────────────
    if price > 0 and price < floor - 0.01:
        result["issues"].append("PRICE_BELOW_FLOOR")

    if not result["has_image"]:
        result["issues"].append("NO_IMAGE")

    result["issues"] = "; ".join(result["issues"]) if result["issues"] else "OK"
    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=CONFIG_FILE)
    parser.add_argument("--limit",  type=int, default=0)
    parser.add_argument("--only-problems", action="store_true",
                        help="Nur Artikel mit Issues ausgeben")
    args = parser.parse_args()

    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))

    print(f"Audit: {args.config}")
    client = EbayClient.from_env(cfg["ebay"])

    products = load_bab_feed(cfg)
    if args.limit:
        products = products[:args.limit]
        print(f"Limit: {args.limit}")

    # Zähler
    counts = {
        "LIVE": 0, "INACTIVE": 0, "NOT_LISTED": 0,
        "PRICE_BELOW_FLOOR": 0, "NO_IMAGE": 0, "NO_STOCK": 0,
    }
    rows = []

    for i, product in enumerate(products, 1):
        sku = product["sku"]
        print(f"[{i:4}/{len(products)}] {sku} …", end=" ", flush=True)

        result = audit_product(product, client, cfg)
        rows.append(result)

        status = result["status"]
        issues = result["issues"]
        counts[status] = counts.get(status, 0) + 1
        if "PRICE_BELOW_FLOOR" in issues: counts["PRICE_BELOW_FLOOR"] += 1
        if "NO_IMAGE"          in issues: counts["NO_IMAGE"]          += 1
        if "NO_STOCK"          in issues: counts["NO_STOCK"]          += 1

        print(f"{status:12} | {issues[:60]}")
        time.sleep(DELAY)

    # ── Report schreiben ──────────────────────────────────────────────────
    fieldnames = ["sku","ean","title","brand","ek","stock_feed","status",
                  "issues","ebay_price","floor_price","normal_price",
                  "offer_status","has_image","offer_id"]

    output_rows = rows if not args.only_problems else [
        r for r in rows if r["issues"] != "OK"
    ]

    with open(REPORT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    total = len(products)
    print(f"\n{'═'*60}")
    print(f"  Gesamt geprüft:      {total}")
    print(f"  ✅ LIVE:             {counts['LIVE']}  ({counts['LIVE']/total*100:.1f}%)")
    print(f"  ⚠️  INAKTIV:          {counts['INACTIVE']}")
    print(f"  ❌ NICHT GELISTET:   {counts['NOT_LISTED']}")
    print(f"  💸 Preis < Floor:    {counts['PRICE_BELOW_FLOOR']}")
    print(f"  🖼️  Kein Bild:        {counts['NO_IMAGE']}")
    print(f"  📦 Kein Bestand:     {counts['NO_STOCK']}")
    print(f"  Report: {REPORT_FILE}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
