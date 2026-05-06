"""
Authenticate with the TCGPlayer API using OAuth client credentials.

TCGPlayer API v2 uses standard OAuth 2.0 client_credentials:
  POST https://api.tcgplayer.com/token
  grant_type=client_credentials&client_id={publicKey}&client_secret={privateKey}

Where to find your keys:
  1. Go to https://developer.tcgplayer.com
  2. Click "Apps" (or "My Apps") in the navigation
  3. Open your app — Public Key and Private Key are listed there
  NOTE: Do NOT use the "Authorize an App" page — that is for third-party access.

Set both in your .env file:
  TCGPLAYER_PUBLIC_KEY=your_public_key_here
  TCGPLAYER_PRIVATE_KEY=your_private_key_here

Tokens expire after ~2 weeks; cached in .tmp/tcgplayer_auth_key.json.

Usage (standalone test):
    python execution/auth_tcgplayer.py
"""

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

CACHE_FILE = Path(".tmp/tcgplayer_auth_key.json")
TOKEN_URL = "https://api.tcgplayer.com/token"


def _save_token(token: str) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"access_token": token}))


def _fetch_new_token(public_key: str, private_key: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": public_key,
            "client_secret": private_key,
        },
        timeout=15,
    )

    if resp.status_code == 400:
        raise RuntimeError(
            "TCGPlayer returned 400 — credentials rejected.\n"
            "Double-check TCGPLAYER_PUBLIC_KEY and TCGPLAYER_PRIVATE_KEY in .env.\n"
            f"Response: {resp.text[:300]}"
        )
    if resp.status_code == 401:
        raise RuntimeError(
            "TCGPlayer returned 401 — Public Key or Private Key is incorrect.\n"
            "Find them in the developer portal: Apps → your app."
        )
    resp.raise_for_status()

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in TCGPlayer response: {data}")
    return token


def get_token() -> str:
    """Return a working Bearer token, fetching and caching if needed."""
    load_dotenv()

    # Return cached token (valid ~2 weeks per TCGPlayer docs)
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        token = cached.get("access_token")
        if token:
            return token

    public_key = os.environ.get("TCGPLAYER_PUBLIC_KEY")
    private_key = os.environ.get("TCGPLAYER_PRIVATE_KEY")

    if not public_key or not private_key:
        raise EnvironmentError(
            "Missing TCGPLAYER_PUBLIC_KEY or TCGPLAYER_PRIVATE_KEY in .env\n\n"
            "Where to find them:\n"
            "  1. Go to https://developer.tcgplayer.com\n"
            "  2. Click 'Apps' in the navigation\n"
            "  3. Open your app — Public Key and Private Key are listed there\n"
            "  (Do NOT use the 'Authorize an App' page — that is for third-party access)\n\n"
            "Then add to .env:\n"
            "  TCGPLAYER_PUBLIC_KEY=your_public_key\n"
            "  TCGPLAYER_PRIVATE_KEY=your_private_key"
        )

    print("Fetching TCGPlayer access token...")
    token = _fetch_new_token(public_key, private_key)
    _save_token(token)
    print("Token obtained and cached.")
    return token


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


if __name__ == "__main__":
    token = get_token()
    print(f"Token (first 20 chars): {token[:20]}...")
