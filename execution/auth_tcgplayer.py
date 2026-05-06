"""
Authenticate with the TCGPlayer API.

TCGPlayer issues either:
  (a) A permanent authorizationKey — use directly as Bearer token, OR
  (b) A one-time authCode — POST to /app/authorize/{authCode} to get the key

This script tries (a) first by making a test API call. If that fails with 401,
it tries (b) to exchange the code for a permanent key and caches the result.

Set TCGPLAYER_AUTH_CODE in .env to whichever value TCGPlayer gave you.

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
TEST_URL = f"{BASE_URL}/v2/catalog/categories?limit=1"  # lightweight test endpoint


def _save_key(key: str) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"authorization_key": key}))


def _test_key(key: str) -> bool:
    """Return True if the key works as a Bearer token."""
    resp = requests.get(
        TEST_URL,
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    return resp.status_code == 200


def get_token() -> str:
    """Return a working Bearer token, caching after the first successful auth."""
    load_dotenv()

    # Return cached key immediately
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        key = cached.get("authorization_key")
        if key:
            return key

    auth_code = os.environ.get("TCGPLAYER_AUTH_CODE")
    if not auth_code:
        raise EnvironmentError(
            "Missing TCGPLAYER_AUTH_CODE in .env\n"
            "Set it to the key/code shown in your TCGPlayer developer portal."
        )

    # Try using it directly as the permanent authorization key
    if _test_key(auth_code):
        print("TCGPLAYER_AUTH_CODE works directly as a Bearer token — caching it.")
        _save_key(auth_code)
        return auth_code

    # Otherwise treat it as a one-time auth code and exchange it
    print("Treating TCGPLAYER_AUTH_CODE as a one-time auth code, exchanging for permanent key...")
    resp = requests.post(
        f"{BASE_URL}/app/authorize/{auth_code}",
        timeout=15,
    )

    if resp.status_code == 400:
        raise RuntimeError(
            "TCGPlayer returned 400 — auth code is invalid.\n"
            "Check TCGPLAYER_AUTH_CODE in .env."
        )
    if resp.status_code == 404:
        raise RuntimeError(
            "TCGPlayer returned 404 — auth code not found or already used.\n"
            "Auth codes are single-use. If you already exchanged it, the resulting\n"
            "authorizationKey should be in your TCGPlayer developer portal — use that instead."
        )
    resp.raise_for_status()

    data = resp.json()
    results = data.get("results", [])
    if not results or not results[0].get("authorizationKey"):
        raise RuntimeError(f"Unexpected response from TCGPlayer: {data}")

    key = results[0]["authorizationKey"]
    _save_key(key)
    print(f"Authorization key obtained and cached.")
    return key


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


if __name__ == "__main__":
    token = get_token()
    print(f"Token (first 20 chars): {token[:20]}...")
