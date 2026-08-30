"""Generate MediaMTX's camera-path config from X Omni's own camera credentials.

MediaMTX (X:\\MediaMTX) is an independently-managed process outside this
repo. Its mediamtx.yml cannot safely hold the camera's RTSP credentials in
a git-tracked file, and X Omni already owns exactly that material --
DPAPI-protected, via exterior_camera.py. This module is the one bridge
between them: it discovers the camera's ONVIF profiles and resolves their
RTSP source URLs the same way ExteriorCameraService does for live view,
then writes only the resulting `paths:` block into mediamtx.yml, leaving
every server setting above it untouched.

A resolved stream URL is never logged, printed, or returned to a caller --
this camera's firmware embeds the username and password directly in the
RTSP path itself (not the standard user:pass@host form), so even "safe"
substrings of it are not safe. Only bounded, non-secret metadata (profile
name, codec, resolution) is exposed for confirmation/reporting.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from . import exterior_camera as exterior_camera_svc

log = logging.getLogger("xomni.mediamtx_config")

PATH_MAIN = "exterior"
PATH_LIVE = "exterior_sub"
_PATHS_MARKER = "\npaths:"
_STATIC_VIDEO_ENCODINGS = {"JPEG", "MJPEG"}
_H264_ENCODINGS = {"H264", "AVC"}


class MediaMTXConfigError(RuntimeError):
    """Raised when the camera's paths cannot be safely discovered or written."""


@dataclass(frozen=True)
class CameraPathPlan:
    path_name: str
    rtsp_source_url: str = field(repr=False)
    profile_name: str
    encoding: str
    width: int
    height: int
    record: bool = True


async def discover_camera_path_plans(
    exterior_camera: exterior_camera_svc.ExteriorCameraService,
) -> list[CameraPathPlan]:
    """Discover the camera's profiles and resolve MAIN + LIVE stream URLs.

    MAIN ("exterior") is the highest-resolution advertised video profile --
    the primary archive. This camera's ONVIF metadata claims it is H.264
    but the real bitstream is HEVC (a known quirk, already handled the same
    way by the old DVR's bitstream probing); it is not used for browser
    playback.

    LIVE ("exterior_sub") is the smallest advertised profile that is
    genuinely H.264-encoded, recorded in parallel so live view and
    historical scrubbing both play natively in a browser with zero
    transcoding. If no H.264 profile exists at all, only MAIN is returned
    and browser playback will need a different resolution later.
    """
    credentials = exterior_camera._load_credentials()
    timeout = httpx.Timeout(exterior_camera.onvif_timeout_seconds)
    async with httpx.AsyncClient(
        transport=exterior_camera._onvif_transport,
        timeout=timeout, follow_redirects=False, trust_env=False,
    ) as client:
        try:
            profiles_body = await exterior_camera._post_onvif(
                client, credentials=credentials, operation="GetProfiles"
            )
            profiles = exterior_camera._profiles_from_response(profiles_body)
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise MediaMTXConfigError(f"Could not discover camera profiles: {exc}") from exc

        video_profiles = [p for p in profiles if p.encoding.replace(".", "") not in _STATIC_VIDEO_ENCODINGS]
        if not video_profiles:
            raise MediaMTXConfigError("The camera did not advertise any video (non-snapshot) profile.")

        main_profile = max(video_profiles, key=lambda p: p.width * p.height)
        h264_profiles = [p for p in video_profiles if p.encoding.replace(".", "") in _H264_ENCODINGS]
        live_profile = min(h264_profiles, key=lambda p: p.width * p.height) if h264_profiles else None
        if live_profile is not None and live_profile.token == main_profile.token:
            live_profile = None

        async def resolve(profile) -> str:
            def stream_setup(operation: ET.Element) -> None:
                media_ns = "http://www.onvif.org/ver10/media/wsdl"
                schema_ns = "http://www.onvif.org/ver10/schema"
                setup = ET.SubElement(operation, f"{{{media_ns}}}StreamSetup")
                ET.SubElement(setup, f"{{{schema_ns}}}Stream").text = "RTP-Unicast"
                transport = ET.SubElement(setup, f"{{{schema_ns}}}Transport")
                ET.SubElement(transport, f"{{{schema_ns}}}Protocol").text = "RTSP"
                ET.SubElement(operation, f"{{{media_ns}}}ProfileToken").text = profile.token

            uri_body = await exterior_camera._post_onvif(
                client, credentials=credentials, operation="GetStreamUri", body_builder=stream_setup,
            )
            return exterior_camera._stream_uri_from_response(uri_body, host=credentials.host)

        try:
            main_url = await resolve(main_profile)
            live_url = await resolve(live_profile) if live_profile is not None else None
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise MediaMTXConfigError(f"Could not resolve a camera stream URL: {exc}") from exc

    plans = [
        CameraPathPlan(
            path_name=PATH_MAIN, rtsp_source_url=main_url, profile_name=main_profile.name,
            encoding=main_profile.encoding, width=main_profile.width, height=main_profile.height,
        )
    ]
    if live_profile is not None and live_url is not None:
        plans.append(
            CameraPathPlan(
                path_name=PATH_LIVE, rtsp_source_url=live_url, profile_name=live_profile.name,
                encoding=live_profile.encoding, width=live_profile.width, height=live_profile.height,
            )
        )
    return plans


def _yaml_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_paths_block(plans: list[CameraPathPlan]) -> str:
    lines = ["paths:", ""]
    for plan in plans:
        lines.append(f"  {plan.path_name}:")
        lines.append(f"    source: {_yaml_single_quoted(plan.rtsp_source_url)}")
        lines.append("    sourceOnDemand: false")
        lines.append("    rtspTransport: tcp")
        lines.append(f"    record: {'yes' if plan.record else 'no'}")
        lines.append("")
    lines.append("  all_others:")
    lines.append("    record: no")
    lines.append("")
    return "\n".join(lines)


def update_mediamtx_yaml(yaml_path: Path, plans: list[CameraPathPlan]) -> None:
    """Replace only the `paths:` block of mediamtx.yml, atomically.

    Every server setting above `paths:` (listeners, recording defaults,
    logging) is preserved verbatim -- this only ever rewrites the camera
    path definitions, never anything a human configured by hand above them.
    """
    if not plans:
        raise MediaMTXConfigError("No camera path plans to write.")
    existing = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
    marker_index = existing.find(_PATHS_MARKER)
    header = existing[: marker_index + 1] if marker_index >= 0 else existing.rstrip("\n") + "\n\n"
    new_content = header + render_paths_block(plans)

    temp_path = yaml_path.with_name(f".{yaml_path.name}.{secrets.token_hex(8)}.tmp")
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(new_content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, yaml_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    log.info(
        "MediaMTX camera paths configured: %s",
        ", ".join(f"{p.path_name} ({p.encoding} {p.width}x{p.height})" for p in plans),
    )
