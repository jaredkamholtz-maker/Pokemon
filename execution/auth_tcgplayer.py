"""
Fetch and cache a TCGPlayer API v2 bearer token.

Usage (standalone test):
    python execution/auth_tcgplayer.py
"""

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

CACHE_FILE = Path(".tmp/tcgplayer_token.json")
TOKEN_URL = "https://api.tcgplayer.com/token"


def get_token() -> str:
    """Return a valid bearer token, refreshing from the API when needed."""
    load_dotenv()

    client_id = os.environ.get("TCGPLAYER_CLIENT_ID")
    client_secret = os.environ.get("TCGPLAYER_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise EnvironmentError(
            "Missing TCGPLAYER_CLIENT_ID or TCGPLAYER_CLIENT_SECRET in .env"
        )

    # Return cached token if still valid (with 60s buffer)
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        if cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(
            {
                "access_token": data["access_token"],
                "expires_at": time.time() + int(data.get("expires_in", 1209600)),
            }
        )
    )
    return data["access_token"]


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


if __name__ == "__main__":
    token = get_token()
    print(f"Token obtained (first 20 chars): {token[:20]}...")
