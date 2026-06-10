"""
publish_offers.py — Unpublished Offers aktivieren
===================================================
Liest audit_report.csv und published alle Offers die UNPUBLISHED sind
aber ein Bild haben. Offers ohne Bild werden übersprungen (eBay würde
sie eh ablehnen).

Aufruf:
  python publish_offers.py --dry-run    # nur zeigen was passieren würde
  python publish_offers.py              # live publishen
  python publish_offers.py --all        # auch Offers ohne Bild versuchen
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

AUDIT_FILE  = "audit_report.csv"
REPORT_FILE = "publish_report.csv"
CONFIG_FILE = "config_shop2.yaml"
DELAY       = 0.5


def refresh_user_token(client_id, client_secret, refresh_token, sandbox=False):
    base = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
    r = requests.post(
        f"{base}/identity/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "refresh_token",
              "refresh_token": refresh_token,
              "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def publish_offer(base_url: str, token: str, offer_id: str) -> tuple[bool, str]:
    """Publishes ein einzelnes Offer. Gibt (ok, detail) zurück."""
    r = requests.post(
        f"{base_url}/sell/inventory/v1/offer/{offer_id}/publish",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code in (200, 201):
        listing_id = r.json().get("listingId", "")
        return True, f"listingId={listing_id}"

    try:
        errs   = r.json().get("errors", [])
        codes  = [str(e.get("errorId", "")) for e in errs]
        msgs   = [e.get("message", "") for e in errs]
        detail = f"HTTP {r.status_code} | Codes: {','.join(codes)} | {'; '.join(msgs)}"
    except Exception:
        detail = f"HTTP {r.status_code} | {r.text[:150]}"
    return False, detail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all",     action="store_true",
                        help="Auch Offers ohne Bild versuchen")
    parser.add_argument("--config",  default=CONFIG_FILE)
    args = parser.parse_args()

    load_dotenv()
    cfg     = yaml.safe_load(open(args.config, encoding="utf-8"))
    sandbox = cfg["ebay"].get("sandbox", False)
    base_url = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"

    # Token
    if not args.dry_run:
        client_id     = os.getenv("EBAY_CLIENT_ID", "")
        client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
        refresh_token = os.getenv(cfg["ebay"]["refresh_token_env_var"], "")
        token = refresh_user_token(client_id, client_secret, refresh_token, sandbox)
    else:
        token = ""

    # Audit Report laden
    if not Path(AUDIT_FILE).exists():
        print(f"FEHLER: {AUDIT_FILE} nicht gefunden. Erst audit.py ausführen.")
        return

    with open(AUDIT_FILE, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # Candidates: INACTIVE (= UNPUBLISHED) mit Offer ID
    candidates = [
        r for r in rows
        if r["status"] == "INACTIVE"
        and r["offer_id"]
        and (args.all or r.get("has_image", "").lower() in ("true", "1", "yes"))
    ]

    skipped_no_image = [
        r for r in rows
        if r["status"] == "INACTIVE" and r["offer_id"]
        and r.get("has_image", "").lower() not in ("true", "1", "yes")
    ]

    print(f"Zu publishen:        {len(candidates)}")
    print(f"Übersprungen (kein Bild): {len(skipped_no_image)}")
    if args.dry_run:
        print("\n[DRY-RUN] Erste 10:\n")
        for r in candidates[:10]:
            print(f"  SKU={r['sku']}  OfferID={r['offer_id']}  Bild={r.get('has_image')}")
        return

    # Publishen
    success, errors = 0, 0
    report = []

    for i, row in enumerate(candidates, 1):
        sku      = row["sku"]
        offer_id = row["offer_id"]
        print(f"[{i:3}/{len(candidates)}] SKU={sku} OfferID={offer_id} …", end=" ", flush=True)

        ok, detail = publish_offer(base_url, token, offer_id)
        if ok:
            print(f"✓ {detail}")
            success += 1
            report.append({"sku": sku, "offer_id": offer_id, "status": "published", "detail": detail})
        else:
            print(f"FEHLER: {detail}")
            errors += 1
            report.append({"sku": sku, "offer_id": offer_id, "status": "error", "detail": detail})

        time.sleep(DELAY)

    # Report
    with open(REPORT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["sku", "offer_id", "status", "detail"])
        writer.writeheader()
        writer.writerows(report)

    print(f"\n{'─'*50}")
    print(f"  Publiziert: {success}")
    print(f"  Fehler:     {errors}")
    print(f"  Report:     {REPORT_FILE}")


if __name__ == "__main__":
    main()
