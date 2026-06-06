"""
ebay_diagnose.py
================
Diagnose-Tool: Zeigt gültige eBay.de Kategorien per Taxonomy API.

Aufruf:
  python3 ebay_diagnose.py                    # Kategorie-Vorschläge für Testtitel
  python3 ebay_diagnose.py "USB Kabel 2m"     # Vorschläge für eigenen Suchbegriff
  python3 ebay_diagnose.py --account          # Account-Status prüfen
  python3 ebay_diagnose.py --policies         # Business Policies auflisten
"""
from __future__ import annotations
import argparse
import base64
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

PROD_BASE = "https://api.ebay.com"
EBAY_DE_CATEGORY_TREE_ID = "77"  # eBay Deutschland

TEST_TITLES = [
    "USB-C Kabel 2m",
    "HDMI Kabel 1m",
    "Netzwerk Switch 8 Port",
    "RAM Speicher 8GB DDR4",
    "SSD 512GB",
    "Drucker Tinte schwarz",
    "Laptop Netzteil",
    "Webcam HD",
]


def get_user_token() -> str:
    """User Token (Refresh Grant) für Selling APIs."""
    client_id = os.environ.get("EBAY_CLIENT_ID", "")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "")
    refresh_token = os.environ.get("EBAY_REFRESH_TOKEN", "")
    if not all([client_id, client_secret, refresh_token]):
        print("FEHLER: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN müssen in .env stehen!")
        sys.exit(1)
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        f"{PROD_BASE}/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"Token-Fehler: {r.status_code} {r.text[:300]}")
        sys.exit(1)
    return r.json()["access_token"]


def get_app_token() -> str:
    """Application Token (Client Credentials) für Taxonomy/Browse APIs."""
    client_id = os.environ.get("EBAY_CLIENT_ID", "")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "")
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        f"{PROD_BASE}/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"App-Token-Fehler: {r.status_code} {r.text[:300]}")
        sys.exit(1)
    return r.json()["access_token"]


def get_token() -> str:
    return get_user_token()


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_category_suggestions(token: str, query: str) -> list:
    r = requests.get(
        f"{PROD_BASE}/commerce/taxonomy/v1/category_tree/{EBAY_DE_CATEGORY_TREE_ID}/get_category_suggestions",
        headers=headers(token),
        params={"q": query},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  Taxonomy API Fehler: {r.status_code} {r.text[:200]}")
        return []
    return r.json().get("categorySuggestions", [])


def check_account(token: str):
    """Prüft den Seller Account Status."""
    print("\n=== ACCOUNT STATUS ===")
    r = requests.get(
        f"{PROD_BASE}/sell/account/v1/privilege",
        headers=headers(token),
        timeout=30,
    )
    if r.status_code == 200:
        data = r.json()
        print(f"  sellerRegistrationCompleted: {data.get('sellerRegistrationCompleted', '?')}")
        privs = data.get('sellingLimit', {})
        print(f"  sellingLimit: {privs}")
    else:
        print(f"  Fehler: {r.status_code} {r.text[:300]}")


def check_policies(token: str):
    """Listet alle Business Policies auf."""
    print("\n=== FULFILLMENT POLICIES ===")
    r = requests.get(
        f"{PROD_BASE}/sell/account/v1/fulfillment_policy",
        headers=headers(token),
        params={"marketplace_id": "EBAY_DE"},
        timeout=30,
    )
    if r.status_code == 200:
        for p in r.json().get("fulfillmentPolicies", []):
            print(f"  ID: {p['fulfillmentPolicyId']}  Name: {p['name']}")
            for opt in p.get("shippingOptions", []):
                for svc in opt.get("shippingServices", []):
                    print(f"    → Service: {svc.get('shippingServiceCode')}  Kosten: {svc.get('shippingCost', {}).get('value', '?')}€")
    else:
        print(f"  Fehler: {r.status_code} {r.text[:300]}")

    print("\n=== PAYMENT POLICIES ===")
    r = requests.get(
        f"{PROD_BASE}/sell/account/v1/payment_policy",
        headers=headers(token),
        params={"marketplace_id": "EBAY_DE"},
        timeout=30,
    )
    if r.status_code == 200:
        for p in r.json().get("paymentPolicies", []):
            print(f"  ID: {p['paymentPolicyId']}  Name: {p['name']}")
    else:
        print(f"  Fehler: {r.status_code} {r.text[:300]}")

    print("\n=== RETURN POLICIES ===")
    r = requests.get(
        f"{PROD_BASE}/sell/account/v1/return_policy",
        headers=headers(token),
        params={"marketplace_id": "EBAY_DE"},
        timeout=30,
    )
    if r.status_code == 200:
        for p in r.json().get("returnPolicies", []):
            print(f"  ID: {p['returnPolicyId']}  Name: {p['name']}")
    else:
        print(f"  Fehler: {r.status_code} {r.text[:300]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default=None, help="Suchbegriff für Kategorie-Vorschläge")
    parser.add_argument("--account", action="store_true", help="Account-Status prüfen")
    parser.add_argument("--policies", action="store_true", help="Business Policies auflisten")
    args = parser.parse_args()

    user_token = get_user_token()
    print("User-Token OK ✓")

    if args.account:
        check_account(user_token)

    if args.policies:
        check_policies(user_token)

    if not args.account and not args.policies:
        # Kategorie-Vorschläge mit Application Token
        app_token = get_app_token()
        print("App-Token OK ✓")
        queries = [args.query] if args.query else TEST_TITLES
        print(f"\n=== KATEGORIE-VORSCHLÄGE (eBay.de, Tree ID: {EBAY_DE_CATEGORY_TREE_ID}) ===")
        for q in queries:
            print(f"\n  Suche: '{q}'")
            suggestions = get_category_suggestions(app_token, q)
            for s in suggestions[:3]:
                cat = s.get("category", {})
                path = " > ".join(
                    a.get("categoryName", "") for a in s.get("categoryTreeNodeAncestors", [])
                )
                print(f"    ID: {cat.get('categoryId'):>8}  {path} > {cat.get('categoryName')}")


if __name__ == "__main__":
    main()
