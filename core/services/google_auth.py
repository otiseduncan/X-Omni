"""
X Omni -- Google OAuth.

One consent flow does two jobs: it proves you are the owner, and it
carries the Calendar scopes. There is no second auth system.

Sole-owner binding: the first successful login writes its `sub` claim
into the owner table permanently. Any other Google account is refused
at the callback -- there is no registration path to exploit.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger("xomni.google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


class GoogleAuthError(RuntimeError):
    pass


def authorization_url(settings, redirect_uri: str, state: str, nonce: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # offline + consent are what guarantee a refresh_token. Without
        # them the integration dies silently about an hour after login.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "scope": " ".join(SCOPES),
        "state": state,
        "nonce": nonce,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(settings, code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    if resp.status_code >= 400:
        raise GoogleAuthError(f"Token exchange failed (HTTP {resp.status_code}).")
    token = resp.json()
    if not token.get("access_token") or not token.get("id_token"):
        raise GoogleAuthError("Token exchange did not return the required tokens.")
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    return token


async def fetch_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(USERINFO_URL,
                                headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code >= 400:
        raise GoogleAuthError("Could not read Google profile.")
    return resp.json()


async def refresh_access_token(settings, refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "grant_type": "refresh_token",
        })
    if resp.status_code >= 400:
        raise GoogleAuthError(
            "Refresh failed -- the Google connection needs to be re-authorized."
        )
    token = resp.json()
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    return token


async def valid_access_token(settings, store) -> Optional[str]:
    """Return a usable access token, refreshing if it's near expiry.
    Returns None when Google was never connected."""
    row = store.get_google_token()
    if not row or not row.get("access_token"):
        return None
    expires_at = int(row.get("expires_at") or 0)
    if expires_at - 120 > int(time.time()):
        return row["access_token"]
    if not row.get("refresh_token"):
        log.warning("Google access token expired and no refresh token is stored.")
        return None
    fresh = await refresh_access_token(settings, row["refresh_token"])
    store.save_google_token({
        "access_token": fresh.get("access_token"),
        "refresh_token": fresh.get("refresh_token"),  # usually absent; COALESCE keeps the old one
        "scope": fresh.get("scope") or row.get("scope"),
        "expires_at": fresh.get("expires_at"),
        "account_email": row.get("account_email"),
    })
    return fresh.get("access_token")
