from flask import Flask, request
import requests
import os
from dotenv import load_dotenv

load_dotenv(".env")

app = Flask(__name__)

SHOP = "neovodeal.myshopify.com"

CLIENT_ID = os.getenv("SHOPIFY_API_KEY")
CLIENT_SECRET = os.getenv("SHOPIFY_API_SECRET")

SCOPES = (
    "read_products,"
    "write_products,"
    "read_inventory,"
    "write_inventory,"
    "read_locations,"
    "read_orders"
)

REDIRECT_URI = "https://127.0.0.1:5000/callback"


@app.route("/")
def index():

    auth_url = (
        f"https://{SHOP}/admin/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope={SCOPES}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&grant_options[]=per-user"
    )

    return f"""
    <h1>Shopify OAuth Start</h1>
    <p>Client ID: {CLIENT_ID}</p>
    <a href="{auth_url}">App installieren</a>
    """


@app.route("/callback")
def callback():

    code = request.args.get("code")

    token_url = f"https://{SHOP}/admin/oauth/access_token"

    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
    }

    response = requests.post(token_url, json=payload)

    return f"""
    <h1>Token Response</h1>
    <pre>{response.text}</pre>
    """


if __name__ == "__main__":
    app.run(port=5000)

