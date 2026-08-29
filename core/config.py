"""
X Omni -- settings.

Loads config/.env.local (gitignored) into the environment, then reads
everything off env vars with sane defaults. No secrets are ever hardcoded
here and none are logged.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env_file() -> None:
    path = ROOT / "config" / ".env.local"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


@dataclass(frozen=True)
class Settings:
    root: Path
    host: str
    port: int
    workers_config: Path
    tools_config: Path
    db_path: Path
    audio_tmp: Path

    # Auth
    auth_enabled: bool
    google_client_id: str
    google_client_secret: str
    public_origin: str          # https://omega.<tailnet>.ts.net -- for the remote redirect URI
    session_ttl_days: int
    session_secret: str

    # Model
    vram_free_threshold_mib: int
    gpu_index: int
    context_tokens: int
    max_response_tokens: int
    temperature: float

    # Field tools. Defaulted and placed last on purpose: every existing
    # Settings(...) call site -- tests included -- must keep working without
    # being updated. Settings.load() still overrides these from the env.
    adas_si_root: Path = Path(r"X:\ADAS SI")
    calibration_iq_base_url: str = "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq"
    calibration_iq_project_path: Path = Path(r"X:\calibration iq")
    scrapex_base_url: str = "http://127.0.0.1:8125"
    scrapex_project_path: Path = Path(r"X:\ScrapeX")
    automotive_knowledge_db: Path | None = None
    tool_profile: str = "adas_operator"

    @property
    def local_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def redirect_uris(self) -> list[str]:
        """Both must be registered in the Google Cloud console. Desktop uses
        the loopback one; the phone over Tailscale uses the public one."""
        uris = [f"{self.local_origin}/api/auth/callback"]
        if self.public_origin:
            uris.append(f"{self.public_origin}/api/auth/callback")
        return uris

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @classmethod
    def load(cls) -> "Settings":
        port = _int("XOMNI_PORT", 8100)
        return cls(
            root=ROOT,
            # Core always binds loopback. Remote reach is Tailscale's job --
            # never widen this to 0.0.0.0, it would put the operator core on
            # the LAN with nothing in front of it.
            host="127.0.0.1",
            port=port,
            workers_config=ROOT / "config" / "workers.json",
            tools_config=ROOT / "config" / "tools.yaml",
            db_path=ROOT / "data" / "x_omni.sqlite",
            audio_tmp=ROOT / "data" / "audio",
            auth_enabled=_flag("XOMNI_AUTH_ENABLED", True),
            google_client_id=os.getenv("XOMNI_GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.getenv("XOMNI_GOOGLE_CLIENT_SECRET", "").strip(),
            public_origin=os.getenv("XOMNI_PUBLIC_ORIGIN", "").strip().rstrip("/"),
            session_ttl_days=_int("XOMNI_SESSION_TTL_DAYS", 30),
            session_secret=os.getenv("XOMNI_SESSION_SECRET", "").strip() or secrets.token_urlsafe(32),
            # Field tools. The Calibration IQ base URL already includes the
            # /calibration-iq suffix; trailing slash stripped so call sites
            # can safely append "/collection/ros".
            adas_si_root=Path(os.getenv("XOMNI_ADAS_SI_ROOT", r"X:\ADAS SI")),
            calibration_iq_base_url=os.getenv(
                "XOMNI_CALIBRATION_IQ_BASE_URL",
                "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
            ).strip().rstrip("/"),
            calibration_iq_project_path=Path(
                os.getenv("XOMNI_CALIBRATION_IQ_PROJECT_PATH", r"X:\calibration iq")
            ),
            scrapex_base_url=os.getenv(
                "XOMNI_SCRAPEX_BASE_URL", "http://127.0.0.1:8125"
            ).strip().rstrip("/"),
            scrapex_project_path=Path(
                os.getenv("XOMNI_SCRAPEX_PROJECT_PATH", r"X:\ScrapeX")
            ),
            automotive_knowledge_db=Path(
                os.getenv(
                    "XOMNI_AUTOMOTIVE_KNOWLEDGE_DB",
                    str(
                        ROOT
                        / "data"
                        / "capabilities"
                        / "automotive_knowledge"
                        / "knowledge.sqlite"
                    ),
                )
            ),
            tool_profile=(
                os.getenv("XOMNI_TOOL_PROFILE", "adas_operator").strip()
                or "adas_operator"
            ),
            vram_free_threshold_mib=_int("XOMNI_VRAM_FREE_THRESHOLD_MIB", 15000),
            gpu_index=_int("XOMNI_GPU_INDEX", 0),
            context_tokens=_int("XOMNI_CONTEXT_TOKENS", 32768),
            max_response_tokens=_int("XOMNI_MAX_RESPONSE_TOKENS", 1536),
            temperature=float(os.getenv("XOMNI_TEMPERATURE", "0.4")),
        )
