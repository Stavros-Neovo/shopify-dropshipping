"""
repricer.py
===========
Automatisches eBay-Repricing mit Dashboard-Logging.

Strategie:
  - €1,01 unter günstigstem echten Mitbewerber
  - Mindestmarge: 20% auf EK (absoluter Boden, nach allen Gebühren)
  - Normalpreis: 25% auf EK (Zielpreis wenn wir wieder teurer werden können)
  - Nur reprisen wenn ≥ 2 Mitbewerber gefunden (Ausreißer-Schutz)
  - Bin ich schon günstigster → nicht weiter senken
  - Alle teurer → Preis wieder Richtung Normalpreis erhöhen

Sicherheitsmaßnahmen:
  - EK = 0 oder fehlt → überspringen
  - Konkurrent-Preis < 70% meines Floor-Preises → Fehldaten, ignorieren
  - Max. 15% Preissenkung pro Lauf
  - API-Fehler → überspringen, nicht abstürzen
  - Dry-run mode

Dashboard:
  - repricer_report.json wird nach jedem Lauf aktualisiert
  - Enthält Zusammenfassung, alle Änderungen und Verlauf (30 Läufe)

Aufruf:
  python repricer.py                          # Shop 2 (default)
  python repricer.py --config config.yaml     # Shop 1
  python repricer.py --dry-run                # Simulation
  python repricer.py --limit 50               # nur 50 Produkte testen
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import logging
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient

log = logging.getLogger("repricer")

REPORT_FILE    = "repricer_report.json"
OFFER_PATH     = "/sell/inventory/v1/offer"
BROWSE_PATH    = "/buy/browse/v1/item_summary/search"
IDENTITY_PATH  = "/commerce/identity/v1/user/"

# Repricing-Parameter
UNDERCUT_EUR        = 1.01   # €1,01 unter Mitbewerber
MIN_MARGIN_PCT      = 0.20   # Mindestmarge 20% auf EK (absoluter Boden)
TARGET_MARGIN_PCT   = 0.25   # Zielpreis 25% auf EK (Normalpreis)
MIN_COMPETITORS     = 2      # Mindestanzahl Mitbewerber
MAX_DROP_PCT        = 0.15   # Max 15% Preissenkung pro Lauf
COMPETITOR_MIN_RATIO = 0.70  # Konkurrent-Preis muss >= 70% des Floor-Preises sein
DELAY               = 0.4    # Sekunden zwischen Browse-API-Aufrufen


# ─── Preisformel (identisch mit sync.py) ─────────────────────────────────────

def calc_vk(ek: float, margin: float, cfg: dict) -> float:
    """
    VK_brutto = (EK + Versand + EK × Marge) × (1 + MwSt) ÷ (1 - eBay-Gebühr)
    margin = 0.20 → Mindestpreis (20% Nettomarge auf EK)
    margin = 0.25 → Normalpreis  (25% Nettomarge auf EK)
    """
    ep = cfg["ebay_pricing"]
    shipping = ep["shipping_cost_eur"]   # 5.00
    fee      = ep["ebay_fee_rate"]        # 0.13
    vat      = ep["vat_rate"]             # 0.19
    base = ek + shipping + ek * margin
    vk = base * (1 + vat) / (1 - fee)
    return vk


def psychological_round(price: float) -> float:
    """Rundet auf psychologischen Preis: x.99 oder x.95 (je nach Preisniveau)."""
    if price <= 0:
        return price
    floored = math.floor(price)
    # x.99 für Preise < 500€, x.95 für höhere
    cent = 0.99 if price < 500 else 0.95
    candidate = floored + cent
    return candidate if candidate >= price else floored + 1 + cent


# ─── BAB-Feed ────────────────────────────────────────────────────────────────

def load_bab_feed(cfg: dict) -> dict:
    """Lädt BAB-Feed → {EAN: {sku, ek, title, brand}}"""
    c      = cfg["csv"]
    url    = c["url"]
    user   = os.environ.get("CSV_HTTP_USER", "")
    pw     = os.environ.get("CSV_HTTP_PASSWORD", "")
    enc    = c.get("encoding", "utf-8-sig")
    delim  = c.get("delimiter", ";")
    cols   = c.get("columns", {})

    sku_col   = cols.get("sku",   "ItemNo")
    ean_col   = cols.get("ean",   "GTIN")
    ek_col    = cols.get("purchase_price", "Price_B2B")
    title_col = cols.get("title", "Description")
    brand_col = cols.get("brand", "ManufacturerName")

    log.info("Lade BAB-Feed …")
    req = urllib.request.Request(url)
    if user and pw:
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(f"{user}:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode(enc, errors="replace")

    mapping = {}
    for row in csv.DictReader(io.StringIO(raw), delimiter=delim):
        ean   = (row.get(ean_col) or "").strip()
        sku   = (row.get(sku_col) or "").strip()
        ek_s  = (row.get(ek_col)  or "0").strip().replace(",", ".")
        title = (row.get(title_col) or "").strip()
        brand = (row.get(brand_col) or "").strip()
        if not ean or not sku:
            continue
        try:
            ek = float(ek_s)
        except ValueError:
            continue
        if ek <= 0:
            continue
        mapping[ean] = {"sku": sku, "ek": ek, "title": title, "brand": brand}

    log.info(f"BAB-Feed: {len(mapping)} Produkte mit EAN+EK geladen")
    return mapping


# ─── eBay Offers ──────────────────────────────────────────────────────────────

def get_all_offers(client: EbayClient) -> dict:
    """Alle aktiven eBay-Offers → {SKU: {offerId, currentPrice}}"""
    offers_map = {}
    offset, limit = 0, 100
    while True:
        try:
            data = client._request(
                "GET", OFFER_PATH,
                params={"marketplace_id": client.marketplace_id,
                        "limit": limit, "offset": offset}
            ) or {}
        except Exception as e:
            log.warning(f"Offers-Fetch Fehler: {e}")
            break

        batch = data.get("offers", [])
        if not batch:
            break

        for offer in batch:
            sku = (offer.get("sku") or "").strip()
            if not sku:
                continue
            price_val = 0.0
            try:
                price_val = float(
                    offer.get("pricingSummary", {})
                        .get("price", {})
                        .get("value", 0)
                )
            except (TypeError, ValueError):
                pass
            offers_map[sku] = {
                "offerId": offer.get("offerId", ""),
                "currentPrice": price_val,
                "status": offer.get("status", ""),
            }

        offset += limit
        if offset >= data.get("total", 0):
            break

    log.info(f"eBay Offers geladen: {len(offers_map)} aktive Listings")
    return offers_map


def update_offer_price(client: EbayClient, offer_id: str, sku: str, new_price: float):
    """Aktualisiert NUR den Preis eines bestehenden Offers."""
    # Aktuelles Offer holen, nur Preis ändern
    existing = client._request("GET", f"{OFFER_PATH}/{offer_id}") or {}
    if not existing:
        raise RuntimeError(f"Offer {offer_id} nicht gefunden")

    existing.setdefault("pricingSummary", {})
    existing["pricingSummary"]["price"] = {
        "value": f"{new_price:.2f}",
        "currency": "EUR",
    }
    # Felder die beim PUT nicht erlaubt sind entfernen
    for field in ["offerId", "listing", "marketplaceId", "status",
                  "listingId", "auditInfo", "availableQuantity"]:
        existing.pop(field, None)

    client._request("PUT", f"{OFFER_PATH}/{offer_id}", json_body=existing)


# ─── Browse API: Mitbewerber-Preise ──────────────────────────────────────────

def get_competitor_prices(
    ean: str,
    app_token: str,
    base_url: str,
    my_price: float,
    floor_price: float,
) -> list[float]:
    """
    Sucht eBay.de nach Festpreis-Listings für diese EAN.
    Filtert:
      - Eigene Listings (gleicher Preis wie mein aktueller Preis)
      - Unrealistische Preise (< 70% des Floor-Preises)
    Gibt sortierte Liste der Mitbewerber-Preise zurück.
    """
    try:
        r = requests.get(
            f"{base_url}{BROWSE_PATH}",
            headers={
                "Authorization": f"Bearer {app_token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE",
                "Accept": "application/json",
            },
            params={
                "q": ean,
                "filter": "buyingOptions:{FIXED_PRICE}",
                "sort": "price",
                "limit": "25",
            },
            timeout=12,
        )
        if r.status_code != 200:
            log.debug(f"Browse API {r.status_code} für EAN {ean}: {r.text[:100]}")
            return []

        items = r.json().get("itemSummaries", [])
        prices = []
        sanity_floor = floor_price * COMPETITOR_MIN_RATIO

        for item in items:
            try:
                price = float(item.get("price", {}).get("value", 0))
            except (TypeError, ValueError):
                continue

            if price <= 0:
                continue
            # Ausreißer filtern (Dumpingpreise / Fehldaten)
            if price < sanity_floor:
                log.debug(f"  Mitbewerber-Preis {price:.2f}€ < Sanity-Floor {sanity_floor:.2f}€ → ignoriert")
                continue
            # Eigenes Listing herausfiltern (gleicher Preis ±0.02€)
            if abs(price - my_price) < 0.03:
                log.debug(f"  Preis {price:.2f}€ ≈ mein Preis → vermutlich eigenes Listing → übersprungen")
                continue

            prices.append(price)

        return sorted(prices)

    except Exception as e:
        log.debug(f"Browse API Fehler EAN {ean}: {e}")
        return []


# ─── Repricing-Logik ─────────────────────────────────────────────────────────

def reprice_product(
    ean: str,
    sku: str,
    ek: float,
    title: str,
    current_price: float,
    offer_id: str,
    cfg: dict,
    app_token: str,
    base_url: str,
    dry_run: bool,
    client: EbayClient,
) -> dict:
    """
    Repricing-Entscheidung für ein einzelnes Produkt.
    Gibt ein Ergebnis-Dict zurück (für Report/Dashboard).
    """
    floor_price  = psychological_round(calc_vk(ek, MIN_MARGIN_PCT,    cfg))
    normal_price = psychological_round(calc_vk(ek, TARGET_MARGIN_PCT, cfg))
    min_margin   = cfg["ebay_pricing"].get("min_margin_eur", 5.0)

    result = {
        "ean":              ean,
        "sku":              sku,
        "title":            title[:80],
        "ek":               round(ek, 2),
        "floor_price":      floor_price,
        "normal_price":     normal_price,
        "old_price":        current_price,
        "new_price":        current_price,
        "competitor_count": 0,
        "lowest_competitor": None,
        "action":           "unchanged",
        "reason":           "",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }

    # ── Sicherheit 1: EK-Sanity ───────────────────────────────────────────
    if ek <= 0:
        result["action"] = "skipped"
        result["reason"] = "ek_zero"
        return result

    if current_price <= 0 or not offer_id:
        result["action"] = "skipped"
        result["reason"] = "no_active_offer"
        return result

    # ── Mitbewerber abfragen ──────────────────────────────────────────────
    comp_prices = get_competitor_prices(
        ean, app_token, base_url, current_price, floor_price
    )
    result["competitor_count"] = len(comp_prices)

    if not comp_prices:
        result["action"] = "skipped"
        result["reason"] = "no_competitors_found"
        return result

    if len(comp_prices) < MIN_COMPETITORS:
        result["action"] = "skipped"
        result["reason"] = f"too_few_competitors_{len(comp_prices)}"
        return result

    lowest = comp_prices[0]
    result["lowest_competitor"] = round(lowest, 2)

    # ── Bin ich schon günstigster? ────────────────────────────────────────
    if current_price < lowest:
        # Bin bereits billiger — aber bin ich weit unter Normalpreis?
        # Falls ja → Preis wieder etwas erhöhen Richtung normal
        if current_price < normal_price - 0.02:
            # Preis erhöhen: €1,01 unter dem Günstigsten (oder Normalpreis falls kein Konkurrent günstiger)
            target = psychological_round(min(lowest - UNDERCUT_EUR, normal_price))
            target = max(target, floor_price)
            if target > current_price + 0.02:
                new_price = target
                result["action"] = "raised"
                result["reason"] = "cheaper_than_all_raise_toward_normal"
                result["new_price"] = new_price
                log.info(f"  ↑ {sku}: {current_price:.2f}€ → {new_price:.2f}€ (unter allen, erhöhe Richtung Normal)")
                if not dry_run:
                    update_offer_price(client, offer_id, sku, new_price)
            else:
                result["action"] = "unchanged"
                result["reason"] = "already_cheapest_close_to_normal"
        else:
            result["action"] = "unchanged"
            result["reason"] = "already_cheapest_at_normal"
        return result

    # ── Mitbewerber ist günstiger → unterbieten ───────────────────────────
    target_price = psychological_round(lowest - UNDERCUT_EUR)

    # Sicherheit: Nicht unter Floor
    if target_price < floor_price:
        result["action"] = "skipped"
        result["reason"] = "floor_would_be_breached"
        result["new_price"] = floor_price
        log.info(f"  ⚠ {sku}: Zielpreis {target_price:.2f}€ < Floor {floor_price:.2f}€ → nicht senken")
        return result

    # Sicherheit: Max 15% Senkung pro Lauf
    max_allowed_drop = current_price * (1 - MAX_DROP_PCT)
    if target_price < max_allowed_drop:
        target_price = psychological_round(max(max_allowed_drop, floor_price))
        result["reason"] = "max_drop_limit_applied"
        log.info(f"  ⚠ {sku}: Max-Drop-Limit → {target_price:.2f}€ statt {lowest - UNDERCUT_EUR:.2f}€")

    # Sicherheit: Mindest-Absolutmarge
    if (target_price - ek) < min_margin:
        result["action"] = "skipped"
        result["reason"] = "min_absolute_margin_breached"
        log.info(f"  ⚠ {sku}: Absolutmarge würde unter {min_margin:.2f}€ fallen")
        return result

    # Keine echte Änderung nötig?
    if abs(target_price - current_price) < 0.02:
        result["action"] = "unchanged"
        result["reason"] = "price_already_optimal"
        return result

    result["new_price"] = target_price
    result["action"]    = "lowered"
    if not result["reason"]:
        result["reason"] = "competitor_undercut"
    log.info(f"  ↓ {sku}: {current_price:.2f}€ → {target_price:.2f}€ (Mitbewerber: {lowest:.2f}€)")
    if not dry_run:
        update_offer_price(client, offer_id, sku, target_price)

    return result


# ─── Report / Dashboard ──────────────────────────────────────────────────────

def load_report(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"history": [], "last_changes": []}


def save_report(path: str, summary: dict, changes: list):
    """
    Speichert Report für Dashboard:
    - last_run: Zusammenfassung des aktuellen Laufs
    - last_changes: Alle Änderungen dieses Laufs
    - history: Letzte 30 Lauf-Zusammenfassungen (Trend)
    """
    report = load_report(path)

    # Letzte 30 Einträge im Verlauf behalten
    history = report.get("history", [])
    history.append({
        "timestamp":    summary["timestamp"],
        "checked":      summary["checked"],
        "lowered":      summary["lowered"],
        "raised":       summary["raised"],
        "skipped_floor":summary["skipped_floor"],
        "skipped_few":  summary["skipped_few"],
        "unchanged":    summary["unchanged"],
        "errors":       summary["errors"],
    })
    if len(history) > 30:
        history = history[-30:]

    report["last_run"]     = summary
    report["last_changes"] = changes
    report["history"]      = history

    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",  default="config_shop2.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit",   type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))

    if not cfg.get("ebay", {}).get("enabled", False):
        log.error(f"eBay nicht aktiviert in {args.config}")
        sys.exit(1)

    if args.dry_run:
        log.info("🔍 DRY-RUN Modus — keine echten Preisänderungen")

    # ── Clients & Tokens ──────────────────────────────────────────────────
    client    = EbayClient.from_env(cfg["ebay"])
    app_token = client._get_app_token()
    base_url  = client.base

    # ── Daten laden ───────────────────────────────────────────────────────
    bab_feed = load_bab_feed(cfg)
    offers   = get_all_offers(client)

    # ── EAN → {ek, sku, title, offerId, currentPrice} zusammenführen ─────
    products = []
    for ean, feed_data in bab_feed.items():
        sku = feed_data["sku"]
        if sku not in offers:
            continue
        offer = offers[sku]
        if offer["currentPrice"] <= 0:
            continue
        products.append({
            "ean":          ean,
            "sku":          sku,
            "ek":           feed_data["ek"],
            "title":        feed_data["title"],
            "brand":        feed_data["brand"],
            "offerId":      offer["offerId"],
            "currentPrice": offer["currentPrice"],
        })

    log.info(f"Produkte zum Repricing: {len(products)}")

    if args.limit:
        products = products[: args.limit]
        log.info(f"Limit: {args.limit}")

    # ── Repricing ─────────────────────────────────────────────────────────
    stats = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "checked":      0,
        "lowered":      0,
        "raised":       0,
        "skipped_floor":0,
        "skipped_few":  0,
        "unchanged":    0,
        "errors":       0,
        "dry_run":      args.dry_run,
    }
    all_changes = []

    for n, p in enumerate(products, 1):
        log.info(f"[{n}/{len(products)}] {p['sku']}  {p['title'][:50]}")
        stats["checked"] += 1

        try:
            result = reprice_product(
                ean=p["ean"], sku=p["sku"], ek=p["ek"],
                title=p["title"], current_price=p["currentPrice"],
                offer_id=p["offerId"], cfg=cfg,
                app_token=app_token, base_url=base_url,
                dry_run=args.dry_run, client=client,
            )
        except Exception as e:
            log.error(f"  FEHLER {p['sku']}: {e}")
            stats["errors"] += 1
            continue

        action = result["action"]
        reason = result["reason"]

        if action == "lowered":
            stats["lowered"] += 1
            all_changes.append(result)
        elif action == "raised":
            stats["raised"] += 1
            all_changes.append(result)
        elif action == "skipped":
            if "floor" in reason:
                stats["skipped_floor"] += 1
            elif "few_comp" in reason or "no_comp" in reason:
                stats["skipped_few"] += 1
        else:
            stats["unchanged"] += 1

        time.sleep(DELAY)

    # ── Ergebnis ──────────────────────────────────────────────────────────
    log.info(f"\n{'─'*60}")
    log.info(f"  Geprüft:             {stats['checked']}")
    log.info(f"  Preis gesenkt:       {stats['lowered']}")
    log.info(f"  Preis erhöht:        {stats['raised']}")
    log.info(f"  Floor-Schutz aktiv:  {stats['skipped_floor']}")
    log.info(f"  Zu wenig Konkurrenz: {stats['skipped_few']}")
    log.info(f"  Unverändert:         {stats['unchanged']}")
    log.info(f"  Fehler:              {stats['errors']}")
    if args.dry_run:
        log.info("  [DRY-RUN — keine echten Änderungen]")

    # ── Report speichern ──────────────────────────────────────────────────
    save_report(REPORT_FILE, stats, all_changes)
    log.info(f"\n✓ Report gespeichert: {REPORT_FILE}")


if __name__ == "__main__":
    main()
