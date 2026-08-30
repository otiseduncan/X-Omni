"""Regenerate MediaMTX's camera `paths:` block from X Omni's stored credentials.

    cd "X:\\X Omni"
    .venv\\Scripts\\python.exe scripts\\sync-mediamtx-config.py [mediamtx.yml path]

Run before starting MediaMTX (see scripts/launch-mediamtx.ps1) so the
exterior camera's paths always reflect whatever credentials/host X Omni
currently has configured, without ever committing them to a config file
under git. Prints only bounded, non-secret metadata -- never a resolved
stream URL, which embeds this camera's credentials directly in its path.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings  # noqa: E402
from core.services import exterior_camera as exterior_camera_svc  # noqa: E402
from core.services import mediamtx_config  # noqa: E402

DEFAULT_MEDIAMTX_YAML = Path("X:/MediaMTX/mediamtx.yml")


async def main() -> int:
    yaml_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MEDIAMTX_YAML
    settings = Settings.load()
    exterior_camera = exterior_camera_svc.ExteriorCameraService(settings.root)
    try:
        plans = await mediamtx_config.discover_camera_path_plans(exterior_camera)
    except mediamtx_config.MediaMTXConfigError as exc:
        print(f"Could not configure MediaMTX camera paths: {exc}", file=sys.stderr)
        return 1
    finally:
        await exterior_camera.shutdown()
    mediamtx_config.update_mediamtx_yaml(yaml_path, plans)
    for plan in plans:
        print(f"  {plan.path_name}: {plan.profile_name} -- {plan.encoding} {plan.width}x{plan.height}")
    print(f"Wrote {yaml_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
