"""
upload_tracking.py — Tracking aus BAB-Mails automatisch zu eBay hochladen
=========================================================================
Liest unverarbeitete "Abliefernachweis"-Mails von BAB aus Gmail,
extrahiert Tracking-Nummer + eBay-Bestell-Nr. und lädt sie direkt
via eBay Sell Fulfillment API hoch.

Ablauf:
  1. Gmail: Suche nach ungelesenen BAB Abliefernachweis-Mails
  2. Subject parsen: Tracking (DL...) + Externe Belegnummer (eBay Order ID)
  3. eBay Fulfillment API: GET Order → POST shipping_fulfillment
  4. Gmail: Thread als gelesen markieren (verhindert Doppelverarbeitung)
  5. Ergebnis in tracking_result.json speichern

Aufruf:
  python upload_tracking.py
  python upload_tracking.py --dry-run      # Nur anzeigen, nicht hochladen
  python upload_tracking.py --days 14      # Mails der letzten 14 Tage (Standard: 7)

Benötigte Secrets:
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN_2
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

log = logging.getLogger("upload_tracking")

# BAB verwendet DPD als Hauptdienstleister.
# DL-Nummern sind DPD-Lieferscheinnummern.
DEFAULT_CARRIER = "DPD"
CARRIER_MAP = {
    "DL": "DPD",   # DPD Lieferschein
    "JJ": "DHL",   # DHL Express
    "00": "DHL",   # DHL Standard
    "1Z": "UPS",
}

RESULT_FILE = "tracking_result.json"


# ---------------------------------------------------------------------------
# eBay Token
# ---------------------------------------------------------------------------
def get_ebay_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": (
                "https://api.ebay.com/oauth/api_scope/sell.fulfillment "
                "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
            ),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Gmail Token
# ---------------------------------------------------------------------------
def get_gmail_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Gmail: Abliefernachweis-Mails suchen
# ---------------------------------------------------------------------------
def search_gmail_threads(token: str, days: int = 7) -> list[dict]:
    """Sucht nach ungelesenen BAB Abliefernachweis-Mails."""
    query = f'from:noreply@bab-distribution.de subject:"Abliefernachweis" is:unread newer_than:{days}d'
    resp = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/threads",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "maxResults": 50},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("threads", [])


def get_thread_subject(token: str, thread_id: str) -> str | None:
    """Holt Subject der ersten Message in einem Thread."""
    resp = requests.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"format": "metadata", "metadataHeaders": "Subject"},
        timeout=15,
    )
    if not resp.ok:
        return None
    messages = resp.json().get("messages", [])
    if not messages:
        return None
    headers = messages[0].get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == "subject":
            return h.get("value", "")
    return None


def mark_thread_read(token: str, thread_id: str):
    """Markiert Thread als gelesen (verhindert Doppelverarbeitung)."""
    requests.post(
        f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}/modify",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"removeLabelIds": ["UNREAD"]},
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Subject parsen
# ---------------------------------------------------------------------------
def parse_subject(subject: str) -> tuple[str | None, str | None]:
    """
    Parst BAB Abliefernachweis-Subject.

    Beispiel: "Ihr Abliefernachweis DL373320B vom DB1292341B / Externe Belegnummer: 23-14765-26114 / Kommission:"
    Returns: (tracking_nr, ebay_order_nr)
    """
    # Tracking-Nummer: nach "Abliefernachweis "
    tracking_match = re.search(r'Abliefernachweis\s+([A-Z0-9]+)', subject)
    tracking = tracking_match.group(1) if tracking_match else None

    # eBay-Bestell-Nr.: nach "Externe Belegnummer: "
    order_match = re.search(r'Externe Belegnummer:\s*([0-9\-]+)', subject)
    order_nr = order_match.group(1).strip() if order_match else None

    # Leere Bestell-Nr. ignorieren
    if order_nr and not re.match(r'\d{2}-\d{5}-\d{5}', order_nr):
        order_nr = None

    return tracking, order_nr


def detect_carrier(tracking: str) -> str:
    """Erkennt Carrier anhand Tracking-Präfix."""
    if not tracking:
        return DEFAULT_CARRIER
    prefix = tracking[:2].upper()
    return CARRIER_MAP.get(prefix, DEFAULT_CARRIER)


# ---------------------------------------------------------------------------
# eBay Fulfillment API
# ---------------------------------------------------------------------------
def get_ebay_order(token: str, order_id: str) -> dict | None:
    """Holt eBay-Bestellung und gibt OrderDetails zurück."""
    resp = requests.get(
        f"https://api.ebay.com/sell/fulfillment/v1/order/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code == 404:
        log.warning(f"  eBay Order {order_id} nicht gefunden (404)")
        return None
    if not resp.ok:
        log.error(f"  eBay Order GET Fehler: {resp.status_code} — {resp.text[:200]}")
        return None
    return resp.json()


def upload_tracking_to_ebay(
    token: str,
    order_id: str,
    tracking_nr: str,
    carrier: str,
    line_items: list[dict],
) -> bool:
    """
    Lädt Tracking-Nummer via eBay Sell Fulfillment API hoch.
    POST /sell/fulfillment/v1/order/{orderId}/shipping_fulfillment
    """
    shipped_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "lineItems": [
            {"lineItemId": li["lineItemId"], "quantity": li.get("quantity", 1)}
            for li in line_items
        ],
        "shippedDate":       shipped_date,
        "shippingCarrierCode": carrier,
        "trackingNumber":    tracking_nr,
    }

    log.info(f"  POST fulfillment: Order {order_id} | Tracking {tracking_nr} ({carrier})")

    resp = requests.post(
        f"https://api.ebay.com/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment",
        headers={
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json",
            "Content-Language": "de-DE",
        },
        json=payload,
        timeout=15,
    )

    if resp.status_code in (200, 201, 204):
        log.info(f"  ✓ Tracking hochgeladen: {tracking_nr} für Order {order_id}")
        return True

    log.error(f"  ✗ Fehler: {resp.status_code} — {resp.text[:400]}")
    return False


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------
def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="BAB Tracking → eBay Upload")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht hochladen")
    parser.add_argument("--days",    type=int, default=7,  help="Mails der letzten N Tage (Standard: 7)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Credentials
    gmail_client_id     = os.getenv("GMAIL_CLIENT_ID", "")
    gmail_client_secret = os.getenv("GMAIL_CLIENT_SECRET", "")
    gmail_refresh       = os.getenv("GMAIL_REFRESH_TOKEN", "")
    ebay_client_id      = os.getenv("EBAY_CLIENT_ID", "")
    ebay_client_secret  = os.getenv("EBAY_CLIENT_SECRET", "")
    ebay_refresh        = os.getenv("EBAY_REFRESH_TOKEN_2", "")

    if not gmail_refresh:
        log.error("GMAIL_REFRESH_TOKEN fehlt — siehe README für Gmail OAuth Setup")
        sys.exit(1)
    if not ebay_refresh:
        log.error("EBAY_REFRESH_TOKEN_2 fehlt")
        sys.exit(1)

    # Tokens holen
    log.info("=== Tokens holen ===")
    try:
        gmail_token = get_gmail_token(gmail_client_id, gmail_client_secret, gmail_refresh)
        log.info("  Gmail Token ✓")
    except Exception as e:
        log.error(f"Gmail Token Fehler: {e}")
        sys.exit(1)

    try:
        ebay_token = get_ebay_token(ebay_client_id, ebay_client_secret, ebay_refresh)
        log.info("  eBay Token ✓")
    except Exception as e:
        log.error(f"eBay Token Fehler: {e}")
        sys.exit(1)

    # Gmail durchsuchen
    log.info(f"=== Gmail: Abliefernachweis-Mails (letzte {args.days} Tage) ===")
    threads = search_gmail_threads(gmail_token, days=args.days)
    log.info(f"  {len(threads)} ungelesene Mails gefunden")

    if not threads:
        log.info("  Keine neuen Trackings — fertig")
        Path(RESULT_FILE).write_text(json.dumps({"processed": 0, "ok": 0, "skip": 0, "error": 0}))
        return

    results = []
    stats = {"processed": 0, "ok": 0, "skip": 0, "error": 0}

    for thread in threads:
        thread_id = thread["id"]
        subject = get_thread_subject(gmail_token, thread_id)
        if not subject:
            continue

        log.info(f"\nMail: {subject[:80]}")

        tracking_nr, order_nr = parse_subject(subject)

        if not tracking_nr:
            log.warning(f"  Kein Tracking im Subject — übersprungen")
            stats["skip"] += 1
            continue

        if not order_nr:
            log.warning(f"  Keine eBay-Bestell-Nr. im Subject (Externe Belegnummer leer) — übersprungen")
            stats["skip"] += 1
            continue

        carrier = detect_carrier(tracking_nr)
        log.info(f"  Tracking: {tracking_nr} ({carrier}) | eBay Order: {order_nr}")
        stats["processed"] += 1

        if args.dry_run:
            log.warning(f"  DRY-RUN — würde hochladen: {tracking_nr} für {order_nr}")
            results.append({"order_id": order_nr, "tracking": tracking_nr, "carrier": carrier, "status": "dry_run"})
            stats["ok"] += 1
            continue

        # eBay Order holen
        order = get_ebay_order(ebay_token, order_nr)
        if not order:
            stats["error"] += 1
            results.append({"order_id": order_nr, "tracking": tracking_nr, "status": "order_not_found"})
            continue

        # Prüfen ob schon ein Tracking vorhanden
        fulfillments = order.get("fulfillmentHrefs", []) or order.get("fulfillmentStartInstructions", [])
        existing_tracking = []
        for f in order.get("shippingFulfillments", []):
            existing_tracking.append(f.get("trackingNumber", ""))

        if tracking_nr in existing_tracking:
            log.info(f"  Tracking {tracking_nr} bereits auf eBay — übersprungen")
            mark_thread_read(gmail_token, thread_id)
            stats["skip"] += 1
            results.append({"order_id": order_nr, "tracking": tracking_nr, "status": "already_uploaded"})
            continue

        # Bestell-Status prüfen
        order_status = order.get("orderFulfillmentStatus", "")
        if order_status == "FULFILLED":
            log.info(f"  Order {order_nr} bereits FULFILLED — Tracking trotzdem hochladen")

        # Line Items extrahieren
        line_items = order.get("lineItems", [])
        if not line_items:
            log.error(f"  Keine Line Items in Order {order_nr}")
            stats["error"] += 1
            continue

        # Tracking hochladen
        ok = upload_tracking_to_ebay(ebay_token, order_nr, tracking_nr, carrier, line_items)

        if ok:
            mark_thread_read(gmail_token, thread_id)
            stats["ok"] += 1
            results.append({"order_id": order_nr, "tracking": tracking_nr, "carrier": carrier, "status": "uploaded"})
        else:
            stats["error"] += 1
            results.append({"order_id": order_nr, "tracking": tracking_nr, "status": "error"})

        time.sleep(0.5)

    # Ergebnis speichern
    output = {**stats, "results": results, "ran_at": datetime.now(timezone.utc).isoformat()}
    Path(RESULT_FILE).write_text(json.dumps(output, indent=2, ensure_ascii=False))

    log.info(
        f"\n=== Fertig: {stats['ok']} hochgeladen | "
        f"{stats['skip']} übersprungen | "
        f"{stats['error']} Fehler | "
        f"{stats['processed']} verarbeitet ==="
    )


if __name__ == "__main__":
    main()
