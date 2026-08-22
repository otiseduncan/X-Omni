from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter

from core.services import research_operator


def test_research_routes_skip_minimal_settings_without_application_root():
    router = APIRouter(prefix="/api")

    async def require_session():
        return {"role": "owner", "user_id": "local-dev"}

    # Historical route tests intentionally use settings doubles that contain
    # only the attributes needed by the route under test. Research browser
    # routes require a real application root for their protected browser profile
    # and must remain optional for these fixtures.
    research_operator.install_http_routes(
        router,
        SimpleNamespace(local_origin="http://127.0.0.1:8100", public_origin=""),
        require_session,
    )

    paths = {route.path for route in router.routes}
    assert not any("/research/providers/alldata" in path for path in paths)


def test_research_routes_attach_when_real_application_root_exists(tmp_path):
    router = APIRouter(prefix="/api")

    async def require_session():
        return {"role": "owner", "user_id": "local-dev"}

    settings = SimpleNamespace(
        root=tmp_path,
        local_origin="http://127.0.0.1:8100",
        public_origin="",
    )
    research_operator.install_http_routes(router, settings, require_session)

    paths = {route.path for route in router.routes}
    assert "/api/research/providers/alldata/status" in paths
    assert "/api/research/providers/alldata/credentials" in paths
    assert "/api/research/providers/alldata/sessions" in paths
