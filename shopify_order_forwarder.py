#!/usr/bin/env python3
"""
shopify_order_forwarder.py
==========================
Leitet neue, bezahlte & unversandte Shopify-Bestellungen per E-Mail an den
Lieferanten (BAB Distribution) weiter — 1:1 wie ebay_order_forwarder.py, nur
Datenquelle = Shopify Admin API statt eBay.

Ablauf:
  1. Offene Bestellungen der letzten N Stunden von Shopify holen
     (financial_status=paid, fulfillment_status=unfulfilled)
  2. Bereits verarbeitete überspringen (processed_orders_shopify.json)
  3. E-Mail an BAB: SKU, Menge, Lieferadresse
  4. Bestellung als verarbeitet markieren

ENV (GitHub-Secrets, wie eBay-Forwarder):
  SHOPIFY_STORE          z.B. ijadcz-hp.myshopify.com
  SHOPIFY_ADMIN_TOKEN    Admin-API-Access-Token (Custom App, scope read_orders)
  SMTP_HOST/PORT/USER/PASSWORD
"""
from __future__ import annotations
import argparse, json, logging, os, smtplib, sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List

import requests
import yaml
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(): pass

log = logging.getLogger("shopify_orders")

API_VERSION = "2026-01"
PROCESSED_FILE = "processed_orders_shopify.json"


# ---------------------------------------------------------------------------
# Shopify: neue bezahlte, unversandte Bestellungen holen
# ---------------------------------------------------------------------------
def fetch_new_orders(store: str, token: str, since_hours: int = 24) -> List[Dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    url = f"https://{store}/admin/api/{API_VERSION}/orders.json"
    params = {
        "status": "open",
        "financial_status": "paid",
        "fulfillment_status": "unfulfilled",
        "created_at_min": since,
        "limit": 100,
    }
    r = requests.get(url, params=params,
                     headers={"X-Shopify-Access-Token": token}, timeout=30)
    r.raise_for_status()
    return r.json().get("orders", [])


# ---------------------------------------------------------------------------
# E-Mail bauen (gleiches Format wie eBay-Forwarder)
# ---------------------------------------------------------------------------
def build_email(order: Dict[str, Any], shop_name: str = "Neovodeals") -> tuple[str, str]:
    order_no = order.get("name") or f"#{order.get('order_number', '?')}"
    order_date = (order.get("created_at") or "")[:10]

    ship = order.get("shipping_address") or order.get("billing_address") or {}
    addr = {
        "name": ship.get("name", ""),
        "address1": ship.get("address1", ""),
        "address2": ship.get("address2", ""),
        "city": ship.get("city", ""),
        "zip": ship.get("zip", ""),
        "country": ship.get("country_code") or ship.get("country", ""),
        "phone": ship.get("phone", "") or order.get("phone", ""),
    }

    subject = f"Neue Bestellung {order_no} ({shop_name}) – bitte versenden"

    lines = [
        "Hallo Sebastian,",
        "",
        f"bitte folgende Bestellung ({shop_name}) direkt an den Endkunden versenden:",
        "",
        "─" * 50,
        "LIEFERADRESSE",
        "─" * 50,
        addr["name"],
        addr["address1"],
        addr["address2"] if addr["address2"] else None,
        f"{addr['zip']} {addr['city']}".strip(),
        addr["country"],
        f"Tel: {addr['phone']}" if addr["phone"] else None,
        "",
        "─" * 50,
        "ARTIKEL",
        "─" * 50,
    ]
    for item in order.get("line_items", []):
        sku = item.get("sku") or "??"
        qty = item.get("quantity", 1)
        title = item.get("name") or item.get("title", "")
        lines.append(f"  {qty}x  {sku}  –  {title}")

    lines += [
        "",
        "─" * 50,
        f"Bestellnummer:  {order_no}",
        f"Bestelldatum:   {order_date}",
        "",
        "Bitte Sendungsverfolgungsnummer per Antwort-Mail.",
        "",
        "Vielen Dank!",
    ]
    return subject, "\n".join(l for l in lines if l is not None)


# ---------------------------------------------------------------------------
# E-Mail senden (identisch zum eBay-Forwarder: SMTP oder outbox/)
# ---------------------------------------------------------------------------
def send_email(cfg: dict, subject: str, body: str, dry_run: bool = False):
    supplier_cfg = cfg.get("shopify_supplier_email") or cfg.get("supplier_email", {})
    sender = os.environ.get("SMTP_USER", "no-reply@example.com")
    from_name = supplier_cfg.get("from_name", "Neovodeals")
    to = supplier_cfg.get("to", "")
    auto_send = bool(supplier_cfg.get("auto_send", False))

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if dry_run or not auto_send:
        outbox = Path("outbox"); outbox.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = outbox / f"shopify_order_{ts}.eml"
        path.write_bytes(bytes(msg))
        log.warning(f"{'DRY-RUN' if dry_run else 'auto_send=false'} – Mail abgelegt: {path}")
        return

    host = os.environ["SMTP_HOST"]; port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]; pw = os.environ["SMTP_PASSWORD"]
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    log.info(f"Sende Bestellmail an {to} via {host}:{port} ...")
    if port == 465:
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(user, pw); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            if use_tls: s.starttls()
            s.login(user, pw); s.send_message(msg)
    log.info(f"Mail versendet: {subject}")


def load_processed(path: str) -> set:
    p = Path(path)
    return set(json.loads(p.read_text())) if p.exists() else set()


def save_processed(path: str, processed: set):
    Path(path).write_text(json.dumps(sorted(processed), indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    load_dotenv()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))

    store = os.environ.get("SHOPIFY_STORE", "").strip()
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()
    if not (store and token):
        log.error("SHOPIFY_STORE / SHOPIFY_ADMIN_TOKEN fehlen — abbruch")
        sys.exit(1)

    processed = load_processed(PROCESSED_FILE)
    try:
        orders = fetch_new_orders(store, token, since_hours=args.hours)
    except Exception as e:
        log.error(f"Shopify-Abruf fehlgeschlagen: {e}")
        sys.exit(1)

    log.info(f"{len(orders)} offene bezahlte Bestellung(en) gefunden")
    sent = 0
    for order in orders:
        oid = str(order.get("id"))
        if oid in processed:
            continue
        subject, body = build_email(order)
        send_email(cfg, subject, body, dry_run=args.dry_run)
        if not args.dry_run:
            processed.add(oid)
        sent += 1
        log.info(f"  → {order.get('name')} verarbeitet")

    if not args.dry_run:
        save_processed(PROCESSED_FILE, processed)
    log.info(f"Fertig: {sent} neue Bestellung(en) weitergeleitet")


# ── Self-Check (ponytail) ──────────────────────────────────────────────────
def _selftest():
    order = {"name": "#1001", "created_at": "2026-07-01T10:00:00Z",
             "shipping_address": {"name": "Max Muster", "address1": "Teststr. 1",
                                  "zip": "10115", "city": "Berlin", "country_code": "DE"},
             "line_items": [{"sku": "SSDS0123", "quantity": 2, "name": "Samsung SSD"}]}
    subj, body = build_email(order)
    assert "#1001" in subj and "SSDS0123" in body and "Max Muster" in body and "2x" in body
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
