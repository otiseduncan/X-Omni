"""Reliability patch for the licensed-research Windows credential vault.

ctypes.get_last_error() is only guaranteed to track a DLL call when that DLL
was loaded with use_last_error=True. The base research module intentionally
keeps its Win32 surface tiny; this installs the same vault using an Advapi32
handle that reliably preserves ERROR_NOT_FOUND and other failure codes.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any

from . import research_operator as ro


class ReliableWindowsCredentialVault(ro.WindowsCredentialVault):
    def __init__(self, target: str = ro.CREDENTIAL_TARGET):
        self.target = target
        if not hasattr(ctypes, "WinDLL"):
            self._advapi = None
            return
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(ro._CREDENTIALW), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ro._PCREDENTIALW),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None


def install() -> dict[str, Any]:
    ro.VAULT = ReliableWindowsCredentialVault()
    return {
        "vault": "windows_credential_manager",
        "reliable_last_error": True,
    }
