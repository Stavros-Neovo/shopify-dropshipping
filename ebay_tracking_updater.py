"""
ebay_tracking_updater.py
========================
Liest BAB-Antwortmails per IMAP, extrahiert Sendungsnummern und
markiert die entsprechenden eBay-Bestellungen als versendet.

Ablauf:
  1. IMAP: Ungelesene Mails von BAB (sebastian@bab-distribution.de) abrufen
  2. Bestellnummer + Tracking aus Betreff/Body extrahieren (Regex)
  3. eBay Fulfillment API: POST /order/{orderId}/shipping_fulfillment
  4. Mail als gelesen markieren

Aufruf:
  python ebay_tracking_updater.py
  python ebay_tracking_updater.py --config config_shop2.yaml
  python ebay_tracking_updater.py --dry-run

Voraussetzungen (.env):
  IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASSWORD
  (meistens identisch mit SMTP_USER/SMTP_PASSWORD)
"""
from __future__ import annotations

import argparse
import email
import imaplib
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from dotenv import load_dotenv

from ebay_client import EbayClient

# PDF-Parsing (optional — wird nur genutzt wenn installiert)
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

log = logging.getLogger("ebay_tracking_updater")

FULFILLMENT_PATH = "/sell/fulfillment/v1/order"

# ---------------------------------------------------------------------------
# Tracking-Nummern Regex (DHL, DPD, UPS, GLS, Hermes)
# ---------------------------------------------------------------------------
TRACKING_PATTERNS = [
    # DHL Express / Paket (12+ Ziffern oder 1Z...)
    re.compile(r"\b(1Z[A-Z0-9]{16})\b"),                    # UPS
    re.compile(r"\b([0-9]{12,14})\b"),                       # DHL/DPD/GLS numerisch
    re.compile(r"\b(JD[0-9]{18})\b"),                        # DHL Express JD...
    re.compile(r"\b(00[0-9]{18})\b"),                        # DHL GS1
    re.compile(r"\b([A-Z]{2}[0-9]{9}[A-Z]{2})\b"),          # Post International
]

# Carrier erkennen
def _detect_carrier(tracking: str) -> str:
    if tracking.startswith("1Z"):
        return "UPS"
    if tracking.startswith("JD") or tracking.startswith("00"):
        return "DHL"
    if len(tracking) == 14:
        return "DPD"
    return "DHL"  # Default: DHL (häufigster BAB-Carrier)


# ---------------------------------------------------------------------------
# IMAP: Mails von BAB lesen
# ---------------------------------------------------------------------------
BAB_SENDERS = [
    "sschulze@bab-distribution.de",
    "noreply@bab-distribution.de",
    "info@bab-distribution.de",
]


def fetch_bab_emails(supplier_email: str) -> list[tuple[bytes, email.message.Message]]:
    """Holt ungelesene Mails von BAB (alle bekannten Absender)."""
    host = os.environ.get("IMAP_HOST") or os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", 993))
    user = os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER", "")
    pw = os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")

    if not all([host, user, pw]):
        log.error("IMAP-Zugangsdaten fehlen (IMAP_HOST/IMAP_USER/IMAP_PASSWORD)")
        return []

    # Alle BAB-Absender abfragen (inkl. aus config)
    senders = list({supplier_email.lower()} | {s.lower() for s in BAB_SENDERS})

    results = []
    seen_ids: set = set()
    try:
        M = imaplib.IMAP4_SSL(host, port)
        M.login(user, pw)
        M.select("INBOX")

        for sender in senders:
            search = f'(FROM "{sender}" UNSEEN)'
            _, data = M.search(None, search)
            ids = data[0].split()
            log.info(f"IMAP: {len(ids)} ungelesene Mails von {sender}")
            for msg_id in ids:
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                _, msg_data = M.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                results.append((msg_id, msg))

        M.close()
        M.logout()
    except Exception as e:
        log.error(f"IMAP-Fehler: {e}")

    return results


def mark_as_read(msg_id: bytes, supplier_email: str):
    """Markiert eine Mail als gelesen."""
    host = os.environ.get("IMAP_HOST") or os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", 993))
    user = os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER", "")
    pw = os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")
    try:
        M = imaplib.IMAP4_SSL(host, port)
        M.login(user, pw)
        M.select("INBOX")
        M.store(msg_id, "+FLAGS", "\\Seen")
        M.close()
        M.logout()
    except Exception as e:
        log.warning(f"Mail als gelesen markieren fehlgeschlagen: {e}")


# ---------------------------------------------------------------------------
# Text + PDF-Anhänge aus Mail extrahieren
# ---------------------------------------------------------------------------
def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extrahiert Text aus einem PDF (benötigt pdfplumber)."""
    if not HAS_PDFPLUMBER:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        log.warning(f"PDF-Parsing fehlgeschlagen: {e}")
        return ""


def extract_text(msg: email.message.Message) -> str:
    """Extrahiert Text aus Email-Body + PDF-Anhängen."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    parts.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    ))
                except Exception:
                    pass
            elif ct == "application/pdf":
                # PDF-Anhang → Text extrahieren
                pdf_bytes = part.get_payload(decode=True)
                if pdf_bytes:
                    pdf_text = extract_pdf_text(pdf_bytes)
                    if pdf_text:
                        log.info(f"PDF-Anhang '{part.get_filename()}' gelesen ({len(pdf_text)} Zeichen)")
                        parts.append(pdf_text)
    else:
        try:
            parts.append(msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            ))
        except Exception:
            pass
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Bestellnummer + Tracking aus Mail-Text extrahieren
# ---------------------------------------------------------------------------
def extract_order_and_tracking(subject: str, body: str) -> tuple[Optional[str], Optional[str]]:
    """
    Gibt (order_id, tracking_number) zurück oder (None, None).
    Sucht eBay-Bestellnummer (z.B. 12-34567-89012) und Tracking im Text.
    """
    full_text = f"{subject}\n{body}"

    # eBay Bestellnummer: Format XX-XXXXX-XXXXX
    order_match = re.search(r"\b(\d{2}-\d{5}-\d{5})\b", full_text)
    order_id = order_match.group(1) if order_match else None

    # Tracking-Nummer
    tracking = None
    for pattern in TRACKING_PATTERNS:
        m = pattern.search(full_text)
        if m:
            candidate = m.group(1)
            # Kurze reine Ziffern (< 12) ignorieren — zu generisch
            if candidate.isdigit() and len(candidate) < 12:
                continue
            tracking = candidate
            break

    return order_id, tracking


# ---------------------------------------------------------------------------
# eBay: Versand melden
# ---------------------------------------------------------------------------
def get_line_item_ids(client: EbayClient, order_id: str) -> list[str]:
    """Holt alle Line Item IDs einer eBay-Bestellung."""
    try:
        order = client._request("GET", f"{FULFILLMENT_PATH}/{order_id}")
        return [item["lineItemId"] for item in order.get("lineItems", [])]
    except Exception as e:
        log.warning(f"Line Items für {order_id} nicht abrufbar: {e}")
        return []


def mark_shipped_on_ebay(client: EbayClient, order_id: str, tracking: str, dry_run: bool = False):
    """Meldet den Versand einer eBay-Bestellung mit Tracking-Nummer."""
    carrier = _detect_carrier(tracking)

    # Line Item IDs abrufen (eBay erfordert diese)
    line_item_ids = get_line_item_ids(client, order_id)
    if not line_item_ids:
        log.error(f"Keine Line Items für Order {order_id} gefunden — Abbruch")
        return

    payload = {
        "lineItems": [{"lineItemId": lid} for lid in line_item_ids],
        "shippedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "shippingCarrierCode": carrier,
        "trackingNumber": tracking,
    }

    log.info(f"eBay Versand melden: Order {order_id} | {carrier} {tracking} | Items: {line_item_ids}")

    if dry_run:
        log.warning(f"DRY-RUN — würde senden: {json.dumps(payload, indent=2)}")
        return

    try:
        client._request(
            "POST",
            f"{FULFILLMENT_PATH}/{order_id}/shipping_fulfillment",
            json_body=payload,
        )
        log.info(f"✓ eBay Bestellung {order_id} als versendet markiert ({carrier} {tracking})")
    except RuntimeError as e:
        log.error(f"eBay Versand-Fehler für {order_id}: {e}")


# ---------------------------------------------------------------------------
# State: bereits verarbeitete Trackings
# ---------------------------------------------------------------------------
def load_tracked(path: str) -> set:
    p = Path(path)
    return set(json.loads(p.read_text())) if p.exists() else set()


def save_tracked(path: str, tracked: set):
    Path(path).write_text(json.dumps(sorted(tracked), indent=2))


# ---------------------------------------------------------------------------
# Haupt-Logik
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
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

    supplier_email = cfg.get("supplier_email", {}).get("to", "")
    if not supplier_email:
        log.error("supplier_email.to fehlt in config.yaml")
        return

    # State-Datei für bereits verarbeitete Trackings
    state_base = Path(cfg["runtime"]["state_file"]).stem
    tracked_path = f"tracked_shipments_{state_base}.json"
    tracked = load_tracked(tracked_path)

    # eBay Client
    try:
        client = EbayClient.from_env(ebay_cfg)
    except Exception as e:
        log.error(f"eBay Client Fehler: {e}")
        return

    # Mails lesen
    mails = fetch_bab_emails(supplier_email)
    updated = 0

    for msg_id, msg in mails:
        subject = msg.get("Subject", "")
        body = extract_text(msg)

        order_id, tracking = extract_order_and_tracking(subject, body)

        if not tracking:
            log.warning(f"Keine Tracking-Nummer in Mail: '{subject[:80]}'")
            mark_as_read(msg_id, supplier_email)
            continue

        if not order_id:
            log.warning(f"Keine eBay-Bestellnummer in Mail: '{subject[:80]}' | Tracking: {tracking}")
            # Trotzdem als gelesen markieren
            mark_as_read(msg_id, supplier_email)
            continue

        if tracking in tracked:
            log.info(f"Tracking {tracking} bereits verarbeitet — übersprungen")
            mark_as_read(msg_id, supplier_email)
            continue

        log.info(f"Mail verarbeite: Order {order_id} | Tracking {tracking}")
        mark_shipped_on_ebay(client, order_id, tracking, dry_run=args.dry_run)

        tracked.add(tracking)
        mark_as_read(msg_id, supplier_email)
        updated += 1

    save_tracked(tracked_path, tracked)
    log.info(f"=== Fertig: {updated} Bestellungen als versendet markiert ===")


if __name__ == "__main__":
    main()
