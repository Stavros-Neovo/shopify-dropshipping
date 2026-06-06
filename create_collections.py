"""
create_collections.py
=====================
Erstellt automatische Smart Collections in Shopify basierend auf Produkttypen.

Aufruf:
  cd ~/Documents/dropshipping
  python3 create_collections.py
"""
import requests
import json
import time

import os
from dotenv import load_dotenv
load_dotenv()

SHOP = os.environ.get("SHOPIFY_SHOP_DOMAIN", "neovodeals.myshopify.com")
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")

HEADERS = {
    "X-Shopify-Access-Token": TOKEN,
    "Content-Type": "application/json",
}

BASE_URL = f"https://{SHOP}/admin/api/2026-04"

# ---------------------------------------------------------------------------
# Collections Definition
# Jede Collection hat: Titel, Beschreibung, Regel (product_type oder tag)
# ---------------------------------------------------------------------------
COLLECTIONS = [
    {
        "title": "Prozessoren",
        "body_html": "<p>AMD und Intel Prozessoren für Desktop und Server.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "AMD"},
            {"column": "type", "relation": "equals", "condition": "INTEL"},
        ],
        "disjunctive": True,  # ODER-Verknüpfung
    },
    {
        "title": "Arbeitsspeicher",
        "body_html": "<p>DDR3, DDR4 und DDR5 RAM Module.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "DDR3"},
            {"column": "type", "relation": "equals", "condition": "DDR4"},
            {"column": "type", "relation": "equals", "condition": "DDR5"},
        ],
        "disjunctive": True,
    },
    {
        "title": "Festplatten & SSDs",
        "body_html": "<p>Interne und externe Festplatten sowie SSDs.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "SSD"},
            {"column": "type", "relation": "equals", "condition": "INTERN"},
            {"column": "type", "relation": "equals", "condition": "INTERN G"},
            {"column": "type", "relation": "equals", "condition": "SATA"},
        ],
        "disjunctive": True,
    },
    {
        "title": "Externe Speicher",
        "body_html": "<p>Externe Festplatten und Speicherlösungen.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "EXTERN"},
        ],
        "disjunctive": False,
    },
    {
        "title": "USB-Sticks & Speicherkarten",
        "body_html": "<p>USB-Sticks, SD-Karten und Flash-Speicher.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "FLASH"},
            {"column": "type", "relation": "equals", "condition": "VERBATIM"},
        ],
        "disjunctive": True,
    },
    {
        "title": "Monitore",
        "body_html": "<p>Monitore für Büro, Gaming und professionelle Anwendungen.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "MONITOR"},
        ],
        "disjunctive": False,
    },
    {
        "title": "Docking Stations",
        "body_html": "<p>Docking Stations für Laptops und Notebooks.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "DOCKING"},
        ],
        "disjunctive": False,
    },
    {
        "title": "Netzwerk",
        "body_html": "<p>Router, Switches und Netzwerkzubehör.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "ROUTER"},
            {"column": "type", "relation": "equals", "condition": "SWITCH"},
        ],
        "disjunctive": True,
    },
    {
        "title": "Netzteile",
        "body_html": "<p>PC Netzteile und Stromversorgung.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "NETZTEIL"},
        ],
        "disjunctive": False,
    },
    {
        "title": "Tastatur, Maus & Peripherie",
        "body_html": "<p>Tastaturen, Mäuse, Headsets und Webcams von Logitech, Cherry und mehr.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "LOGITECH"},
            {"column": "type", "relation": "equals", "condition": "CHERRY"},
            {"column": "type", "relation": "equals", "condition": "MICROSOFT"},
        ],
        "disjunctive": True,
    },
    {
        "title": "Sicherheit",
        "body_html": "<p>Sicherheitskameras und Zugangskontrolle.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "SECURITY"},
        ],
        "disjunctive": False,
    },
    {
        "title": "Kabel & Zubehör",
        "body_html": "<p>USB-Kabel, Patchkabel, Adapter und weiteres Zubehör.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "ZUBEHÖR"},
        ],
        "disjunctive": False,
    },
    {
        "title": "Angebote & B-Ware",
        "body_html": "<p>Reduzierte Artikel, Grade A und B-Ware zum Schnäppchenpreis.</p>",
        "rules": [
            {"column": "type", "relation": "equals", "condition": "GRADE A"},
            {"column": "type", "relation": "equals", "condition": "GRADE B"},
            {"column": "type", "relation": "equals", "condition": "NEW OPEN BOXED"},
        ],
        "disjunctive": True,
    },
]


def get_existing_collections():
    """Lädt alle bestehenden Smart Collections."""
    url = f"{BASE_URL}/smart_collections.json?limit=250"
    r = requests.get(url, headers=HEADERS)
    if r.status_code == 200:
        return {c["title"]: c["id"] for c in r.json().get("smart_collections", [])}
    return {}


def create_collection(col: dict, existing: dict):
    """Erstellt eine Smart Collection falls sie noch nicht existiert."""
    title = col["title"]

    if title in existing:
        print(f"  ↷ Übersprungen (existiert bereits): {title}")
        return True

    payload = {
        "smart_collection": {
            "title": title,
            "body_html": col["body_html"],
            "rules": col["rules"],
            "disjunctive": col.get("disjunctive", False),
            "published": True,
            "sort_order": "best-selling",
        }
    }

    url = f"{BASE_URL}/smart_collections.json"
    r = requests.post(url, headers=HEADERS, json=payload)

    if r.status_code == 201:
        cid = r.json()["smart_collection"]["id"]
        print(f"  ✓ Erstellt: {title} (ID: {cid})")
        return True
    else:
        print(f"  ✗ Fehler bei '{title}': {r.status_code} – {r.text[:200]}")
        return False


def main():
    print(f"Verbinde mit {SHOP} …")

    # Test-Verbindung
    r = requests.get(f"{BASE_URL}/shop.json", headers=HEADERS)
    if r.status_code != 200:
        print(f"❌ Verbindung fehlgeschlagen: {r.status_code} – {r.text[:200]}")
        print("   Prüfe Token und Shop-URL")
        return

    shop_name = r.json()["shop"]["name"]
    print(f"✓ Verbunden mit: {shop_name}\n")

    existing = get_existing_collections()
    print(f"Bestehende Collections: {len(existing)}")
    print(f"Zu erstellen: {len(COLLECTIONS)}\n")

    created = 0
    skipped = 0
    errors = 0

    for col in COLLECTIONS:
        success = create_collection(col, existing)
        if col["title"] in existing:
            skipped += 1
        elif success:
            created += 1
        else:
            errors += 1
        time.sleep(0.5)  # Rate-Limit beachten

    print(f"\n{'='*40}")
    print(f"Fertig: {created} erstellt, {skipped} übersprungen, {errors} Fehler")
    print(f"\nCollections sind jetzt in Shopify sichtbar unter:")
    print(f"  Online Store → Navigation → Collections")


if __name__ == "__main__":
    main()
