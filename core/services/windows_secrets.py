"""Machine-bound secret storage for service credentials X must hold.

X Omni already protected the exterior camera's ONVIF password with Windows
DPAPI rather than a plaintext ``.env`` entry. That camera is now Frigate's
to talk to, but the pattern it established is the right one for any
credential X keeps, so it lives here instead of inside one service: bytes
encrypted to the current Windows user with an application-specific entropy
string, written to a file only that user can decrypt.

Nothing here ever logs, formats, or repr()s a secret value. Callers get
bytes back or an exception; there is deliberately no "show me the stored
password" accessor, because no caller needs one.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
from typing import Any, Optional

CRYPTPROTECT_UI_FORBIDDEN = 0x1
MAX_SECRET_FILE_BYTES = 64 * 1024


class SecretStoreError(RuntimeError):
    """A secret could not be sealed or opened on this machine."""


class SecretNotConfigured(SecretStoreError):
    """No credential has been registered yet."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(raw: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(raw)
    return (
        _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
        buffer,
    )


def _dpapi_transform(raw: bytes, *, entropy: bytes, description: str, protect: bool) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("Protected credentials require Windows DPAPI.")
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_blob, input_buffer = _blob(raw)
    entropy_blob, entropy_buffer = _blob(entropy)
    output_blob = _DataBlob()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            description,
            ctypes.byref(entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    # Keep the backing buffers alive until the call returns.
    del input_buffer, entropy_buffer
    if not ok:
        raise SecretStoreError(
            "The stored credential could not be opened with Windows DPAPI. "
            "It was sealed for a different Windows user or machine."
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


class SecretStore:
    """One DPAPI-sealed JSON document on disk, for one service's credential.

    ``protect``/``unprotect`` are injectable so tests never need real DPAPI
    (and so a non-Windows checkout can still exercise the logic), but the
    default is the real thing -- there is no plaintext fallback, because a
    silent fallback is how credentials end up readable on disk.
    """

    def __init__(
        self,
        path: Path,
        *,
        entropy: bytes,
        description: str,
        protect=None,
        unprotect=None,
    ):
        self.path = Path(path)
        self._entropy = bytes(entropy)
        self._description = str(description)
        self._protect = protect or (
            lambda raw: _dpapi_transform(
                raw, entropy=self._entropy, description=self._description, protect=True
            )
        )
        self._unprotect = unprotect or (
            lambda raw: _dpapi_transform(
                raw, entropy=self._entropy, description=self._description, protect=False
            )
        )

    def configured(self) -> bool:
        try:
            return self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            return False

    def save(self, document: dict[str, Any]) -> None:
        raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
        sealed = self._protect(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(sealed)
        temporary.replace(self.path)
        if os.name != "nt":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def load(self) -> dict[str, Any]:
        if not self.configured():
            raise SecretNotConfigured("No credential has been registered.")
        try:
            sealed = self.path.read_bytes()
        except OSError as exc:
            raise SecretStoreError("The stored credential could not be read.") from exc
        if len(sealed) > MAX_SECRET_FILE_BYTES:
            raise SecretStoreError("The stored credential file is implausibly large.")
        raw = self._unprotect(sealed)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SecretStoreError("The stored credential is not readable.") from exc
        if not isinstance(document, dict):
            raise SecretStoreError("The stored credential has an unexpected shape.")
        return document

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SecretStoreError("The stored credential could not be removed.") from exc

    def __repr__(self) -> str:  # never leak contents through a traceback
        return f"<SecretStore path={self.path.name!r} configured={self.configured()}>"


def non_secret_summary(document: dict[str, Any], *, secret_keys: tuple[str, ...]) -> dict[str, Any]:
    """A copy safe to log or return over the API: secret values replaced by presence."""
    summary: dict[str, Any] = {}
    for key, value in document.items():
        if key in secret_keys:
            summary[key] = bool(str(value or ""))
        else:
            summary[key] = value
    return summary


def optional_text(value: object, *, maximum: int) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:maximum]
