"""
X Omni -- shared config/.env.local read/atomic-update helpers.

Split out of core/api/auth.py so core/config.py can reuse the same
preserve-unrelated-keys write path (e.g. to persist an auto-generated VAPID
keypair) without importing the auth router into the settings module.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def read_env_values(path: Path) -> dict[str, str]:
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


def atomic_update_env(path: Path, updates: dict[str, str]) -> None:
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
