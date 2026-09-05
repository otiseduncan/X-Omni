"""Authenticated post-collision research operator for X Omni.

The first licensed provider is ALLDATA Repair/Collision. Credentials are kept in
Windows Credential Manager and are never returned through tools, prompts, logs,
or conversation history. A persistent server-side Chrome profile keeps the
licensed session alive between research turns. When MFA, CAPTCHA, a changed
login flow, or another human-only step appears, the same browser can be viewed
and controlled from X's mobile chat card through same-origin screenshot/input
endpoints.

This module also gives X bounded public-source research for OEM collision sites
and manufacturer publications. It deliberately does not bypass access controls,
CAPTCHAs, paywalls, subscription limits, robots, or download restrictions. The
operator uses the access Otis already has and stops for human authentication
when a site requires it.
"""

from __future__ import annotations

import asyncio
import ctypes
import html
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import uuid
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, SecretStr

log = logging.getLogger("xomni.research_operator")

PROVIDER = "alldata"
PROVIDER_LABEL = "ALLDATA Repair / Collision"
ALLDATA_HOME = "https://my.alldata.com/"
ALLDATA_ALLOWED_SUFFIXES = ("alldata.com",)
CREDENTIAL_TARGET = "XOmni/ResearchProvider/ALLDATA"
MAX_USERNAME_CHARS = 320
MAX_PASSWORD_CHARS = 4096
MAX_URL_CHARS = 2048
MAX_TEXT_CHARS = 120_000
MAX_PUBLIC_BYTES = 4 * 1024 * 1024
SCREENSHOT_WIDTH = 1280
SCREENSHOT_HEIGHT = 900
SESSION_IDLE_SECONDS = 60 * 60


# ---------------------------------------------------------------------------
# Windows Credential Manager -- no plaintext secret file, DB row, or env var.
# ---------------------------------------------------------------------------

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


class CredentialVaultError(RuntimeError):
    pass


class WindowsCredentialVault:
    """Tiny CredWrite/CredRead wrapper with a secret-safe public status."""

    def __init__(self, target: str = CREDENTIAL_TARGET):
        self.target = target
        if not hasattr(ctypes, "windll"):
            self._advapi = None
            return
        self._advapi = ctypes.windll.advapi32
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIALW),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def _require_windows(self) -> None:
        if self._advapi is None:
            raise CredentialVaultError("Windows Credential Manager is unavailable on this host.")

    @staticmethod
    def _validate(username: str, password: str) -> tuple[str, str]:
        username = str(username or "").strip()
        password = str(password or "")
        if not username or len(username) > MAX_USERNAME_CHARS:
            raise ValueError("ALLDATA username is required and must be under 320 characters.")
        if not password or len(password) > MAX_PASSWORD_CHARS:
            raise ValueError("ALLDATA password is required and is too long.")
        if any(ord(ch) < 32 for ch in username):
            raise ValueError("ALLDATA username contains invalid control characters.")
        return username, password

    def write(self, username: str, password: str) -> dict[str, Any]:
        self._require_windows()
        username, password = self._validate(username, password)
        secret = password.encode("utf-8")
        if len(secret) > 5 * 512:
            raise ValueError("ALLDATA password exceeds the Windows credential blob limit.")
        blob = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
        credential = _CREDENTIALW()
        credential.Type = CRED_TYPE_GENERIC
        credential.TargetName = self.target
        credential.Comment = "X Omni licensed research provider credential"
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = username
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialVaultError(
                f"Windows Credential Manager rejected the write (error {ctypes.get_last_error()})."
            )
        return self.status()

    def read(self) -> tuple[str, str] | None:
        self._require_windows()
        pointer = _PCREDENTIALW()
        if not self._advapi.CredReadW(self.target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialVaultError(f"Windows Credential Manager read failed (error {error}).")
        try:
            credential = pointer.contents
            username = str(credential.UserName or "")
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return username, raw.decode("utf-8")
        finally:
            self._advapi.CredFree(pointer)

    def delete(self) -> bool:
        self._require_windows()
        if self._advapi.CredDeleteW(self.target, CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == 1168:
            return False
        raise CredentialVaultError(f"Windows Credential Manager delete failed (error {error}).")

    def status(self) -> dict[str, Any]:
        try:
            found = self.read()
        except CredentialVaultError as exc:
            return {
                "provider": PROVIDER,
                "configured": False,
                "vault": "windows_credential_manager",
                "error": str(exc),
            }
        return {
            "provider": PROVIDER,
            "provider_label": PROVIDER_LABEL,
            "configured": found is not None,
            "username": found[0] if found else "",
            "password_stored": found is not None,
            "vault": "windows_credential_manager",
            "secret_exposed_to_model": False,
        }


VAULT = WindowsCredentialVault()


# ---------------------------------------------------------------------------
# Persistent licensed browser session.
# ---------------------------------------------------------------------------


def _safe_filename(value: str, default: str = "research") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._() -]+", " ", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:150] or default).strip()


def _is_alldata_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith(f".{suffix}") for suffix in ALLDATA_ALLOWED_SUFFIXES
    )


class BrowserUnavailable(RuntimeError):
    pass


class LicensedBrowser:
    def __init__(self, root: Path, adas: Any | None = None):
        self.root = Path(root).resolve()
        # 'credentials' is intentionally a protected path segment in Registry.
        # It contains session cookies and must never be exposed through file tools.
        self.profile_root = self.root / "data" / "credentials" / "browser" / PROVIDER
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.adas = adas
        self._playwright = None
        self._context = None
        self._page = None
        self._session_id: Optional[str] = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        if self._context is not None and self._page is not None:
            self._last_used = time.monotonic()
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed. Pull X Omni and run pip install -r requirements.lock.txt."
            ) from exc
        self._playwright = await async_playwright().start()
        launch_args = dict(
            user_data_dir=str(self.profile_root),
            headless=True,
            viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT},
            locale="en-US",
            accept_downloads=False,
        )
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chrome", **launch_args
            )
        except Exception as chrome_exc:  # noqa: BLE001
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **launch_args
                )
            except Exception as bundled_exc:  # noqa: BLE001
                await self._playwright.stop()
                self._playwright = None
                raise BrowserUnavailable(
                    "Could not launch Chrome for authenticated research. Install Chrome or run "
                    "'.venv\\Scripts\\python.exe -m playwright install chromium'. "
                    f"Chrome: {type(chrome_exc).__name__}; Chromium: {type(bundled_exc).__name__}."
                ) from bundled_exc
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._session_id = uuid.uuid4().hex
        self._last_used = time.monotonic()

    async def close(self) -> None:
        async with self._lock:
            context, runner = self._context, self._playwright
            self._context = self._page = self._playwright = None
            self._session_id = None
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass
            if runner is not None:
                try:
                    await runner.stop()
                except Exception:  # noqa: BLE001
                    pass

    async def start(self, *, auto_login: bool = True) -> dict[str, Any]:
        async with self._lock:
            await self._ensure()
            assert self._page is not None
            if not _is_alldata_url(self._page.url):
                await self._page.goto(ALLDATA_HOME, wait_until="domcontentloaded", timeout=45_000)
            if auto_login:
                await self._try_saved_login()
            return await self.status()

    async def _try_saved_login(self) -> None:
        assert self._page is not None
        credential = VAULT.read()
        if credential is None:
            return
        username, password = credential
        page = self._page
        # Generic selectors are intentional. ALLDATA has changed its sign-in UI
        # over time; labels/types are more stable than a brittle CSS class.
        password_box = page.locator("input[type='password']").first
        try:
            if not await password_box.is_visible(timeout=3_000):
                return  # persistent profile is probably already authenticated
        except Exception:  # noqa: BLE001
            return
        username_selectors = [
            "input[type='email']",
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[name*='email' i]",
            "input[id*='email' i]",
            "input[type='text']",
        ]
        username_box = None
        for selector in username_selectors:
            candidate = page.locator(selector).first
            try:
                if await candidate.is_visible(timeout=500):
                    username_box = candidate
                    break
            except Exception:  # noqa: BLE001
                continue
        try:
            if username_box is not None:
                await username_box.fill(username)
            await password_box.fill(password)
            submit = page.locator("button[type='submit'], input[type='submit']").first
            if await submit.count():
                await submit.click()
            else:
                await password_box.press("Enter")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass
        finally:
            # Do not leave local Python references to secrets around longer than needed.
            username = ""
            password = ""

    async def status(self) -> dict[str, Any]:
        page = self._page
        if page is None:
            return {
                "provider": PROVIDER,
                "browser_active": False,
                "credential": VAULT.status(),
                "login_url": ALLDATA_HOME,
            }
        try:
            title = await page.title()
            password_visible = await page.locator("input[type='password']").first.is_visible(timeout=500)
        except Exception:  # noqa: BLE001
            title = ""
            password_visible = False
        url = page.url
        authenticated = _is_alldata_url(url) and not password_visible and "login" not in url.casefold()
        return {
            "provider": PROVIDER,
            "provider_label": PROVIDER_LABEL,
            "browser_active": True,
            "session_id": self._session_id,
            "url": url[:MAX_URL_CHARS],
            "title": title[:300],
            "authenticated": authenticated,
            "human_action_required": bool(password_visible),
            "credential": VAULT.status(),
            "mobile_takeover": True,
            "screenshot_url": (
                f"/api/research/providers/{PROVIDER}/sessions/{self._session_id}/screenshot"
                if self._session_id else None
            ),
        }

    def _require_session(self, session_id: str) -> None:
        if not self._session_id or str(session_id) != self._session_id or self._page is None:
            raise ValueError("The ALLDATA browser session is not active or has changed.")
        self._last_used = time.monotonic()

    async def screenshot(self, session_id: str) -> bytes:
        async with self._lock:
            self._require_session(session_id)
            assert self._page is not None
            return await self._page.screenshot(type="jpeg", quality=72, full_page=False)

    async def human_action(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._require_session(session_id)
            assert self._page is not None
            action = str(payload.get("action") or "").strip().casefold()
            if action == "click":
                x = max(0.0, min(float(payload.get("x") or 0), SCREENSHOT_WIDTH))
                y = max(0.0, min(float(payload.get("y") or 0), SCREENSHOT_HEIGHT))
                await self._page.mouse.click(x, y)
            elif action == "type":
                text = str(payload.get("text") or "")[:2000]
                if not text:
                    raise ValueError("text is required")
                await self._page.keyboard.type(text, delay=20)
            elif action == "press":
                key = str(payload.get("key") or "")
                if key not in {"Enter", "Tab", "Escape", "Backspace", "ArrowUp", "ArrowDown"}:
                    raise ValueError("Unsupported browser key.")
                await self._page.keyboard.press(key)
            elif action == "scroll":
                dy = max(-1800.0, min(float(payload.get("dy") or 0), 1800.0))
                # A real mouse wheel scrolls whatever is under the cursor, not
                # the page as a whole. A popup (a year/make/model picker, for
                # example) commonly renders in a different spot than the
                # field that opened it; without moving there first, the wheel
                # event lands wherever an earlier, unrelated click left the
                # cursor and the popup never scrolls.
                if "x" in payload and "y" in payload:
                    x = max(0.0, min(float(payload.get("x") or 0), SCREENSHOT_WIDTH))
                    y = max(0.0, min(float(payload.get("y") or 0), SCREENSHOT_HEIGHT))
                    await self._page.mouse.move(x, y)
                await self._page.mouse.wheel(0, dy)
            elif action == "refresh":
                await self._page.reload(wait_until="domcontentloaded", timeout=45_000)
            else:
                raise ValueError("Unsupported browser interaction.")
            await asyncio.sleep(0.15)
            return await self.status()

    async def operator_action(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "status").strip().casefold()
        if action in {"setup", "status"}:
            result = await self.status()
            result.update({
                "action": action,
                "setup_in_chat": True,
                "credential_secret_in_model_context": False,
            })
            return result
        if action == "start":
            result = await self.start(auto_login=True)
            result["action"] = action
            return result
        if action == "snapshot":
            await self.start(auto_login=False)
            assert self._page is not None
            text = await self._page.locator("body").inner_text(timeout=10_000)
            return {
                **(await self.status()),
                "action": action,
                "page_text": text[:MAX_TEXT_CHARS],
            }
        if action == "goto":
            url = str(args.get("url") or "").strip()
            if not _is_alldata_url(url):
                raise ValueError("Licensed-browser navigation is confined to ALLDATA domains.")
            await self.start(auto_login=True)
            assert self._page is not None
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return {**(await self.status()), "action": action}
        if action == "click_text":
            text = str(args.get("text") or "").strip()[:300]
            if not text:
                raise ValueError("text is required")
            await self.start(auto_login=True)
            assert self._page is not None
            locator = self._page.get_by_text(text, exact=False).first
            await locator.click(timeout=10_000)
            return {**(await self.status()), "action": action}
        if action == "fill":
            # Deliberately cannot fill secrets. Credentials are only handled by
            # _try_saved_login; model-authored arguments never carry passwords.
            selector = str(args.get("selector") or "").strip()[:500]
            text = str(args.get("text") or "")[:2000]
            if not selector or not text:
                raise ValueError("selector and text are required")
            if "password" in selector.casefold():
                raise ValueError("Password fields cannot be filled through model tools.")
            await self.start(auto_login=True)
            assert self._page is not None
            await self._page.locator(selector).first.fill(text, timeout=10_000)
            return {**(await self.status()), "action": action}
        if action == "press":
            key = str(args.get("key") or "Enter")
            if key not in {"Enter", "Tab", "Escape", "ArrowUp", "ArrowDown"}:
                raise ValueError("Unsupported browser key.")
            await self.start(auto_login=True)
            assert self._page is not None
            await self._page.keyboard.press(key)
            return {**(await self.status()), "action": action}
        if action == "extract":
            await self.start(auto_login=True)
            assert self._page is not None
            text = await self._page.locator("body").inner_text(timeout=10_000)
            return {
                **(await self.status()),
                "action": action,
                "page_text": text[:MAX_TEXT_CHARS],
                "provenance": {
                    "provider": PROVIDER_LABEL,
                    "url": self._page.url[:MAX_URL_CHARS],
                    "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "licensed_session": True,
                },
            }
        if action == "alldata_vehicle_research":
            from ..config import Settings
            from . import research_alldata_navigation as navigation
            from . import research_navigator_agent

            vehicle_label = " ".join(str(args.get("vehicle") or "").split()).strip()
            parsed_vehicle = (
                navigation.vehicle_from_query(vehicle_label) if vehicle_label else {}
            )
            target = {
                "year": args.get("vehicle_year") or parsed_vehicle.get("year"),
                "make": args.get("vehicle_make") or parsed_vehicle.get("make"),
                "model": args.get("vehicle_model") or parsed_vehicle.get("model_trim"),
                "trim": args.get("vehicle_trim"),
            }
            if not (target["year"] and target["make"] and target["model"]):
                raise ValueError(
                    "ALLDATA vehicle research requires exact year, make, and model."
                )
            topic = " ".join(str(args.get("topic") or "").split()).strip()
            if not topic:
                raise ValueError("ALLDATA vehicle research requires a procedure topic.")
            client = research_navigator_agent.current_model_client()
            if client is None:
                return {
                    "status": "model_context_unavailable",
                    "action": action,
                    "attempted": False,
                    "searched": False,
                    "verified": False,
                    "captured": False,
                    "message": (
                        "The agentic ALLDATA Navigator requires the active X model "
                        "execution context; the retired scripted fallback was not run."
                    ),
                }
            result = await research_navigator_agent.run_navigator_search(
                client=client,
                settings=Settings.load(),
                provider="alldata",
                target=target,
                topic=topic,
            )
            result["action"] = action
            result["status"] = (
                "success"
                if result.get("verified") is True and result.get("captured") is True
                else "unverified"
            )
            result["success"] = bool(
                result.get("verified") is True and result.get("captured") is True
            )
            return result
        if action == "capture_to_adas":
            return await self._capture_to_adas(args)
        if action == "public_search":
            source_depth = str(args.get("source_depth") or "standard").strip().casefold()
            if source_depth not in {
                "standard",
                "calibration_requirements",
                "repair_policy",
            }:
                raise ValueError(
                    "source_depth must be standard, calibration_requirements, or repair_policy"
                )
            if source_depth == "standard":
                return await public_search(args)
            from . import research_policy_depth

            result = await research_policy_depth.deep_search_public_oem(
                str(args.get("query") or ""),
                str(args.get("manufacturer") or "").strip() or None,
                source_depth=source_depth,
            )
            result["status"] = "success" if result.get("verified") else "no_result"
            result["action"] = action
            result["source_depth"] = source_depth
            return result
        if action == "public_read":
            return await public_read(args)
        raise ValueError(f"Unsupported collision research action: {action}")

    async def _capture_to_adas(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.adas is None or not self.adas.available():
            raise ValueError("ADAS SI is unavailable; cannot preserve the research source.")
        await self.start(auto_login=True)
        assert self._page is not None
        if not _is_alldata_url(self._page.url):
            raise ValueError("Only an active ALLDATA page can be captured through this action.")

        # Acquisition into ADAS SI is the highest-consequence step in this whole
        # pipeline -- it becomes the answer for every future query about this
        # vehicle. Whatever upstream navigation path produced this page
        # (deterministic search or the model-driven agent loop), independently
        # re-confirm the claimed vehicle here, against the live page, through the
        # same bounded signal research_verification relies on elsewhere. Never
        # trust the caller-supplied vehicle label on its own.
        vehicle_label_arg = str(args.get("vehicle") or "").strip()
        if vehicle_label_arg:
            from . import research_alldata_navigation as nav

            parsed_vehicle = nav.vehicle_from_query(vehicle_label_arg)
            if parsed_vehicle.get("year") and parsed_vehicle.get("make"):
                current_label = await nav._current_vehicle_label(self._page)  # noqa: SLF001
                if not await nav._confirms_identity(current_label, parsed_vehicle):  # noqa: SLF001
                    raise ValueError(
                        f"Refusing to preserve this page as '{vehicle_label_arg}': the current "
                        "ALLDATA page does not confirm that vehicle through a bounded selection "
                        "signal. Select the exact vehicle before capturing."
                    )

        from . import adas_storage
        from . import research_alldata_navigation as nav

        parsed_storage_vehicle = nav.vehicle_from_query(
            str(args.get("vehicle") or "")
        )
        storage_vehicle = adas_storage.normalize_vehicle_identity(
            {
                "year": args.get("vehicle_year") or parsed_storage_vehicle.get("year"),
                "make": args.get("vehicle_make") or parsed_storage_vehicle.get("make"),
                "model": args.get("vehicle_model") or parsed_storage_vehicle.get("model_trim"),
            }
        )
        if storage_vehicle is None:
            raise ValueError(
                "Saving ALLDATA material into ADAS SI requires exact year, make, and model."
            )
        vehicle = _safe_filename(args.get("vehicle") or "Vehicle")
        topic = _safe_filename(args.get("topic") or self._page.title() or "Research")
        folder = adas_storage.service_information_directory(
            self.adas.source_root,
            storage_vehicle,
            "ALLDATA",
        )
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        base = _safe_filename(f"{vehicle} {topic} ALLDATA {stamp}")
        pdf_path = folder / f"{base}.pdf"
        sidecar = folder / f"{base}.source.json"
        pdf_bytes = await self._page.pdf(
            format="Letter", print_background=True, prefer_css_page_size=True
        )
        pdf_path.write_bytes(pdf_bytes)
        provenance = {
            "provider": PROVIDER_LABEL,
            "url": self._page.url[:MAX_URL_CHARS],
            "title": (await self._page.title())[:300],
            "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "licensed_access": True,
            "targeted_research": True,
            "storage_policy": "year/make/model",
            "vehicle": storage_vehicle,
            "original_web_source_retained_as": "print_snapshot_pdf",
            "credential_secret_stored_in_document": False,
        }
        sidecar.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
        # Make the new source immediately searchable; OCR/native extraction is
        # automatically applied by the installed ADAS SI page pipeline.
        self.adas.inventory._cache = None
        pages = self.adas._pages(pdf_path)
        return {
            "status": "success",
            "action": "capture_to_adas",
            "saved": True,
            "relative_path": self.adas.relative_of(pdf_path),
            "source_sidecar": self.adas.relative_of(sidecar),
            "pages": len(pages),
            "readable_pages": sum(1 for _number, text in pages if str(text or "").strip()),
            "provenance": provenance,
        }


_BROWSER: LicensedBrowser | None = None
_BROWSER_LOCK = threading.Lock()


def get_browser(root: Path, adas: Any | None = None) -> LicensedBrowser:
    global _BROWSER
    with _BROWSER_LOCK:
        if _BROWSER is None:
            _BROWSER = LicensedBrowser(root, adas=adas)
        elif adas is not None and _BROWSER.adas is None:
            _BROWSER.adas = adas
        return _BROWSER


# ---------------------------------------------------------------------------
# Public OEM/manufacturer research fallback.
# ---------------------------------------------------------------------------


def _public_host(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A public http/https URL is required.")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in research URLs.")
    host = parsed.hostname.casefold().rstrip(".")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise ValueError("Research source hostname could not be resolved.") from exc
    for record in addresses:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise ValueError("Research source resolved to a private or non-public address.")
    return host


async def public_search(args: dict[str, Any]) -> dict[str, Any]:
    from . import research as research_svc

    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    manufacturer = str(args.get("manufacturer") or "").strip()
    # Collision/position-statement vocabulary raises official collision portals
    # above generic consumer results without pretending every answer is in SI.
    enriched = " ".join(
        part for part in (
            manufacturer,
            query,
            "OEM collision repair position statement service bulletin technical information",
        ) if part
    )[:400]
    result = await research_svc.search_current({"query": enriched, "max_results": 8})
    result["action"] = "public_search"
    result["research_scope"] = "post_collision_oem_public_sources"
    result["authority_note"] = (
        "Separate OEM requirements, insurer requirements, and legal/regulatory requirements; "
        "do not treat one as another."
    )
    return result


async def public_read(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    if len(url) > MAX_URL_CHARS:
        raise ValueError("url is too long")
    _public_host(url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; X-Omni/1.0; post-collision-research)",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            target = response.headers.get("location", "")
            if not target:
                raise ValueError("Public source redirected without a target.")
            from urllib.parse import urljoin
            target = urljoin(url, target)
            _public_host(target)
            response = await client.get(target, headers={"User-Agent": "Mozilla/5.0 (compatible; X-Omni/1.0)"})
            url = target
        response.raise_for_status()
        raw = response.content[:MAX_PUBLIC_BYTES]
    content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
    if content_type == "application/pdf" or raw.startswith(b"%PDF"):
        return {
            "status": "success",
            "action": "public_read",
            "url": url,
            "content_type": "application/pdf",
            "bytes": len(raw),
            "message": "The source is a PDF. Preserve/import it before relying on exact page claims.",
        }
    text = raw.decode(response.encoding or "utf-8", errors="replace")
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    title = html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))).strip() if title_match else ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(html.unescape(text).split())
    return {
        "status": "success",
        "action": "public_read",
        "url": url,
        "title": title[:300],
        "content_type": content_type or "text/html",
        "page_text": text[:MAX_TEXT_CHARS],
        "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source_type": "public_oem_or_web_source",
    }


# ---------------------------------------------------------------------------
# Same-origin mobile credential/browser UI routes.
# ---------------------------------------------------------------------------


class CredentialRequest(BaseModel):
    username: str
    password: SecretStr


class BrowserActionRequest(BaseModel):
    action: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None
    dy: float | None = None


def _require_owner(session: dict) -> None:
    if str(session.get("role") or "owner") != "owner":
        raise HTTPException(403, "Owner authorization is required for licensed research access.")


def _require_origin(request: Request, settings: Any) -> None:
    origin = str(request.headers.get("origin") or "").strip().rstrip("/")
    allowed = {
        str(getattr(settings, "local_origin", "") or "").rstrip("/"),
        str(getattr(settings, "public_origin", "") or "").rstrip("/"),
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
    allowed.discard("")
    if origin not in allowed:
        raise HTTPException(403, "Licensed research changes require the exact X Omni origin.")


def _setup_html() -> str:
    # This is a fallback full-page UI. The normal path is the inline chat card,
    # but keeping the same endpoints usable directly is useful on tiny phones.
    return r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ALLDATA Research Access</title><style>
body{font-family:system-ui;background:#0d1117;color:#e6edf3;margin:0;padding:18px}main{max-width:760px;margin:auto}.box{background:#151b23;border:1px solid #30363d;border-radius:14px;padding:16px;margin-bottom:14px}input,button{box-sizing:border-box;width:100%;font:inherit;padding:12px;border-radius:9px;border:1px solid #3d444d;margin-top:8px}input{background:#0d1117;color:#e6edf3}button{background:#238636;color:white;font-weight:700}img{width:100%;border-radius:9px;border:1px solid #30363d;margin-top:10px;touch-action:manipulation}.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.note{color:#9da7b3;font-size:.9rem}#msg{min-height:1.4em}</style></head><body><main><h2>ALLDATA Research Access</h2><div class="box"><p class="note">Credentials go directly to X Omni Core and Windows Credential Manager. They are not stored in chat or sent to the model.</p><form id="cred"><input id="user" autocomplete="username" placeholder="ALLDATA username" required><input id="pass" type="password" autocomplete="current-password" placeholder="ALLDATA password" required><button>Save credentials</button></form><p id="msg"></p><button id="start" type="button">Start / resume ALLDATA browser</button></div><div id="remote" class="box" hidden><p class="note">Tap the browser image to click. Use the text box for MFA or other human-only input, then X can resume.</p><img id="shot" alt="ALLDATA browser"><input id="type" autocomplete="one-time-code" placeholder="Type into focused browser field"><div class="row"><button id="send" type="button">Type text</button><button id="enter" type="button">Enter</button><button id="tab" type="button">Tab</button><button id="down" type="button">Scroll down</button></div></div><script>
let sid=null, timer=null;const j=async(u,o={})=>{const r=await fetch(u,{credentials:'include',cache:'no-store',...o});const p=await r.json().catch(()=>({}));if(!r.ok)throw Error(typeof p.detail==='string'?p.detail:'Request failed');return p};
const refresh=()=>{if(!sid)return;document.querySelector('#shot').src=`/api/research/providers/alldata/sessions/${sid}/screenshot?t=${Date.now()}`};
document.querySelector('#cred').onsubmit=async e=>{e.preventDefault();const pass=document.querySelector('#pass');let password=pass.value;pass.value='';try{const p=await j('/api/research/providers/alldata/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.querySelector('#user').value,password})});document.querySelector('#msg').textContent=p.configured?'Credentials saved securely.':'Credentials were not saved.'}catch(x){document.querySelector('#msg').textContent=x.message}finally{password=''}};
document.querySelector('#start').onclick=async()=>{try{const p=await j('/api/research/providers/alldata/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});sid=p.session_id;document.querySelector('#remote').hidden=false;refresh();clearInterval(timer);timer=setInterval(refresh,1800)}catch(x){document.querySelector('#msg').textContent=x.message}};
const act=async payload=>{if(!sid)return;await j(`/api/research/providers/alldata/sessions/${sid}/action`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});refresh()};
document.querySelector('#shot').onclick=e=>{const r=e.currentTarget.getBoundingClientRect();act({action:'click',x:(e.clientX-r.left)/r.width*1280,y:(e.clientY-r.top)/r.height*900})};
document.querySelector('#send').onclick=()=>{const el=document.querySelector('#type');const text=el.value;el.value='';if(text)act({action:'type',text})};document.querySelector('#enter').onclick=()=>act({action:'press',key:'Enter'});document.querySelector('#tab').onclick=()=>act({action:'press',key:'Tab'});document.querySelector('#down').onclick=()=>act({action:'scroll',dy:700});
</script></main></body></html>"""


def install_http_routes(router: Any, settings: Any, require_session: Any, *, adas: Any | None = None) -> None:
    browser = get_browser(Path(settings.root), adas=adas)

    @router.get(f"/research/providers/{PROVIDER}/setup", response_class=HTMLResponse)
    async def alldata_setup_page(session: dict = Depends(require_session)):
        _require_owner(session)
        return HTMLResponse(_setup_html(), headers={"Cache-Control": "no-store"})

    @router.get(f"/research/providers/{PROVIDER}/status")
    async def alldata_status(session: dict = Depends(require_session)):
        _require_owner(session)
        return await browser.status()

    @router.post(f"/research/providers/{PROVIDER}/credentials")
    async def alldata_credentials(
        body: CredentialRequest,
        request: Request,
        session: dict = Depends(require_session),
    ):
        _require_owner(session)
        _require_origin(request, settings)
        try:
            return VAULT.write(body.username, body.password.get_secret_value())
        except (ValueError, CredentialVaultError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete(f"/research/providers/{PROVIDER}/credentials")
    async def alldata_credentials_delete(request: Request, session: dict = Depends(require_session)):
        _require_owner(session)
        _require_origin(request, settings)
        try:
            deleted = VAULT.delete()
        except CredentialVaultError as exc:
            raise HTTPException(400, str(exc)) from exc
        await browser.close()
        return {"deleted": deleted, **VAULT.status()}

    @router.post(f"/research/providers/{PROVIDER}/sessions")
    async def alldata_session_start(request: Request, session: dict = Depends(require_session)):
        _require_owner(session)
        _require_origin(request, settings)
        try:
            return await browser.start(auto_login=True)
        except (BrowserUnavailable, CredentialVaultError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get(f"/research/providers/{PROVIDER}/sessions/{{session_id}}/screenshot")
    async def alldata_session_screenshot(session_id: str, session: dict = Depends(require_session)):
        _require_owner(session)
        try:
            data = await browser.screenshot(session_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.post(f"/research/providers/{PROVIDER}/sessions/{{session_id}}/action")
    async def alldata_session_action(
        session_id: str,
        body: BrowserActionRequest,
        request: Request,
        session: dict = Depends(require_session),
    ):
        _require_owner(session)
        _require_origin(request, settings)
        try:
            return await browser.human_action(session_id, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete(f"/research/providers/{PROVIDER}/sessions/{{session_id}}")
    async def alldata_session_stop(
        session_id: str,
        request: Request,
        session: dict = Depends(require_session),
    ):
        _require_owner(session)
        _require_origin(request, settings)
        browser._require_session(session_id)  # noqa: SLF001 - route enforces current session identity
        await browser.close()
        return {"closed": True, "provider": PROVIDER}


# ---------------------------------------------------------------------------
# Tool and framework patch installation. Keeps the feature isolated from the
# existing operator/query code while still appearing as a normal first-class
# X capability.
# ---------------------------------------------------------------------------

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        registry_mod.TOOL_SCHEMAS.setdefault(
            "collision_research",
            {
                "description": (
                    "Acquire post-collision evidence from the authorized licensed ALLDATA "
                    "session or bounded public OEM sources. Vehicle-specific ALLDATA work "
                    "requires an exact vehicle label or structured year/make/model, plus a "
                    "topic and result-proven selection "
                    "before evidence is attributed to that vehicle. Browser actions never "
                    "carry credentials; capture_to_adas preserves a targeted source with "
                    "provenance. Never bypass access controls or CAPTCHA."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "setup", "status", "start", "snapshot", "goto", "click_text",
                                "fill", "press", "extract", "capture_to_adas", "public_search",
                                "public_read", "alldata_vehicle_research",
                            ],
                        },
                        "query": {"type": "string"},
                        "manufacturer": {"type": "string"},
                        "url": {"type": "string"},
                        "text": {"type": "string"},
                        "selector": {"type": "string"},
                        "key": {"type": "string"},
                        "vehicle": {"type": "string"},
                        "vehicle_year": {"type": "integer", "minimum": 1900, "maximum": 2199},
                        "vehicle_make": {"type": "string"},
                        "vehicle_model": {"type": "string"},
                        "vehicle_trim": {"type": "string"},
                        "topic": {"type": "string"},
                        "source_depth": {
                            "type": "string",
                            "enum": [
                                "standard",
                                "calibration_requirements",
                                "repair_policy",
                            ],
                            "description": (
                                "For public_search only: explicitly request the bounded full-PDF "
                                "and one-hop same-host reader for buried calibration requirements "
                                "or collision repair policy. Default standard."
                            ),
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        )

        original_init = registry_mod.Registry.__init__
        if not getattr(original_init, "_xomni_collision_research", False):
            def registry_init(self, *args, **kwargs):
                original_init(self, *args, **kwargs)
                self.policy.setdefault(
                    "collision_research",
                    {
                        "tier": "operator_authorized",
                        "description": (
                            "Targeted licensed ALLDATA and public OEM post-collision research. "
                            "Credentials remain outside model context."
                        ),
                    },
                )
                async def handler(tool_args: dict[str, Any]):
                    from ..config import Settings
                    from . import adas_si as adas_si_mod
                    settings = Settings.load()
                    adas = adas_si_mod.AdasSI(
                        settings.adas_si_root,
                        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
                    )
                    browser = get_browser(settings.root, adas=adas)
                    return await browser.operator_action(tool_args)
                self.register("collision_research", handler)

            registry_init._xomni_collision_research = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        try:
            from ..orchestrator import loop as loop_mod
            loop_mod.ARTIFACT_FOR_TOOL["collision_research"] = "research_provider"
        except Exception:  # noqa: BLE001 - loop can be imported later in unit tests
            pass

        # Patch the existing router factory. main.py already imports core.api.routes
        # before service package wiring, and calls create_router only after services
        # are imported, so the wrapped factory is active in production.
        try:
            from ..api import routes as routes_mod
            original_create_router = routes_mod.create_router
            if not getattr(original_create_router, "_xomni_collision_research", False):
                def create_router(*args, **kwargs):
                    built = original_create_router(*args, **kwargs)
                    settings = args[0] if args else kwargs.get("settings")
                    require_session = args[4] if len(args) > 4 else kwargs.get("require_session")
                    adas = kwargs.get("adas")
                    if settings is not None and require_session is not None:
                        install_http_routes(built, settings, require_session, adas=adas)
                    return built
                create_router._xomni_collision_research = True  # type: ignore[attr-defined]
                routes_mod.create_router = create_router
        except Exception:  # noqa: BLE001
            log.exception("Could not install collision research HTTP routes")

        _INSTALLED = True
