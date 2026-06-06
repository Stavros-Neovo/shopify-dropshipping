"""
ebay_oauth.py
=============
Einmaliger OAuth 2.0 Flow für eBay — generiert den Refresh Token.

NUR einmal ausführen! Der Refresh Token ist ~18 Monate gültig.
Danach in .env eintragen: EBAY_REFRESH_TOKEN=...

Voraussetzungen:
  - EBAY_CLIENT_ID und EBAY_CLIENT_SECRET in .env
  - RuName (eBay Redirect URL Name) im Developer Portal konfiguriert

Aufruf:
  python ebay_oauth.py             # Sandbox
  python ebay_oauth.py --live      # Produktion
"""
from __future__ import annotations

import argparse
import base64
import os
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv

load_dotenv()

SANDBOX_AUTH_URL = "https://auth.sandbox.ebay.com/oauth2/authorize"
PROD_AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
SANDBOX_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
PROD_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])

# Temporärer lokaler Redirect-Server
REDIRECT_PORT = 8765
REDIRECT_URI = "Stavros_Coucour-StavrosC-Dropsh-xywyhuo"
REDIRECT_LANDING = "https://www.example.com"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Produktion statt Sandbox")
    args = parser.parse_args()

    client_id = os.environ.get("EBAY_CLIENT_ID", "")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("FEHLER: EBAY_CLIENT_ID und EBAY_CLIENT_SECRET müssen in .env stehen!")
        return

    auth_url = PROD_AUTH_URL if args.live else SANDBOX_AUTH_URL
    token_url = PROD_TOKEN_URL if args.live else SANDBOX_TOKEN_URL
    mode = "PRODUKTION" if args.live else "SANDBOX"

    print(f"\n{'='*60}")
    print(f"  eBay OAuth Flow — {mode}")
    print(f"{'='*60}")
    print(f"\nWICHTIG: In deinem eBay Developer Portal muss als RuName")
    print(f"         diese Redirect URI eingetragen sein:")
    print(f"         {REDIRECT_URI}\n")

    # Authorization URL bauen
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "prompt": "login",
    }
    full_auth_url = f"{auth_url}?{urllib.parse.urlencode(params)}"

    print(f"Browser wird geöffnet...")
    print(f"Falls er sich nicht öffnet, öffne manuell:\n{full_auth_url}\n")
    webbrowser.open(full_auth_url)

    print("Nach dem eBay-Login wirst du auf example.com weitergeleitet.")
    print("Die Seite zeigt nur 'Example Domain' — das ist korrekt!")
    print("Kopiere die KOMPLETTE URL aus der Adresszeile des Browsers.")
    print("Sie sieht so aus: https://www.example.com/?code=XXXXXX&...")
    print()
    callback_url = input("Füge hier die komplette URL ein: ").strip()

    parsed_cb = urllib.parse.urlparse(callback_url)
    params_cb = urllib.parse.parse_qs(parsed_cb.query)
    code = params_cb.get("code", [None])[0]
    if not code:
        print("FEHLER: Kein Code in der URL gefunden. Bitte nochmal versuchen.")
        return
    print(f"\nAuthorization Code erhalten ✓")

    # Access Token + Refresh Token holen
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        token_url,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )

    if r.status_code != 200:
        print(f"FEHLER beim Token-Abruf: {r.status_code} {r.text}")
        return

    data = r.json()
    refresh_token = data.get("refresh_token", "")
    access_token = data.get("access_token", "")

    print(f"\n{'='*60}")
    print(f"  ERFOLG! Trage folgendes in deine .env Datei ein:")
    print(f"{'='*60}")
    print(f"\nEBAY_REFRESH_TOKEN={refresh_token}")
    print(f"\n(Access Token läuft in {data.get('expires_in', '?')}s ab, Refresh Token in ~18 Monaten)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
