"""
apply_seo_titles.py
===================
Pusht die SEO-Titel aus enrichment_index.csv (Spalte: title_seo) zu eBay.

Voraussetzung:
  - title_optimizer.py wurde ausgeführt → enrichment_index.csv hat title_seo
  - Du hast title_review.csv geprüft und bist zufrieden

Was dieser Script tut:
  - Lädt alle Produkte aus dem BAB-Feed (SKU + EAN)
  - Sucht zu jeder EAN den optimierten Titel aus enrichment_index.csv
  - Ruft eBay Inventory API auf → Offer per SKU abrufen
  - Setzt den neuen Titel via PUT /sell/inventory/v1/inventory_item/{sku}
  - Exportiert apply_report.csv mit Status pro SKU

Aufruf:
  python apply_seo_titles.py --dry-run      # nur simulieren
  python apply_seo_titles.py --limit 50     # erste 50 echte Updates
  python apply_seo_titles.py                # alle
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Optional

import requests
import yaml

ENRICHMENT_FILE  = "enrichment_index.csv"
REPORT_FILE      = "apply_report.csv"
CONFIG_FILE      = "config_shop2.yaml"
DELAY            = 0.4  # Sekunden zwischen API-Calls

# ─── eBay OAuth ───────────────────────────────────────────────────────────────

def get_app_token(client_id: str, client_secret: str, sandbox: bool) -> str:
    base = "https://api.sandbox.ebay.com" if sandbox else "https://api.ebay.com"
    r = requests.post(
        f"{base}/identity/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def refresh_user_token(client_id: str, client_secret: str,
                       refresh_token: str, sandbox: bool) -> str:
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


# ─── Inventory API ────────────────────────────────────────────────────────────

def get_inventory_item(base_url: str, user_token: str, sku: str) -> Optional[dict]:
    r = requests.get(
        f"{base_url}/sell/inventory/v1/inventory_item/{sku}",
        headers={"Authorization": f"Bearer {user_token}",
                 "Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def update_inventory_item_title(base_url: str, user_token: str,
                                sku: str, item: dict, new_title: str) -> bool:
    """Aktualisiert nur den Titel im bestehenden Inventory Item."""
    item["product"]["title"] = new_title
    r = requests.put(
        f"{base_url}/sell/inventory/v1/inventory_item/{sku}",
        headers={"Authorization": f"Bearer {user_token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        json=item,
        timeout=30,
    )
    return r.status_code in (200, 204)


# ─── BAB-Feed einlesen ────────────────────────────────────────────────────────

def load_bab_feed(cfg: dict) -> dict[str, str]:
    """Gibt {ean: sku} zurück."""
    import io
    csv_cfg = cfg["csv"]
    url      = csv_cfg["url"]
    enc      = csv_cfg.get("encoding", "utf-8-sig")
    delim    = csv_cfg.get("delimiter", ";")
    col_sku  = csv_cfg["columns"]["sku"]
    col_ean  = csv_cfg["columns"]["ean"]

    # HTTP-Auth
    http_user = os.getenv("CSV_HTTP_USER", "")
    http_pass = os.getenv("CSV_HTTP_PASSWORD", "")
    auth      = (http_user, http_pass) if http_user else None

    print("Lade BAB-Feed …")
    r = requests.get(url, auth=auth, timeout=60)
    r.raise_for_status()

    reader = csv.DictReader(io.StringIO(r.content.decode(enc)), delimiter=delim)
    mapping: dict[str, str] = {}
    for row in reader:
        ean = (row.get(col_ean) or "").strip()
        sku = (row.get(col_sku) or "").strip()
        if ean and sku:
            mapping[ean] = sku
    print(f"  {len(mapping)} Produkte geladen")
    return mapping


# ─── Enrichment laden ─────────────────────────────────────────────────────────

def load_seo_titles() -> dict[str, str]:
    """Gibt {ean: title_seo} zurück."""
    with open(ENRICHMENT_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return {
            row["ean"]: row["title_seo"]
            for row in reader
            if row.get("title_seo", "").strip() and row.get("ean", "").strip()
        }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # Config laden
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sandbox  = cfg["ebay"].get("sandbox", False)
    base_url = ("https://api.sandbox.ebay.com" if sandbox
                else "https://api.ebay.com")

    # Credentials
    client_id     = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    refresh_token = os.getenv(cfg["ebay"]["refresh_token_env_var"], "")

    if not all([client_id, client_secret, refresh_token]) and not args.dry_run:
        print("FEHLER: EBAY_CLIENT_ID / EBAY_CLIENT_SECRET / EBAY_REFRESH_TOKEN_2 fehlen.")
        return

    # Tokens
    user_token = ""
    if not args.dry_run:
        print("eBay Token holen …")
        user_token = refresh_user_token(client_id, client_secret, refresh_token, sandbox)

    # Daten laden
    ean_to_sku   = load_bab_feed(cfg)
    ean_to_title = load_seo_titles()

    # Schnittmenge: EANs die sowohl Feed als auch SEO-Titel haben
    common_eans = sorted(set(ean_to_sku.keys()) & set(ean_to_title.keys()))
    if args.limit:
        common_eans = common_eans[:args.limit]

    print(f"Zu aktualisieren: {len(common_eans)} Produkte")
    if args.dry_run:
        print("[DRY-RUN] Erste 10 Beispiele:\n")
        for ean in common_eans[:10]:
            sku   = ean_to_sku[ean]
            title = ean_to_title[ean]
            print(f"  SKU={sku}  EAN={ean}")
            print(f"  Titel: {title}\n")
        return

    # Live-Update
    report   = []
    success  = 0
    skipped  = 0
    errors   = 0

    for i, ean in enumerate(common_eans, 1):
        sku       = ean_to_sku[ean]
        new_title = ean_to_title[ean]

        print(f"[{i:4}/{len(common_eans)}] SKU={sku} …", end=" ", flush=True)

        try:
            item = get_inventory_item(base_url, user_token, sku)
            if item is None:
                print("nicht auf eBay")
                report.append({"ean": ean, "sku": sku, "title": new_title,
                                "status": "not_found"})
                skipped += 1
                continue

            old_title = item.get("product", {}).get("title", "")
            if old_title == new_title:
                print("unverändert")
                report.append({"ean": ean, "sku": sku, "title": new_title,
                                "status": "unchanged"})
                skipped += 1
                continue

            ok = update_inventory_item_title(base_url, user_token, sku, item, new_title)
            if ok:
                print(f"✓ → {new_title[:50]}")
                report.append({"ean": ean, "sku": sku, "title": new_title,
                                "status": "updated"})
                success += 1
            else:
                print("FEHLER")
                report.append({"ean": ean, "sku": sku, "title": new_title,
                                "status": "error"})
                errors += 1

        except Exception as e:
            print(f"EXCEPTION: {e}")
            report.append({"ean": ean, "sku": sku, "title": new_title,
                            "status": f"error: {e}"})
            errors += 1

        time.sleep(DELAY)

    # Report schreiben
    with open(REPORT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ean", "sku", "title", "status"])
        writer.writeheader()
        writer.writerows(report)

    print(f"\n─── Ergebnis ───────────────────────────────")
    print(f"  Aktualisiert:  {success}")
    print(f"  Übersprungen:  {skipped}")
    print(f"  Fehler:        {errors}")
    print(f"  Report:        {REPORT_FILE}")


if __name__ == "__main__":
    main()
