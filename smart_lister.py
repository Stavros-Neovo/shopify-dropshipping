"""
smart_lister.py — Wettbewerbs-basierter Artikel-Auswähler
==========================================================
Scannt BAB + Kosatec CSV, berechnet Scores basierend auf
eBay Watch Count + Konkurrenz + Marge und wählt die besten
Artikel für den eBay Top-Shop aus (max. 2.500 Slots).

Ablauf:
  1. BAB + Kosatec einlesen → EAN-Merge (günstigster EK)
  2. eBay Browse API → Watch Count + Konkurrenz (gecacht)
  3. Score berechnen → Top-N auswählen
  4. supplier_map.json schreiben
  5. Ausgabe: Kandidaten-Liste zum Listen

Aufruf:
  python smart_lister.py --scan         # Nur Score-Cache aktualisieren (5000 Calls/Tag)
  python smart_lister.py --select       # Top-Artikel auswählen + supplier_map schreiben
  python smart_lister.py --scan --select  # Beides
  python smart_lister.py --dry-run      # Nur anzeigen, nichts schreiben
  python smart_lister.py --top 100      # Nur Top 100 (Standard: 2300)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
import yaml
from dotenv import load_dotenv

log = logging.getLogger("smart_lister")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
BAB_CSV         = "bab_preisliste.csv"
KOSATEC_CSV     = "kosatec_preisliste.csv"
ARTIKELDATEN    = "artikeldaten.csv"
ENRICHMENT      = "enrichment_index.csv"
SCORE_CACHE     = "ebay_score_cache.json"
SUPPLIER_MAP    = "supplier_map.json"
CONFIG_FILE     = "config_shop2.yaml"

EBAY_FEE        = 0.13
CAMPAIGN_FEE    = 0.08   # 8% Promoted Listings Standard
TOTAL_FEE       = EBAY_FEE + CAMPAIGN_FEE  # 21% gesamt
VAT_FACTOR      = 1.19
BUYER_SHIP      = 3.99
SHIP_COST       = 5.0
MARGIN_TARGET   = 0.25   # 25% Zielmarge

TOP_SHOP_LIMIT  = 2300   # Freie Slots für neue Artikel
CACHE_TTL_DAYS  = 7      # Score nach 7 Tagen neu abfragen
DAILY_API_LIMIT = 4800   # Sicherheitspuffer unter 5000

BAB_EMAIL       = "SSchulze@bab-distribution.de"
KOSATEC_EMAIL   = "bestellungen@kosatec.de"

# ---------------------------------------------------------------------------
# Preisformel
# ---------------------------------------------------------------------------
def calc_vk(ek: float) -> float:
    """Berechnet VK-Brutto mit 25% Marge (psychological .99)."""
    # VK so dass: (VK + BUYER_SHIP) * (1-EBAY_FEE) / VAT_FACTOR - SHIP_COST - EK >= EK * MARGIN_TARGET
    # Vereinfacht: VK_netto = EK * (1 + MARGIN_TARGET) + SHIP_COST
    # VK_brutto = VK_netto * VAT_FACTOR / (1 - EBAY_FEE)
    vk_netto = ek * (1 + MARGIN_TARGET) + SHIP_COST
    vk_brutto = vk_netto * VAT_FACTOR / (1 - TOTAL_FEE)
    vk_brutto -= BUYER_SHIP  # Käufer zahlt Versand separat
    # Psychological rounding
    vk_rounded = round(vk_brutto) - 0.01
    if vk_rounded < vk_brutto:
        vk_rounded += 1.0
    return round(vk_rounded, 2)


def calc_profit(vk: float, ek: float) -> float:
    """Nettogewinn nach allen Kosten inkl. 8% Kampagnengebühr."""
    total = vk + BUYER_SHIP
    fee = total * TOTAL_FEE   # eBay 13% + Kampagne 8%
    vat = (total - fee) * (1 - 1 / VAT_FACTOR)
    netto = total - fee - vat
    return round(netto - SHIP_COST - ek, 2)


def min_profit(ek: float) -> float:
    """Mindestgewinn: deckt Fixkosten + Retourenrisiko."""
    # 315€/Monat Fixkosten / 500 Verkäufe/Monat ≈ 0.63€ + 5% Retoure * 12€
    return max(5.0, ek * 0.08)


# ---------------------------------------------------------------------------
# Daten einlesen
# ---------------------------------------------------------------------------
def load_bab(path: str = BAB_CSV) -> dict[str, dict]:
    """Gibt {ean: {ek, name, sku, supplier}} zurück."""
    result = {}
    if not Path(path).exists():
        log.warning(f"BAB CSV nicht gefunden: {path}")
        return result
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            ean = row.get("GTIN", "").strip()
            if not ean or int(row.get("Stock", "0") or 0) < 1:
                continue
            try:
                ek = float(row["Price_B2B"].replace(",", "."))
            except (ValueError, KeyError):
                continue
            result[ean] = {
                "ean": ean,
                "sku": row.get("ItemNo", "").strip(),
                "name": row.get("Description", "").strip(),
                "ek": ek,
                "supplier": "BAB",
                "supplier_email": BAB_EMAIL,
                "menge": int(row.get("Stock", "0") or 0),
            }
    log.info(f"BAB: {len(result)} Artikel mit EAN + Bestand")
    return result


def load_kosatec(path: str = KOSATEC_CSV) -> dict[str, dict]:
    """Gibt {ean: {ek, name, sku, supplier}} zurück."""
    result = {}
    if not Path(path).exists():
        log.warning(f"Kosatec CSV nicht gefunden: {path}")
        return result
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            ean = row.get("ean", "").strip()
            if not ean or row.get("verfuegbar", "") != "A":
                continue
            try:
                ek = float(row["hek"].replace(",", "."))
            except (ValueError, KeyError):
                continue
            result[ean] = {
                "ean": ean,
                "sku": row.get("artnr", "").strip(),
                "name": row.get("artname", "").strip(),
                "ek": ek,
                "supplier": "Kosatec",
                "supplier_email": KOSATEC_EMAIL,
                "menge": int(row.get("menge", "0") or 0),
                "brand": row.get("hersteller", "").strip(),
                "kat": row.get("kat1", "").strip(),
            }
    log.info(f"Kosatec: {len(result)} Artikel verfügbar mit EAN")
    return result


def load_artikeldaten(path: str = ARTIKELDATEN) -> dict[str, dict]:
    """Gibt {ean: {title, image, description}} zurück."""
    result = {}
    if not Path(path).exists():
        return result
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            ean = row.get("ean", "").strip()
            if not ean:
                continue
            image = (row.get("images_xl") or row.get("images_l") or "").split("|")[0].strip()
            result[ean] = {
                "title": (row.get("title") or row.get("artname") or "").strip(),
                "image": image,
                "description": (row.get("short_summary") or "").strip(),
            }
    return result


def load_enrichment(path: str = ENRICHMENT) -> dict[str, dict]:
    """Gibt {ean: {title_seo, image_main}} zurück."""
    result = {}
    if not Path(path).exists():
        return result
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ean = row.get("ean", "").strip()
            if ean:
                result[ean] = {
                    "title_seo": row.get("title_seo", "").strip(),
                    "image_main": row.get("image_main", "").strip(),
                }
    return result


def merge_catalogs(bab: dict, kosatec: dict) -> dict[str, dict]:
    """
    Merged BAB + Kosatec: bei gleicher EAN wird günstigerer EK genommen.
    Gibt {ean: product_dict} zurück.
    """
    merged = {}

    # Kosatec als Basis (größerer Katalog)
    for ean, prod in kosatec.items():
        merged[ean] = prod.copy()

    # BAB: überschreiben wenn günstiger
    for ean, prod in bab.items():
        if ean not in merged:
            merged[ean] = prod.copy()
        elif prod["ek"] < merged[ean]["ek"]:
            merged[ean] = prod.copy()
            log.debug(f"EAN {ean}: BAB günstiger ({prod['ek']:.2f}€ vs {merged[ean]['ek']:.2f}€)")

    log.info(f"Merged: {len(merged)} einzigartige Artikel ({len(bab)} BAB + {len(kosatec)} Kosatec)")
    return merged


# ---------------------------------------------------------------------------
# eBay Browse API — Score-Cache
# ---------------------------------------------------------------------------
def load_score_cache(path: str = SCORE_CACHE) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_score_cache(cache: dict, path: str = SCORE_CACHE):
    Path(path).write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def get_app_token(client_id: str, client_secret: str) -> str:
    """Application Token für Browse API (kein User-Token nötig)."""
    import base64
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def query_ebay_score(ean: str, token: str) -> dict:
    """
    Fragt eBay Browse API für eine EAN ab.
    Gibt {watch_count, competitor_count, min_price, max_price} zurück.
    """
    try:
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "q": ean,
                "filter": "buyingOptions:{FIXED_PRICE},deliveryCountry:DE",
                "fieldgroups": "EXTENDED",
                "limit": 10,
            },
            timeout=10,
        )
        if resp.status_code == 429:
            time.sleep(5)
            return {"watch_count": 0, "competitor_count": 0, "min_price": 0.0, "max_price": 0.0}
        if not resp.ok:
            return {"watch_count": 0, "competitor_count": 0, "min_price": 0.0, "max_price": 0.0}

        data = resp.json()
        items = data.get("itemSummaries", [])
        total = int(data.get("total", len(items)))

        watch_count = sum(int(i.get("watchCount", 0)) for i in items)
        prices = []
        for i in items:
            try:
                prices.append(float(i.get("price", {}).get("value", 0)))
            except Exception:
                pass

        return {
            "watch_count": watch_count,
            "competitor_count": total,
            "min_price": min(prices) if prices else 0.0,
            "max_price": max(prices) if prices else 0.0,
            "queried_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.warning(f"eBay Score Fehler für EAN {ean}: {e}")
        return {"watch_count": 0, "competitor_count": 0, "min_price": 0.0, "max_price": 0.0}


def needs_refresh(entry: dict) -> bool:
    """True wenn Cache-Eintrag älter als CACHE_TTL_DAYS ist."""
    queried = entry.get("queried_at")
    if not queried:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(queried)
        return age > timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return True


def update_score_cache(eans: list[str], cache: dict, client_id: str, client_secret: str,
                       max_calls: int = DAILY_API_LIMIT) -> dict:
    """
    Aktualisiert Score-Cache für bis zu max_calls EANs.
    Priorisiert: noch nicht gecacht > älteste Einträge.
    """
    # Welche EANs brauchen ein Update?
    to_update = [e for e in eans if e not in cache or needs_refresh(cache.get(e, {}))]
    # Sortieren: keine Einträge zuerst, dann älteste
    to_update.sort(key=lambda e: cache.get(e, {}).get("queried_at", "0"))
    to_update = to_update[:max_calls]

    if not to_update:
        log.info("Score-Cache ist aktuell — keine API-Calls nötig")
        return cache

    log.info(f"Score-Cache: {len(to_update)} EANs abfragen (Limit: {max_calls})")

    try:
        token = get_app_token(client_id, client_secret)
    except Exception as e:
        log.error(f"eBay App-Token fehlgeschlagen: {e}")
        return cache

    updated = 0
    for i, ean in enumerate(to_update):
        score = query_ebay_score(ean, token)
        cache[ean] = score
        updated += 1

        # Alle 100 Calls kurz pausieren + Cache sichern
        if updated % 100 == 0:
            save_score_cache(cache)
            log.info(f"  {updated}/{len(to_update)} EANs abgefragt...")
            time.sleep(0.5)

    save_score_cache(cache)
    log.info(f"Score-Cache aktualisiert: {updated} EANs neu abgefragt")
    return cache


# ---------------------------------------------------------------------------
# Scoring & Selektion
# ---------------------------------------------------------------------------
def score_product(prod: dict, ebay: dict, vk: float) -> float:
    """
    Berechnet Score 0-100:
      Watch Count:        45%
      Verkäufe/Demand:    30%  (competitor_count als Proxy)
      Wenig Konkurrenz:   25%
    """
    watch = min(ebay.get("watch_count", 0), 100)   # cap bei 100
    demand = min(ebay.get("competitor_count", 0), 50)  # Nachfrage-Proxy
    competition = ebay.get("competitor_count", 0)

    # Konkurrenz-Score: weniger = besser, aber 0 = kein Markt
    if competition == 0:
        comp_score = 10  # unbekannt — neutral
    elif competition <= 3:
        comp_score = 100  # wenig Konkurrenz = gut
    elif competition <= 10:
        comp_score = 70
    elif competition <= 25:
        comp_score = 40
    else:
        comp_score = max(0, 100 - competition)

    # Preisattraktivität: sind wir günstiger als Durchschnitt?
    min_price = ebay.get("min_price", 0)
    price_score = 0
    if min_price > 0 and vk < min_price:
        price_score = 20  # Bonus: wir sind günstiger

    # Velocity-Bonus: günstige Artikel verkaufen sich öfter
    if vk <= 50:
        velocity_score = 30
    elif vk <= 100:
        velocity_score = 25
    elif vk <= 150:
        velocity_score = 20
    elif vk <= 200:
        velocity_score = 10
    elif vk <= 300:
        velocity_score = 5
    else:
        velocity_score = 0

    score = (
        watch * 0.40 +
        comp_score * 0.25 +
        price_score +
        velocity_score
    )
    return round(score, 2)


def select_top_products(catalog: dict, cache: dict, artikeldaten: dict,
                        enrichment: dict, already_listed: set,
                        top_n: int = TOP_SHOP_LIMIT) -> list[dict]:
    """
    Wählt die besten top_n Artikel aus dem Katalog aus.
    Filtert: kein Bild, Gewinn unter Minimum, bereits gelistet.
    """
    candidates = []
    dbg = {"listed": 0, "profit": 0, "no_image": 0, "no_title": 0}

    for ean, prod in catalog.items():
        if ean in already_listed:
            dbg["listed"] += 1
            continue

        ek = prod["ek"]
        vk = calc_vk(ek)
        profit = calc_profit(vk, ek)
        min_p = min_profit(ek)

        if profit < min_p:
            dbg["profit"] += 1
            continue

        # Marktpreis-Check: wenn Cache vorhanden und Konkurrenz günstiger → überspringen
        ebay_data = cache.get(ean, {})
        min_price = ebay_data.get("min_price", 0)
        if min_price > 0 and vk > min_price * 1.05:  # 5% Toleranz
            dbg["profit"] += 1
            continue

        # Bild vorhanden?
        art = artikeldaten.get(ean, {})
        enr = enrichment.get(ean, {})
        image = enr.get("image_main") or art.get("image") or ""
        if not image:
            dbg["no_image"] += 1
            continue

        # Titel
        title = enr.get("title_seo") or art.get("title") or prod.get("name", "")
        if not title:
            dbg["no_title"] += 1
            continue

        ebay = cache.get(ean, {})
        score = score_product(prod, ebay, vk)

        candidates.append({
            "ean": ean,
            "sku": prod["sku"],
            "name": title,
            "supplier": prod["supplier"],
            "supplier_email": prod["supplier_email"],
            "ek": ek,
            "vk": vk,
            "profit": profit,
            "watch_count": ebay.get("watch_count", 0),
            "competitor_count": ebay.get("competitor_count", 0),
            "image": image,
            "score": score,
        })

    # Sortieren nach Score
    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(
        f"Filter: {dbg['listed']} bereits gelistet | "
        f"{dbg['profit']} Gewinn zu gering | "
        f"{dbg['no_image']} kein Bild | "
        f"{dbg['no_title']} kein Titel"
    )
    log.info(f"Kandidaten: {len(candidates)} → Top {min(top_n, len(candidates))} ausgewählt")
    return candidates[:top_n]


# ---------------------------------------------------------------------------
# Supplier Map
# ---------------------------------------------------------------------------
def load_supplier_map(path: str = SUPPLIER_MAP) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_supplier_map(smap: dict, path: str = SUPPLIER_MAP):
    Path(path).write_text(json.dumps(smap, indent=2, ensure_ascii=False), encoding="utf-8")


def update_supplier_map(selected: list[dict], smap: dict) -> dict:
    """Schreibt/aktualisiert supplier_map für alle ausgewählten Artikel."""
    now = datetime.now(timezone.utc).isoformat()
    for prod in selected:
        sku = prod["sku"]
        smap[sku] = {
            "ean": prod["ean"],
            "supplier": prod["supplier"],
            "supplier_email": prod["supplier_email"],
            "ek": prod["ek"],
            "vk": prod["vk"],
            "listed_at": smap.get(sku, {}).get("listed_at", now),
            "updated_at": now,
        }
    return smap


# ---------------------------------------------------------------------------
# Bereits gelistete Artikel ermitteln
# ---------------------------------------------------------------------------
def load_already_listed(enrichment_path: str = ENRICHMENT) -> set[str]:
    """EANs die bereits in enrichment_index sind (= schon gelistet oder bekannt)."""
    eans = set()
    if not Path(enrichment_path).exists():
        return eans
    with open(enrichment_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ean = row.get("ean", "").strip()
            if ean:
                eans.add(ean)
    return eans


# ---------------------------------------------------------------------------
# Haupt-Logik
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Smart Lister — Wettbewerbs-basierter Artikel-Auswähler")
    parser.add_argument("--scan",     action="store_true", help="eBay Score-Cache aktualisieren")
    parser.add_argument("--select",   action="store_true", help="Top-Artikel auswählen + supplier_map schreiben")
    parser.add_argument("--dry-run",  action="store_true", help="Nichts schreiben, nur anzeigen")
    parser.add_argument("--top",      type=int, default=TOP_SHOP_LIMIT, help=f"Anzahl Artikel (Standard: {TOP_SHOP_LIMIT})")
    parser.add_argument("--limit",    type=int, default=DAILY_API_LIMIT, help=f"Max API-Calls pro Run (Standard: {DAILY_API_LIMIT})")
    parser.add_argument("--list",     action="store_true", help="Ausgewählte Artikel auf eBay listen (liest supplier_map.json)")
    parser.add_argument("--config",   default=CONFIG_FILE)
    args = parser.parse_args()

    if not args.scan and not args.select and not args.list:
        parser.print_help()
        sys.exit(0)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))

    client_id     = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")

    # Daten laden
    log.info("=== Kataloge einlesen ===")
    bab      = load_bab()
    kosatec  = load_kosatec()
    catalog  = merge_catalogs(bab, kosatec)
    artdata  = load_artikeldaten()
    enrich   = load_enrichment()
    listed   = load_already_listed()

    log.info(
        f"Katalog: {len(catalog)} EANs | "
        f"Artikeldaten: {len(artdata)} | "
        f"Enrichment: {len(enrich)} | "
        f"Bereits bekannt: {len(listed)}"
    )

    # Score-Cache aktualisieren
    cache = load_score_cache()

    # Graceful shutdown: Cache bei SIGTERM/SIGINT speichern
    def _save_and_exit(signum, frame):
        log.warning(f"Signal {signum} empfangen — Cache speichern und beenden...")
        save_score_cache(cache)
        log.info(f"Cache gespeichert ({len(cache)} EANs). Beende.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _save_and_exit)
    signal.signal(signal.SIGINT, _save_and_exit)

    if args.scan:
        log.info("=== eBay Score-Cache aktualisieren ===")
        if not client_id or not client_secret:
            log.error("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET fehlen")
            sys.exit(1)
        all_eans = list(catalog.keys())
        cache = update_score_cache(all_eans, cache, client_id, client_secret, max_calls=args.limit)

    # Top-Artikel auswählen
    if args.select:
        log.info("=== Top-Artikel auswählen ===")

        # --- Nicht mehr verfügbare Artikel deaktivieren ---
        smap_existing = load_supplier_map()
        if smap_existing and (client_id and client_secret):
            log.info("Prüfe supplier_map auf nicht mehr verfügbare Artikel...")
            from ebay_client import EbayClient
            try:
                ebay_cfg = cfg.get("ebay", {})
                ebay_client_inst = EbayClient.from_env(ebay_cfg)
                offlined = 0
                for sku, entry in list(smap_existing.items()):
                    ean = entry.get("ean", "")
                    if ean not in catalog:
                        # Nicht mehr im Katalog (out-of-stock oder nicht mehr verfügbar)
                        log.warning(f"  OFFLINE: SKU {sku} EAN {ean} nicht mehr verfügbar → deaktivieren")
                        if not args.dry_run:
                            try:
                                ebay_client_inst.set_inventory(sku, 0)
                                offer = ebay_client_inst.get_offer_for_sku(sku)
                                if offer:
                                    ebay_client_inst.withdraw_offer(offer["offerId"])
                                del smap_existing[sku]
                                offlined += 1
                            except Exception as e:
                                log.error(f"  Fehler beim Deaktivieren SKU {sku}: {e}")
                if offlined:
                    save_supplier_map(smap_existing)
                    log.info(f"  {offlined} Artikel deaktiviert + aus supplier_map entfernt")
                else:
                    log.info("  Alle gelisteten Artikel noch verfügbar ✓")
            except Exception as e:
                log.warning(f"  Deaktivierungs-Check übersprungen: {e}")

        selected = select_top_products(catalog, cache, artdata, enrich, listed, top_n=args.top)

        if not selected:
            log.warning("Keine Kandidaten gefunden — Cache leer? Zuerst --scan ausführen.")
            sys.exit(0)

        # Supplier Map aktualisieren
        smap = load_supplier_map()
        smap = update_supplier_map(selected, smap)

        if not args.dry_run:
            save_supplier_map(smap)
            log.info(f"supplier_map.json: {len(smap)} Einträge gespeichert")

        # Ausgabe
        log.info(f"\n{'='*70}")
        log.info(f"TOP {len(selected)} ARTIKEL (Score-Erklärung: Watch=Nachfrage, Komp=Konkurrenz, Preis=günstiger als Markt):")
        log.info(f"{'#':<4} {'EAN':<15} {'EK':>7} {'VK':>7} {'Profit':>7} {'Score':>6} {'Watch':>5} {'Komp':>5} {'Grund'}")
        log.info("-" * 80)
        for i, p in enumerate(selected[:50], 1):
            # Score-Begründung
            reasons = []
            if p["watch_count"] >= 20:
                reasons.append(f"🔥{p['watch_count']} Watches")
            elif p["watch_count"] > 0:
                reasons.append(f"{p['watch_count']} Watches")
            if p["competitor_count"] <= 3 and p["competitor_count"] > 0:
                reasons.append(f"wenig Konkurrenz ({p['competitor_count']})")
            elif p["competitor_count"] == 0:
                reasons.append("kein Cache (neutral)")
            if p["profit"] >= 30:
                reasons.append(f"💰 {p['profit']:.0f}€ Gewinn")
            reason_str = " | ".join(reasons) if reasons else "Standardauswahl"
            log.info(
                f"{i:<4} {p['ean']:<15} {p['ek']:>7.2f} {p['vk']:>7.2f} "
                f"{p['profit']:>7.2f} {p['score']:>6.1f} {p['watch_count']:>5} "
                f"{p['competitor_count']:>5}  {p['name'][:30]} | {reason_str}"
            )
        if len(selected) > 50:
            log.info(f"  ... und {len(selected)-50} weitere")

        # Zusammenfassung
        bab_count = sum(1 for p in selected if p["supplier"] == "BAB")
        kos_count = sum(1 for p in selected if p["supplier"] == "Kosatec")
        avg_profit = sum(p["profit"] for p in selected) / len(selected) if selected else 0
        log.info(f"\n{'='*60}")
        log.info(f"Zusammenfassung:")
        log.info(f"  BAB:      {bab_count} Artikel")
        log.info(f"  Kosatec:  {kos_count} Artikel")
        log.info(f"  Ø Gewinn: {avg_profit:.2f}€")
        log.info(f"  Gesamt:   {len(selected)} Artikel bereit zum Listen")

        if args.dry_run:
            log.warning("DRY-RUN — supplier_map.json nicht gespeichert")

    # Artikel listen
    if args.list:
        log.info("=== Artikel auf eBay listen ===")
        # Aus supplier_map.json lesen (bereits durch --select gefüllt)
        smap = load_supplier_map()
        if not smap:
            log.error("supplier_map.json leer — zuerst --select ausführen")
        else:
            # Supplier-Map in selected-Format konvertieren
            to_list = []
            for sku, entry in smap.items():
                ean = entry.get("ean", "")
                prod = catalog.get(ean, {})
                art  = artdata.get(ean, {})
                enr  = enrich.get(ean, {})
                image = enr.get("image_main") or art.get("image") or ""
                title = enr.get("title_seo") or art.get("title") or prod.get("name", sku)
                to_list.append({
                    "sku":      sku,
                    "ean":      ean,
                    "name":     title,
                    "supplier": entry.get("supplier", ""),
                    "vk":       entry.get("vk", 0.0),
                    "image":    image,
                })
            # Auf top_n beschränken wenn --select + --list zusammen läuft
            if len(to_list) > args.top:
                to_list = to_list[:args.top]
            log.info(f"Artikel zum Listen: {len(to_list)}")
            list_products(to_list, cfg, dry_run=args.dry_run)

    log.info("=== Fertig ===")


# ---------------------------------------------------------------------------
# --list: Ausgewählte Artikel auf eBay listen
# ---------------------------------------------------------------------------
def list_products(selected: list[dict], cfg: dict, dry_run: bool = False) -> dict:
    """
    Listet alle Artikel aus selected[] auf eBay via Inventory API.
    Nutzt ebay_client.upsert_product() (Inventory + Offer + Publish).
    Gibt {"ok": N, "skip": N, "error": N} zurück.
    """
    from ebay_client import EbayClient

    ebay_cfg = cfg.get("ebay", {})
    if not ebay_cfg.get("enabled", False):
        log.error("eBay nicht aktiviert in config — abgebrochen")
        return {"ok": 0, "skip": 0, "error": 0}

    try:
        client = EbayClient.from_env(ebay_cfg)
    except Exception as e:
        log.error(f"eBay Client Fehler: {e}")
        return {"ok": 0, "skip": 0, "error": 0}

    stats = {"ok": 0, "skip": 0, "error": 0}
    total = len(selected)

    for i, prod in enumerate(selected, 1):
        sku  = prod["sku"]
        ean  = prod["ean"]
        name = prod["name"]
        vk   = prod["vk"]

        # Produkt-Dict für ebay_client aufbauen
        product = {
            "sku":         sku,
            "ean":         ean,
            "title":       name,
            "description": name,  # Basis-Beschreibung — später Anthropic API
            "category":    "",    # ebay_client bestimmt per Taxonomy-API
            "stock":       1,     # Dropshipping: immer 1 verfügbar
            "image_url":   prod.get("image", ""),
            "image_urls":  [prod["image"]] if prod.get("image") else [],
            "condition":   "NEW",
        }

        log.info(f"[{i}/{total}] Liste: SKU {sku} | {name[:50]} | VK {vk:.2f}€")

        if dry_run:
            log.warning(f"  DRY-RUN — würde listen: {sku}")
            stats["ok"] += 1
            continue

        try:
            result = client.upsert_product(product, vk)
            listing_id = result.get("listing_id", "")
            if listing_id:
                log.info(f"  ✓ Gelistet: listingId {listing_id}")
                stats["ok"] += 1
            else:
                log.warning(f"  ⚠ Kein listingId (Draft?): SKU {sku}")
                stats["skip"] += 1
        except Exception as e:
            log.error(f"  ✗ Fehler SKU {sku}: {e}")
            stats["error"] += 1

        # Kurze Pause um Rate-Limits zu vermeiden
        time.sleep(0.3)

    log.info(f"=== Listing abgeschlossen: {stats['ok']} OK | {stats['skip']} Drafts | {stats['error']} Fehler ===")
    return stats


if __name__ == "__main__":
    main()
