from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core.config import ROOT, Settings
from core.main import build_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root=ROOT,
        host="127.0.0.1",
        port=8100,
        workers_config=ROOT / "config" / "workers.json",
        tools_config=ROOT / "config" / "tools.yaml",
        db_path=tmp_path / "state.sqlite",
        audio_tmp=tmp_path / "audio",
        auth_enabled=False,
        google_client_id="",
        google_client_secret="",
        public_origin="",
        session_ttl_days=30,
        session_secret="test-secret",
        vram_free_threshold_mib=15000,
        gpu_index=0,
        context_tokens=32768,
        max_response_tokens=128,
        temperature=0.1,
        automotive_knowledge_db=tmp_path / "automotive-knowledge.sqlite",
    )


@pytest.mark.asyncio
async def test_health_is_503_until_full_model_contract_is_ready(tmp_path):
    app = build_app(_settings(tmp_path))

    async def stopped_health():
        return {"ready": False, "state": "stopped", "error": "no_active_worker"}

    app.state.router.health = stopped_health
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stopped = await client.get("/healthz")
        assert stopped.status_code == 503
        assert stopped.json()["ok"] is False
        assert stopped.json()["core"] == "running"
        assert stopped.headers["cache-control"] == "no-store"
        assert stopped.headers["x-content-type-options"] == "nosniff"
        assert stopped.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in stopped.headers["content-security-policy"]

        async def ready_health():
            return {
                "ready": True,
                "state": "healthy",
                "alias_ok": True,
                "context_ok": True,
                "gpu_ok": True,
            }

        app.state.router.active_name = "omni"
        app.state.router.health = ready_health
        ready = await client.get("/healthz")
        assert ready.status_code == 200
        assert ready.json()["ok"] is True

    app.state.automotive_knowledge.repository.close()
    app.state.store.close()
