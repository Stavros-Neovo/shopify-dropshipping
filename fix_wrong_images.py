"""
fix_wrong_images.py — Falsche Bilder entfernen
================================================
Liest wrong_images.json (aus image_review.html exportiert),
entfernt die Bilder aus enrichment_index.csv und von eBay.

Aufruf:
  python fix_wrong_images.py --dry-run   # nur zeigen
  python fix_wrong_images.py             # live entfernen
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

WRONG_FILE      = "wrong_images.json"
ENRICHMENT_FILE = "enrichment_index.csv"
CONFIG_FILE     = "config_shop2.yaml"
DELAY           = 0.4


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


def remove_image_from_ebay(base_url: str, token: str, sku: str) -> tuple[bool, str]:
    """Entfernt das Bild vom eBay Inventory Item (setzt imageUrls auf leer)."""
    # Erst aktuelles Item holen
    r = requests.get(
        f"{base_url}/sell/inventory/v1/inventory_item/{sku}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code == 404:
        return True, "nicht auf eBay"
    if r.status_code != 200:
        return False, f"GET HTTP {r.status_code}"

    item = r.json()
    item.setdefault("product", {})
    item["product"]["imageUrls"] = []   # Bilder leeren

    # Felder entfernen die beim PUT nicht erlaubt sind
    for field in ["offerId", "listing", "marketplaceId", "status",
                  "listingId", "auditInfo"]:
        item.pop(field, None)

    r2 = requests.put(
        f"{base_url}/sell/inventory/v1/inventory_item/{sku}",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Content-Language": "de-DE"},
        json=item,
        timeout=30,
    )
    if r2.status_code in (200, 204):
        return True, "Bild entfernt"

    try:
        errs   = r2.json().get("errors", [])
        codes  = [str(e.get("errorId", "")) for e in errs]
        msgs   = [e.get("message", "") for e in errs]
        detail = f"HTTP {r2.status_code} | {','.join(codes)} | {'; '.join(msgs)}"
    except Exception:
        detail = f"HTTP {r2.status_code} | {r2.text[:100]}"
    return False, detail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--local-only", action="store_true",
                        help="Nur enrichment_index.csv bereinigen, kein eBay API Call")
    args = parser.parse_args()

    # wrong_images.json laden
    if not Path(WRONG_FILE).exists():
        print(f"FEHLER: {WRONG_FILE} nicht gefunden.")
        print("Erst image_review.html öffnen, Bilder markieren, exportieren.")
        return

    wrong = json.loads(Path(WRONG_FILE).read_text(encoding="utf-8"))
    wrong_skus = {w["sku"] for w in wrong}
    wrong_eans = {w["ean"] for w in wrong if w.get("ean")}
    print(f"{len(wrong)} Produkte mit falschem Bild (SKUs: {len(wrong_skus)}, EANs: {len(wrong_eans)})")

    if args.dry_run:
        print("\n[DRY-RUN] Betroffene SKUs:")
        for w in wrong:
            print(f"  SKU={w['sku']}  Titel={w['title'][:50]}")
        return

    # ── enrichment_index.csv bereinigen ──────────────────────────────────
    shutil.copy2(ENRICHMENT_FILE, ENRICHMENT_FILE + ".bak")
    print(f"Backup: {ENRICHMENT_FILE}.bak")

    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    cleaned = 0
    for row in rows:
        sku = (row.get("sku") or row.get("ItemNo") or "").strip()
        ean = (row.get("ean") or "").strip()
        if sku in wrong_skus or ean in wrong_eans:
            row["image_main"]  = ""
            row["images_all"]  = ""
            if "image_source" in fieldnames: row["image_source"] = ""
            if "source"        in fieldnames: row["source"]       = ""
            cleaned += 1

    with open(ENRICHMENT_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"✓ enrichment_index.csv: {cleaned} Bilder entfernt")

    # ── eBay API ──────────────────────────────────────────────────────────
    if args.local_only:
        print("--local-only: eBay API übersprungen")
        return

    load_dotenv()
    cfg     = yaml.safe_load(open(CONFIG_FILE, encoding="utf-8"))
    sandbox = cfg["ebay"].get("sandbox", False)
    base_url = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"

    client_id     = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    refresh_token = os.getenv(cfg["ebay"]["refresh_token_env_var"], "")
    token = refresh_user_token(client_id, client_secret, refresh_token, sandbox)

    success, errors = 0, 0
    for i, w in enumerate(wrong, 1):
        sku = w["sku"]
        print(f"[{i:3}/{len(wrong)}] SKU={sku} …", end=" ", flush=True)
        ok, detail = remove_image_from_ebay(base_url, token, sku)
        if ok:
            print(f"✓ {detail}")
            success += 1
        else:
            print(f"FEHLER: {detail}")
            errors += 1
        time.sleep(DELAY)

    print(f"\n{'─'*50}")
    print(f"  eBay bereinigt: {success}")
    print(f"  Fehler:         {errors}")
    print(f"\nNächster Schritt: fix_images Workflow laufen lassen")
    print(f"um neue Bilder für die bereinigten Produkte zu finden.")


if __name__ == "__main__":
    main()
