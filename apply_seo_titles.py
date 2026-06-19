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
STATE_FILE       = "seo_state.json"
CONFIG_FILE      = "config_shop2.yaml"
DELAY            = 0.4  # Sekunden zwischen API-Calls
CHUNK_SIZE       = 200  # Produkte pro Run (~25 Min)

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

def get_offer_status(base_url: str, user_token: str, sku: str) -> str:
    """Gibt den Offer-Status ('PUBLISHED'/'UNPUBLISHED'/'') fuer eine SKU zurueck."""
    r = requests.get(
        f"{base_url}/sell/inventory/v1/offer",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        params={"sku": sku},
        timeout=30,
    )
    if not r.ok:
        return ""
    offers = r.json().get("offers", [])
    return offers[0].get("status", "") if offers else ""


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
                                sku: str, item: dict, new_title: str) -> tuple[bool, str]:
    """Aktualisiert nur den Titel. Gibt (ok, fehler_msg) zurück."""
    item["product"]["title"] = new_title
    # Felder entfernen die beim PUT nicht erlaubt sind
    for field in ["offerId", "listing", "marketplaceId", "status",
                  "listingId", "auditInfo"]:
        item.pop(field, None)

    r = requests.put(
        f"{base_url}/sell/inventory/v1/inventory_item/{sku}",
        headers={"Authorization": f"Bearer {user_token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json",
                 "Content-Language": "de-DE"},
        json=item,
        timeout=30,
    )
    if r.status_code in (200, 204):
        return True, ""

    # Fehlermeldung extrahieren
    try:
        errs = r.json().get("errors", [])
        codes = [str(e.get("errorId","")) for e in errs]
        msgs  = [e.get("message","") for e in errs]
        detail = f"HTTP {r.status_code} | Codes: {','.join(codes)} | {'; '.join(msgs)}"
    except Exception:
        detail = f"HTTP {r.status_code} | {r.text[:120]}"

    return False, detail


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


# ─── Checkpoint ───────────────────────────────────────────────────────────────

def load_seo_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {"offset": 0, "cycle": 1, "total_updated": 0}


def save_seo_state(offset: int, cycle: int, total_updated: int):
    Path(STATE_FILE).write_text(
        json.dumps({"offset": offset, "cycle": cycle,
                    "total_updated": total_updated,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                   indent=2),
        encoding="utf-8"
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit",   type=int, default=0,
                        help="Überschreibt CHUNK_SIZE für diesen Run")
    parser.add_argument("--reset",   action="store_true",
                        help="Checkpoint zurücksetzen")
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

    # Nur SKUs mit auflösungsgeprüftem Bild (supplier_map.json::image_verified) -
    # SEO-Titel optimieren wir ausschließlich bei Listings, die 100% sicher ein
    # korrektes Bild haben, nicht bei den 654 noch ungeklärten
    try:
        smap = json.loads(Path("supplier_map.json").read_text(encoding="utf-8"))
        image_verified_skus = {sku for sku, v in smap.items() if v.get("image_verified")}
    except Exception:
        image_verified_skus = set()
    print(f"supplier_map.json: {len(image_verified_skus)} SKUs mit verifiziertem Bild")

    # Schnittmenge: EANs die Feed + SEO-Titel haben UND deren SKU verifiziertes Bild hat
    all_eans = sorted(
        ean for ean in (set(ean_to_sku.keys()) & set(ean_to_title.keys()))
        if ean_to_sku[ean] in image_verified_skus
    )
    total    = len(all_eans)

    print(f"Gesamt zu aktualisieren (nur verifizierte Bilder): {total} Produkte")

    if args.dry_run:
        print("[DRY-RUN] Erste 10 Beispiele:\n")
        for ean in all_eans[:10]:
            sku   = ean_to_sku[ean]
            title = ean_to_title[ean]
            print(f"  SKU={sku}  EAN={ean}")
            print(f"  Titel: {title}\n")
        return

    # Checkpoint laden
    if args.reset:
        save_seo_state(0, 1, 0)
        print("Checkpoint zurückgesetzt.")

    state         = load_seo_state()
    offset        = state["offset"]
    cycle         = state["cycle"]
    total_updated = state["total_updated"]
    chunk         = args.limit if args.limit else CHUNK_SIZE

    chunk_eans     = all_eans[offset: offset + chunk]
    next_offset    = offset + len(chunk_eans)
    cycle_complete = next_offset >= total

    print(f"Zyklus {cycle} | EANs {offset+1}–{next_offset} von {total} "
          f"({'letzter Block' if cycle_complete else f'weiter ab {next_offset}'})")

    # Live-Update
    report   = []
    success  = 0
    skipped  = 0
    errors   = 0

    for i, ean in enumerate(chunk_eans, 1):
        sku       = ean_to_sku[ean]
        new_title = ean_to_title[ean]

        print(f"[{offset+i:4}/{total}] SKU={sku} …", end=" ", flush=True)

        try:
            offer_status = get_offer_status(base_url, user_token, sku)
            if offer_status != "PUBLISHED":
                print(f"nicht live ({offer_status or 'kein Offer'})")
                report.append({"ean": ean, "sku": sku, "title": new_title,
                                "status": "not_live"})
                skipped += 1
                continue

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

            ok, err_msg = update_inventory_item_title(base_url, user_token, sku, item, new_title)
            if ok:
                print(f"✓ → {new_title[:50]}")
                report.append({"ean": ean, "sku": sku, "title": new_title,
                                "status": "updated"})
                success += 1
            else:
                print(f"FEHLER: {err_msg}")
                report.append({"ean": ean, "sku": sku, "title": new_title,
                                "status": f"error: {err_msg}"})
                errors += 1

        except Exception as e:
            print(f"EXCEPTION: {e}")
            report.append({"ean": ean, "sku": sku, "title": new_title,
                            "status": f"error: {e}"})
            errors += 1

        time.sleep(DELAY)

    # Checkpoint speichern
    total_updated += success
    if cycle_complete:
        save_seo_state(0, cycle + 1, total_updated)
        print(f"\n✅ Zyklus {cycle} komplett — alle {total} Produkte einmal aktualisiert")
    else:
        save_seo_state(next_offset, cycle, total_updated)

    # Report schreiben (anhängen wenn nicht erster Block)
    report_path = Path(REPORT_FILE)
    write_header = not report_path.exists() or offset == 0
    with open(REPORT_FILE, "a" if not write_header else "w",
              encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ean", "sku", "title", "status"])
        if write_header:
            writer.writeheader()
        writer.writerows(report)

    print(f"\n─── Ergebnis ───────────────────────────────")
    print(f"  Zyklus:        {cycle} ({'komplett' if cycle_complete else f'Offset → {next_offset}'})")
    print(f"  Aktualisiert:  {success}")
    print(f"  Übersprungen:  {skipped}")
    print(f"  Fehler:        {errors}")
    print(f"  Gesamt bisher: {total_updated}")
    print(f"  Report:        {REPORT_FILE}")


if __name__ == "__main__":
    main()
