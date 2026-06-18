"""
fetch_ebay_data.py — eBay Bestellungen + Retouren → docs/orders.json + docs/returns.json
==========================================================================================
Aufruf:
  python fetch_ebay_data.py              # letzte 90 Tage
  python fetch_ebay_data.py --days 30    # letzte 30 Tage
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("fetch_ebay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

EBAY_AUTH_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_ORDER_URL = "https://api.ebay.com/sell/fulfillment/v1/order"
EBAY_RETURN_URL = "https://api.ebay.com/post-order/v2/return"

OUT_ORDERS  = Path("docs/orders.json")
OUT_RETURNS = Path("docs/returns.json")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    client_id     = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]
    refresh_token = os.environ.get("EBAY_REFRESH_TOKEN_2") or os.environ.get("EBAY_REFRESH_TOKEN", "")
    if not refresh_token:
        raise ValueError("EBAY_REFRESH_TOKEN_2 oder EBAY_REFRESH_TOKEN fehlt")

    resp = requests.post(
        EBAY_AUTH_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account https://api.ebay.com/oauth/api_scope/sell.fulfillment"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Bestellungen (Fulfillment API)
# ---------------------------------------------------------------------------

def fetch_orders(token: str, days: int = 90) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    orders = []
    offset = 0
    limit  = 50

    while True:
        resp = requests.get(
            EBAY_ORDER_URL,
            headers=headers,
            params={"filter": f"creationdate:[{since}..]", "limit": limit, "offset": offset},
            timeout=20,
        )
        if not resp.ok:
            log.error(f"Orders API Fehler {resp.status_code}: {resp.text[:300]}")
            break

        data  = resp.json()
        items = data.get("orders", [])
        orders.extend(items)
        log.info(f"  Orders geladen: {len(orders)}/{data.get('total', '?')}")

        if len(orders) >= data.get("total", 0) or not items:
            break
        offset += limit

    return orders


def normalize_order(raw: dict) -> dict:
    """eBay Order → einfaches Dict für Dashboard."""
    items = raw.get("lineItems", [])
    first = items[0] if items else {}
    buyer = raw.get("buyer", {})

    # VK aus pricingSummary
    pricing = raw.get("pricingSummary", {})
    vk = float(pricing.get("total", {}).get("value", 0))

    # EK aus supplier_map (wird im Dashboard ergänzt)
    sku  = first.get("sku", "")
    name = first.get("title", "")

    created = raw.get("creationDate", "")[:10]

    status_raw = raw.get("orderFulfillmentStatus", "")
    if status_raw in ("FULFILLED", "FULLY_SHIPPED"):
        status = "done"
    else:
        status = "open"

    return {
        "id":     raw.get("orderId", ""),
        "nr":     raw.get("legacyOrderId", raw.get("orderId", "")),
        "date":   created,
        "buyer":  buyer.get("username", ""),
        "sku":    sku,
        "name":   name,
        "vk":     round(vk, 2),
        "ek":     0,   # wird vom Dashboard via supplier_map ergänzt
        "note":   "",
        "status": status,
        "tracking": _get_tracking(raw),
        "items_count": len(items),
    }


def _get_tracking(raw: dict) -> str:
    for ship in raw.get("fulfillmentStartInstructions", []):
        for pkg in ship.get("shippingStep", {}).get("shipTo", {}).get("contactAddress", {}).values():
            pass
    for item in raw.get("lineItems", []):
        for ref in item.get("lineItemFulfillmentInstructions", {}).get("minEstimatedDeliveryDate", ""):
            pass
    # Tracking aus paymentSummary nicht verfügbar — nutze separate Fulfillment-Abfrage
    return ""


# ---------------------------------------------------------------------------
# Retouren (Post-Order API)
# ---------------------------------------------------------------------------

def fetch_returns(token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_DE"}
    returns = []
    offset  = 0
    limit   = 50

    while True:
        resp = requests.get(
            EBAY_RETURN_URL,
            headers=headers,
            params={"role": "SELLER", "limit": limit, "offset": offset},
            timeout=20,
        )
        if not resp.ok:
            log.warning(f"Returns API {resp.status_code}: {resp.text[:200]}")
            break

        data  = resp.json()
        items = data.get("returns", [])
        returns.extend(items)
        log.info(f"  Returns geladen: {len(returns)}")

        if len(items) < limit:
            break
        offset += limit

    return returns


def normalize_return(raw: dict) -> dict:
    rma_id  = raw.get("returnId", "")
    state   = raw.get("returnStatus", {}).get("state", "")
    reason  = raw.get("returnReason", "")
    item    = raw.get("item", {})
    order_id = raw.get("legacyOrderId", "")
    created = raw.get("creationInfo", {}).get("creationDate", {}).get("value", "")[:10]
    vk      = float(raw.get("sellerResponsibility", {}).get("returnShippingCost", {}).get("amount", {}).get("value", 0))

    status_map = {
        "RETURN_REQUESTED": "angemeldet",
        "RETURN_SHIPPED":   "unterwegs",
        "RETURN_DELIVERED": "angekommen",
        "REFUND_ISSUED":    "erstattet",
        "CLOSED":           "geschlossen",
    }

    return {
        "id":       rma_id,
        "order_nr": order_id,
        "date":     created,
        "sku":      item.get("itemId", ""),
        "name":     item.get("title", ""),
        "reason":   reason,
        "status":   status_map.get(state, state),
        "vk":       round(vk, 2),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    log.info("eBay Token holen …")
    token = get_access_token()
    log.info("✓ Token OK")

    # Bestellungen
    log.info(f"Lade Bestellungen (letzte {args.days} Tage) …")
    raw_orders = fetch_orders(token, args.days)
    orders = [normalize_order(o) for o in raw_orders]
    orders.sort(key=lambda o: o["date"], reverse=True)
    OUT_ORDERS.parent.mkdir(exist_ok=True)
    OUT_ORDERS.write_text(json.dumps({"updated": datetime.now(timezone.utc).isoformat(),
                                       "orders": orders}, ensure_ascii=False, indent=2))
    log.info(f"✓ {len(orders)} Bestellungen → {OUT_ORDERS}")

    # Retouren
    log.info("Lade Retouren …")
    raw_returns = fetch_returns(token)
    returns = [normalize_return(r) for r in raw_returns]
    returns.sort(key=lambda r: r["date"], reverse=True)
    OUT_RETURNS.write_text(json.dumps({"updated": datetime.now(timezone.utc).isoformat(),
                                        "returns": returns}, ensure_ascii=False, indent=2))
    log.info(f"✓ {len(returns)} Retouren → {OUT_RETURNS}")

    log.info("=== Fertig ===")


if __name__ == "__main__":
    main()
