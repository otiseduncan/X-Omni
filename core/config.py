"""
X Omni -- settings.

Loads config/.env.local (gitignored) into the environment, then reads
everything off env vars with sane defaults. No secrets are ever hardcoded
here and none are logged.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

from .env_file import atomic_update_env

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


def _rooted_path(name: str, default: Path) -> Path:
    """Resolve a configurable path against the repository, never the CWD."""
    configured = Path(os.getenv(name, str(default))).expanduser()
    if not configured.is_absolute():
        configured = ROOT / configured
    return configured.resolve(strict=False)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_vapid_keypair() -> tuple[str, str]:
    """(public_key, private_key), base64url-encoded in the raw
    uncompressed-point / raw-scalar format Web Push, py_vapid, and pywebpush
    all expect -- not PEM."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_b64 = _b64url(private_key.private_numbers().private_value.to_bytes(32, "big"))
    public_numbers = private_key.public_key().public_numbers()
    public_raw = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
    return _b64url(public_raw), private_b64


def _ensure_vapid_keys(env_path: Path) -> tuple[str, str]:
    """Web Push needs a stable keypair -- a subscription is bound to the
    public key it was created against, so unlike session_secret's fresh-
    every-restart fallback, this one is generated once and persisted so
    existing browser subscriptions keep working across restarts."""
    public_key = os.getenv("XOMNI_VAPID_PUBLIC_KEY", "").strip()
    private_key = os.getenv("XOMNI_VAPID_PRIVATE_KEY", "").strip()
    if public_key and private_key:
        return public_key, private_key
    public_key, private_key = _generate_vapid_keypair()
    atomic_update_env(env_path, {
        "XOMNI_VAPID_PUBLIC_KEY": public_key,
        "XOMNI_VAPID_PRIVATE_KEY": private_key,
    })
    os.environ["XOMNI_VAPID_PUBLIC_KEY"] = public_key
    os.environ["XOMNI_VAPID_PRIVATE_KEY"] = private_key
    return public_key, private_key


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
    # ALLDATA is sunset (core.services.alldata_sunset). This stays False and is
    # not read from the environment: re-enabling ALLDATA is a code change and a
    # redeploy, never a setting.
    alldata_navigator_enabled: bool = False

    # Web Push
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""

    # Operator file attachments. Originals are stored content-addressed here
    # with their extracted text beside them, so the same file re-attached
    # costs nothing and `read_attachment` can page through a long document.
    attachment_dir: Path = Path("data") / "attachments"

    # Frigate -- the exterior camera's recorder, running in Docker on its own
    # Ubuntu machine with the camera and the recording disk attached to it.
    # X is a client of its authenticated API and nothing more: it does not
    # start Frigate, supervise it, record anything itself, or touch the
    # camera. The address is configuration rather than a constant so the
    # same build works against a LAN name, a LAN address, or (if the Owner
    # ever wants it) a Tailscale name, with no code change.
    #
    # Port 8971 is Frigate's authenticated API. Port 5000 is its
    # unauthenticated internal API and is deliberately never used here.
    frigate_base_url: str = ""
    frigate_camera: str = "exterior"
    # Frigate ships a self-signed certificate on 8971, so verification is
    # off by default for that install and on the moment a real certificate
    # exists -- a setting, never a silent exception.
    frigate_verify_tls: bool = False
    frigate_timeout_seconds: float = 10.0
    frigate_clip_timeout_seconds: float = 120.0
    frigate_max_clip_seconds: int = 300
    operator_timezone: str = "America/New_York"
    # The Frigate account password is never stored here or in .env. It is
    # sealed with Windows DPAPI in this file (see windows_secrets.py).
    frigate_credential_path: Path = Path("data") / "credentials" / "frigate.bin"

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
        vapid_public_key, vapid_private_key = _ensure_vapid_keys(
            ROOT / "config" / ".env.local"
        )
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
            vapid_public_key=vapid_public_key,
            vapid_private_key=vapid_private_key,
            vapid_subject=os.getenv("XOMNI_VAPID_SUBJECT", "mailto:otiseduncan@gmail.com").strip(),
            attachment_dir=Path(
                os.getenv("XOMNI_ATTACHMENT_DIR", str(ROOT / "data" / "attachments"))
            ),
            frigate_base_url=os.getenv("FRIGATE_BASE_URL", "").strip(),
            frigate_camera=os.getenv("FRIGATE_CAMERA", "exterior").strip() or "exterior",
            frigate_verify_tls=_flag("FRIGATE_VERIFY_TLS", False),
            frigate_timeout_seconds=float(os.getenv("FRIGATE_TIMEOUT_SECONDS", "10.0")),
            frigate_clip_timeout_seconds=float(
                os.getenv("FRIGATE_CLIP_TIMEOUT_SECONDS", "120.0")
            ),
            frigate_max_clip_seconds=_int("FRIGATE_MAX_CLIP_SECONDS", 300),
            frigate_credential_path=_rooted_path(
                "FRIGATE_CREDENTIAL_PATH",
                ROOT / "data" / "credentials" / "frigate.bin",
            ),
            operator_timezone=(
                os.getenv("XOMNI_OPERATOR_TIMEZONE", "America/New_York").strip()
                or "America/New_York"
            ),
        )
