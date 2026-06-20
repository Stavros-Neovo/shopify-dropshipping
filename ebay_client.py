"""
ebay_client.py
==============
eBay REST Sell API Client für die Dropshipping-Automation.

Verwendet:
  - Inventory API  → Produkte & Bestand verwalten (SKU-basiert)
  - Offer API      → Listings erstellen/aktivieren
  - OAuth 2.0      → Access Token automatisch refreshen

Aufruf-Muster (analog shopify_client.py):
    client = EbayClient.from_env(cfg["ebay"])
    client.upsert_product(product, vk_gross)
    client.set_inventory(sku, qty)

Doku:
    https://developer.ebay.com/api-docs/sell/inventory/overview.html
    https://developer.ebay.com/api-docs/sell/account/overview.html
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import yaml

log = logging.getLogger(__name__)

# eBay REST Basis-URLs
PROD_BASE = "https://api.ebay.com"
SANDBOX_BASE = "https://api.sandbox.ebay.com"

# Endpunkte
TOKEN_PATH = "/identity/v1/oauth2/token"
INVENTORY_PATH = "/sell/inventory/v1/inventory_item"
OFFER_PATH = "/sell/inventory/v1/offer"
LOCATION_PATH = "/sell/inventory/v1/location"


# Kategorie-Mapping laden (einmalig beim Import)
_CAT_MAP: Dict[str, str] = {}
_DEFAULT_CAT = "31388"  # Computer, Tablets & Netzwerk → Zubehör (eBay.de Blatt-Kategorie)

# eBay.de Taxonomy Tree ID
_EBAY_DE_TREE_ID = "77"

# ---------------------------------------------------------------------------
# Sicherheits-Bestandslogik: verhindert Überverkäufe
# ---------------------------------------------------------------------------
# Regeln:
#   - Stock = 0          → 0 (Artikel offline)
#   - Stock 1–3          → 1 (niedriger Bestand: immer nur 1 anzeigen)
#   - Stock 4–20         → echten Bestand anzeigen
#   - Stock > 20         → auf 20 deckeln (kein Risiko mit zu hohen Mengen)
# Hintergrund: Der Feed wird in Batches aktualisiert (rotierender Offset).
# Zwischen zwei Batch-Runs können 1-4 Stunden vergehen. Mit dieser Regel
# schützen wir uns gegen Überverkauf bei niedrigem Lagerbestand.
STOCK_LOW_THRESHOLD  = 3   # ≤ X Stück → nur 1 auf eBay anzeigen
STOCK_MAX_DISPLAY    = 20  # > X Stück → auf 20 deckeln


def safe_ebay_stock(raw_stock: int) -> int:
    """Gibt den sicheren eBay-Anzeigebestand zurück.
    Dropshipping-Prinzip: immer 1 anzeigen solange Lieferant Stock hat.
    Nach Verkauf → nächster Sync setzt wieder auf 1.
    Verhindert Überverkäufe und reduziert eBay-Listenwert (Limit-Schutz).
    """
    if raw_stock <= 0:
        return 0
    return 1


# Kategorien die Pflichtmerkmale erfordern die wir nicht haben (CPU-Typ, GPU-Modell etc.)
# Taxonomy-Vorschläge aus diesen Kategorien werden ignoriert → Fallback auf DEFAULT
_COMPLEX_CATEGORIES = {
    "177",    # PC Notebooks & Netbooks  → Bildschirmgröße, RAM etc. nicht füllbar
    "179",    # Desktop-PCs              → zu viele Hardware-Specs
    "171485", # Tablets & eBook-Reader   → Betriebssystem/OS komplex
    "15032",  # Tablet-Zubehör           → Plattform-spezifisch
    "11211",  # Server-Hardware          → Prozessor, Formfaktor nicht füllbar
    "168061", # Notebook-Ersatzteile     → Modell-Kompatibilität nicht füllbar
    # CPUs (164), Grafikkarten (27386), Mainboards (1244) → werden über
    # _category_is_feasible() + _fill_aspect() dynamisch geprüft und gefüllt
}

def _load_category_map(path: str = "ebay_categories.yaml"):
    global _CAT_MAP, _DEFAULT_CAT
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        _CAT_MAP = data.get("category_map", {})
        _DEFAULT_CAT = str(data.get("default_category", "31388"))
    except Exception as e:
        log.warning(f"Kategorie-Mapping nicht geladen: {e} — Fallback-Kategorie wird genutzt")

_load_category_map()

# Titel-Keyword-Erkennung fuer gemischte BAB-Gruppen (CE/ZUBEHÖR/HW/GA/PPS),
# die keinem einzelnen ItemMainGroup->eBay-Mapping zugeordnet werden koennen.
# Wird VOR der Taxonomy-API gepruft, da die in get_category_for_title() oft
# Vorschlaege liefert die an _category_is_feasible() scheitern und dann auf
# den "Kabel & Adapter"-Default zurueckfallen, obwohl der Titel eindeutig ist.
# Alle IDs unten am 20.06. gegen die Taxonomy API verifiziert (Vorschlag +
# _category_is_feasible()==True), NICHT geraten.
_TITLE_KEYWORD_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bswitch\b|\brouter\b|access\s*point|range\s*extender", re.I), "51268"),   # Netzwerk
    (re.compile(r"led-lampe|leuchtstreifen|light\s*bar|light\s*panel|glide\s*wall|glide\s*hexa", re.I), "20706"),  # Leuchtmittel
    (re.compile(r"katzenstreu|cat\s*litter|katzenklo|litter\s*box", re.I), "116363"),  # Katzenstreu
    (re.compile(r"powerbank|power\s*bank", re.I),                    "20357"),   # Akkus
    (re.compile(r"zahnbürste|toothbrush", re.I),                     "31770"),   # Elektrische Zahnbürsten
    (re.compile(r"haartrockner|haarstyler", re.I),                   "11858"),   # Haartrockner
    (re.compile(r"\btrimmer\b|rasierer|bartschneider|epilierer", re.I), "67408"), # Haar- & Bartschneidegeräte
    (re.compile(r"dockingstation|dock\b", re.I),                     "31510"),   # Notebooks & Zubehör
    (re.compile(r"überwachungskamera|security\s*camera|haustierroboter", re.I), "48638"),  # Überwachungskameras
]


def _guess_category_from_title(title: str) -> str:
    """Keyword-Fallback fuer gemischte BAB-Gruppen, siehe _TITLE_KEYWORD_MAP."""
    for pattern, cat_id in _TITLE_KEYWORD_MAP:
        if pattern.search(title):
            return cat_id
    return ""

# Cache für Taxonomy-Lookups (title → categoryId)
_TAXONOMY_CACHE: Dict[str, str] = {}

# Cache für Kategorie-Aspekte (categoryId → list of required aspect names)
_ASPECTS_CACHE: Dict[str, list] = {}

# Aspekte die wir IMMER füllen können (Kleinschreibung für Vergleich)
_FILLABLE_ASPECTS = {
    "marke", "produktart", "kompatibilität", "herstellernummer",
    "plattform", "farbe", "produktfarbe", "einheiten im paket",
    "modifizierter artikel", "angepasstes paket", "betriebssystem",
    "anzahl der ports", "kabellänge", "steckertyp", "schnittstelle",
    "typ", "anschluss", "anschlüsse", "farbe/oberfläche", "gewicht",
    "set beinhaltet", "bundlebestandteil", "herstellungsland und -region",
    "mit originalverpackung",
}

# Aspekte die wir NICHT füllen können (führen zu Kategorie-Skip)
# HINWEIS: Prozessormodell, Prozessorfamilie werden aus Titel extrahiert → nicht hier!
_UNFILLABLE_ASPECTS = {
    "chipsatz/gpu-modell", "gpu-modell", "grafikkarte",
    "ram-kapazität", "bildschirmgröße", "auflösung",
    "festplattenkapazität", "festplattentyp",
    "max. ram-kapazität", "speicherkapazität (ram)", "arbeitsspeichertyp",
    "bildschirm", "bildschirmtechnologie", "gpu",
    "anzahl der prozessorkerne",   # zu spezifisch ohne Datenbank
}

# CPU Sockel-Mapping (Modell-Substring → Sockel)
_CPU_SOCKET_MAP = [
    # AMD
    ("9950x", "AM5"), ("9900x", "AM5"), ("9700x", "AM5"), ("9600x", "AM5"),
    ("7950x", "AM5"), ("7900x", "AM5"), ("7800x", "AM5"), ("7700x", "AM5"),
    ("7600x", "AM5"), ("7700",  "AM5"), ("7600",  "AM5"), ("7500f", "AM5"),
    ("5950x", "AM4"), ("5900x", "AM4"), ("5800x", "AM4"), ("5700x", "AM4"),
    ("5600x", "AM4"), ("5600",  "AM4"), ("5500",  "AM4"), ("5300g", "AM4"),
    ("4600g", "AM4"), ("3600x", "AM4"), ("3600",  "AM4"),
    # Intel (LGA)
    ("14900", "LGA1700"), ("14700", "LGA1700"), ("14600", "LGA1700"),
    ("13900", "LGA1700"), ("13700", "LGA1700"), ("13600", "LGA1700"), ("13400", "LGA1700"),
    ("12900", "LGA1700"), ("12700", "LGA1700"), ("12600", "LGA1700"), ("12400", "LGA1700"),
    ("i9-1", "LGA1700"), ("i7-1", "LGA1700"), ("i5-1", "LGA1700"), ("i3-1", "LGA1700"),
]


def get_ebay_category_id(bab_category: str) -> str:
    """Gibt die eBay-Kategorie-ID für eine BAB-Kategorie zurück."""
    return str(_CAT_MAP.get(bab_category, _DEFAULT_CAT))


class EbayClient:
    """Minimaler eBay REST Sell API Client."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        marketplace_id: str = "EBAY_DE",
        fulfillment_policy_id: str = "",
        payment_policy_id: str = "",
        return_policy_id: str = "",
        merchant_location_key: str = "",
        sandbox: bool = False,
    ):
        if not client_id:
            raise ValueError("EBAY_CLIENT_ID fehlt")
        if not client_secret:
            raise ValueError("EBAY_CLIENT_SECRET fehlt")
        if not refresh_token:
            raise ValueError("EBAY_REFRESH_TOKEN fehlt")

        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.marketplace_id = marketplace_id
        self.fulfillment_policy_id = fulfillment_policy_id
        self.payment_policy_id = payment_policy_id
        self.return_policy_id = return_policy_id
        self.merchant_location_key = merchant_location_key
        self.base = SANDBOX_BASE if sandbox else PROD_BASE
        self.sandbox = sandbox

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._app_token: Optional[str] = None
        self._app_token_expires_at: float = 0.0

    @classmethod
    def from_env(cls, ebay_cfg: Dict[str, Any]) -> "EbayClient":
        """Erstellt Client aus config.yaml + .env Umgebungsvariablen."""
        return cls(
            client_id=os.environ.get("EBAY_CLIENT_ID", ""),
            client_secret=os.environ.get("EBAY_CLIENT_SECRET", ""),
            refresh_token=os.environ.get(ebay_cfg.get("refresh_token_env_var", "EBAY_REFRESH_TOKEN"), ""),
            marketplace_id=ebay_cfg.get("marketplace_id", "EBAY_DE"),
            fulfillment_policy_id=ebay_cfg.get("fulfillment_policy_id", ""),
            payment_policy_id=ebay_cfg.get("payment_policy_id", ""),
            return_policy_id=ebay_cfg.get("return_policy_id", ""),
            merchant_location_key=ebay_cfg.get("merchant_location_key", ""),
            sandbox=ebay_cfg.get("sandbox", False),
        )

    # ------------------------------------------------------------------
    # OAuth: Access Token automatisch refreshen
    # ------------------------------------------------------------------
    def _get_access_token(self) -> str:
        """Gibt gültigen Access Token zurück; refresht automatisch wenn nötig."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        r = requests.post(
            f"{self.base}{TOKEN_PATH}",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "scope": (
                    "https://api.ebay.com/oauth/api_scope/sell.inventory "
                    "https://api.ebay.com/oauth/api_scope/sell.account "
                    "https://api.ebay.com/oauth/api_scope/sell.fulfillment"
                ),
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"eBay Token-Refresh fehlgeschlagen: {r.status_code} {r.text[:300]}")

        data = r.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
        log.debug("eBay Access Token erneuert")
        return self._access_token

    def _get_app_token(self) -> str:
        """Application Token (Client Credentials) für öffentliche APIs wie Taxonomy."""
        if self._app_token and time.time() < self._app_token_expires_at - 60:
            return self._app_token

        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        r = requests.post(
            f"{self.base}{TOKEN_PATH}",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"eBay App-Token fehlgeschlagen: {r.status_code} {r.text[:200]}")
        data = r.json()
        self._app_token = data["access_token"]
        self._app_token_expires_at = time.time() + int(data.get("expires_in", 7200))
        return self._app_token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Content-Language": "de-DE",
        }

    # ------------------------------------------------------------------
    # Low-level Request mit Retry
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        json_body: Optional[Dict] = None,
    ) -> Optional[Dict]:
        url = f"{self.base}{path}"
        for attempt in range(3):
            try:
                r = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=15,
                )
            except requests.RequestException as e:
                log.warning(f"eBay Request-Fehler (Versuch {attempt+1}): {e}")
                time.sleep(2)
                continue

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 5))
                log.warning(f"eBay Rate-Limit, warte {wait}s ...")
                time.sleep(wait)
                continue

            if r.status_code in (500, 502, 503, 504):
                log.warning(f"eBay {r.status_code} – retry in 3s ...")
                time.sleep(3)
                continue

            # 204 No Content = Erfolg ohne Body
            if r.status_code == 204:
                return {}

            if r.status_code >= 400:
                raise RuntimeError(
                    f"eBay API Fehler {r.status_code}: {r.text[:500]}"
                )

            return r.json() if r.text else {}

        raise RuntimeError(f"eBay Request fehlgeschlagen nach 5 Versuchen: {url}")

    # ------------------------------------------------------------------
    # Merchant Location: automatisch anlegen wenn nicht vorhanden
    # ------------------------------------------------------------------
    def ensure_merchant_location(self, address_cfg: Dict[str, str], config_path: str = "config.yaml") -> str:
        """
        Stellt sicher dass ein Merchant Location existiert.
        Falls merchant_location_key leer ist:
          1. Prüft ob "hauptlager_de" bereits existiert
          2. Falls nicht → legt ihn an mit der Adresse aus config.yaml
          3. Schreibt den Key zurück in config.yaml

        Returns:
            Den merchant_location_key
        """
        default_key = self.merchant_location_key if self.merchant_location_key else "hauptlager_de"
        log.info(f"Prüfe ob Merchant Location '{default_key}' existiert...")

        # Prüfen ob schon vorhanden
        try:
            result = self._request("GET", f"{LOCATION_PATH}/{default_key}")
            if result:
                log.info(f"Merchant Location '{default_key}' existiert bereits ✓")
                self.merchant_location_key = default_key
                self._write_location_key_to_config(default_key, config_path)
                return default_key
        except RuntimeError:
            pass  # existiert noch nicht → anlegen

        # Anlegen
        log.info(f"Lege Merchant Location '{default_key}' an...")
        payload = {
            "location": {
                "address": {
                    "addressLine1": address_cfg.get("address1", ""),
                    "city": address_cfg.get("city", ""),
                    "postalCode": address_cfg.get("postal_code", ""),
                    "country": address_cfg.get("country", "DE"),
                }
            },
            "locationInstructions": "Dropshipping-Lager",
            "name": address_cfg.get("name", "Hauptlager"),
            "merchantLocationStatus": "ENABLED",
            "locationTypes": ["WAREHOUSE"],
        }

        try:
            self._request("POST", f"{LOCATION_PATH}/{default_key}", json_body=payload)
            log.info(f"Merchant Location '{default_key}' erfolgreich angelegt ✓")
        except RuntimeError as e:
            log.warning(f"Merchant Location konnte nicht angelegt werden: {e}")
            return ""

        self.merchant_location_key = default_key
        self._write_location_key_to_config(default_key, config_path)
        return default_key

    def _write_location_key_to_config(self, key: str, config_path: str):
        """Schreibt den merchant_location_key zurück in config.yaml."""
        try:
            import re
            content = Path(config_path).read_text(encoding="utf-8")
            content = re.sub(
                r'(merchant_location_key:\s*)"[^"]*"',
                f'\\1"{key}"',
                content,
            )
            Path(config_path).write_text(content, encoding="utf-8")
            log.info(f"merchant_location_key '{key}' in config.yaml gespeichert")
        except Exception as e:
            log.warning(f"Konnte merchant_location_key nicht in config.yaml schreiben: {e}")

    # ------------------------------------------------------------------
    # Inventory Item (Produkt-Stammdaten)
    # ------------------------------------------------------------------
    def _build_inventory_item(self, product: Dict, vk_gross: float, category_id: str = "") -> Dict:
        """Erstellt eBay Inventory Item Payload aus unserem internen Format."""
        # Beschreibung: aus enrichment_index oder Fallback auf Title
        description = product.get("description") or product.get("title", "")
        if description == product.get("title", ""):
            # Minimal-Beschreibung wenn keine echte vorhanden
            description = (
                f"{product.get('title', '')}<br>"
                f"Marke: {product.get('brand', '')}<br>"
                f"Kategorie: {product.get('category', '')}"
            )

        # Aspekte (Pflichtmerkmale) ableiten
        aspects = self._build_aspects(product, category_id)

        payload: Dict[str, Any] = {
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": safe_ebay_stock(int(product.get("stock", 0)))
                }
            },
            "condition": "NEW",
            "product": {
                "title": product.get("title", "")[:80],  # eBay max 80 Zeichen
                "description": description[:4000],        # eBay max 4000 Zeichen
                "aspects": aspects,
            },
        }

        # EAN/GTIN wenn vorhanden
        ean = product.get("ean", "")
        if ean and len(str(ean)) in (8, 12, 13):
            payload["product"]["ean"] = [str(ean)]

        # Bilder: mehrere wenn vorhanden (eBay erlaubt bis zu 12)
        image_urls = product.get("image_urls") or []
        if not image_urls and product.get("image_url"):
            image_urls = [product["image_url"]]
        if image_urls:
            payload["product"]["imageUrls"] = image_urls[:12]

        # Gewicht
        weight_kg = product.get("weight_kg", 0)
        if weight_kg and float(weight_kg) > 0:
            payload["packageWeightAndSize"] = {
                "weight": {
                    "value": float(weight_kg),
                    "unit": "KILOGRAM",
                }
            }

        return payload

    def create_or_update_inventory_item(self, sku: str, product: Dict, vk_gross: float, category_id: str = ""):
        """Legt Inventory Item an oder aktualisiert es (PUT = upsert)."""
        payload = self._build_inventory_item(product, vk_gross, category_id=category_id)
        self._request("PUT", f"{INVENTORY_PATH}/{sku}", json_body=payload)
        log.info(f"eBay Inventory Item gesetzt: SKU {sku}")

    # ------------------------------------------------------------------
    # Offer (Listing = Preis + Policies)
    # ------------------------------------------------------------------
    def get_offer_for_sku(self, sku: str) -> Optional[Dict]:
        """Gibt das erste Offer für eine SKU zurück, oder None."""
        try:
            data = self._request(
                "GET", OFFER_PATH, params={"sku": sku, "marketplace_id": self.marketplace_id}
            )
            offers = (data or {}).get("offers", [])
            return offers[0] if offers else None
        except RuntimeError:
            return None

    @staticmethod
    def _derive_produktart(title: str, category_id: str) -> str:
        """Leitet den eBay-Aspekt 'Produktart' aus Titel und Kategorie ab."""
        t = title.lower()

        # Kabel & Adapter (44932, 158840, 32834)
        if category_id in ("44932", "158840", "32834", "31388"):
            if "hdmi" in t:                               return "HDMI-Kabel"
            if "displayport" in t or " dp " in t:         return "DisplayPort-Kabel"
            if "thunderbolt" in t:                        return "Thunderbolt-Kabel"
            if ("usb-c" in t or "usb c" in t or "type-c" in t or "type c" in t):
                if "hub" in t:                            return "USB-Hub"
                if "adapter" in t:                        return "USB-Adapter"
                return "USB-C-Kabel"
            if "usb" in t:
                if "hub" in t:                            return "USB-Hub"
                if "adapter" in t:                        return "USB-Adapter"
                if "verlänger" in t:                      return "USB-Verlängerungskabel"
                return "USB-Kabel"
            if "ethernet" in t or "rj45" in t or " lan " in t or "patch" in t:
                return "Netzwerkkabel"
            if "klinke" in t or "aux" in t or "3,5" in t or "3.5mm" in t:
                return "Audiokabel"
            if "vga" in t:                                return "VGA-Kabel"
            if "dvi" in t:                                return "DVI-Kabel"
            if "optical" in t or "optisch" in t:          return "Glasfaserkabel"
            if "adapter" in t:                            return "Adapter"
            if "hub" in t:                                return "Hub"
            if "kabel" in t or "cable" in t:              return "Kabel"
            return "Adapter"

        # Netzwerk (51268, 11180)
        if category_id in ("51268", "11180"):
            if "switch" in t:  return "Netzwerk-Switch"
            if "router" in t:  return "Router"
            if "wlan" in t or "wifi" in t or "wi-fi" in t or "wireless" in t:
                return "WLAN-Access-Point"
            return "Netzwerk-Switch"

        # Storage
        if category_id == "175669": return "Interne SSD"
        if category_id == "56083":  return "Interne Festplatte"
        if category_id == "175673":
            if "stick" in t or "flash" in t:  return "USB-Stick"
            if "micro" in t:                  return "MicroSD-Karte"
            if "sd" in t:                     return "SD-Karte"
            return "Speicherkarte"

        # RAM
        if category_id == "170083": return "RAM"

        # Drucker
        if category_id == "1245":
            if "scanner" in t:  return "Scanner"
            return "Drucker"
        if category_id == "16191":
            if "toner" in t:    return "Toner"
            return "Tintenpatrone"

        # Webcam
        if category_id == "4616": return "Webcam"

        # Netzteile
        if category_id in ("42017", "31510"):
            if "notebook" in t or "laptop" in t: return "Notebook-Netzteil"
            return "Netzteil"

        # Beamer
        if category_id == "26392": return "Beamer"

        # CPUs (category 164)
        if category_id == "164":
            if "box" in t:   return "Prozessor (CPU) – Boxed"
            if "tray" in t:  return "Prozessor (CPU) – Tray"
            return "Prozessor (CPU)"

        # Grafikkarten (27386)
        if category_id == "27386":
            if "amd" in t or "radeon" in t:    return "AMD Grafikkarte"
            if "nvidia" in t or "geforce" in t: return "NVIDIA Grafikkarte"
            return "Grafikkarte"

        # Mainboards (1244)
        if category_id == "1244": return "Mainboard"

        return "Sonstiges"

    def _fetch_category_aspects(self, category_id: str) -> list:
        """
        Holt Pflichtmerkmale für eine Kategorie via eBay Taxonomy API.
        Gibt Liste der required Aspect-Namen zurück (Kleinschreibung).
        Cached pro Kategorie.
        """
        if category_id in _ASPECTS_CACHE:
            return _ASPECTS_CACHE[category_id]
        try:
            app_token = self._get_app_token()
            r = requests.get(
                f"{self.base}/commerce/taxonomy/v1/category_tree/{_EBAY_DE_TREE_ID}"
                f"/get_item_aspects_for_category",
                headers={"Authorization": f"Bearer {app_token}", "Accept": "application/json"},
                params={"category_id": category_id},
                timeout=15,
            )
            if r.status_code == 200:
                required = [
                    a["localizedAspectName"].lower()
                    for a in r.json().get("aspects", [])
                    if a.get("aspectConstraint", {}).get("aspectRequired", False)
                ]
                _ASPECTS_CACHE[category_id] = required
                log.debug(f"Pflichtmerkmale für Kat {category_id}: {required}")
                return required
        except Exception as e:
            log.debug(f"Aspects-Fetch Kat {category_id} fehlgeschlagen: {e}")
        _ASPECTS_CACHE[category_id] = []
        return []

    def _category_is_feasible(self, category_id: str) -> bool:
        """
        Prüft ob wir alle Pflichtmerkmale für diese Kategorie füllen können.
        Kategorien mit unfüllbaren Pflichtmerkmalen (CPU-Modell, GPU etc.) → False.
        """
        if category_id in _COMPLEX_CATEGORIES:
            return False
        required = self._fetch_category_aspects(category_id)
        for asp in required:
            if asp in _UNFILLABLE_ASPECTS:
                log.debug(f"Kategorie {category_id} nicht verwendbar: Pflichtmerkmal '{asp}' nicht füllbar")
                _COMPLEX_CATEGORIES.add(category_id)  # für diesen Run merken
                return False
        return True

    @staticmethod
    def _extract_from_title(title: str, patterns: list, default: str = "") -> str:
        """Einfacher Keyword-Extraktor aus Produkttitel."""
        import re
        t = title.lower()
        for pattern, value in patterns:
            if re.search(pattern, t):
                return value
        return default

    def _fill_aspect(self, name: str, product: Dict, category_id: str) -> Optional[list]:
        """
        Versucht einen Aspekt-Wert aus Produktdaten abzuleiten.
        Gibt None zurück wenn nicht möglich.
        """
        import re
        title = product.get("title", "")
        t = title.lower()
        brand = product.get("brand", "")

        n = name.lower()

        if n == "marke":
            return [brand] if brand else ["Ohne Markierung"]

        if n == "produktart":
            return [self._derive_produktart(title, category_id)]

        if n in ("kompatibilität", "kompatibilit"):
            return ["Universal"]

        if n == "plattform":
            if "mac" in t or "apple" in t:        return ["Mac OS", "Windows"]
            if "linux" in t:                       return ["Linux", "Windows"]
            if "android" in t:                     return ["Android"]
            return ["Windows", "Mac OS", "Linux"]

        if n == "betriebssystem":
            if "windows 11" in t:  return ["Windows 11"]
            if "windows 10" in t:  return ["Windows 10"]
            if "mac" in t:         return ["macOS"]
            if "linux" in t:       return ["Linux"]
            return ["Windows 10", "Windows 11"]

        if n in ("farbe", "produktfarbe", "farbe/oberfläche"):
            colors = {
                "schwarz": "Schwarz", "black": "Schwarz",
                "weiß": "Weiß", "weiss": "Weiß", "white": "Weiß",
                "silber": "Silber", "silver": "Silber",
                "grau": "Grau", "gray": "Grau", "grey": "Grau",
                "blau": "Blau", "blue": "Blau",
                "rot": "Rot", "red": "Rot",
                "grün": "Grün", "green": "Grün",
            }
            for kw, color in colors.items():
                if kw in t:
                    return [color]
            return ["Schwarz"]

        if n == "herstellernummer":
            return [product.get("sku", "")]

        if n in ("einheiten im paket", "anzahl pro paket"):
            return ["1"]

        if n in ("modifizierter artikel",):
            return ["Nein"]

        if n in ("angepasstes paket", "individuelle bundle"):
            return ["Nein"]

        if n in ("mit originalverpackung",):
            return ["Ja"]

        if n in ("herstellungsland und -region", "herkunftsland"):
            return ["China"]

        if n in ("anzahl der ports", "anzahl ports"):
            m = re.search(r"(\d+)\s*-?\s*port", t)
            if m: return [m.group(1)]
            return ["1"]

        if n in ("kabellänge", "länge", "kabellänge (m)"):
            m = re.search(r"(\d+(?:[,.]\d+)?)\s*(m\b|meter|cm)", t)
            if m:
                val, unit = m.group(1), m.group(2)
                if "cm" in unit:
                    return [f"{float(val.replace(',','.'))/100:.1f} m"]
                return [f"{val} m"]
            return ["1 m"]

        if n in ("steckertyp", "stecker typ a", "stecker typ b", "anschluss a", "anschluss b"):
            if "usb-c" in t or "type-c" in t:  return ["USB-C"]
            if "usb-a" in t or "type-a" in t:  return ["USB-A"]
            if "usb-b" in t:                   return ["USB-B"]
            if "hdmi" in t:                     return ["HDMI"]
            if "rj45" in t:                     return ["RJ-45"]
            if "lightning" in t:                return ["Lightning"]
            if "usb" in t:                      return ["USB-A"]
            return ["USB-A"]

        if n in ("schnittstelle", "anschlüsse"):
            if "usb 3" in t or "usb3" in t:   return ["USB 3.0"]
            if "usb 2" in t or "usb2" in t:   return ["USB 2.0"]
            if "hdmi 2" in t:                  return ["HDMI 2.0"]
            if "hdmi" in t:                    return ["HDMI"]
            if "displayport" in t:             return ["DisplayPort"]
            if "usb-c" in t:                   return ["USB-C"]
            if "usb" in t:                     return ["USB"]
            if "ethernet" in t or "rj45" in t: return ["RJ-45 (Ethernet)"]
            return ["USB"]

        if n in ("übertragungsrate", "datenübertragungsrate"):
            if "usb 3.2" in t:                return ["10 Gbit/s"]
            if "usb 3.1" in t:                return ["10 Gbit/s"]
            if "usb 3.0" in t or "usb 3" in t: return ["5 Gbit/s"]
            if "usb 2.0" in t or "usb 2" in t: return ["480 Mbit/s"]
            if "1000" in t or "gigabit" in t or "gbit" in t: return ["1 Gbit/s"]
            if "100mbit" in t or "100 mbit" in t: return ["100 Mbit/s"]
            return ["480 Mbit/s"]

        if n == "typ":
            return [self._derive_produktart(title, category_id)]

        # --- CPU-spezifische Aspekte ---
        if n in ("prozessormodell", "modellnummer"):
            import re as _re
            # AMD Ryzen 9 5950X → "Ryzen 9 5950X"
            m = _re.search(r"(ryzen\s*[359]\s*[\w]+|threadripper\s*[\w]+|athlon\s*[\w]+)", t)
            if m: return [m.group(0).title()]
            # Intel Core i9-13900K → "Core i9-13900K"
            m = _re.search(r"(core\s*i[3579]-[\w]+|xeon\s*[\w-]+|celeron\s*[\w]+|pentium\s*[\w]+)", t)
            if m: return [m.group(0).title()]
            # Intel N100, N200, N305 etc.
            m = _re.search(r"\b(n\d{3,4}|ultra\s*[\w-]+)\b", t)
            if m: return [m.group(0).title()]
            # Fallback: alles nach "amd"/"intel" bis box/tray/ende
            m = _re.search(r"(?:amd|intel)\s+([\w\s-]{3,30}?)(?:\s+box|\s+tray|\s+\||\s*$)", t)
            if m: return [m.group(1).strip().title()]
            # Letzter Fallback: ganzer Titel ohne Marke
            cleaned = _re.sub(r"(?i)\b(intel|amd|box|tray)\b", "", title).strip(" -–")
            if cleaned:
                return [cleaned[:80]]
            return None

        if n == "prozessorfamilie":
            if "ryzen 9" in t: return ["AMD Ryzen 9"]
            if "ryzen 7" in t: return ["AMD Ryzen 7"]
            if "ryzen 5" in t: return ["AMD Ryzen 5"]
            if "ryzen 3" in t: return ["AMD Ryzen 3"]
            if "threadripper" in t: return ["AMD Threadripper"]
            if "athlon" in t:   return ["AMD Athlon"]
            if "core i9" in t:  return ["Intel Core i9"]
            if "core i7" in t:  return ["Intel Core i7"]
            if "core i5" in t:  return ["Intel Core i5"]
            if "core i3" in t:  return ["Intel Core i3"]
            if "xeon" in t:     return ["Intel Xeon"]
            if "celeron" in t:  return ["Intel Celeron"]
            if "pentium" in t:  return ["Intel Pentium"]
            return None

        if n in ("sockel", "buchse/buchsentyp", "buchse"):
            tl = t
            for substr, socket in _CPU_SOCKET_MAP:
                if substr in tl:
                    return [socket]
            return None

        if n in ("prozessorgeschwindigkeit", "taktrate"):
            import re as _re
            m = _re.search(r"(\d+[,.]\d+)\s*ghz", t)
            if m: return [f"{m.group(1).replace(',','.')} GHz"]
            return None

        if n in ("anzahl der kerne", "kernanzahl"):
            # Known core counts from model names
            core_map = {
                "7950x": "16", "7900x": "12", "7800x3d": "8", "7700x": "8", "7700": "8",
                "7600x": "6", "7600": "6", "5950x": "16", "5900x": "12", "5800x": "8",
                "5600x": "6", "5600": "6", "14900": "24", "13900": "24", "12900": "16",
                "14700": "20", "13700": "16", "12700": "12", "14600": "14", "13600": "14",
                "12600": "10", "13400": "10", "12400": "6",
            }
            for model, cores in core_map.items():
                if model in t:
                    return [cores]
            return None

        return None

    def _build_aspects(self, product: Dict, category_id: str) -> Dict:
        """
        Generiert Item Specifics für das Inventory Item.
        Nutzt getItemAspectsForCategory um alle Pflichtmerkmale zu kennen,
        und füllt sie automatisch aus Produktdaten.
        """
        title = product.get("title", "")
        brand = product.get("brand", "")
        aspects: Dict[str, list] = {}

        # Immer-Aspekte (eBay akzeptiert diese in fast allen IT-Kategorien)
        aspects["Marke"] = [brand] if brand else ["Ohne Markierung"]
        aspects["Produktart"] = [self._derive_produktart(title, category_id)]
        aspects["Kompatibilität"] = ["Universal"]

        if product.get("sku"):
            aspects["Herstellernummer"] = [product["sku"]]

        # Pflichtmerkmale der Kategorie dynamisch füllen
        required_aspects = self._fetch_category_aspects(category_id)
        for asp_name in required_aspects:
            # Bereits gesetzt? → skip
            if any(asp_name.lower() == k.lower() for k in aspects):
                continue
            value = self._fill_aspect(asp_name, product, category_id)
            if value:
                aspects[asp_name] = value
            else:
                log.debug(f"Aspekt '{asp_name}' für SKU {product.get('sku','')} nicht füllbar")

        return aspects

    def get_category_for_title(self, title: str, bab_category: str = "") -> str:
        """
        Gibt eine gültige eBay.de Blatt-Kategorie-ID zurück.
        Prüft zuerst das YAML-Mapping, dann fragt die Taxonomy API.
        """
        # 1. Manuelles Mapping hat Vorrang
        if bab_category and bab_category in _CAT_MAP:
            return _CAT_MAP[bab_category]

        # 1b. Titel-Keyword-Erkennung fuer gemischte BAB-Gruppen (CE/ZUBEHÖR/HW/GA/PPS)
        guessed = _guess_category_from_title(title)
        if guessed:
            return guessed

        # 2. Taxonomy-Cache prüfen
        cache_key = title[:40].lower()
        if cache_key in _TAXONOMY_CACHE:
            return _TAXONOMY_CACHE[cache_key]

        # 3. eBay Taxonomy API anfragen (benötigt Application Token)
        try:
            app_token = self._get_app_token()
            r = requests.get(
                f"{self.base}/commerce/taxonomy/v1/category_tree/{_EBAY_DE_TREE_ID}/get_category_suggestions",
                headers={"Authorization": f"Bearer {app_token}", "Accept": "application/json"},
                params={"q": title[:80]},
                timeout=10,
            )
            if r.status_code == 200:
                suggestions = r.json().get("categorySuggestions", [])
                for s in suggestions:
                    cat_id = str(s["category"]["categoryId"])
                    cat_name = s["category"].get("categoryName", "?")
                    if not self._category_is_feasible(cat_id):
                        log.info(f"Taxonomy '{title[:30]}': {cat_id} ({cat_name}) übersprungen (Pflichtmerkmale nicht füllbar)")
                        continue
                    _TAXONOMY_CACHE[cache_key] = cat_id
                    log.info(f"Taxonomy-Kategorie für '{title[:40]}': {cat_id} ({cat_name})")
                    return cat_id
            else:
                log.warning(f"Taxonomy-Lookup HTTP {r.status_code} für '{title[:40]}': {r.text[:100]}")
        except Exception as e:
            log.warning(f"Taxonomy-Lookup fehlgeschlagen: {e}")

        # 4. Fallback
        return _DEFAULT_CAT

    def _build_offer_payload(self, sku: str, vk_gross: float, category: str = "", description: str = "", title: str = "") -> Dict:
        """Erstellt Offer-Payload (Preis, Versand, Policies)."""
        listing_desc = description or sku  # Fallback auf SKU wenn keine Beschreibung
        cat_id = self.get_category_for_title(title or sku, bab_category=category)
        payload: Dict[str, Any] = {
            "sku": sku,
            "marketplaceId": self.marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": 1,  # wird über Inventory gesetzt
            "categoryId": cat_id,
            "listingDescription": listing_desc[:500000],
            "pricingSummary": {
                "price": {
                    "value": f"{vk_gross:.2f}",
                    "currency": "EUR",
                }
            },
            "tax": {
                "vatPercentage": 19.0,
                "applyTax": True,
            },
            "listingDuration": "GTC",  # Good Till Cancelled
            "includeCatalogProductDetails": False,
        }

        # Business-Policies (müssen in eBay-Account angelegt sein)
        merchant_loc = self.merchant_location_key
        if merchant_loc:
            payload["merchantLocationKey"] = merchant_loc

        policies: Dict[str, Any] = {}
        if self.fulfillment_policy_id:
            policies["fulfillmentPolicyId"] = self.fulfillment_policy_id
        if self.payment_policy_id:
            policies["paymentPolicyId"] = self.payment_policy_id
        if self.return_policy_id:
            policies["returnPolicyId"] = self.return_policy_id
        if policies:
            payload["listingPolicies"] = policies

        return payload

    def create_or_update_offer(self, sku: str, vk_gross: float, category: str = "", description: str = "", title: str = "") -> str:
        """
        Aktualisiert den Preis eines BESTEHENDEN Offers.
        KEIN neues Offer erstellen — verhindert ungewollte Einstellgebühren (0,06€/Stk).
        Gibt die offerId zurück, oder "" wenn kein Offer gefunden.
        """
        existing = self.get_offer_for_sku(sku)

        if not existing:
            # GESPERRT: Kein neues Offer erstellen. Nur bestehende aktualisieren.
            log.info(f"eBay SKU {sku}: kein Offer gefunden → ÜBERSPRUNGEN (kein neues Listing)")
            return ""

        offer_id = existing["offerId"]
        # Kategorie korrigieren falls alter Offer mit ungültiger ID (z.B. 58058)
        valid_cat = self.get_category_for_title(title or sku, bab_category=category)
        # Preis + Kategorie + Policies + Location aktualisieren
        update_payload: Dict[str, Any] = {
            "sku": sku,
            "marketplaceId": self.marketplace_id,
            "format": "FIXED_PRICE",
            "categoryId": valid_cat,
            "listingDuration": "GTC",
            "includeCatalogProductDetails": False,
            "pricingSummary": {
                "price": {
                    "value": f"{vk_gross:.2f}",
                    "currency": "EUR",
                }
            },
            "tax": {
                "vatPercentage": 19.0,
                "applyTax": True,
            },
        }
        if self.merchant_location_key:
            update_payload["merchantLocationKey"] = self.merchant_location_key
        policies: Dict[str, Any] = {}
        if self.fulfillment_policy_id:
            policies["fulfillmentPolicyId"] = self.fulfillment_policy_id
        if self.payment_policy_id:
            policies["paymentPolicyId"] = self.payment_policy_id
        if self.return_policy_id:
            policies["returnPolicyId"] = self.return_policy_id
        if policies:
            update_payload["listingPolicies"] = policies
        try:
            self._request("PUT", f"{OFFER_PATH}/{offer_id}", json_body=update_payload)
            log.info(f"eBay Offer aktualisiert: SKU {sku}, Kat {valid_cat}, Preis {vk_gross:.2f}€")
            return offer_id
        except RuntimeError as e:
            if "25713" in str(e) or "404" in str(e) or "not available" in str(e).lower():
                # Offer existiert nicht mehr auf eBay → NICHT neu erstellen, überspringen
                log.warning(f"eBay Offer {offer_id} nicht mehr vorhanden → ÜBERSPRUNGEN (kein neues Listing)")
                return ""
            raise
        else:
            payload = self._build_offer_payload(sku, vk_gross, category=category, description=description, title=title)
            result = self._request("POST", OFFER_PATH, json_body=payload)
            offer_id = (result or {}).get("offerId", "")
            log.info(f"eBay Offer erstellt: SKU {sku}, offerId {offer_id}")
            return offer_id

    def publish_offer(self, offer_id: str) -> Optional[str]:
        """Veröffentlicht ein Offer (erstellt/aktualisiert das eBay-Listing)."""
        result = self._request("POST", f"{OFFER_PATH}/{offer_id}/publish")
        listing_id = (result or {}).get("listingId", "")
        if listing_id:
            log.info(f"eBay Listing veröffentlicht: listingId {listing_id}")
        return listing_id

    def withdraw_offer(self, offer_id: str):
        """Zieht ein Offer zurück (deaktiviert das Listing ohne es zu löschen)."""
        self._request("POST", f"{OFFER_PATH}/{offer_id}/withdraw")
        log.info(f"eBay Offer zurückgezogen: offerId {offer_id}")

    def delete_offer(self, offer_id: str):
        """Löscht ein Offer vollständig (DELETE). Nötig wenn Revise nicht möglich."""
        try:
            self._request("DELETE", f"{OFFER_PATH}/{offer_id}")
            log.info(f"eBay Offer gelöscht: offerId {offer_id}")
        except RuntimeError as e:
            log.warning(f"eBay Offer löschen fehlgeschlagen {offer_id}: {e}")

    # ------------------------------------------------------------------
    # Bestand aktualisieren
    # ------------------------------------------------------------------
    def set_inventory(self, sku: str, quantity: int):
        """Aktualisiert nur den Lagerbestand eines bestehenden Inventory Items."""
        safe_qty = safe_ebay_stock(quantity)
        payload = {
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": safe_qty
                }
            }
        }
        # PATCH ist nicht verfügbar, daher GET + PUT
        # Wir updaten nur den quantity-Feld via direktem Inventory-Update
        existing = self._request("GET", f"{INVENTORY_PATH}/{sku}")
        if existing:
            existing["availability"] = payload["availability"]
            self._request("PUT", f"{INVENTORY_PATH}/{sku}", json_body=existing)
            log.info(f"eBay Bestand aktualisiert: SKU {sku} → {safe_qty} (roh: {quantity})")

    # ------------------------------------------------------------------
    # Haupt-Methode: Produkt komplett anlegen/aktualisieren
    # ------------------------------------------------------------------
    def upsert_product(self, product: Dict, vk_gross: float) -> Dict[str, str]:
        """
        Vollständiger Upsert: Inventory Item + Offer + Publish.

        Returns:
            {"sku": ..., "offer_id": ..., "listing_id": ...}
        """
        sku = product["sku"]

        # Kategorie einmal bestimmen und überall verwenden
        resolved_category = self.get_category_for_title(
            product.get("title", ""),
            bab_category=product.get("category", ""),
        )

        # 1. Inventory Item (Stammdaten + Bestand + Aspekte)
        self.create_or_update_inventory_item(sku, product, vk_gross, category_id=resolved_category)

        # 2. Offer (Preis + Policies + Kategorie)
        offer_id = self.create_or_update_offer(
            sku, vk_gross,
            category=product.get("category", ""),
            description=product.get("description", "") or product.get("title", ""),
            title=product.get("title", ""),
        )

        # 3. Veröffentlichen (nur wenn Bestand > 0 UND Bild vorhanden)
        has_image = bool(
            (product.get("image_urls") or []) or product.get("image_url", "")
        )
        listing_id = ""
        if not has_image:
            log.info(f"eBay SKU {sku}: kein Bild vorhanden — Offer bleibt als Draft")
        if int(product.get("stock", 0)) > 0 and offer_id and has_image:
            try:
                listing_id = self.publish_offer(offer_id) or ""
            except RuntimeError as e:
                err_str = str(e)
                if (
                    "25013" in err_str or "25019" in err_str
                    or "revise" in err_str.lower()
                    or "unzulässige" in err_str
                ):
                    # Offer ist "stuck" (altes Listing mit falschem Zustand) →
                    # Offer löschen und im NÄCHSTEN Sync-Run neu anlegen
                    log.warning(
                        f"eBay Publish SKU {sku}: Listing kann nicht revidiert werden (25019) "
                        f"→ Offer wird gelöscht und beim nächsten Lauf neu angelegt"
                    )
                    self.delete_offer(offer_id)
                elif "Artikelmerkmal" in err_str or ("25002" in err_str and "fehlt" in err_str):
                    # Pflichtmerkmal fehlt → Kategorie auf sicheren Default zurücksetzen und retry
                    log.warning(f"eBay Publish SKU {sku}: Pflichtmerkmal fehlt → Fallback-Kategorie {_DEFAULT_CAT}")
                    try:
                        fallback_policies: Dict[str, Any] = {}
                        if self.fulfillment_policy_id:
                            fallback_policies["fulfillmentPolicyId"] = self.fulfillment_policy_id
                        if self.payment_policy_id:
                            fallback_policies["paymentPolicyId"] = self.payment_policy_id
                        if self.return_policy_id:
                            fallback_policies["returnPolicyId"] = self.return_policy_id
                        fallback_payload: Dict[str, Any] = {
                            "sku": sku,
                            "marketplaceId": self.marketplace_id,
                            "format": "FIXED_PRICE",
                            "categoryId": _DEFAULT_CAT,
                            "listingDuration": "GTC",
                            "includeCatalogProductDetails": False,
                            "pricingSummary": {
                                "price": {"value": f"{vk_gross:.2f}", "currency": "EUR"}
                            },
                            "tax": {
                                "vatPercentage": 19.0,
                                "applyTax": True,
                            },
                        }
                        if self.merchant_location_key:
                            fallback_payload["merchantLocationKey"] = self.merchant_location_key
                        if fallback_policies:
                            fallback_payload["listingPolicies"] = fallback_policies
                        self._request("PUT", f"{OFFER_PATH}/{offer_id}", json_body=fallback_payload)
                        listing_id = self.publish_offer(offer_id) or ""
                        log.info(f"eBay Publish SKU {sku} mit Fallback-Kategorie erfolgreich: {listing_id}")
                    except RuntimeError as e2:
                        e2_str = str(e2)
                        log.warning(f"eBay Publish SKU {sku} auch mit Fallback-Kategorie fehlgeschlagen: {e2}")
                        if "25019" in e2_str or "revise" in e2_str.lower() or "unzulässige" in e2_str:
                            log.warning(f"eBay SKU {sku}: Offer wird gelöscht (25019 im Fallback)")
                            self.delete_offer(offer_id)
                else:
                    log.warning(f"eBay Publish fehlgeschlagen für SKU {sku}: {e}")

        return {"sku": sku, "offer_id": offer_id, "listing_id": listing_id}
