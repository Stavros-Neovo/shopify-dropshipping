"""
feedback_mailer.py
==================
Automatisches Feedback-System:
  1. Holt abgeschlossene eBay-Bestellungen (FULFILLED, 7-14 Tage alt)
  2. Hinterlässt positive Käuferbewertung via eBay Feedback API
  3. Schickt freundliche Nachricht via eBay Post-Order API (Feedback-Bitte)
  4. Merkt sich bereits bearbeitete Orders in feedback_state.json

Aufruf:
  python feedback_mailer.py
  python feedback_mailer.py --dry-run
  python feedback_mailer.py --days-min 5 --days-max 21

Voraussetzungen (.env / GitHub Secrets):
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN_2
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient

log = logging.getLogger("feedback_mailer")

FULFILLMENT_PATH = "/sell/fulfillment/v1/order"
FEEDBACK_PATH    = "/sell/feedback/v1/feedback"
POSTORDER_PATH   = "/post-order/v2/inquiry"
MSG_PATH         = "/post-order/v2/cancellation"   # Fallback

STATE_FILE       = "feedback_state.json"
CONFIG_FILE      = "config_shop2.yaml"

# ---------------------------------------------------------------------------
# Nachrichten-Template
# ---------------------------------------------------------------------------
FEEDBACK_REQUEST_DE = """\
Hallo {buyer_name},

vielen Dank für Ihre Bestellung! Wir hoffen, Sie sind mit Ihrem Artikel zufrieden.

Falls Sie einen Moment Zeit haben, würden wir uns sehr über eine Bewertung freuen — das hilft uns, unseren Service weiter zu verbessern.

Bei Fragen oder Problemen stehen wir Ihnen jederzeit zur Verfügung.

Herzliche Grüße,
Ihr Neovogen Shop-Team
"""

# ---------------------------------------------------------------------------
# State: bereits verarbeitete Orders
# ---------------------------------------------------------------------------
def load_state(path: str = STATE_FILE) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict, path: str = STATE_FILE):
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# eBay: Abgeschlossene Bestellungen abrufen
# ---------------------------------------------------------------------------
def fetch_fulfilled_orders(client: EbayClient, days_min: int = 7, days_max: int = 14) -> list[dict]:
    """Holt FULFILLED Orders die zwischen days_min und days_max alt sind."""
    now = datetime.now(timezone.utc)
    cutoff_old  = now - timedelta(days=days_max)
    cutoff_new  = now - timedelta(days=days_min)

    results = []
    offset = 0
    limit  = 50

    while True:
        try:
            data = client._request(
                "GET", FULFILLMENT_PATH,
                params={
                    "limit":  limit,
                    "offset": offset,
                    "filter": "orderFulfillmentStatus:{FULFILLED}",
                }
            )
        except Exception as e:
            log.error(f"Bestellungen abrufen fehlgeschlagen: {e}")
            break

        orders = (data or {}).get("orders", [])
        if not orders:
            break

        for order in orders:
            creation_date = order.get("creationDate", "")
            try:
                order_dt = datetime.fromisoformat(creation_date.replace("Z", "+00:00"))
            except Exception:
                continue

            # Nur Orders im Zeitfenster
            if cutoff_old <= order_dt <= cutoff_new:
                results.append(order)

        # Pagination
        total = (data or {}).get("total", 0)
        offset += limit
        if offset >= total or offset >= 200:  # Max 200 Orders
            break

    log.info(f"Gefundene Orders im Zeitfenster ({days_min}-{days_max} Tage): {len(results)}")
    return results


# ---------------------------------------------------------------------------
# eBay: Positive Käuferbewertung hinterlassen
# ---------------------------------------------------------------------------
def leave_buyer_feedback(client: EbayClient, order_id: str, buyer_username: str, dry_run: bool = False) -> bool:
    """Hinterlässt positive Bewertung für den Käufer."""
    payload = {
        "itemId":          "",           # wird aus orderId abgeleitet
        "transactionId":   "",
        "orderId":         order_id,
        "recipientUserId": buyer_username,
        "commentText":     "Reibungsloser Kauf, schnelle Zahlung. Sehr gerne wieder!",
        "commentType":     "POSITIVE",
        "role":            "BUYER",
    }

    log.info(f"Käuferbewertung für {buyer_username} (Order {order_id})")

    if dry_run:
        log.warning(f"  DRY-RUN — würde Bewertung hinterlassen: {buyer_username}")
        return True

    try:
        client._request("POST", FEEDBACK_PATH, json_body=payload)
        log.info(f"  ✓ Bewertung hinterlassen: {buyer_username}")
        return True
    except Exception as e:
        err = str(e)
        if "already left" in err.lower() or "55003" in err or "55007" in err:
            log.info(f"  ℹ Bewertung bereits vorhanden für {buyer_username}")
            return True  # Kein Fehler — schon bewertet
        log.warning(f"  ⚠ Bewertung fehlgeschlagen für {buyer_username}: {e}")
        return False


# ---------------------------------------------------------------------------
# eBay: Nachricht an Käufer senden (Post-Order Messaging)
# ---------------------------------------------------------------------------
def send_feedback_request(client: EbayClient, order: dict, dry_run: bool = False) -> bool:
    """Sendet freundliche Feedback-Bitte via eBay-Nachrichtensystem."""
    order_id = order.get("orderId", "")
    buyer    = order.get("buyer", {})
    buyer_username = buyer.get("username", "")
    buyer_name     = buyer.get("buyerRegistrationAddress", {}).get("fullName", "") or "Kunde"

    # Nur Vorname verwenden
    first_name = buyer_name.split()[0] if buyer_name else "Kunde"

    message_text = FEEDBACK_REQUEST_DE.format(buyer_name=first_name)

    # eBay Post-Order Messaging API
    payload = {
        "recipientUsername": buyer_username,
        "subject":           "Wie war Ihre Erfahrung? 😊",
        "message":           message_text,
        "orderId":           order_id,
    }

    log.info(f"Feedback-Bitte an {buyer_username} (Order {order_id})")

    if dry_run:
        log.warning(f"  DRY-RUN — würde senden an: {buyer_username}")
        return True

    try:
        # eBay Sell Messaging API (Post-Order)
        client._request(
            "POST",
            f"/sell/messaging/v1/message",
            json_body={
                "recipients": [{"username": buyer_username}],
                "subject":    "Wie war Ihre Erfahrung? 😊",
                "body":       message_text,
                "orderId":    order_id,
            }
        )
        log.info(f"  ✓ Nachricht gesendet an: {buyer_username}")
        return True
    except Exception as e:
        log.warning(f"  ⚠ Nachricht fehlgeschlagen ({buyer_username}): {e}")
        return False


# ---------------------------------------------------------------------------
# Haupt-Logik
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Automatischer Feedback-Mailer")
    parser.add_argument("--dry-run",  action="store_true", help="Nichts senden, nur simulieren")
    parser.add_argument("--days-min", type=int, default=7,  help="Mindest-Alter der Bestellung in Tagen (Standard: 7)")
    parser.add_argument("--days-max", type=int, default=14, help="Maximales Alter der Bestellung in Tagen (Standard: 14)")
    parser.add_argument("--config",   default=CONFIG_FILE)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    ebay_cfg = cfg.get("ebay", {})

    if not ebay_cfg.get("enabled", False):
        log.info("eBay nicht aktiviert — nichts zu tun")
        return

    try:
        client = EbayClient.from_env(ebay_cfg)
    except Exception as e:
        log.error(f"eBay Client Fehler: {e}")
        return

    state = load_state()
    orders = fetch_fulfilled_orders(client, days_min=args.days_min, days_max=args.days_max)

    stats = {"feedback": 0, "message": 0, "skip": 0, "error": 0}

    for order in orders:
        order_id = order.get("orderId", "")
        buyer_username = order.get("buyer", {}).get("username", "")

        if not order_id or not buyer_username:
            stats["skip"] += 1
            continue

        # Bereits verarbeitet?
        if state.get(order_id, {}).get("done"):
            log.debug(f"Order {order_id} bereits verarbeitet — übersprungen")
            stats["skip"] += 1
            continue

        log.info(f"Verarbeite Order {order_id} | Käufer: {buyer_username}")

        # 1. Positive Käuferbewertung hinterlassen
        fb_ok = leave_buyer_feedback(client, order_id, buyer_username, dry_run=args.dry_run)
        if fb_ok:
            stats["feedback"] += 1

        # 2. Feedback-Bitte senden
        msg_ok = send_feedback_request(client, order, dry_run=args.dry_run)
        if msg_ok:
            stats["message"] += 1
        else:
            stats["error"] += 1

        # State merken
        state[order_id] = {
            "buyer":      buyer_username,
            "done":       True,
            "processed":  datetime.now(timezone.utc).isoformat(),
            "dry_run":    args.dry_run,
        }

    if not args.dry_run:
        save_state(state)

    log.info(
        f"=== Fertig: {stats['feedback']} Bewertungen | "
        f"{stats['message']} Nachrichten | "
        f"{stats['skip']} übersprungen | "
        f"{stats['error']} Fehler ==="
    )


if __name__ == "__main__":
    main()
