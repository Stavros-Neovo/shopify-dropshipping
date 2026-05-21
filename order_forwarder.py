"""
order_forwarder.py
==================
Auto-Order-Mail-Modul.

Empfängt einen Shopify-Order-Webhook (oder kann manuell mit einer Order-JSON
aufgerufen werden), erstellt eine strukturierte Bestell-Mail mit allen
relevanten Daten (SKUs, Mengen, Lieferadresse) und schickt sie an die
in config.yaml hinterlegte Lieferanten-Mail.

ZWEI BETRIEBSMODI:
  A) Webhook-Server: starte mit `python order_forwarder.py serve --port 8080`
     -> in Shopify einen Webhook auf "orders/create" einrichten, der auf
        https://<deine-domain>/webhook zeigt.
  B) Manueller Lauf: `python order_forwarder.py send <order.json>`
     -> nützlich für Tests oder wenn du Bestellungen aus einem CSV-Export
        abfeuerst.

WICHTIG:
  - In config.yaml `supplier_email.auto_send: false` lassen, bis du das
    System getestet hast! Sonst gehen ggf. falsche Bestellungen raus.
  - Bei `auto_send: false` wird die Mail nur als .eml im outbox/ Ordner
    abgelegt, damit du sie manuell prüfen kannst.
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import logging
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

log = logging.getLogger("order_forwarder")


# ---------------------------------------------------------------------------
# Mail-Body bauen
# ---------------------------------------------------------------------------
def build_email_body(order: Dict[str, Any], lang: str = "de") -> tuple[str, str]:
    """Liefert (subject, body) für eine Shopify-Order."""
    order_no = order.get("name") or order.get("order_number") or "?"
    addr = order.get("shipping_address") or {}
    items = order.get("line_items", [])

    if lang == "de":
        subject = f"Neue Bestellung #{order_no} – bitte versenden"
        lines = [
            "Hallo,",
            "",
            "bitte folgende Bestellung direkt an den Endkunden versenden:",
            "",
            "--- LIEFERADRESSE ---",
            f"{addr.get('first_name','')} {addr.get('last_name','')}".strip(),
            addr.get("company") or "",
            f"{addr.get('address1','')} {addr.get('address2','') or ''}".strip(),
            f"{addr.get('zip','')} {addr.get('city','')}".strip(),
            addr.get("country", ""),
            f"Tel: {addr.get('phone','')}",
            "",
            "--- ARTIKEL ---",
        ]
        for it in items:
            lines.append(
                f"  {it.get('quantity',1)}x  {it.get('sku','??')}  "
                f"– {it.get('title','')}"
            )
        lines += [
            "",
            f"Bestellung-Referenz: {order_no}",
            f"Bestelldatum: {order.get('created_at','')}",
            "",
            "Bitte Sendungsnummer per Antwort-Mail.",
            "",
            "Vielen Dank!",
        ]
    else:
        subject = f"New order #{order_no} – please ship"
        lines = [
            "Hello,",
            "",
            "please ship the following order directly to the end customer:",
            "",
            "--- SHIPPING ADDRESS ---",
            f"{addr.get('first_name','')} {addr.get('last_name','')}".strip(),
            addr.get("company") or "",
            f"{addr.get('address1','')} {addr.get('address2','') or ''}".strip(),
            f"{addr.get('zip','')} {addr.get('city','')}".strip(),
            addr.get("country", ""),
            f"Phone: {addr.get('phone','')}",
            "",
            "--- ITEMS ---",
        ]
        for it in items:
            lines.append(
                f"  {it.get('quantity',1)}x  {it.get('sku','??')}  "
                f"– {it.get('title','')}"
            )
        lines += [
            "",
            f"Order ref: {order_no}",
            f"Order date: {order.get('created_at','')}",
            "",
            "Please reply with tracking number.",
            "",
            "Thank you!",
        ]

    return subject, "\n".join(l for l in lines if l is not None)


def _make_message(cfg: dict, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    sender = os.environ.get("SMTP_USER", "no-reply@example.com")
    msg["From"] = f"{cfg['supplier_email']['from_name']} <{sender}>"
    msg["To"] = cfg["supplier_email"]["to"]
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send_email(cfg: dict, subject: str, body: str) -> None:
    """Sendet die Mail via SMTP (oder schreibt sie nur in outbox/)."""
    msg = _make_message(cfg, subject, body)
    auto_send = bool(cfg["supplier_email"].get("auto_send", False))

    if not auto_send:
        outbox = Path("outbox")
        outbox.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = outbox / f"order_{ts}.eml"
        path.write_bytes(bytes(msg))
        log.warning(
            f"auto_send=false – Mail NICHT versendet, abgelegt: {path}"
        )
        return

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASSWORD"]
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

    log.info(f"Sende Mail via {host}:{port} ...")
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
    log.info("Mail versendet")


# ---------------------------------------------------------------------------
# Shopify Webhook Verifizierung
# ---------------------------------------------------------------------------
def verify_hmac(body: bytes, header_hmac: str, secret: str) -> bool:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    import base64
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header_hmac)


# ---------------------------------------------------------------------------
# Modus A: Webhook-Server (Flask)
# ---------------------------------------------------------------------------
def serve(cfg: dict, port: int):
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        print("Bitte 'pip install flask' ausführen.", file=sys.stderr)
        sys.exit(1)

    app = Flask(__name__)
    secret = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")

    @app.route("/webhook", methods=["POST"])
    def webhook():
        raw = request.get_data()
        if secret:
            given = request.headers.get("X-Shopify-Hmac-Sha256", "")
            if not verify_hmac(raw, given, secret):
                return jsonify({"error": "invalid hmac"}), 401
        order = json.loads(raw.decode("utf-8"))
        subject, body = build_email_body(order, cfg["supplier_email"].get("language", "de"))
        send_email(cfg, subject, body)
        return jsonify({"ok": True})

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "ts": datetime.now().isoformat()})

    log.info(f"Webhook-Server läuft auf Port {port}")
    app.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# Modus B: Manueller Send aus Order-JSON
# ---------------------------------------------------------------------------
def send_from_file(cfg: dict, path: str):
    order = json.loads(Path(path).read_text(encoding="utf-8"))
    subject, body = build_email_body(order, cfg["supplier_email"].get("language", "de"))
    send_email(cfg, subject, body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=8080)
    p_send = sub.add_parser("send")
    p_send.add_argument("order_json")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    load_dotenv()

    if args.cmd == "serve":
        serve(cfg, args.port)
    elif args.cmd == "send":
        send_from_file(cfg, args.order_json)


if __name__ == "__main__":
    main()
