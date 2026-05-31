#!/usr/bin/env python3
"""Upstox OAuth2 manual auth script.

Run this once to generate an access token. Paste the token into .env as
UPSTOX_ACCESS_TOKEN. Tokens expire daily; re-run each trading day.

Usage:
    uv run python scripts/upstox_auth.py

The script will:
1. Open the Upstox login URL in your browser
2. After login and 2FA, Upstox redirects to your redirect_uri with ?code=...
3. Paste the full redirect URL here
4. The script exchanges the auth code for an access token
5. Prints the token and updates .env automatically

Required env vars (set in .env before running):
    UPSTOX_API_KEY       — from Upstox developer portal
    UPSTOX_API_SECRET    — from Upstox developer portal
    UPSTOX_REDIRECT_URI  — must match the redirect URI registered in the portal
"""
from __future__ import annotations

import os
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key

load_dotenv()

ENV_FILE = Path(__file__).parent.parent / ".env"

_AUTH_BASE = "https://api.upstox.com/v2/login/authorization/dialog"
_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


def main() -> None:
    api_key = os.environ.get("UPSTOX_API_KEY")
    api_secret = os.environ.get("UPSTOX_API_SECRET")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI", "http://localhost:8080/callback")

    if not api_key or not api_secret:
        print("ERROR: UPSTOX_API_KEY and UPSTOX_API_SECRET must be set in .env")
        sys.exit(1)

    auth_url = (
        f"{_AUTH_BASE}"
        f"?response_type=code"
        f"&client_id={api_key}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
    )

    print("\n=== Upstox OAuth2 Auth Flow ===\n")
    print(f"Opening browser to:\n  {auth_url}\n")
    print("If the browser doesn't open, paste the URL manually.\n")
    webbrowser.open(auth_url)

    redirect_url = input("After login + 2FA, paste the full redirect URL here:\n> ").strip()

    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)
    auth_code = params.get("code", [None])[0]

    if not auth_code:
        print("ERROR: No 'code' parameter found in the redirect URL.")
        sys.exit(1)

    print(f"\nExchanging auth code for access token...")

    resp = httpx.post(
        _TOKEN_URL,
        data={
            "code": auth_code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed ({resp.status_code}):\n{resp.text}")
        sys.exit(1)

    data = resp.json()
    access_token = data.get("access_token")

    if not access_token:
        print(f"ERROR: No access_token in response:\n{data}")
        sys.exit(1)

    print(f"\nAccess token obtained successfully.")

    if ENV_FILE.exists():
        set_key(str(ENV_FILE), "UPSTOX_ACCESS_TOKEN", access_token)
        print(f"Token written to {ENV_FILE}")
    else:
        print(f"\nAdd this to your .env file:\n\nUPSTOX_ACCESS_TOKEN={access_token}\n")


if __name__ == "__main__":
    main()
