"""
kosatec_stock_check.py
======================
Stündlicher Bestandscheck für Kosatec-Artikel.

Läuft jede Stunde (via Makefile + hourly_sync.yml):
  1. Lädt frische kosatec_preisliste.csv
  2. Prüft alle SKUs aus supplier_map.json
  3. Artikel die nicht mehr verfügbar sind → eBay deaktivieren + aus supplier_map entfernen

Aufruf:
  python kosatec_stock_check.py
  python kosatec_stock_check.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from ebay_client import EbayClient

log = logging.getLogger("kosatec_stock")

SUPPLIER_MAP   = "supplier_map.json"
KOSATEC_CSV    = "kosatec_preisliste.csv"
CONFIG_FILE    = "config_shop2.yaml"


def load_kosatec_available(path: str = KOSATEC_CSV) -> set[str]:
    """Gibt Set aller EANs zurück die verfügbar sind (verfuegbar=A, menge>0)."""
    available = set()
    if not Path(path).exists():
        log.error(f"Kosatec CSV nicht gefunden: {path}")
        return available
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            ean = row.get("ean", "").strip()
            if not ean:
                continue
            if row.get("verfuegbar", "") != "A":
                continue
            try:
                menge = int(row.get("menge", "0") or 0)
            except ValueError:
                menge = 0
            if menge > 0:
                available.add(ean)
    return available


def load_supplier_map(path: str = SUPPLIER_MAP) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_supplier_map(smap: dict, path: str = SUPPLIER_MAP):
    Path(path).write_text(json.dumps(smap, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Kosatec Bestandscheck — stündlich")
    parser.add_argument("--dry-run",  action="store_true", help="Nur prüfen, nichts deaktivieren")
    parser.add_argument("--config",   default=CONFIG_FILE)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    load_dotenv()

    smap = load_supplier_map()
    if not smap:
        log.info("supplier_map.json leer oder nicht vorhanden — nichts zu prüfen")
        return

    available = load_kosatec_available()
    if not available:
        log.warning("Keine verfügbaren Kosatec-Artikel geladen — CSV fehlt oder leer")
        return

    log.info(f"supplier_map: {len(smap)} SKUs | Kosatec verfügbar: {len(available)} EANs")

    # Artikel prüfen
    to_offline = {
        sku: entry for sku, entry in smap.items()
        if entry.get("supplier") == "Kosatec" and entry.get("ean", "") not in available
    }

    if not to_offline:
        log.info("Alle Kosatec-Artikel verfügbar ✓")
        return

    log.warning(f"{len(to_offline)} Kosatec-Artikel nicht mehr verfügbar → deaktivieren")

    if args.dry_run:
        for sku, entry in to_offline.items():
            log.warning(f"  DRY-RUN: würde deaktivieren SKU {sku} EAN {entry.get('ean')}")
        return

    # eBay Client initialisieren
    try:
        cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
        client = EbayClient.from_env(cfg["ebay"])
    except Exception as e:
        log.error(f"eBay Client Fehler: {e}")
        return

    offlined = errors = 0

    for sku, entry in to_offline.items():
        ean = entry.get("ean", "")
        log.warning(f"  OFFLINE: SKU {sku} EAN {ean}")
        try:
            # Listing deaktivieren (muss vor set_inventory(0) passieren - eBay
            # lehnt Bestand=0 bei noch PUBLISHED Offers mit Fehler 25004 ab)
            offer = client.get_offer_for_sku(sku)
            if offer:
                client.withdraw_offer(offer["offerId"])
                log.info(f"  ✓ SKU {sku} deaktiviert (Listing zurückgezogen)")
            else:
                log.info(f"  ✓ SKU {sku} kein aktives Listing gefunden")
            # Bestand auf 0
            client.set_inventory(sku, 0)
            # Aus supplier_map entfernen
            del smap[sku]
            offlined += 1
        except Exception as e:
            log.error(f"  ✗ SKU {sku}: {e}")
            errors += 1
        time.sleep(0.2)

    if offlined:
        save_supplier_map(smap)
        log.info(f"supplier_map.json aktualisiert ({offlined} entfernt)")

    log.info(f"=== Fertig: {offlined} deaktiviert | {errors} Fehler ===")


if __name__ == "__main__":
    main()
