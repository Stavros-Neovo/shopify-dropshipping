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

# Sicherheitsnetz-Imports
from datetime import timedelta

# Gefahrgut-Keywords (Artikel werden NICHT automatisch weitergeleitet)
DANGEROUS_KEYWORDS = [
    "batterie", "battery", "akku", "akkus", "lithium", "lipo",
    "li-ion", "lithium-ion", "accumulator", "accu", "powerbank",
    "power bank", "ladegerät", "charger",
]

# Haltezeit in Minuten — Storno-Fenster abwarten
ORDER_HOLD_MINUTES = 45

# Datei für manuelle Prüfung
FLAGGED_ORDERS_FILE = "flagged_orders.json"


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
                    "limit": limit,
                    "offset": offset,
                    "orderIds": "",  # alle Orders holen, lokal filtern
                },
            )
        except RuntimeError as e:
            log.error(f"Fehler beim Abrufen der Bestellungen: {e}")
            break

        orders = (result or {}).get("orders", [])

        # Nur unversendete Bestellungen (lokal filtern)
        for o in orders:
            status = o.get("orderFulfillmentStatus", "")
            if status in ("NOT_STARTED", "IN_PROGRESS", "UNFULFILLED", ""):
                all_orders.append(o)

        total = (result or {}).get("total", 0)
        offset += limit
        if offset >= total or not orders:
            break

    log.info(f"{len(all_orders)} unversendete eBay-Bestellungen abgerufen")
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
def write_to_pending(order: dict, cfg_path: str, pending_file: str = "pending_orders.json"):
    """Speichert Bestellung in pending_orders.json fuer Dashboard-Freigabe."""
    from pathlib import Path as _P
    p = _P(pending_file)
    pending = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    order_id = order.get("orderId", "")
    if order_id not in pending:
        addr = {}
        fulfillments = order.get("fulfillmentStartInstructions", [])
        if fulfillments:
            ship_to = fulfillments[0].get("shippingStep", {}).get("shipTo", {})
            contact = ship_to.get("contactAddress", {})
            addr = {
                "name": ship_to.get("fullName", ""),
                "city": contact.get("city", ""),
                "zip": contact.get("postalCode", ""),
                "country": contact.get("countryCode", ""),
            }
        pending[order_id] = {
            "orderId": order_id,
            "creationDate": order.get("creationDate", ""),
            "pending_since": datetime.now(timezone.utc).isoformat(),
            "shop_config": cfg_path,
            "address": addr,
            "lineItems": [
                {"sku": i.get("sku", ""), "title": i.get("title", ""), "quantity": i.get("quantity", 1)}
                for i in order.get("lineItems", [])
            ],
            "_raw": order,
        }
        p.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info(f"Bestellung {order_id} in pending_orders.json gespeichert")


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
def flag_for_manual_review(order_id: str, order: dict, reason: str):
    """Schreibt Bestellung in flagged_orders.json für manuelle Prüfung."""
    p = Path(FLAGGED_ORDERS_FILE)
    flagged = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    flagged[order_id] = {
        "reason": reason,
        "flagged_at": datetime.now(timezone.utc).isoformat(),
        "items": [
            {"sku": i.get("sku"), "title": i.get("title"), "qty": i.get("quantity")}
            for i in order.get("lineItems", [])
        ],
        "cancel_state": order.get("cancelStatus", {}).get("cancelState", "NONE"),
        "payment_status": [
            p.get("paymentStatus") for p in
            order.get("paymentSummary", {}).get("payments", [])
        ],
    }
    p.write_text(json.dumps(flagged, indent=2, ensure_ascii=False), encoding="utf-8")
    log.warning(f"⚠️  Bestellung {order_id} geflaggt: {reason} → {FLAGGED_ORDERS_FILE}")


def send_alert_email(cfg: dict, order_id: str, reason: str, order: dict):
    """Sendet Alert-Mail an den Shop-Besitzer (nicht an BAB)."""
    supplier_cfg = cfg.get("supplier_email", {})
    sender = os.environ.get("SMTP_USER", "no-reply@example.com")
    from_name = supplier_cfg.get("from_name", "eBay Shop")
    # Alert geht an den Absender selbst (Shop-Besitzer)
    to = sender

    items_text = "\n".join(
        f"  - {i.get('quantity')}x {i.get('sku')} – {i.get('title')}"
        for i in order.get("lineItems", [])
    )

    subject = f"⚠️ MANUELLE PRÜFUNG: Bestellung #{order_id} — {reason}"
    body = f"""ACHTUNG: Diese Bestellung wurde NICHT automatisch weitergeleitet.

Grund: {reason}

Bestellung: {order_id}
Datum: {order.get("creationDate", "")[:10]}

Artikel:
{items_text}

Bitte manuell prüfen und in flagged_orders.json verarbeiten.
"""

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        host = os.environ["SMTP_HOST"]
        port = int(os.environ["SMTP_PORT"])
        user = os.environ["SMTP_USER"]
        pw = os.environ["SMTP_PASSWORD"]
        use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
        if port == 465:
            with smtplib.SMTP_SSL(host, port) as s:
                s.login(user, pw); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as s:
                if use_tls: s.starttls()
                s.login(user, pw); s.send_message(msg)
        log.info(f"Alert-Mail gesendet: {subject}")
    except Exception as e:
        log.error(f"Alert-Mail fehlgeschlagen: {e}")


def check_order_safety(order: dict, cfg: dict, dry_run: bool = False) -> tuple[bool, str]:
    """
    Prüft ob eine Bestellung sicher weitergeleitet werden kann.
    Gibt (ok: bool, reason: str) zurück.
    
    Schichten:
      1. Storno-Check    → sofort blockieren
      2. Zahlungs-Check  → warten (retry)
      3. Haltezeit 45min → warten (retry)
      4. Gefahrgut-Flag  → manuelle Prüfung
    """
    order_id = order.get("orderId", "?")

    # ── 1. Storno-Check ──────────────────────────────────────────────────────
    cancel_state = order.get("cancelStatus", {}).get("cancelState", "NONE")
    if cancel_state in ("CANCEL_REQUESTED", "CANCELLED"):
        return False, f"STORNIERT ({cancel_state})"

    # ── 2. Zahlungs-Check ────────────────────────────────────────────────────
    payments = order.get("paymentSummary", {}).get("payments", [])
    if payments:
        paid = any(p.get("paymentStatus") == "PAID" for p in payments)
        if not paid:
            statuses = [p.get("paymentStatus") for p in payments]
            return False, f"NICHT BEZAHLT ({', '.join(statuses)})"

    # ── 3. Haltezeit: Storno-Fenster ─────────────────────────────────────────
    creation_str = order.get("creationDate", "")
    if creation_str:
        try:
            creation = datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - creation).total_seconds() / 60
            if age_minutes < ORDER_HOLD_MINUTES:
                return False, f"HALTEZEIT ({age_minutes:.0f}/{ORDER_HOLD_MINUTES} min)"
        except Exception:
            pass  # wenn Datum nicht parsbar → weitermachen

    # ── 4. Gefahrgut-Filter ──────────────────────────────────────────────────
    for item in order.get("lineItems", []):
        title_lower = item.get("title", "").lower()
        sku_lower = item.get("sku", "").lower()
        hit = next(
            (kw for kw in DANGEROUS_KEYWORDS if kw in title_lower or kw in sku_lower),
            None,
        )
        if hit:
            return False, f"GEFAHRGUT-VERDACHT ('{hit}' in '{item.get('title')}')"

    return True, "OK"


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

        # ── Sicherheitsnetz ──────────────────────────────────────────────────
        ok, reason = check_order_safety(order, cfg, dry_run=args.dry_run)

        if not ok:
            if reason.startswith("STORNIERT"):
                log.warning(f"❌ Bestellung {order_id} STORNIERT — wird nicht weitergeleitet")
                processed.add(order_id)  # dauerhaft überspringen
            elif reason.startswith("NICHT BEZAHLT"):
                log.warning(f"⏳ Bestellung {order_id} {reason} — nächster Run prüft erneut")
                # NICHT als processed markieren → nächster Run prüft nochmal
            elif reason.startswith("HALTEZEIT"):
                log.info(f"⏳ Bestellung {order_id} {reason} — wird noch gehalten")
                # NICHT als processed markieren → nächster Run prüft nochmal
            elif reason.startswith("GEFAHRGUT"):
                log.warning(f"⚠️  Bestellung {order_id} GEFAHRGUT — manuelle Prüfung!")
                flag_for_manual_review(order_id, order, reason)
                if not args.dry_run:
                    send_alert_email(cfg, order_id, reason, order)
                processed.add(order_id)  # nicht nochmal flaggen
            continue

        # In pending_orders.json schreiben -- Dashboard entscheidet
        log.info(f"Neue Bestellung: {order_id} -> wartet auf Dashboard-Freigabe")
        if not args.dry_run:
            write_to_pending(order, args.config)
        else:
            log.info(f"DRY-RUN: wuerde {order_id} in pending_orders.json schreiben")
        processed.add(order_id)
        new_count += 1

    save_processed(processed_path, processed)

    log.info(f"=== Fertig: {new_count} neue Bestellungen weitergeleitet ===")


if __name__ == "__main__":
    main()
