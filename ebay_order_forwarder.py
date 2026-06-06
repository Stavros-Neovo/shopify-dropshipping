"""
ebay_order_forwarder.py
=======================
Holt neue eBay-Bestellungen (Fulfillment API) und leitet sie per Email
an den Lieferanten (Sebastian / BAB Distribution) weiter.

Läuft stündlich via GitHub Actions — erkennt automatisch welche Bestellungen
bereits verarbeitet wurden (via processed_orders.json).

Aufruf:
  python ebay_order_forwarder.py                          # Shop 1
  python ebay_order_forwarder.py --config config_shop2.yaml  # Shop 2
  python ebay_order_forwarder.py --dry-run                # Test ohne Mail

Was es macht:
  1. eBay Fulfillment API: alle Bestellungen mit Status AWAITING_SHIPMENT holen
  2. Bereits verarbeitete Bestellungen überspringen (processed_orders.json)
  3. Email an BAB: SKU, Menge, Lieferadresse
  4. Bestellung als verarbeitet markieren
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient

log = logging.getLogger("ebay_order_forwarder")

FULFILLMENT_PATH = "/sell/fulfillment/v1/order"


# ---------------------------------------------------------------------------
# eBay Bestellungen holen
# ---------------------------------------------------------------------------
def fetch_new_orders(client: EbayClient, since_hours: int = 2) -> List[Dict]:
    """
    Holt eBay-Bestellungen der letzten N Stunden mit Status AWAITING_SHIPMENT.
    Gibt eine Liste von Order-Dicts zurück.
    """
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    all_orders = []
    offset = 0
    limit = 50

    while True:
        try:
            result = client._request(
                "GET",
                FULFILLMENT_PATH,
                params={
                    "filter": f"orderfulfillmentstatus:{{{{'UNFULFILLED','IN_PROGRESS'}}}}",
                    "limit": limit,
                    "offset": offset,
                },
            )
        except RuntimeError as e:
            log.error(f"Fehler beim Abrufen der Bestellungen: {e}")
            break

        orders = (result or {}).get("orders", [])
        all_orders.extend(orders)

        total = (result or {}).get("total", 0)
        offset += limit
        if offset >= total or not orders:
            break

    log.info(f"{len(all_orders)} eBay-Bestellungen abgerufen")
    return all_orders


# ---------------------------------------------------------------------------
# Email-Body bauen
# ---------------------------------------------------------------------------
def build_email(order: Dict[str, Any], shop_name: str = "eBay Shop") -> tuple[str, str]:
    """Erstellt Betreff und Text für die BAB-Bestellmail."""
    order_id = order.get("orderId", "?")
    order_date = order.get("creationDate", "")[:10]

    # Lieferadresse
    addr = {}
    fulfillments = order.get("fulfillmentStartInstructions", [])
    if fulfillments:
        ship_to = fulfillments[0].get("shippingStep", {}).get("shipTo", {})
        contact = ship_to.get("contactAddress", {})
        addr = {
            "name": ship_to.get("fullName", ""),
            "address1": contact.get("addressLine1", ""),
            "address2": contact.get("addressLine2", ""),
            "city": contact.get("city", ""),
            "zip": contact.get("postalCode", ""),
            "country": contact.get("countryCode", ""),
            "phone": ship_to.get("primaryPhone", {}).get("phoneNumber", ""),
        }

    # Artikel
    line_items = order.get("lineItems", [])

    subject = f"Neue eBay-Bestellung #{order_id} – bitte versenden"

    lines = [
        "Hallo Sebastian,",
        "",
        f"bitte folgende eBay-Bestellung ({shop_name}) direkt an den Endkunden versenden:",
        "",
        "─" * 50,
        "LIEFERADRESSE",
        "─" * 50,
        addr.get("name", ""),
        addr.get("address1", ""),
        addr.get("address2", "") if addr.get("address2") else None,
        f"{addr.get('zip', '')} {addr.get('city', '')}".strip(),
        addr.get("country", ""),
        f"Tel: {addr.get('phone', '')}" if addr.get("phone") else None,
        "",
        "─" * 50,
        "ARTIKEL",
        "─" * 50,
    ]

    for item in line_items:
        sku = item.get("sku", "??")
        qty = item.get("quantity", 1)
        title = item.get("title", "")
        lines.append(f"  {qty}x  {sku}  –  {title}")

    lines += [
        "",
        "─" * 50,
        f"eBay Bestellnummer:  {order_id}",
        f"Bestelldatum:        {order_date}",
        "",
        "Bitte Sendungsverfolgungsnummer per Antwort-Mail.",
        "",
        "Vielen Dank!",
    ]

    body = "\n".join(line for line in lines if line is not None)
    return subject, body


# ---------------------------------------------------------------------------
# Email senden
# ---------------------------------------------------------------------------
def send_email(cfg: dict, subject: str, body: str, dry_run: bool = False):
    """Sendet die Bestellmail oder legt sie im outbox/ Ordner ab."""
    supplier_cfg = cfg.get("supplier_email", {})
    sender = os.environ.get("SMTP_USER", "no-reply@example.com")
    from_name = supplier_cfg.get("from_name", "eBay Shop")
    to = supplier_cfg.get("to", "")
    auto_send = bool(supplier_cfg.get("auto_send", False))

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if dry_run or not auto_send:
        outbox = Path("outbox")
        outbox.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = outbox / f"ebay_order_{ts}.eml"
        path.write_bytes(bytes(msg))
        log.warning(
            f"{'DRY-RUN' if dry_run else 'auto_send=false'} – "
            f"Mail abgelegt: {path}"
        )
        return

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASSWORD"]
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

    log.info(f"Sende Bestellmail an {to} via {host}:{port} ...")
    if port == 465:
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            if use_tls:
                s.starttls()
            s.login(user, pw)
            s.send_message(msg)
    log.info(f"Mail versendet: {subject}")


# ---------------------------------------------------------------------------
# Verarbeitete Bestellungen tracken
# ---------------------------------------------------------------------------
def load_processed(path: str) -> set:
    p = Path(path)
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()


def save_processed(path: str, processed: set):
    Path(path).write_text(json.dumps(sorted(processed), indent=2))


# ---------------------------------------------------------------------------
# Haupt-Logik
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hours", type=int, default=2,
                        help="Bestellungen der letzten N Stunden prüfen (default: 2)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    load_dotenv()

    ebay_cfg = cfg.get("ebay", {})
    if not ebay_cfg.get("enabled", False):
        log.info("eBay nicht aktiviert (enabled: false) — nichts zu tun")
        return

    # Datei für verarbeitete Bestellungen (pro Shop getrennt)
    state_base = Path(cfg["runtime"]["state_file"]).stem
    processed_path = f"processed_orders_{state_base}.json"
    processed = load_processed(processed_path)

    # eBay Client
    try:
        client = EbayClient.from_env(ebay_cfg)
    except Exception as e:
        log.error(f"eBay Client Fehler: {e}")
        return

    # Bestellungen holen
    orders = fetch_new_orders(client, since_hours=args.hours)

    shop_name = cfg.get("supplier_email", {}).get("from_name", "eBay Shop")
    new_count = 0

    for order in orders:
        order_id = order.get("orderId", "")
        if not order_id:
            continue

        if order_id in processed:
            log.info(f"Bestellung {order_id} bereits verarbeitet — übersprungen")
            continue

        log.info(f"Neue Bestellung: {order_id}")
        subject, body = build_email(order, shop_name=shop_name)

        try:
            send_email(cfg, subject, body, dry_run=args.dry_run)
            processed.add(order_id)
            new_count += 1
        except Exception as e:
            log.error(f"Fehler beim Senden für Bestellung {order_id}: {e}")

    save_processed(processed_path, processed)

    log.info(f"=== Fertig: {new_count} neue Bestellungen weitergeleitet ===")


if __name__ == "__main__":
    main()
