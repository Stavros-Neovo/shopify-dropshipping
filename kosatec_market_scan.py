"""
kosatec_market_scan.py
=======================
Scannt das GESAMTE Kosatec-Sortiment (abzueglich bereits bei uns gelisteter
EANs) gegen echte eBay-Mitbewerberpreise. Checkpointed/fortsetzbar, da das
volle Sortiment (~69k EANs) das Browse-API-Tageslimit klar uebersteigt.

Aufruf:
  python kosatec_market_scan.py              # naechste Charge abarbeiten
  python kosatec_market_scan.py --reset       # von vorne beginnen
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path

import yaml
from dotenv import load_dotenv
load_dotenv()

import requests

from ebay_client import EbayClient
from repricer import calc_vk, get_margin_tier, psychological_round, COMPETITOR_MIN_RATIO, BROWSE_PATH

CANDIDATES_FILE = "/tmp/kosatec_full_candidates.json"
STATE_FILE      = "kosatec_scan_state.json"
RESULTS_FILE    = "kosatec_scan_results.json"


class RateLimited(Exception):
    pass


def real_net_profit(vk_brutto: float, ek: float, cfg: dict) -> float:
    """Echter Gewinn nach eBay-Gebuehr, MwSt., Versand, Retouren-Ruecklage -
    calc_vk()/get_margin_tier() allein reicht NICHT als Gewinn-Check: bei sehr
    guenstigen Artikeln (EK<10€) frisst die feste Versandpauschale die Marge
    komplett auf, ohne dass der Prozent-Floor das abfaengt (das passiert nur
    innerhalb von repricer.py::reprice_product, nicht in calc_vk() selbst)."""
    ep = cfg["ebay_pricing"]
    total_fee = ep.get("ebay_fee_rate", 0.13) + ep.get("campaign_fee_rate", 0.0)
    vat = ep.get("vat_rate", 0.19)
    ship = ep.get("shipping_cost_eur", 5.0)
    return_reserve = ek * ep.get("return_reserve_rate", 0.0)
    netto_vk = vk_brutto * (1 - total_fee) / (1 + vat)
    return netto_vk - ek - ship - return_reserve


def get_competitor_prices_strict(ean: str, app_token: str, base_url: str,
                                  my_price: float, floor_price: float) -> list[float]:
    """Wie repricer.py::get_competitor_prices, aber wirft bei 429 statt leise [] zurueckzugeben -
    sonst wuerde ein Rate-Limit als 'keine Konkurrenz gefunden' fehlinterpretiert."""
    r = requests.get(
        f"{base_url}{BROWSE_PATH}",
        headers={"Authorization": f"Bearer {app_token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE", "Accept": "application/json"},
        params={"q": ean, "filter": "buyingOptions:{FIXED_PRICE}", "sort": "price", "limit": "25"},
        timeout=12,
    )
    if r.status_code == 429:
        raise RateLimited(f"429 Too Many Requests: {r.text[:150]}")
    if r.status_code != 200:
        return []
    items = r.json().get("itemSummaries", [])
    sanity_floor = floor_price * COMPETITOR_MIN_RATIO
    prices = []
    for item in items:
        try:
            price = float(item.get("price", {}).get("value", 0))
        except (TypeError, ValueError):
            continue
        if price <= 0 or price < sanity_floor or abs(price - my_price) < 0.03:
            continue
        prices.append(price)
    return sorted(prices)


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        return json.loads(Path(STATE_FILE).read_text())
    return {"offset": 0, "checked": 0, "rate_limited_at": None}


def save_state(state: dict):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


def load_results() -> list:
    if Path(RESULTS_FILE).exists():
        return json.loads(Path(RESULTS_FILE).read_text())
    return []


def save_results(results: list):
    Path(RESULTS_FILE).write_text(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    if args.reset:
        Path(STATE_FILE).unlink(missing_ok=True)
        Path(RESULTS_FILE).unlink(missing_ok=True)
        print("Zurueckgesetzt.")

    candidates = json.loads(Path(CANDIDATES_FILE).read_text())
    state = load_state()
    results = load_results()

    cfg = yaml.safe_load(open("config_shop2.yaml"))
    client = EbayClient.from_env(cfg.get("ebay", {}))
    app_token = client._get_app_token()
    base_url = "https://api.ebay.com"

    offset = state["offset"]
    total = len(candidates)
    print(f"Fortsetzen ab Position {offset}/{total} (bereits {len(results)} Treffer mit Konkurrenzdaten gefunden)", flush=True)

    checked_this_run = 0
    rate_limited = False
    i = offset
    while i < total:
        c = candidates[i]
        try:
            floor_m, target_m = get_margin_tier(c["hek"], cfg)
            floor_vk = psychological_round(calc_vk(c["hek"], floor_m, cfg))
            target_vk = psychological_round(calc_vk(c["hek"], target_m, cfg))

            comp_prices = get_competitor_prices_strict(c["ean"], app_token, base_url, target_vk, floor_vk)

            if len(comp_prices) >= 2:
                lowest = comp_prices[0]
                min_margin = cfg["ebay_pricing"].get("min_margin_eur", 5.0)
                undercut_price = psychological_round(min(lowest - 1.01, target_vk))
                # Bei sehr guenstigen Artikeln reicht der Prozent-Floor nicht -
                # echten absoluten Gewinn pruefen, ggf. Preis bis knapp unter
                # den Mitbewerber anheben, sonst gilt es nicht als Treffer
                profit = real_net_profit(undercut_price, c["hek"], cfg)
                while profit < min_margin and undercut_price < lowest - 1.01:
                    undercut_price = psychological_round(undercut_price + 1.0)
                    profit = real_net_profit(undercut_price, c["hek"], cfg)
                would_win = undercut_price <= lowest - 0.99 and profit >= min_margin
                results.append({
                    "artnr": c["artnr"], "ean": c["ean"], "name": c["name"], "hersteller": c["hersteller"],
                    "hek": c["hek"], "menge": c["menge"], "kat1": c["kat1"], "kat2": c["kat2"],
                    "floor_vk": floor_vk, "target_vk": target_vk,
                    "lowest_competitor": lowest, "competitor_count": len(comp_prices),
                    "would_win": would_win, "undercut_price": undercut_price, "real_profit": round(profit, 2),
                })
                if would_win:
                    print(f"  GEWINNBAR: {c['hersteller']} {c['name'][:40]} EK={c['hek']:.2f} VK={undercut_price:.2f} Mitbewerber={lowest:.2f} Gewinn={profit:.2f}", flush=True)
        except RateLimited as e:
            print(f"RATE LIMIT erreicht bei Position {i} ({c['artnr']}): {e}", flush=True)
            rate_limited = True
            break
        except Exception as e:
            print(f"  Fehler {c['artnr']}: {str(e)[:100]}", flush=True)

        checked_this_run += 1
        i += 1
        if checked_this_run % 100 == 0:
            print(f"  {i}/{total} geprueft ({checked_this_run} in diesem Lauf, {len(results)} mit Konkurrenzdaten)...", flush=True)
            state["offset"] = i
            state["checked"] = state.get("checked", 0) + checked_this_run
            save_state(state)
            save_results(results)
            checked_this_run = 0
        time.sleep(0.25)

    state["offset"] = i
    state["checked"] = state.get("checked", 0) + checked_this_run
    if rate_limited:
        state["rate_limited_at"] = i
    save_state(state)
    save_results(results)

    winners = [r for r in results if r["would_win"]]
    print(f"\nLAUF BEENDET (Position {i}/{total}, rate_limited={rate_limited})", flush=True)
    print(f"Gesamt mit Konkurrenzdaten: {len(results)} | Konkurrenzfaehig: {len(winners)}", flush=True)


if __name__ == "__main__":
    main()
