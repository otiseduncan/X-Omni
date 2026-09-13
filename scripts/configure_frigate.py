"""Non-interactive half of configure-frigate.ps1.

The PowerShell wrapper owns the SecureString prompt. This process receives the
password on stdin, verifies the authenticated Frigate API and logical camera,
and only then commits the canonical non-secret settings. Nothing prints the
password, JWT, response bodies, or request headers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import Settings  # noqa: E402
from core.env_file import atomic_update_env  # noqa: E402
from core.services.frigate_client import (  # noqa: E402
    FrigateClient,
    FrigateError,
    credential_store,
    normalize_base_url,
    validate_camera_name,
)
from core.services.windows_secrets import (  # noqa: E402
    SecretNotConfigured,
    SecretStoreError,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--username")
    parser.add_argument("--base-url")
    parser.add_argument("--camera")
    parser.add_argument("--clear", action="store_true")
    return parser.parse_args()


async def _configure(args: argparse.Namespace) -> int:
    settings = Settings.load()
    store = credential_store(settings.frigate_credential_path)
    if args.clear:
        print("Frigate credential:", "removed" if store.clear() else "not registered")
        print("Credential store:", store.path)
        return 0

    username = str(args.username or "").strip()
    password = sys.stdin.readline().rstrip("\r\n")
    if not username or not password:
        print("Frigate configuration failed: username and password are required.", file=sys.stderr)
        return 2

    try:
        base_url = normalize_base_url(args.base_url or settings.frigate_base_url)
        camera = validate_camera_name(args.camera or settings.frigate_camera)
    except FrigateError as exc:
        print(f"Frigate configuration failed: {exc}", file=sys.stderr)
        return 2

    previous = None
    had_previous = False
    try:
        previous = store.load()
        had_previous = True
    except SecretNotConfigured:
        pass
    except SecretStoreError as exc:
        print(f"Frigate configuration failed: {exc}", file=sys.stderr)
        return 2

    client = FrigateClient(
        base_url=base_url,
        camera=camera,
        credential_path=settings.frigate_credential_path,
        verify_tls=settings.frigate_verify_tls,
        timeout_seconds=settings.frigate_timeout_seconds,
        clip_timeout_seconds=settings.frigate_clip_timeout_seconds,
        max_clip_seconds=settings.frigate_max_clip_seconds,
        store=store,
    )
    try:
        verified = await client.save_credential_verified(
            username=username, password=password
        )
        atomic_update_env(
            settings.root / "config" / ".env.local",
            {"FRIGATE_BASE_URL": base_url, "FRIGATE_CAMERA": camera},
        )
    except (FrigateError, SecretStoreError, OSError, ValueError) as exc:
        # save_credential_verified rolls back authentication/camera failures.
        # Restore here as well if the final non-secret config write failed.
        if had_previous and previous is not None:
            store.save(previous)
        elif store.configured():
            store.clear()
        print(f"Frigate configuration failed: {exc}", file=sys.stderr)
        return 1

    health = verified["health"]
    print("Frigate authentication: verified")
    print("Frigate camera:", health.get("camera"))
    print("Camera configured:", bool(health.get("camera_configured")))
    print("Camera running:", bool(health.get("camera_running")))
    print("Frigate URL:", base_url)
    print("Credential store:", store.path)
    return 0


def main() -> int:
    try:
        return asyncio.run(_configure(_arguments()))
    except KeyboardInterrupt:
        print("Frigate configuration cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
