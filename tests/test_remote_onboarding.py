from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI

from core.api import auth
from core.services import google_auth
from core.state.db import Store
from core.tools.registry import Registry


def settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
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


def tailscale_headers(login: str) -> dict[str, str]:
    return {
        "host": "omega.example.ts.net",
        "x-forwarded-proto": "https",
        "tailscale-user-login": login,
    }


def app_for(settings_, store) -> FastAPI:
    app = FastAPI()
    app.include_router(auth.create_router(settings_, store))
    return app


async def complete_google_callback(
    monkeypatch, client: httpx.AsyncClient, headers: dict[str, str], *,
    sub: str, email: str, verified: bool = True,
):
    async def exchange(*_args, **_kwargs):
        return {
            "access_token": "access",
            "id_token": "signed-id-token",
            "expires_at": 9999999999,
            "scope": "openid email profile",
        }

    async def verify(*_args, **_kwargs):
        if not verified:
            raise google_auth.GoogleAuthError("A verified Google email is required.")
        return {
            "sub": sub,
            "email": email,
            "email_verified": True,
            "name": "Remote Tester",
            "picture": "https://example.com/avatar.png",
        }

    async def userinfo(*_args, **_kwargs):
        return {"sub": sub, "email": email, "email_verified": verified}

    monkeypatch.setattr(google_auth, "exchange_code", exchange)
    monkeypatch.setattr(auth, "verify_google_id_token", verify)
    monkeypatch.setattr(google_auth, "fetch_userinfo", userinfo)
    state = next(reversed(auth._pending_states))
    return await client.get(
        "/api/auth/callback",
        params={"code": "code", "state": state},
        headers=headers,
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_matching_remote_google_login_provisions_and_reuses_profile(tmp_path, monkeypatch):
    auth._pending_states.clear()
    store = Store(tmp_path / "state.sqlite")
    store.bind_owner("owner-sub", "owner@example.com", "Owner")
    invited = store.invite_test_user("tester@example.com")
    app = app_for(settings(tmp_path), store)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://omega.example.ts.net"
    ) as client:
        login = await client.get(
            "/api/auth/login", headers=tailscale_headers("Tester@Example.com"),
            follow_redirects=False,
        )
        assert login.status_code in {302, 307}
        assert "calendar" not in login.headers["location"]
        assert "access_type=offline" not in login.headers["location"]
        callback = await complete_google_callback(
            monkeypatch, client, tailscale_headers("tester@example.com"),
            sub="tester-google-sub", email="tester@example.com",
        )
        assert callback.status_code in {302, 307}
        status = await client.get(
            "/api/auth/status", headers=tailscale_headers("tester@example.com")
        )
        assert status.json()["signed_in"] is True
        assert status.json()["current_user"]["role"] == "test_user"

    provisioned = store.get_user(invited["id"])
    assert provisioned["google_sub"] == "tester-google-sub"
    assert provisioned["tailscale_login"] == "tester@example.com"
    assert provisioned["status"] == "active"

    async with httpx.AsyncClient(
        transport=transport, base_url="https://omega.example.ts.net"
    ) as second:
        await second.get(
            "/api/auth/login", headers=tailscale_headers("tester@example.com"),
            follow_redirects=False,
        )
        repeated = await complete_google_callback(
            monkeypatch, second, tailscale_headers("tester@example.com"),
            sub="tester-google-sub", email="tester@example.com",
        )
        assert repeated.status_code in {302, 307}
    assert len(store.list_test_users()) == 1
    assert store.get_user(invited["id"])["id"] == provisioned["id"]


@pytest.mark.asyncio
async def test_returning_remote_owner_reuses_durable_google_grant(tmp_path, monkeypatch):
    auth._pending_states.clear()
    store = Store(tmp_path / "state.sqlite")
    store.bind_owner("owner-sub", "owner@example.com", "Owner")
    store.save_google_token({
        "access_token": "old-access",
        "refresh_token": "durable-refresh",
        "scope": "openid email profile calendar",
        "expires_at": 1,
        "account_email": None,
    })
    app = app_for(settings(tmp_path), store)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://omega.example.ts.net"
    ) as client:
        login = await client.get(
            "/api/auth/login", headers=tailscale_headers("owner@example.com"),
            follow_redirects=False,
        )
        assert login.status_code in {302, 307}
        assert "access_type=offline" in login.headers["location"]
        assert "prompt=consent" not in login.headers["location"]

        callback = await complete_google_callback(
            monkeypatch, client, tailscale_headers("owner@example.com"),
            sub="owner-sub", email="owner@example.com",
        )
        assert callback.status_code in {302, 307}
        assert callback.headers["location"] == "/"

    token = store.get_google_token()
    assert token["access_token"] == "access"
    assert token["refresh_token"] == "durable-refresh"


@pytest.mark.asyncio
async def test_remote_enrollment_rejects_mismatch_unverified_and_revoked(tmp_path, monkeypatch):
    auth._pending_states.clear()
    store = Store(tmp_path / "state.sqlite")
    store.bind_owner("owner-sub", "owner@example.com", "Owner")
    invited = store.invite_test_user("tester@example.com")
    app = app_for(settings(tmp_path), store)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://omega.example.ts.net"
    ) as client:
        await client.get(
            "/api/auth/login", headers=tailscale_headers("tester@example.com"),
            follow_redirects=False,
        )
        mismatch = await complete_google_callback(
            monkeypatch, client, tailscale_headers("tester@example.com"),
            sub="other-sub", email="other@example.com",
        )
        assert mismatch.headers["location"] == "/?auth_error=identity_mismatch"
        assert store.get_user(invited["id"])["google_sub"] is None

        await client.get(
            "/api/auth/login", headers=tailscale_headers("tester@example.com"),
            follow_redirects=False,
        )
        unverified = await complete_google_callback(
            monkeypatch, client, tailscale_headers("tester@example.com"),
            sub="tester-sub", email="tester@example.com", verified=False,
        )
        assert unverified.headers["location"] == "/?auth_error=exchange_failed"
        assert store.get_user(invited["id"])["google_sub"] is None

    store.set_test_user_status(invited["id"], "revoked")
    async with httpx.AsyncClient(
        transport=transport, base_url="https://omega.example.ts.net"
    ) as revoked_client:
        rejected = await revoked_client.get(
            "/api/auth/login", headers=tailscale_headers("tester@example.com"),
            follow_redirects=False,
        )
    assert rejected.status_code == 403


@pytest.mark.asyncio
async def test_tailscale_headers_are_trusted_only_on_exact_loopback_serve_origin(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.bind_owner("owner-sub", "owner@example.com", "Owner")
    store.invite_test_user("tester@example.com")
    app = app_for(settings(tmp_path), store)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8100"
    ) as local:
        spoof = await local.get(
            "/api/auth/status",
            headers={"host": "127.0.0.1:8100", "tailscale-user-login": "tester@example.com"},
        )
    assert spoof.json()["access_path"] == "local"
    assert spoof.json()["tailscale_identity"] is None

    async with httpx.AsyncClient(
        transport=transport, base_url="https://omega.example.ts.net"
    ) as remote:
        missing = await remote.get(
            "/api/auth/status",
            headers={"host": "omega.example.ts.net", "x-forwarded-proto": "https"},
        )
        unproved = await remote.get(
            "/api/auth/status",
            headers={"host": "omega.example.ts.net", "tailscale-user-login": "tester@example.com"},
        )
        trusted = await remote.get(
            "/api/auth/status", headers=tailscale_headers("tester@example.com")
        )
    assert missing.json()["access_path"] == "denied"
    assert unproved.json()["access_path"] == "denied"
    assert trusted.json()["remote_authorized"] is True

    unsafe_settings = settings(tmp_path)
    unsafe_settings.host = "0.0.0.0"
    unsafe_app = app_for(unsafe_settings, store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unsafe_app),
        base_url="https://omega.example.ts.net",
    ) as unsafe_remote:
        unsafe = await unsafe_remote.get(
            "/api/auth/status", headers=tailscale_headers("tester@example.com")
        )
    assert unsafe.json()["access_path"] == "denied"
    assert "bind loopback" in unsafe.json()["remote_access_error"]


@pytest.mark.asyncio
async def test_session_remains_bound_to_same_tailscale_identity_and_revocation(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.bind_owner("owner-sub", "owner@example.com", "Owner")
    invited = store.invite_test_user("tester@example.com")
    user = store.provision_test_user(
        google_sub="tester-sub", email="tester@example.com", display_name="Tester",
        avatar_url=None, tailscale_login="tester@example.com",
    )
    token = store.create_session(
        "tester-sub", "browser", 30, user_id=user["id"],
        tailscale_login="tester@example.com",
    )
    dependency = auth.make_require_session(settings(tmp_path), store)
    app = FastAPI()

    @app.get("/private")
    async def private(session=Depends(dependency)):
        return {"user_id": session["user_id"]}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://omega.example.ts.net",
        cookies={auth.SESSION_COOKIE: token},
    ) as client:
        accepted = await client.get("/private", headers=tailscale_headers("tester@example.com"))
        crossed = await client.get("/private", headers=tailscale_headers("other@example.com"))
    assert accepted.status_code == 200
    assert crossed.status_code == 403

    store.set_test_user_status(invited["id"], "revoked")
    assert store.get_session(token) is None


def test_user_scoped_data_and_tool_roles_fail_closed(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.bind_owner("owner-sub", "owner@example.com", "Owner")
    first = store.invite_test_user("first@example.com")
    second = store.invite_test_user("second@example.com")
    first_id = first["id"]
    second_id = second["id"]
    owner_id = store.owner_user()["id"]
    owner_conversation = store.create_conversation("Owner private", user_id=owner_id)
    first_conversation = store.create_conversation("First private", user_id=first_id)
    store.add_message(owner_conversation, "user", "owner secret")
    store.add_message(first_conversation, "user", "first secret")

    assert store.list_conversations(user_id=second_id) == []
    assert store.get_messages(owner_conversation, user_id=first_id) == []
    assert not store.conversation_exists(owner_conversation, user_id=first_id)
    assert Registry.role_allows_tool("test_user", "web_research_current")
    assert Registry.role_allows_tool("test_user", "add_task")
    assert not Registry.role_allows_tool("test_user", "read_file")
    assert not Registry.role_allows_tool("test_user", "run_powershell")
    assert not Registry.role_allows_tool("test_user", "get_calendar")


@pytest.mark.asyncio
async def test_owner_admin_invitation_qr_and_reenrollment_are_local_and_audited(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite")
    store.bind_owner("owner-sub", "owner@example.com", "Owner")
    owner = store.owner_user()
    token = store.create_session("owner-sub", "browser", 30, user_id=owner["id"])
    app = app_for(settings(tmp_path), store)
    transport = httpx.ASGITransport(app=app)
    invite_url = "https://login.tailscale.com/a/example-token"
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8100",
        cookies={auth.SESSION_COOKIE: token},
    ) as client:
        created = await client.post(
            "/api/auth/admin/test-users",
            json={"email": "tester@example.com", "tailscale_invite_url": invite_url},
            headers={"origin": "http://127.0.0.1:8100"},
        )
        assert created.status_code == 200
        user = created.json()
        qr = await client.get(f"/api/auth/admin/test-users/{user['id']}/invite-qr.png")
        assert qr.status_code == 200
        assert qr.headers["content-type"] == "image/png"
        assert qr.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert qr.headers["cache-control"] == "no-store"
        revoked = await client.patch(
            f"/api/auth/admin/test-users/{user['id']}",
            json={"status": "revoked"},
            headers={"origin": "http://127.0.0.1:8100"},
        )
        reset = await client.patch(
            f"/api/auth/admin/test-users/{user['id']}",
            json={"status": "pending"},
            headers={"origin": "http://127.0.0.1:8100"},
        )
    assert revoked.json()["status"] == "revoked"
    assert reset.json()["status"] == "pending"
    audit_text = "\n".join(
        str(row["detail_json"]) for row in store.conn.execute("SELECT detail_json FROM audit_log")
    )
    assert invite_url not in audit_text
