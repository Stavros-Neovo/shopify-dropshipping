"""
sync.py
=======
HAUPT-SKRIPT der Dropshipping-Automation.

Was es macht (in dieser Reihenfolge):
  1. Lädt config.yaml und .env
  2. Lädt den CSV-Feed des Lieferanten
  3. Filtert ungewünschte Produkte raus (Gewicht, Kategorien, etc.)
  4. Berechnet für jedes Produkt den Verkaufspreis
  5. Legt Produkte in Shopify an / aktualisiert sie
  6. Synchronisiert Lagerbestand

Aufruf:
  python sync.py            # normaler Lauf (respektiert dry_run aus config)
  python sync.py --live     # erzwingt Live-Modus (sendet an Shopify)
  python sync.py --dry-run  # erzwingt Dry-Run
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from csv_loader import load_supplier_feed
from pricing import calculate_vk
from shopify_client import ShopifyClient


def setup_logging(log_dir: str):
    Path(log_dir).mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = Path(log_dir) / f"sync_{ts}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def should_keep(product: dict, filters_cfg: dict) -> tuple[bool, str]:
    """Entscheidet, ob ein Produkt importiert werden soll."""
    if product.get("purchase_price", 0) <= 0:
        return False, "EK ist 0 oder fehlt"
    if product.get("purchase_price", 0) > filters_cfg.get("max_purchase_price_eur", 1e9):
        return False, f"EK > {filters_cfg['max_purchase_price_eur']}€"
    if product.get("weight_kg", 999) > filters_cfg.get("max_weight_kg", 999):
        return False, f"Gewicht > {filters_cfg['max_weight_kg']}kg"
    if product.get("stock", 0) < filters_cfg.get("min_stock", 0):
        return False, f"Stock < {filters_cfg['min_stock']}"

    allowed = filters_cfg.get("allowed_categories") or []
    if allowed and product.get("category", "") not in allowed:
        return False, "Kategorie nicht erlaubt"

    blob = " ".join([
        product.get("title", ""),
        product.get("brand", ""),
        product.get("description", ""),
    ]).lower()
    for kw in filters_cfg.get("excluded_keywords") or []:
        if kw.lower() in blob:
            return False, f"Excluded-Keyword '{kw}'"

    return True, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--live", action="store_true", help="erzwingt Live-Modus")
    parser.add_argument("--dry-run", action="store_true", help="erzwingt Dry-Run")
    args = parser.parse_args()

    # Config + Secrets laden
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    load_dotenv()

    log_file = setup_logging(cfg["runtime"]["log_dir"])
    log = logging.getLogger("sync")
    log.info(f"=== Sync-Run gestartet, Log: {log_file} ===")

    # Dry-Run-Logik
    dry_run = cfg["runtime"]["dry_run"]
    if args.live:
        dry_run = False
    if args.dry_run:
        dry_run = True
    log.info(f"Dry-Run: {dry_run}")

    # Counters für Report
    stats = {"total": 0, "filtered": 0, "created": 0, "updated": 0, "errors": 0}
    filter_reasons: dict[str, int] = {}

    # Shopify-Client – nur initialisieren wenn Live
    shopify = None
    location_id = None
    if not dry_run:
        shopify = ShopifyClient(
            shop_domain=cfg["shopify"]["shop_domain"],
            admin_token=os.environ["SHOPIFY_ADMIN_TOKEN"],
            api_version=cfg["shopify"].get("api_version", "2024-10"),
        )
        location_id = cfg["shopify"].get("location_id") \
            or shopify.get_primary_location_id()
        log.info(f"Shopify-Location-ID: {location_id}")

    # State-Datei laden (für Diff)
    state_path = Path(cfg["runtime"]["state_file"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    limit = int(cfg["runtime"].get("max_products_per_run", 0))
    processed = 0

    for product in load_supplier_feed(cfg):
        stats["total"] += 1

        keep, reason = should_keep(product, cfg["filters"])
        if not keep:
            stats["filtered"] += 1
            filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
            continue

        try:
            pr = calculate_vk(product["purchase_price"], cfg["pricing"])
        except Exception as e:
            log.error(f"Pricing-Fehler für SKU {product.get('sku')}: {e}")
            stats["errors"] += 1
            continue

        sku = product["sku"]
        log.info(
            f"SKU {sku} | EK {pr.purchase_price_net:.2f} -> "
            f"VK {pr.vk_gross:.2f} | Marge {pr.margin_eur:.2f}€ "
            f"({pr.margin_pct:.0f}%) | Stock {product['stock']}"
        )

        if dry_run:
            stats["created" if sku not in state else "updated"] += 1
        else:
            payload = ShopifyClient.build_product_payload(
                product, pr.vk_gross, status="active"
            )
            result = shopify.upsert_product(payload)
            prod = result.get("product", {})
            variant = (prod.get("variants") or [{}])[0]
            inv_item_id = variant.get("inventory_item_id")
            if inv_item_id:
                shopify.set_inventory(
                    inventory_item_id=inv_item_id,
                    location_id=location_id,
                    available=int(product["stock"]),
                )
            stats["created" if sku not in state else "updated"] += 1

        state[sku] = {
            "vk": pr.vk_gross,
            "stock": product["stock"],
            "last_seen": datetime.now().isoformat(),
        }

        processed += 1
        if limit and processed >= limit:
            log.warning(f"Limit {limit} erreicht – breche ab")
            break

    # State persistieren
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    # Schlussreport
    log.info("=" * 60)
    log.info("ZUSAMMENFASSUNG")
    for k, v in stats.items():
        log.info(f"  {k:>10}: {v}")
    if filter_reasons:
        log.info("  Filter-Gründe:")
        for r, c in sorted(filter_reasons.items(), key=lambda x: -x[1]):
            log.info(f"    {c:>4}x  {r}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
