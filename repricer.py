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
STATE_FILE     = "repricer_state.json"   # Checkpoint: wo haben wir aufgehört?
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
CHUNK_SIZE          = 9999   # Alle Produkte pro Run (17 Min < 30 Min Timeout)


# ─── Preisformel (identisch mit sync.py) ─────────────────────────────────────

def calc_vk(ek: float, margin: float, cfg: dict) -> float:
    """
    Korrekte Formel — eBay nimmt Gebühr auf (VK + Käufer-Versand):

      (VK + buyer_ship) × (1 − total_fee) / (1 + vat) = EK × (1 + margin) + carrier

    Auflösen nach VK:
      VK = (EK × (1 + margin) + carrier) × (1 + vat) / (1 − total_fee) − buyer_ship

    total_fee = ebay_fee (13%) + campaign_fee (8%) = 21%
    margin = 0.20 → Mindestpreis (20% Marge auf EK nach allen Kosten)
    margin = 0.25 → Normalpreis  (25% Ziel-Marge)
    """
    ep           = cfg["ebay_pricing"]
    carrier      = ep.get("shipping_cost_eur", 5.00)
    buyer_ship   = ep.get("buyer_shipping_eur", 0.00)
    ebay_fee     = ep.get("ebay_fee_rate", 0.13)
    campaign_fee = ep.get("campaign_fee_rate", 0.08)  # Promoted Listings 8%
    total_fee    = ebay_fee + campaign_fee             # 21%
    vat          = ep.get("vat_rate", 0.19)
    vk = (ek * (1 + margin) + carrier) * (1 + vat) / (1 - total_fee) - buyer_ship
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

def get_offer_data(client: EbayClient, sku: str) -> Optional[dict]:
    """
    Gibt {offerId, currentPrice} für eine SKU zurück oder None.
    GET /offer?sku=X ist der korrekte Endpoint (braucht zwingend SKU).
    """
    try:
        offer = client.get_offer_for_sku(sku)
        if not offer:
            return None
        price_val = 0.0
        try:
            price_val = float(
                offer.get("pricingSummary", {})
                    .get("price", {})
                    .get("value", 0)
            )
        except (TypeError, ValueError):
            pass
        return {
            "offerId":      offer.get("offerId", ""),
            "currentPrice": price_val,
            "status":       offer.get("status", ""),
        }
    except Exception as e:
        log.debug(f"Offer-Fetch SKU={sku}: {e}")
        return None


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

    try:
        client._request("PUT", f"{OFFER_PATH}/{offer_id}", json_body=existing)
    except RuntimeError as e:
        err_str = str(e)
        # Fehler 25002: Bild zu niedrig aufgelöst → Preis-Update nicht möglich
        if "25002" in err_str:
            raise RuntimeError(
                f"Bild-Auflösung zu gering (eBay Error 25002) – SKU {sku} übersprungen."
            ) from None
        # Fehler 25604: Availability nicht gefunden → Inventory Item unvollständig
        # eBay verweigert den PUT weil das Inventory Item keine Quantity/Availability hat.
        # Produkt überspringen, kein Crash.
        if "25604" in err_str:
            raise RuntimeError(
                f"Kein Availability im Inventory Item (eBay Error 25604) – SKU {sku} übersprungen."
            ) from None
        raise


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

    # ── EK gestiegen? Preis unter Floor → sofort auf Floor anheben ───────
    # Lieferanten-CSV ändert sich stündlich; wenn EK teurer wird,
    # steigt der Floor automatisch → wir müssen den eBay-Preis anpassen
    # bevor wir überhaupt Mitbewerber abfragen.
    if current_price < floor_price - 0.01:
        result["action"]    = "raised"
        result["reason"]    = "ek_increased_price_below_floor"
        result["new_price"] = floor_price
        log.info(
            f"  ↑ {sku}: EK gestiegen! eBay-Preis {current_price:.2f}€ "
            f"< Floor {floor_price:.2f}€ (EK={ek:.2f}€) → anheben"
        )
        if not dry_run:
            update_offer_price(client, offer_id, sku, floor_price)
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
    # Aber: statt komplett zu skippen → auf Floor senken (bestmöglich ohne Verlust)
    if target_price < floor_price:
        if current_price <= floor_price + 0.02:
            # Sind schon am Floor → nichts zu tun
            result["action"] = "unchanged"
            result["reason"] = "at_floor_cannot_undercut"
            log.debug(f"  {sku}: bereits am Floor {floor_price:.2f}€, Mitbewerber günstiger → keine Änderung")
            return result
        # Preis auf Floor senken — so günstig wie möglich ohne Verlust
        result["action"]    = "lowered"
        result["reason"]    = "floor_protection_set_to_floor"
        result["new_price"] = floor_price
        log.info(
            f"  ↓ {sku}: {current_price:.2f}€ → {floor_price:.2f}€ "
            f"(Mitbewerber {lowest:.2f}€ günstiger als Floor — auf Floor setzen)"
        )
        if not dry_run:
            update_offer_price(client, offer_id, sku, floor_price)
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

def load_state(config_key: str) -> dict:
    """Lädt den Checkpoint-State: welcher Offset ist dran."""
    try:
        data = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
        return data.get(config_key, {"offset": 0, "cycle": 1})
    except Exception:
        return {"offset": 0, "cycle": 1}


def save_state(config_key: str, offset: int, total: int, cycle: int):
    """Speichert den Checkpoint-State nach jedem Run."""
    try:
        data = json.loads(Path(STATE_FILE).read_text(encoding="utf-8")) if Path(STATE_FILE).exists() else {}
    except Exception:
        data = {}
    data[config_key] = {"offset": offset, "total": total, "cycle": cycle,
                        "updated": datetime.now(timezone.utc).isoformat()}
    Path(STATE_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
    parser.add_argument("--config",     default="config_shop2.yaml")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--limit",      type=int, default=0,
                        help="Überschreibt CHUNK_SIZE für diesen Run")
    parser.add_argument("--reset",      action="store_true",
                        help="Checkpoint zurücksetzen (von vorne anfangen)")
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

    # ── BAB-Feed laden ────────────────────────────────────────────────────
    bab_feed = load_bab_feed(cfg)
    products = list(bab_feed.items())   # [(ean, feed_data), ...]
    total    = len(products)
    log.info(f"Produkte aus Feed: {total}")

    # ── Checkpoint laden ──────────────────────────────────────────────────
    config_key = Path(args.config).stem   # "config_shop2"
    if args.reset:
        save_state(config_key, 0, total, 1)
        log.info("Checkpoint zurückgesetzt.")

    state     = load_state(config_key)
    offset    = state["offset"]
    cycle     = state["cycle"]
    chunk     = args.limit if args.limit else CHUNK_SIZE

    # Chunk aus der Produktliste schneiden
    chunk_products = products[offset: offset + chunk]
    next_offset    = offset + len(chunk_products)
    cycle_complete = next_offset >= total

    log.info(f"Zyklus {cycle} | Produkte {offset+1}–{next_offset} von {total} "
             f"({'letzter Block' if cycle_complete else f'nächster Start: {next_offset}'})")

    # ── Repricing ─────────────────────────────────────────────────────────
    stats = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "checked":      0,
        "lowered":      0,
        "raised":       0,
        "set_to_floor": 0,   # Mitbewerber günstiger als Floor → auf Floor gesetzt
        "skipped_floor":0,   # bereits am Floor, kein Spielraum mehr
        "skipped_few":  0,
        "unchanged":    0,
        "errors":       0,
        "dry_run":      args.dry_run,
    }
    all_changes = []

    # Bild-Problemliste laden (SKUs mit eBay Error 25002)
    IMAGE_FIX_FILE = "image_fix_needed.json"
    try:
        image_fix = json.loads(Path(IMAGE_FIX_FILE).read_text(encoding="utf-8")) if Path(IMAGE_FIX_FILE).exists() else {}
    except Exception:
        image_fix = {}

    for n, (ean, feed_data) in enumerate(chunk_products, 1):
        sku   = feed_data["sku"]
        ek    = feed_data["ek"]
        title = feed_data["title"]
        log.info(f"[{offset+n}/{total}] {sku}  {title[:50]}")
        stats["checked"] += 1

        # Offer von eBay abrufen (Preis + offerId)
        offer = get_offer_data(client, sku)
        if not offer or offer["currentPrice"] <= 0:
            log.debug(f"  Kein aktives Offer für SKU {sku} → überspringen")
            stats["unchanged"] += 1
            time.sleep(0.2)
            continue

        try:
            result = reprice_product(
                ean=ean, sku=sku, ek=ek,
                title=title, current_price=offer["currentPrice"],
                offer_id=offer["offerId"], cfg=cfg,
                app_token=app_token, base_url=base_url,
                dry_run=args.dry_run, client=client,
            )
        except Exception as e:
            err_str = str(e)
            log.error(f"  FEHLER {sku}: {e}")
            stats["errors"] += 1
            # 25002 = Bild-Auflösung zu gering → für Fixer merken
            if "25002" in err_str and not args.dry_run:
                image_fix[sku] = {
                    "ean":   ean,
                    "title": title,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
            continue

        action = result["action"]
        reason = result["reason"]

        if action == "lowered":
            if reason == "floor_protection_set_to_floor":
                stats["set_to_floor"] += 1
            else:
                stats["lowered"] += 1
            all_changes.append(result)
        elif action == "raised":
            stats["raised"] += 1
            all_changes.append(result)
        elif action == "skipped":
            if "floor" in reason or "at_floor" in reason:
                stats["skipped_floor"] += 1
            elif "few_comp" in reason or "no_comp" in reason:
                stats["skipped_few"] += 1
        else:
            stats["unchanged"] += 1

        time.sleep(DELAY)

    # ── Bild-Problemliste speichern ───────────────────────────────────────
    if not args.dry_run and image_fix:
        Path(IMAGE_FIX_FILE).write_text(
            json.dumps(image_fix, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"  Bilder zu fixen: {len(image_fix)} SKUs → {IMAGE_FIX_FILE}")

    # ── Checkpoint speichern ──────────────────────────────────────────────
    if not args.dry_run:
        if cycle_complete:
            new_offset = 0
            new_cycle  = cycle + 1
            log.info(f"✅ Zyklus {cycle} abgeschlossen — starte nächsten Zyklus {new_cycle} von vorne")
        else:
            new_offset = next_offset
            new_cycle  = cycle
        save_state(config_key, new_offset, total, new_cycle)

    # ── Ergebnis ──────────────────────────────────────────────────────────
    log.info(f"\n{'─'*60}")
    log.info(f"  Zyklus:                     {cycle} ({'komplett' if cycle_complete else f'Offset → {next_offset}'})")
    log.info(f"  Geprüft:                    {stats['checked']}")
    log.info(f"  Preis gesenkt:              {stats['lowered']}")
    log.info(f"  Auf Floor gesetzt:          {stats['set_to_floor']}")
    log.info(f"  Preis erhöht:               {stats['raised']}")
    log.info(f"  Bereits am Floor (kein Sp): {stats['skipped_floor']}")
    log.info(f"  Zu wenig Konkurrenz:        {stats['skipped_few']}")
    log.info(f"  Unverändert:                {stats['unchanged']}")
    log.info(f"  Fehler:                     {stats['errors']}")
    if args.dry_run:
        log.info("  [DRY-RUN — keine echten Änderungen, kein Checkpoint gespeichert]")

    # ── Report speichern ──────────────────────────────────────────────────
    save_report(REPORT_FILE, stats, all_changes)
    log.info(f"✓ Report gespeichert: {REPORT_FILE}")


if __name__ == "__main__":
    main()
