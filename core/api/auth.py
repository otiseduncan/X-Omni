"""
X Omni -- auth routes and the session dependency.

Sole-owner Google OAuth. The first successful login binds this instance
to that Google account permanently; every other account is refused.

XOMNI_AUTH_ENABLED=0 bypasses all of it for local development. Core only
ever binds 127.0.0.1, so with auth off the exposure is "any process on
Omega can talk to Core" -- but Tailscale proxies remote traffic through
loopback too, so DO NOT leave auth off while Tailscale serve is running.
Startup logs a loud warning when it's disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel, SecretStr

from ..services import google_auth

log = logging.getLogger("xomni.auth")

SESSION_COOKIE = "xomni_session"
OAUTH_BINDING_COOKIE = "xomni_oauth_binding"
OAUTH_ATTEMPT_TTL_SECONDS = 600
GOOGLE_JWT_CLOCK_SKEW_SECONDS = 5
_pending_states: dict[str, dict] = {}
_setup_lock = threading.Lock()


class LocalOAuthSetup(BaseModel):
    client_id: str
    client_secret: SecretStr
    public_origin: Optional[str] = None


def _validate_setup_values(request: LocalOAuthSetup) -> tuple[str, str, Optional[str]]:
    client_id = request.client_id.strip()
    client_secret = request.client_secret.get_secret_value().strip()
    if (
        not 10 <= len(client_id) <= 512
        or not client_id.endswith(".apps.googleusercontent.com")
        or any(character.isspace() for character in client_id)
        or any(character in client_id for character in "\x00\r\n=")
    ):
        raise HTTPException(400, "Google OAuth client id is invalid.")
    if (
        not 8 <= len(client_secret) <= 512
        or any(character.isspace() for character in client_secret)
        or any(character in client_secret for character in "\x00\r\n=")
    ):
        raise HTTPException(400, "Google OAuth client secret is invalid.")

    public_origin = request.public_origin
    if public_origin is not None:
        public_origin = public_origin.strip().rstrip("/")
        parsed = urlparse(public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or any(character in public_origin for character in "\x00\r\n")
        ):
            raise HTTPException(400, "Public origin must be an HTTPS origin without a path.")
    return client_id, client_secret, public_origin


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _atomic_update_env(path: Path, updates: dict[str, str]) -> None:
    """Preserve unrelated env entries and atomically replace scoped keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    output: list[str] = []
    replaced: set[str] = set()
    for raw in lines:
        stripped = raw.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in updates:
            if key not in replaced:
                output.append(f"{key}={updates[key]}")
                replaced.add(key)
            continue
        output.append(raw)
    for key, value in updates.items():
        if key not in replaced:
            output.append(f"{key}={value}")

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(output).rstrip("\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _binding_digest(settings, binding: str) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        binding.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _prune_pending_states(now: Optional[int] = None) -> None:
    stamp = int(now or time.time())
    expired = [state for state, attempt in _pending_states.items()
               if int(attempt.get("expires_at") or 0) <= stamp]
    for state in expired:
        _pending_states.pop(state, None)


async def verify_google_id_token(id_token: str, nonce: str, settings) -> dict:
    """Validate the signed Google identity boundary without unverified decoding."""
    if not id_token:
        raise google_auth.GoogleAuthError("Google did not return an identity token.")
    try:
        header = jwt.get_unverified_header(id_token)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://www.googleapis.com/oauth2/v3/certs")
            response.raise_for_status()
        jwks = response.json()
        key_data = next(
            (key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid")),
            None,
        )
        if not key_data:
            raise google_auth.GoogleAuthError("Google signing key was not found.")
        claims = jwt.decode(
            id_token,
            key=RSAAlgorithm.from_jwk(json.dumps(key_data)),
            algorithms=["RS256"],
            audience=settings.google_client_id,
            issuer=["https://accounts.google.com", "accounts.google.com"],
            leeway=GOOGLE_JWT_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )
    except google_auth.GoogleAuthError:
        raise
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError) as exc:
        raise google_auth.GoogleAuthError("Google identity token validation failed.") from exc
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise google_auth.GoogleAuthError("OIDC nonce validation failed.")
    if claims.get("email_verified") is not True:
        raise google_auth.GoogleAuthError("A verified Google email is required.")
    return claims


def create_router(settings, store) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    def _require_local_setup_request(request: Request) -> None:
        host = str(request.headers.get("host") or "").strip().casefold()
        local_hosts = {
            f"127.0.0.1:{settings.port}".casefold(): settings.local_origin,
            f"[::1]:{settings.port}".casefold(): f"http://[::1]:{settings.port}",
        }
        if host not in local_hosts:
            raise HTTPException(403, "OAuth setup is available only on a literal loopback host.")
        forwarding_headers = (
            "forwarded", "x-forwarded-for", "x-forwarded-host",
            "x-forwarded-proto", "x-forwarded-port", "x-real-ip",
        )
        if any(header in request.headers for header in forwarding_headers):
            raise HTTPException(403, "OAuth setup cannot be used through a reverse proxy.")
        origin = str(request.headers.get("origin") or "").strip().rstrip("/")
        if origin and origin != local_hosts[host]:
            raise HTTPException(403, "OAuth setup requires the exact local browser origin.")
        content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise HTTPException(415, "OAuth setup requires an application/json body.")

    def _request_origin(request: Request) -> tuple[str, bool]:
        """Resolve only explicitly configured hosts; never trust arbitrary Host.

        A reverse proxy reaches Core from loopback, so the peer address cannot
        distinguish a person at Omega from a remote browser.  Exact Host plus
        HTTPS-forwarding proof is the usable boundary for the configured
        Tailscale origin, while Owner bootstrap additionally requires a literal
        loopback Host.
        """
        host = str(request.headers.get("host") or "").strip().casefold()
        local_hosts = {
            f"127.0.0.1:{settings.port}".casefold(),
            f"localhost:{settings.port}".casefold(),
            f"[::1]:{settings.port}".casefold(),
        }
        if host in local_hosts:
            return settings.local_origin, True
        if settings.public_origin:
            public_host = urlparse(settings.public_origin).netloc.casefold()
            forwarded_proto = str(
                request.headers.get("x-forwarded-proto") or ""
            ).split(",", 1)[0].strip().casefold()
            if host == public_host and forwarded_proto == "https":
                return settings.public_origin, False
        raise HTTPException(400, "Request host is not an allowed X Omni origin.")

    def _redirect_uri(request: Request) -> tuple[str, bool]:
        """Pick the redirect URI matching how this request arrived, so the
        same instance works at 127.0.0.1 on the desktop and at the Tailscale
        hostname from the phone. Both must be registered in Google Cloud."""
        origin, is_local = _request_origin(request)
        return f"{origin}/api/auth/callback", is_local

    @router.post("/setup")
    async def local_oauth_setup(request: Request, setup: LocalOAuthSetup):
        _require_local_setup_request(request)
        if store.get_owner():
            raise HTTPException(403, "Owner is already bound; OAuth setup is permanently closed.")
        if settings.auth_enabled and settings.google_configured:
            raise HTTPException(409, "Google OAuth is already configured.")
        client_id, client_secret, public_origin = _validate_setup_values(setup)
        env_path = Path(settings.root) / "config" / ".env.local"

        with _setup_lock:
            if store.get_owner():
                raise HTTPException(403, "Owner is already bound; OAuth setup is permanently closed.")
            existing = _read_env_values(env_path)
            if (
                existing.get("XOMNI_AUTH_ENABLED", "").casefold() in {"1", "true", "yes", "on"}
                and existing.get("XOMNI_GOOGLE_CLIENT_ID")
                and existing.get("XOMNI_GOOGLE_CLIENT_SECRET")
            ):
                raise HTTPException(409, "OAuth setup was already saved; restart X Omni.")
            updates = {
                "XOMNI_AUTH_ENABLED": "1",
                "XOMNI_GOOGLE_CLIENT_ID": client_id,
                "XOMNI_GOOGLE_CLIENT_SECRET": client_secret,
            }
            if public_origin is not None:
                updates["XOMNI_PUBLIC_ORIGIN"] = public_origin
            _atomic_update_env(env_path, updates)

        audit = getattr(store, "audit", None)
        if callable(audit):
            audit("oauth_setup_saved", {"public_origin_set": public_origin is not None})
        return {
            "saved": True,
            "restart_required": True,
            "callback_uri": f"http://127.0.0.1:{settings.port}/api/auth/callback",
        }

    @router.get("/status")
    async def auth_status(request: Request):
        owner = store.get_owner()
        token = request.cookies.get(SESSION_COOKIE)
        session = store.get_session(token) if token else None
        return {
            "auth_enabled": settings.auth_enabled,
            "google_configured": settings.google_configured,
            "owner_bound": bool(owner),
            "signed_in": bool(session) or not settings.auth_enabled,
            "redirect_uri_count": len(settings.redirect_uris),
        }

    @router.get("/login")
    async def login(request: Request):
        if not settings.auth_enabled:
            return JSONResponse({"detail": "Auth is disabled; you are already signed in."})
        if not settings.google_configured:
            raise HTTPException(
                503,
                "Google OAuth is not configured. Set XOMNI_GOOGLE_CLIENT_ID and "
                "XOMNI_GOOGLE_CLIENT_SECRET in config/.env.local.",
            )
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        binding = secrets.token_urlsafe(32)
        redirect_uri, is_local = _redirect_uri(request)
        if not store.get_owner() and not is_local:
            raise HTTPException(
                403,
                "The first Owner sign-in must begin locally on Omega. "
                "Open X Omni at its 127.0.0.1 address for initial binding.",
            )
        _prune_pending_states()
        _pending_states[state] = {
            "redirect_uri": redirect_uri,
            "nonce": nonce,
            "binding_digest": _binding_digest(settings, binding),
            "bootstrap_local": is_local,
            "expires_at": int(time.time()) + OAUTH_ATTEMPT_TTL_SECONDS,
        }
        response = RedirectResponse(
            google_auth.authorization_url(settings, redirect_uri, state, nonce)
        )
        response.set_cookie(
            OAUTH_BINDING_COOKIE,
            binding,
            httponly=True,
            samesite="lax",
            secure=redirect_uri.startswith("https://"),
            max_age=OAUTH_ATTEMPT_TTL_SECONDS,
            path="/api/auth",
        )
        return response

    @router.get("/callback")
    async def callback(
        request: Request,
        code: Optional[str] = Query(default=None),
        state: Optional[str] = Query(default=None),
        error: Optional[str] = Query(default=None),
    ):
        if error:
            return RedirectResponse(f"/?auth_error={error}")
        _prune_pending_states()
        attempt = _pending_states.pop(state, None) if state else None
        binding = request.cookies.get(OAUTH_BINDING_COOKIE, "")
        if (
            not code
            or not attempt
            or not binding
            or not hmac.compare_digest(
                str(attempt.get("binding_digest") or ""),
                _binding_digest(settings, binding),
            )
        ):
            return RedirectResponse("/?auth_error=invalid_state")

        redirect_uri = str(attempt["redirect_uri"])
        nonce = str(attempt["nonce"])
        if not store.get_owner() and not bool(attempt.get("bootstrap_local")):
            return RedirectResponse("/?auth_error=bootstrap_local_required")

        try:
            token = await google_auth.exchange_code(settings, code, redirect_uri)
            claims = await verify_google_id_token(token["id_token"], nonce, settings)
            info = await google_auth.fetch_userinfo(token["access_token"])
        except google_auth.GoogleAuthError as exc:
            log.error("OAuth failed: %s", exc)
            return RedirectResponse("/?auth_error=exchange_failed")

        sub = claims.get("sub")
        if not sub or info.get("sub") != sub:
            return RedirectResponse("/?auth_error=no_subject")
        if info.get("email_verified") is not True:
            return RedirectResponse("/?auth_error=email_not_verified")

        if not token.get("refresh_token"):
            # Without a refresh token the Calendar link silently dies in an
            # hour. Better to fail loudly at login than mysteriously later.
            log.error("Google returned no refresh_token.")
            return RedirectResponse("/?auth_error=no_refresh_token")

        try:
            owner = store.bind_owner(
                sub, claims.get("email", ""), claims.get("name", "")
            )
        except PermissionError:
            store.audit("auth_rejected_wrong_account", {"account_match": False})
            return RedirectResponse("/?auth_error=not_owner")

        store.save_google_token({
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
            "scope": token.get("scope"),
            "expires_at": token.get("expires_at"),
            "account_email": None,
        })

        session_token = store.create_session(
            owner["google_sub"],
            request.headers.get("user-agent", ""),
            settings.session_ttl_days,
        )
        store.audit("auth_login", {"owner_match": True})

        response = RedirectResponse("/")
        secure = redirect_uri.startswith("https://")
        response.set_cookie(
            SESSION_COOKIE, session_token,
            httponly=True, samesite="lax", secure=secure,
            max_age=settings.session_ttl_days * 86400, path="/",
        )
        response.delete_cookie(OAUTH_BINDING_COOKIE, path="/api/auth")
        return response

    @router.post("/logout")
    async def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.delete_session(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router


def make_require_session(settings, store):
    """Dependency for protected routes."""
    async def require_session(request: Request) -> dict:
        origin = request.headers.get("origin", "").strip()
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and origin:
            allowed = {settings.local_origin, f"http://localhost:{settings.port}"}
            if settings.public_origin:
                allowed.add(settings.public_origin)
            if origin.rstrip("/") not in allowed:
                raise HTTPException(403, "Request origin is not allowed.")
        if not settings.auth_enabled:
            return {"google_sub": "local-dev", "bypass": True}
        token = request.cookies.get(SESSION_COOKIE)
        session = store.get_session(token) if token else None
        if not session:
            raise HTTPException(401, "Not signed in.")
        return session
    return require_session


def session_from_websocket(settings, store, websocket) -> Optional[dict]:
    """WebSockets can't use HTTPException-based dependencies cleanly, so
    the chat socket checks the same cookie itself before accepting."""
    origin = str(websocket.headers.get("origin") or "").rstrip("/")
    allowed = {settings.local_origin, f"http://localhost:{settings.port}"}
    if settings.public_origin:
        allowed.add(settings.public_origin)
    if origin and origin not in allowed:
        return None
    if not settings.auth_enabled:
        return {"google_sub": "local-dev", "bypass": True}
    token = websocket.cookies.get(SESSION_COOKIE)
    return store.get_session(token) if token else None
