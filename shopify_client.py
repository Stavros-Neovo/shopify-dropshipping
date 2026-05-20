"""
shopify_client.py
=================
Minimaler Shopify Admin API Wrapper für unsere Dropshipping-Automation.

Verwendet die REST Admin API (einfacher als GraphQL für Bulk-Operationen).
Dokumentation: https://shopify.dev/docs/api/admin-rest
"""
from __future__ import annotations
import logging
import time
from typing import Optional, Dict, Any, List
import requests

log = logging.getLogger(__name__)


class ShopifyClient:
    def __init__(self, shop_domain: str, admin_token: str, api_version: str = "2024-10"):
        # shop_domain z.B. "mein-shop" -> "mein-shop.myshopify.com"
        if not shop_domain:
            raise ValueError("shop_domain fehlt in config.yaml")
        if not admin_token:
            raise ValueError("SHOPIFY_ADMIN_TOKEN fehlt in .env")
        self.base = f"https://{shop_domain}.myshopify.com/admin/api/{api_version}"
        self.headers = {
            "X-Shopify-Access-Token": admin_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Low-level Request mit Retry/Rate-Limit-Handling
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kw) -> dict:
        url = f"{self.base}{path}"
        for attempt in range(5):
            r = requests.request(method, url, headers=self.headers, timeout=30, **kw)
            if r.status_code == 429:
                # Rate limit – warten und retry
                wait = float(r.headers.get("Retry-After", 2))
                log.warning(f"Rate-limit getroffen, warte {wait}s ...")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log.warning(f"Shopify {r.status_code} – retry in 2s ...")
                time.sleep(2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Shopify-Fehler {r.status_code}: {r.text[:300]}"
                )
            return r.json() if r.text else {}
        raise RuntimeError(f"Shopify-Request fehlgeschlagen nach 5 Versuchen: {url}")

    # ------------------------------------------------------------------
    # Locations (für Stock-Sync nötig)
    # ------------------------------------------------------------------
    def get_primary_location_id(self) -> int:
        data = self._request("GET", "/locations.json")
        locs = data.get("locations", [])
        if not locs:
            raise RuntimeError("Keine Locations im Shopify-Shop gefunden")
        # Primary = die aktive Location
        primary = next((l for l in locs if l.get("active")), locs[0])
        return int(primary["id"])

    # ------------------------------------------------------------------
    # Produkt-Suche per SKU (Eindeutigkeit über Variants)
    # ------------------------------------------------------------------
    def find_product_by_sku(self, sku: str) -> Optional[dict]:
        # Variants-Endpoint kann nach SKU filtern
        data = self._request("GET", f"/variants.json?sku={sku}")
        variants = data.get("variants", [])
        if not variants:
            return None
        variant = variants[0]
        product_id = variant["product_id"]
        data = self._request("GET", f"/products/{product_id}.json")
        return data.get("product")

    # ------------------------------------------------------------------
    # Produkt anlegen / aktualisieren
    # ------------------------------------------------------------------
    def upsert_product(self, product_data: dict) -> dict:
        """
        Erwartet ein bereits Shopify-konformes product-Dict.
        Wenn SKU existiert -> Update, sonst Create.
        """
        sku = product_data["variants"][0]["sku"]
        existing = self.find_product_by_sku(sku)
        if existing:
            pid = existing["id"]
            # Bestehende Variant-ID erhalten
            product_data["variants"][0]["id"] = existing["variants"][0]["id"]
            payload = {"product": {**product_data, "id": pid}}
            log.info(f"Update Produkt {pid} (SKU {sku})")
            return self._request("PUT", f"/products/{pid}.json", json=payload)
        else:
            payload = {"product": product_data}
            log.info(f"Create Produkt SKU {sku}")
            return self._request("POST", "/products.json", json=payload)

    # ------------------------------------------------------------------
    # Lagerbestand setzen
    # ------------------------------------------------------------------
    def set_inventory(self, inventory_item_id: int, location_id: int, available: int):
        payload = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available": int(available),
        }
        return self._request("POST", "/inventory_levels/set.json", json=payload)

    # ------------------------------------------------------------------
    # Helfer: ein normalisiertes Produkt-Dict in Shopify-Format umwandeln
    # ------------------------------------------------------------------
    @staticmethod
    def build_product_payload(product: dict, vk_gross: float,
                              status: str = "active") -> dict:
        """
        Erstellt das Shopify-Produkt-Payload aus unserem internen Format.

        Args:
            product: normalisiertes Produkt-Dict (aus csv_loader)
            vk_gross: berechneter Verkaufspreis brutto
            status: "active" | "draft" | "archived"
        """
        images = []
        if product.get("image_url"):
            images.append({"src": product["image_url"]})

        body_html = product.get("description", "") or product.get("title", "")

        return {
            "title": product.get("title", "Unbenanntes Produkt"),
            "body_html": body_html,
            "vendor": product.get("brand", "") or "",
            "product_type": product.get("category", "") or "",
            "status": status,
            "tags": ",".join(filter(None, [
                product.get("category", ""),
                product.get("brand", ""),
            ])),
            "images": images,
            "variants": [{
                "sku": product["sku"],
                "price": f"{vk_gross:.2f}",
                "barcode": product.get("ean", "") or "",
                "weight": product.get("weight_kg", 0.0),
                "weight_unit": "kg",
                "inventory_management": "shopify",
                "inventory_policy": "deny",  # nicht überverkaufen
                "requires_shipping": True,
                "taxable": True,
            }],
        }
