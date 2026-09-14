r"""Verify Calibration IQ VIN identity before spending an ALLDATA/model run.

This is deliberately not a vehicle fallback. It starts/verifies Calibration IQ,
proves the authenticated repair-order data plane, then reads each selected RO and
prints the exact 17-character VIN X will use. No browser or model is invoked.

Example::

    .venv\Scripts\python.exe scripts\si_vin_preflight.py ^
        --cases scripts\si_research_cases.json ^
        --only kia-k4-front-radar --only toyota-camry-bsm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()

    from core.config import Settings
    from core.services import adas_si_research, calibration_iq

    settings = Settings.load()
    cases: list[dict[str, Any]] = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only)
        cases = [case for case in cases if case.get("id") in wanted]
    if not cases:
        print("No matching cases were selected.")
        return 2

    startup = await calibration_iq.start_native(settings)
    if startup.get("verified") is not True:
        print(
            "Calibration IQ startup/health failed: "
            + str(startup.get("message") or startup.get("detail") or startup.get("status"))
        )
        return 2

    health = await calibration_iq.health(settings)
    data_plane = health.get("data_plane") if isinstance(health.get("data_plane"), dict) else {}
    print(
        "Calibration IQ: "
        f"status={health.get('status')} data_plane={data_plane.get('status')} "
        f"count={data_plane.get('count')}"
    )
    if health.get("status") != "available" or data_plane.get("verified") is not True:
        print(
            health.get("message")
            or "Calibration IQ repair-order data plane was not verified; VIN audit stopped."
        )
        return 2

    failures = 0
    for case in cases:
        case_id = str(case.get("id") or "unnamed")
        ro = str(case.get("ro") or "").strip()
        if not ro:
            failures += 1
            print(f"{case_id}: FAIL no repair order in case definition")
            continue

        try:
            read = await calibration_iq.get_repair_order(
                settings, {"repair_order_id": ro}
            )
        except Exception as exc:  # noqa: BLE001 - audit must name the blocker
            failures += 1
            print(f"{case_id}: FAIL {type(exc).__name__}: {exc}")
            continue

        status = str(read.get("status") or "") if isinstance(read, dict) else "invalid_response"
        vin = adas_si_research.vin_from_read(read) if isinstance(read, dict) else ""
        if vin:
            print(f"{case_id}: PASS RO {ro} VIN {vin}")
            continue

        failures += 1
        message = str(read.get("message") or "") if isinstance(read, dict) else "invalid response"
        if status == "verified":
            print(f"{case_id}: FAIL RO {ro} exists but carries no valid 17-character VIN")
        else:
            print(f"{case_id}: FAIL RO {ro} status={status} {message}".rstrip())

    print(f"VIN preflight: {len(cases) - failures}/{len(cases)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
