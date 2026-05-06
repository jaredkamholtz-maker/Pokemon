"""
Authenticate with the TCGPlayer API using the authorization code flow.

Flow:
  1. POST /app/authorize/{authCode}  →  get a permanent authorizationKey
  2. Cache the key locally so we only call the endpoint once
  3. Use the key as the Bearer token on every API request

Setup: set TCGPLAYER_AUTH_CODE in .env to the code from your TCGPlayer
developer portal, then run this script once to exchange it for the key.

Usage (standalone test):
    python execution/auth_tcgplayer.py
"""

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

CACHE_FILE = Path(".tmp/tcgplayer_auth_key.json")
BASE_URL = "https://api.tcgplayer.com"


def get_token() -> str:
    """Return the cached authorizationKey, exchanging the auth code if needed."""
    load_dotenv()

    # Return cached key if we already exchanged the auth code
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        key = cached.get("authorization_key")
        if key:
            return key

    auth_code = os.environ.get("TCGPLAYER_AUTH_CODE")
    if not auth_code:
        raise EnvironmentError(
            "Missing TCGPLAYER_AUTH_CODE in .env\n"
            "Set it to the authorization code from your TCGPlayer developer portal."
        )

    resp = requests.post(
        f"{BASE_URL}/app/authorize/{auth_code}",
        timeout=15,
    )

    if resp.status_code == 400:
        raise RuntimeError(
            "TCGPlayer returned 400 — authorization code is invalid or already used.\n"
            "Check TCGPLAYER_AUTH_CODE in .env matches the code from your developer portal."
        )
    if resp.status_code == 404:
        raise RuntimeError(
            "TCGPlayer returned 404 — authorization code not found.\n"
            "Make sure you're using the exact code from your TCGPlayer developer portal."
        )
    resp.raise_for_status()

    data = resp.json()
    results = data.get("results", [])
    if not results or not results[0].get("authorizationKey"):
        raise RuntimeError(f"Unexpected response from TCGPlayer: {data}")

    key = results[0]["authorizationKey"]
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"authorization_key": key}))
    print("Authorization key obtained and cached.")
    return key


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


if __name__ == "__main__":
    token = get_token()
    print(f"Token (first 20 chars): {token[:20]}...")
