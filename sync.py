"""
sync.py
=======
HAUPT-SKRIPT der Dropshipping-Automation.

Was es macht (in dieser Reihenfolge):
  1. Lädt config.yaml und .env
  2. Lädt den CSV-Feed des Lieferanten
  3. Filtert ungewünschte Produkte raus (Gewicht, Kategorien, etc.)
  4. Berechnet für jedes Produkt den Verkaufspreis (Shopify + eBay separat)
  5. Legt Produkte in Shopify an / aktualisiert sie
  6. Legt Produkte auf eBay an / aktualisiert sie (wenn aktiviert)
  7. Artikel die NICHT MEHR im Feed sind → Bestand 0 + eBay offline

Aufruf:
  python sync.py            # normaler Lauf (respektiert dry_run aus config)
  python sync.py --live     # erzwingt Live-Modus
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
from pricing import calculate_vk, calculate_ebay_vk
from shopify_client import ShopifyClient
from ebay_client import EbayClient
from build_matrixify_csv import load_enrichment_index, build_description_html


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


def handle_disappeared_products(
    state: dict,
    seen_skus: set,
    shopify,
    ebay,
    ebay_enabled: bool,
    dry_run: bool,
    log,
    stats: dict,
):
    """
    Artikel die im letzten Lauf vorhanden waren aber JETZT NICHT MEHR im Feed
    auftauchen → sofort offline nehmen.

    Das ist kritisch: Wenn der Lieferant out-of-stock geht und der Artikel
    aus dem Feed fällt, müssen wir eBay SOFORT auf Bestand 0 setzen
    und das Listing deaktivieren.
    """
    disappeared = {sku for sku in state if sku not in seen_skus and not sku.startswith("__")}
    if not disappeared:
        return

    log.warning(f"⚠️  {len(disappeared)} Artikel nicht mehr im Feed → werden offline genommen")

    for sku in disappeared:
        log.warning(f"  OFFLINE: SKU {sku} (war zuletzt: {state[sku].get('last_seen', '?')})")

        # Shopify: Bestand auf 0
        if shopify and not dry_run:
            try:
                existing = shopify.find_product_by_sku(sku)
                if existing:
                    variant = (existing.get("variants") or [{}])[0]
                    inv_item_id = variant.get("inventory_item_id")
                    if inv_item_id:
                        # location_id aus state oder direkt holen
                        loc_id = state[sku].get("shopify_location_id")
                        if loc_id:
                            shopify.set_inventory(
                                inventory_item_id=inv_item_id,
                                location_id=loc_id,
                                available=0,
                            )
                            log.info(f"  Shopify Bestand → 0: SKU {sku}")
            except Exception as e:
                log.error(f"  Shopify offline-Fehler SKU {sku}: {e}")

        # eBay: Bestand 0 + Offer zurückziehen (= Listing deaktiviert)
        if ebay_enabled:
            if dry_run:
                log.info(f"  [eBay DRY-RUN] würde SKU {sku} offline nehmen")
                stats["ebay_offlined"] = stats.get("ebay_offlined", 0) + 1
            elif ebay:
                try:
                    # 1. Bestand auf 0 setzen
                    ebay.set_inventory(sku, 0)
                    # 2. Offer zurückziehen (Listing wird deaktiviert, NICHT gelöscht)
                    offer = ebay.get_offer_for_sku(sku)
                    if offer:
                        ebay.withdraw_offer(offer["offerId"])
                    log.warning(f"  [eBay] ✓ SKU {sku} offline (Bestand 0 + Listing deaktiviert)")
                    stats["ebay_offlined"] = stats.get("ebay_offlined", 0) + 1
                except Exception as e:
                    log.error(f"  [eBay] Offline-Fehler SKU {sku}: {e}")
                    stats["ebay_errors"] += 1

        # State markieren (nicht löschen, für Audit-Trail)
        state[sku]["stock"] = 0
        state[sku]["offline_since"] = datetime.now().isoformat()


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
    stats = {
        "total": 0, "filtered": 0,
        "created": 0, "updated": 0, "errors": 0,
        "ebay_created": 0, "ebay_updated": 0, "ebay_errors": 0, "ebay_offlined": 0,
    }
    filter_reasons: dict[str, int] = {}

    # Shopify-Client (nur wenn shop_domain gesetzt und Token vorhanden)
    shopify = None
    location_id = None
    shopify_token = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
    shopify_domain = cfg["shopify"].get("shop_domain", "")
    if not dry_run and shopify_domain and shopify_token:
        try:
            shopify = ShopifyClient(
                shop_domain=shopify_domain,
                admin_token=shopify_token,
                api_version=cfg["shopify"].get("api_version", "2024-10"),
            )
            location_id = cfg["shopify"].get("location_id") \
                or shopify.get_primary_location_id()
            log.info(f"Shopify-Location-ID: {location_id}")
        except Exception as e:
            log.warning(f"Shopify-Init fehlgeschlagen ({e}) — Shopify wird übersprungen, eBay läuft weiter")
            shopify = None
            location_id = None
    elif not dry_run and shopify_domain and not shopify_token:
        log.warning("SHOPIFY_ADMIN_TOKEN fehlt — Shopify wird übersprungen")
    elif not shopify_domain:
        log.info("Shopify nicht konfiguriert — wird übersprungen")

    # eBay-Client
    ebay = None
    ebay_cfg = cfg.get("ebay", {})
    ebay_pricing_cfg = cfg.get("ebay_pricing", cfg.get("pricing"))  # Fallback auf globales Pricing
    ebay_enabled = ebay_cfg.get("enabled", False)
    if ebay_enabled and not dry_run:
        try:
            ebay = EbayClient.from_env(ebay_cfg)
            log.info(f"eBay-Client initialisiert (Sandbox: {ebay_cfg.get('sandbox', False)})")
        except Exception as e:
            log.warning(f"eBay-Client konnte nicht initialisiert werden: {e} — eBay wird übersprungen")
            ebay = None
    elif ebay_enabled and dry_run:
        log.info("eBay aktiviert aber Dry-Run — eBay-Uploads werden simuliert")

    # Merchant Location sicherstellen (einmalig beim ersten Lauf)
    if ebay and not dry_run:
        addr = ebay_cfg.get("merchant_location_address", {})
        ebay.ensure_merchant_location(addr, config_path=args.config)

    # Enrichment-Index laden (Bilder + Beschreibungen für Shopify & eBay)
    enrichment_idx = {}
    enrichment_path = cfg.get("enrichment", {}).get("index_file", "enrichment_index.csv")
    if Path(enrichment_path).exists():
        enrichment_idx = load_enrichment_index(enrichment_path)
        log.info(f"Enrichment-Index geladen: {len(enrichment_idx)} Einträge")
    else:
        log.warning(f"enrichment_index.csv nicht gefunden — keine Bilder/Beschreibungen")

    # State-Datei laden (für Diff + verschwundene Artikel)
    state_path = Path(cfg["runtime"]["state_file"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    limit = int(cfg["runtime"].get("max_products_per_run", 0))
    processed = 0
    seen_skus: set[str] = set()  # alle SKUs die in diesem Lauf gesehen wurden

    # Rotierender Offset: jeder Lauf verarbeitet die NÄCHSTE Batch
    # So werden alle Produkte über mehrere Runs abgedeckt (z.B. 4×250 = 1000)
    offset = 0
    if limit:
        offset = int(state.get("__next_offset__", 0))
        log.info(f"Rotierender Offset: {offset} (Batch-Größe: {limit})")

    feed_items = list(load_supplier_feed(cfg))
    total_feed = len(feed_items)

    # Nur Produkte ab dem Offset, rest wird für seen_skus weiter unten gebraucht
    # seen_skus muss ALLE SKUs enthalten damit verschwundene korrekt erkannt werden
    all_feed_skus: set[str] = set()
    eligible = []
    for product in feed_items:
        keep, _ = should_keep(product, cfg["filters"])
        if keep:
            all_feed_skus.add(product["sku"])
            eligible.append(product)

    # Batch aus dem Offset herausschneiden (rotierend)
    if limit and offset >= len(eligible):
        offset = 0  # wrap around
        log.info("Offset zurückgesetzt (Ende des Feeds erreicht)")
    batch = eligible[offset:offset + limit] if limit else eligible

    log.info(f"Feed: {total_feed} gesamt, {len(eligible)} nach Filter, Batch: {len(batch)} (offset {offset})")

    for product in batch:
        stats["total"] += 1

        # Shopify-Preis berechnen
        try:
            pr = calculate_vk(product["purchase_price"], cfg["pricing"])
        except Exception as e:
            log.error(f"Pricing-Fehler für SKU {product.get('sku')}: {e}")
            stats["errors"] += 1
            continue

        # eBay-Preis berechnen (separate Konfiguration)
        ebay_pr = None
        if ebay_enabled:
            try:
                ebay_pr = calculate_ebay_vk(
                    product["purchase_price"],
                    ebay_pricing_cfg,
                    shopify_vk_gross=pr.vk_gross,
                )
            except Exception as e:
                log.error(f"eBay-Pricing-Fehler für SKU {product.get('sku')}: {e}")

        sku = product["sku"]

        # Enrichment (Bilder + Beschreibung) per EAN aus enrichment_index
        ean = str(product.get("ean", "")).strip()
        enrichment = enrichment_idx.get(ean) if ean else None
        if enrichment:
            # Hauptbild
            product["image_url"] = enrichment.get("image_main", "") or product.get("image_url", "")
            # Alle Bilder (|-getrennt) → Liste für eBay (bis zu 12 erlaubt)
            all_imgs = [u.strip() for u in enrichment.get("images_all", "").split("|") if u.strip().startswith("http")]
            product["image_urls"] = all_imgs[:12]
            # Beschreibung als fertiges HTML (gleiche Funktion wie Shopify/Matrixify)
            product["description"] = build_description_html(product.get("title", ""), enrichment)
            product["specs_html"] = enrichment.get("specs_html", "")

        log.info(
            f"SKU {sku} | EK {pr.purchase_price_net:.2f}€ | "
            f"Shopify {pr.vk_gross:.2f}€ | "
            f"eBay {ebay_pr.vk_gross:.2f}€" if ebay_pr else
            f"SKU {sku} | EK {pr.purchase_price_net:.2f}€ | Shopify {pr.vk_gross:.2f}€"
        )

        # --- Shopify ---
        if dry_run:
            stats["created" if sku not in state else "updated"] += 1
        elif shopify:
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

        # --- eBay ---
        if ebay_enabled and ebay_pr:
            if dry_run:
                action = "ebay_created" if sku not in state else "ebay_updated"
                stats[action] += 1
                log.info(f"  [eBay DRY-RUN] SKU {sku} → {ebay_pr.vk_gross:.2f}€")
            elif ebay:
                try:
                    result = ebay.upsert_product(product, ebay_pr.vk_gross)
                    action = "ebay_created" if sku not in state else "ebay_updated"
                    stats[action] += 1
                    if result.get("listing_id"):
                        log.info(f"  [eBay] Listing live: {result['listing_id']}")
                except Exception as e:
                    log.error(f"  [eBay] Fehler SKU {sku}: {e}")
                    stats["ebay_errors"] += 1

        state[sku] = {
            "vk": pr.vk_gross,
            "ebay_vk": ebay_pr.vk_gross if ebay_pr else None,
            "stock": product["stock"],
            "shopify_location_id": location_id,
            "last_seen": datetime.now().isoformat(),
        }

        processed += 1

    # Nächsten Offset für den nächsten Lauf speichern (rotierend)
    if limit:
        next_offset = offset + processed
        if next_offset >= len(eligible):
            next_offset = 0  # wrap around → nächster Lauf beginnt von vorne
            log.info("Offset-Rotation: nächster Lauf startet wieder bei 0")
        state["__next_offset__"] = next_offset
        log.info(f"Nächster Offset: {next_offset} (von {len(eligible)} gefilterten Produkten)")

    # =========================================================
    # KRITISCH: Verschwundene Artikel offline nehmen
    # Mit Rotation: seen_skus enthält alle Feed-SKUs → sicheres Diff
    # =========================================================
    seen_skus = all_feed_skus  # gesamter Feed (nicht nur Batch) für Diff
    handle_disappeared_products(
        state=state,
        seen_skus=seen_skus,
        shopify=shopify,
        ebay=ebay,
        ebay_enabled=ebay_enabled,
        dry_run=dry_run,
        log=log,
        stats=stats,
    )

    # State persistieren
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    # Schlussreport
    log.info("=" * 60)
    log.info("ZUSAMMENFASSUNG")
    for k, v in stats.items():
        log.info(f"  {k:>15}: {v}")
    if filter_reasons:
        log.info("  Filter-Gründe:")
        for r, c in sorted(filter_reasons.items(), key=lambda x: -x[1]):
            log.info(f"    {c:>4}x  {r}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
