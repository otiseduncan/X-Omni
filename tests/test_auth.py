from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from jwt.algorithms import RSAAlgorithm

from core.api import auth
from core.services.google_auth import GoogleAuthError


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class FakeAsyncClient:
    jwks: dict = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url: str):
        return FakeResponse(self.jwks)


def auth_settings() -> SimpleNamespace:
    return SimpleNamespace(
        auth_enabled=True,
        google_configured=True,
        google_client_id="client-id",
        google_client_secret="client-secret",
        public_origin="https://omega.example.ts.net",
        local_origin="http://127.0.0.1:8100",
        port=8100,
        redirect_uris=[
            "http://127.0.0.1:8100/api/auth/callback",
            "https://omega.example.ts.net/api/auth/callback",
        ],
        session_secret="test-session-secret",
        session_ttl_days=30,
    )


def signed_google_token(*, nonce: str, email_verified: bool = True):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://accounts.google.com",
            "aud": "client-id",
            "sub": "owner-sub",
            "email": "owner@example.com",
            "email_verified": email_verified,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    return token, {"keys": [public_jwk]}


@pytest.mark.asyncio
async def test_google_id_token_requires_matching_nonce_and_verified_email(monkeypatch):
    token, jwks = signed_google_token(nonce="expected")
    FakeAsyncClient.jwks = jwks
    monkeypatch.setattr(auth.httpx, "AsyncClient", FakeAsyncClient)

    claims = await auth.verify_google_id_token(token, "expected", auth_settings())
    assert claims["sub"] == "owner-sub"

    with pytest.raises(GoogleAuthError, match="nonce"):
        await auth.verify_google_id_token(token, "wrong", auth_settings())

    unverified, jwks = signed_google_token(nonce="expected", email_verified=False)
    FakeAsyncClient.jwks = jwks
    with pytest.raises(GoogleAuthError, match="verified Google email"):
        await auth.verify_google_id_token(unverified, "expected", auth_settings())


class FakeStore:
    def __init__(self, owner=None) -> None:
        self.owner = owner
        self.audit_events = []

    def get_owner(self):
        return self.owner

    def get_session(self, _token):
        return None

    def audit(self, event_type, detail):
        self.audit_events.append((event_type, detail))


def setup_settings(tmp_path: Path, *, auth_enabled: bool = False,
                   google_configured: bool = False) -> SimpleNamespace:
    settings = auth_settings()
    settings.root = tmp_path
    settings.auth_enabled = auth_enabled
    settings.google_configured = google_configured
    settings.google_client_id = "" if not google_configured else settings.google_client_id
    settings.google_client_secret = "" if not google_configured else settings.google_client_secret
    return settings


@pytest.mark.asyncio
async def test_local_oauth_setup_atomically_saves_scoped_keys_without_echoing_secret(
    tmp_path, caplog,
):
    settings = setup_settings(tmp_path)
    store = FakeStore()
    config = tmp_path / "config"
    config.mkdir()
    env_path = config / ".env.local"
    env_path.write_text(
        "# keep this comment\nKEEP_ME=present\nXOMNI_AUTH_ENABLED=0\n"
        "XOMNI_GOOGLE_CLIENT_ID=old\nXOMNI_GOOGLE_CLIENT_ID=duplicate\n",
        encoding="utf-8",
    )
    secret = "GOCSPX-local-bootstrap-secret"
    body = {
        "client_id": "123456789-example.apps.googleusercontent.com",
        "client_secret": secret,
        "public_origin": "https://omega.example.ts.net/",
    }
    app = FastAPI()
    app.include_router(auth.create_router(settings, store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=settings.local_origin) as client:
        response = await client.post(
            "/api/auth/setup", json=body,
            headers={"host": "127.0.0.1:8100", "origin": settings.local_origin},
        )

    assert response.status_code == 200
    assert response.json() == {
        "saved": True,
        "restart_required": True,
        "callback_uri": "http://127.0.0.1:8100/api/auth/callback",
    }
    assert secret not in response.text
    assert secret not in caplog.text
    saved = env_path.read_text(encoding="utf-8")
    assert "# keep this comment" in saved
    assert "KEEP_ME=present" in saved
    assert saved.count("XOMNI_AUTH_ENABLED=") == 1
    assert saved.count("XOMNI_GOOGLE_CLIENT_ID=") == 1
    assert saved.count("XOMNI_GOOGLE_CLIENT_SECRET=") == 1
    assert "XOMNI_AUTH_ENABLED=1" in saved
    assert f"XOMNI_GOOGLE_CLIENT_SECRET={secret}" in saved
    assert "XOMNI_PUBLIC_ORIGIN=https://omega.example.ts.net" in saved
    assert not list(config.glob(".*.tmp"))
    assert store.audit_events == [
        ("oauth_setup_saved", {"public_origin_set": True})
    ]


@pytest.mark.asyncio
async def test_local_oauth_setup_is_single_use_until_restart(tmp_path):
    settings = setup_settings(tmp_path)
    store = FakeStore()
    app = FastAPI()
    app.include_router(auth.create_router(settings, store))
    payload = {
        "client_id": "123456789-example.apps.googleusercontent.com",
        "client_secret": "GOCSPX-first-secret",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=settings.local_origin) as client:
        first = await client.post("/api/auth/setup", json=payload)
        second = await client.post(
            "/api/auth/setup",
            json={**payload, "client_secret": "GOCSPX-second-secret"},
        )
    assert first.status_code == 200
    assert second.status_code == 409
    saved = (tmp_path / "config" / ".env.local").read_text(encoding="utf-8")
    assert "GOCSPX-first-secret" in saved
    assert "GOCSPX-second-secret" not in saved
    assert "GOCSPX-second-secret" not in second.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "headers"),
    [
        ("http://localhost:8100", {"host": "localhost:8100"}),
        ("https://omega.example.ts.net", {"host": "omega.example.ts.net"}),
        ("http://127.0.0.1:8100", {"host": "127.0.0.1:8100", "x-forwarded-proto": "https"}),
        ("http://127.0.0.1:8100", {"host": "127.0.0.1:8100", "x-forwarded-for": "127.0.0.1"}),
        ("http://127.0.0.1:8100", {"host": "127.0.0.1:8100", "x-forwarded-host": "127.0.0.1:8100"}),
        ("http://127.0.0.1:8100", {"host": "127.0.0.1:8100", "origin": "https://attacker.example"}),
    ],
)
async def test_local_oauth_setup_rejects_nonlocal_proxy_or_cross_origin_requests(
    tmp_path, base_url, headers,
):
    settings = setup_settings(tmp_path)
    app = FastAPI()
    app.include_router(auth.create_router(settings, FakeStore()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        response = await client.post(
            "/api/auth/setup",
            json={
                "client_id": "123456789-example.apps.googleusercontent.com",
                "client_secret": "GOCSPX-local-secret",
            },
            headers=headers,
        )
    assert response.status_code == 403
    assert not (tmp_path / "config" / ".env.local").exists()


@pytest.mark.asyncio
async def test_local_oauth_setup_closes_after_owner_or_existing_configuration(tmp_path):
    payload = {
        "client_id": "123456789-example.apps.googleusercontent.com",
        "client_secret": "GOCSPX-local-secret",
    }
    cases = [
        (setup_settings(tmp_path / "owner"), FakeStore({"google_sub": "bound"}), 403),
        (setup_settings(tmp_path / "configured", auth_enabled=True, google_configured=True), FakeStore(), 409),
    ]
    for settings, store, expected in cases:
        app = FastAPI()
        app.include_router(auth.create_router(settings, store))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=settings.local_origin) as client:
            response = await client.post("/api/auth/setup", json=payload)
        assert response.status_code == expected
        assert not (Path(settings.root) / "config" / ".env.local").exists()


@pytest.mark.asyncio
async def test_local_oauth_setup_validates_google_values_and_public_origin(tmp_path):
    settings = setup_settings(tmp_path)
    app = FastAPI()
    app.include_router(auth.create_router(settings, FakeStore()))
    transport = httpx.ASGITransport(app=app)
    invalid = [
        {"client_id": "not-a-google-client", "client_secret": "GOCSPX-valid-secret"},
        {
            "client_id": "123456789-example.apps.googleusercontent.com",
            "client_secret": "short",
        },
        {
            "client_id": "123456789-example.apps.googleusercontent.com",
            "client_secret": "GOCSPX-valid-secret",
            "public_origin": "http://omega.example.ts.net/path",
        },
    ]
    async with httpx.AsyncClient(transport=transport, base_url=settings.local_origin) as client:
        responses = [await client.post("/api/auth/setup", json=item) for item in invalid]
    assert [response.status_code for response in responses] == [400, 400, 400]
    assert not (tmp_path / "config" / ".env.local").exists()


@pytest.mark.asyncio
async def test_first_owner_login_must_begin_on_local_origin():
    auth._pending_states.clear()
    app = FastAPI()
    app.include_router(auth.create_router(auth_settings(), FakeStore()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://omega.example.ts.net") as client:
        response = await client.get(
            "/api/auth/login",
            headers={"host": "omega.example.ts.net", "x-forwarded-proto": "https"},
            follow_redirects=False,
        )
    assert response.status_code == 403

    # A proxy request cannot become "local" merely by omitting the forwarded
    # scheme; its unconfigured Host is rejected before OAuth state is issued.
    async with httpx.AsyncClient(transport=transport, base_url="http://omega.example.ts.net") as client:
        unproved_proxy = await client.get(
            "/api/auth/login",
            headers={"host": "omega.example.ts.net"},
            follow_redirects=False,
        )
    assert unproved_proxy.status_code == 400


@pytest.mark.asyncio
async def test_local_login_sets_bound_state_and_nonce_without_leaking_owner():
    auth._pending_states.clear()
    store = FakeStore({"email": "private@example.com"})
    app = FastAPI()
    app.include_router(auth.create_router(auth_settings(), store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8100") as client:
        status = await client.get("/api/auth/status")
        login = await client.get("/api/auth/login", follow_redirects=False)

    assert status.json()["owner_bound"] is True
    assert "private@example.com" not in status.text
    assert login.status_code in {302, 307}
    assert "nonce=" in login.headers["location"]
    assert "state=" in login.headers["location"]
    assert auth.OAUTH_BINDING_COOKIE in login.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_auth_disabled_does_not_disable_browser_origin_boundary():
    settings = auth_settings()
    settings.auth_enabled = False
    require_session = auth.make_require_session(settings, FakeStore())
    app = FastAPI()

    @app.post("/mutate")
    async def mutate(_session=Depends(require_session)):
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=settings.local_origin) as client:
        rejected = await client.post(
            "/mutate", headers={"origin": "https://attacker.example"}
        )
        accepted = await client.post(
            "/mutate", headers={"origin": settings.local_origin}
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
