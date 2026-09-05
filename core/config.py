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


def _ensure_internal_dvr_token(env_path: Path) -> str:
    """A stable, loopback-only secret shared by Core and the DVR service.

    It authenticates Core's server-to-server calls into the DVR service's
    API (Core's tool handlers run with no browser session/cookie of their
    own). Generated once and persisted, like the VAPID keypair, so both
    independently-started processes agree on the same value without a
    coordinated restart.
    """
    token = os.getenv("XOMNI_INTERNAL_DVR_TOKEN", "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    atomic_update_env(env_path, {"XOMNI_INTERNAL_DVR_TOKEN": token})
    os.environ["XOMNI_INTERNAL_DVR_TOKEN"] = token
    return token


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
    # ScrapeX Navigator is the production ALLDATA path. The provider browser
    # remains isolated from the ADAS Map work-profile session.
    alldata_navigator_enabled: bool = True

    # Web Push
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""

    # Background exterior-camera monitoring
    camera_snapshot_dir: Path = Path("data") / "camera-snapshots"
    camera_monitor_interval_seconds: int = 60
    camera_baseline_interval_seconds: int = 600
    camera_snapshot_retention_days: int = 30
    camera_motion_threshold: float = 18.0
    # A motion trigger opens a rolling documentation window: frames are
    # captured every camera_motion_burst_interval_seconds instead of the
    # normal baseline cadence, and continued motion re-arms the window so
    # sustained activity (e.g. floodlights staying on) keeps being
    # documented for as long as it continues.
    camera_motion_burst_seconds: int = 90
    camera_motion_burst_interval_seconds: int = 5

    # X DVR -- the standalone operator GUI (core/dvr_service.py). It survives
    # Core restarts; this is only the address Core's client and the DVR
    # service's own exact-origin check use, not a claim that DVR runs inside
    # Core. Actual recording/playback now lives entirely in MediaMTX, an
    # independently-managed process outside this repo (see mediamtx_client.py).
    dvr_port: int = 8300
    internal_dvr_token: str = ""

    # MediaMTX -- the exterior camera's media transport (RTSP connection,
    # continuous native recording, HLS/WebRTC live delivery, recorded-range
    # playback). All addresses are loopback; MediaMTX itself is started
    # independently (scripts/launch-mediamtx.ps1), never by Core or the DVR
    # GUI process.
    mediamtx_control_base_url: str = "http://127.0.0.1:9997"
    mediamtx_playback_base_url: str = "http://127.0.0.1:9996"
    mediamtx_hls_base_url: str = "http://127.0.0.1:8888"
    mediamtx_webrtc_base_url: str = "http://127.0.0.1:8889"
    mediamtx_rtsp_base_url: str = "rtsp://127.0.0.1:8554"
    mediamtx_recordings_root: Path = Path("E:/MediaMTX/recordings")
    mediamtx_clips_root: Path = Path("E:/MediaMTX/clips")

    @property
    def local_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def dvr_local_origin(self) -> str:
        return f"http://127.0.0.1:{self.dvr_port}"

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
        internal_dvr_token = _ensure_internal_dvr_token(ROOT / "config" / ".env.local")
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
            dvr_port=_int("XOMNI_DVR_PORT", 8300),
            internal_dvr_token=internal_dvr_token,
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
            alldata_navigator_enabled=os.getenv(
                "XOMNI_ALLDATA_NAVIGATOR_ENABLED", "1"
            ).strip().casefold() in {"1", "true", "yes", "on"},
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
            camera_snapshot_dir=Path(
                os.getenv("XOMNI_CAMERA_SNAPSHOT_DIR", str(ROOT / "data" / "camera-snapshots"))
            ),
            camera_monitor_interval_seconds=_int("XOMNI_CAMERA_MONITOR_INTERVAL_SECONDS", 60),
            camera_baseline_interval_seconds=_int("XOMNI_CAMERA_BASELINE_INTERVAL_SECONDS", 600),
            camera_snapshot_retention_days=_int("XOMNI_CAMERA_SNAPSHOT_RETENTION_DAYS", 30),
            camera_motion_threshold=float(os.getenv("XOMNI_CAMERA_MOTION_THRESHOLD", "18.0")),
            camera_motion_burst_seconds=_int("XOMNI_CAMERA_MOTION_BURST_SECONDS", 90),
            camera_motion_burst_interval_seconds=_int(
                "XOMNI_CAMERA_MOTION_BURST_INTERVAL_SECONDS", 5
            ),
            mediamtx_control_base_url=os.getenv(
                "XOMNI_MEDIAMTX_CONTROL_BASE_URL", "http://127.0.0.1:9997"
            ).strip().rstrip("/"),
            mediamtx_playback_base_url=os.getenv(
                "XOMNI_MEDIAMTX_PLAYBACK_BASE_URL", "http://127.0.0.1:9996"
            ).strip().rstrip("/"),
            mediamtx_hls_base_url=os.getenv(
                "XOMNI_MEDIAMTX_HLS_BASE_URL", "http://127.0.0.1:8888"
            ).strip().rstrip("/"),
            mediamtx_webrtc_base_url=os.getenv(
                "XOMNI_MEDIAMTX_WEBRTC_BASE_URL", "http://127.0.0.1:8889"
            ).strip().rstrip("/"),
            mediamtx_rtsp_base_url=os.getenv(
                "XOMNI_MEDIAMTX_RTSP_BASE_URL", "rtsp://127.0.0.1:8554"
            ).strip().rstrip("/"),
            mediamtx_recordings_root=Path(
                os.getenv("XOMNI_MEDIAMTX_RECORDINGS_ROOT", r"E:\MediaMTX\recordings")
            ),
            mediamtx_clips_root=Path(
                os.getenv("XOMNI_MEDIAMTX_CLIPS_ROOT", r"E:\MediaMTX\clips")
            ),
        )
