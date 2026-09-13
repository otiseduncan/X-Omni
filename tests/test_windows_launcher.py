import base64
import os
import re
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def test_double_click_launcher_uses_bounded_windows_launcher() -> None:
    command = (ROOT / "Launch-X-Omni.cmd").read_text(encoding="utf-8")
    assert "scripts\\launch-x-omni.ps1" in command
    assert "-WindowStyle Hidden" in command
    assert "-ExecutionPolicy Bypass" in command


def test_start_script_stamps_runtime_revision_and_health_exposes_it() -> None:
    start = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
    main = (ROOT / "core" / "main.py").read_text(encoding="utf-8")
    assert "XOMNI_SOURCE_REVISION" in start
    assert "git -C $root rev-parse HEAD" in start
    assert '"source_revision": os.environ.get("XOMNI_SOURCE_REVISION") or None' in main


def test_local_deploy_script_pulls_builds_restarts_and_verifies_all_field_services() -> None:
    script = (ROOT / "scripts" / "deploy-local.ps1").read_text(encoding="utf-8")
    assert "git -C $Path fetch origin main" in script
    assert "git -C $Path merge --no-edit origin/main" in script
    assert "git -C $Path merge --abort" in script
    assert "diff --name-only --diff-filter=U" in script
    assert "leftover Git conflict markers" in script
    assert "pull --ff-only" not in script
    assert "scripts\\install.ps1" in script
    assert "Start-Native.ps1" in script
    assert "-Profile Production -NoBrowser" in script
    assert "scripts\\setup.ps1" in script
    assert "scripts\\launch-x-omni.ps1" in script
    assert "scrapex.start_native" in script
    assert "source_revision" in script
    assert "runtime_revision" in script
    assert "LOCAL DEPLOYMENT VERIFIED" in script


def test_x_omni_launcher_source_is_single_clean_script() -> None:
    script = (ROOT / "scripts" / "launch-x-omni.ps1").read_text(encoding="utf-8")

    # The failure worth guarding is a bad merge leaving duplicated function
    # bodies. Check that directly -- every function defined exactly once --
    # instead of inferring it from a byte count, which flags honest growth and
    # would miss a duplicate that arrives alongside a deletion.
    defined = re.findall(r"^function\s+([\w-]+)\s*\{", script, re.MULTILINE)
    duplicated = sorted({name for name in defined if defined.count(name) > 1})
    assert not duplicated, f"duplicated function bodies: {duplicated}"

    # A real cap still applies so the script cannot sprawl unnoticed. Nudged
    # from 18,000 only after auditing the overage and removing what should not
    # have been here: a second inline copy of the Test-XOmniCoreProcess
    # predicate in the straggler sweep, and the source-wide conflict-marker
    # grep that belongs to deploy-local (asserted there). What remains is
    # named features and strict process-identity verification, which must not
    # be compressed away to satisfy a byte count.
    # 18,500 -> 18,700 on 2026-09-12 for two launch failures found the same
    # day, both already trimmed to their shortest honest form: a git warning
    # aborting the launch because ErrorActionPreference Stop treats native
    # stderr as terminating, and a held mutex returning exit 0 in silence so
    # every later launch did nothing while reporting success. Neither is a
    # feature; both are the script failing to start the app.
    assert len(script) < 18_700
    assert script.count("function Get-SourceRevision") == 1
    assert script.count("function Get-PortOwner") == 1
    assert "^[0-9a-fA-F]{40}$" in script
    assert "source revision=$revisionLabel" in script
    assert "Invoke-SetupRepair" in script
    assert "$runtimeMissing -or $interfaceMissing" in script
    assert "Automatic X Omni setup repair completed successfully" in script
    assert "built interface is missing. Run setup before launching." not in script
    assert "Assert-NoMergeConflicts" in script
    assert "diff --name-only --diff-filter=U" in script
    assert "leftover Git conflict markers" not in script


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="Windows PowerShell parser is only available on Windows",
)
def test_x_omni_launcher_parses_in_windows_powershell() -> None:
    import subprocess

    script_path = str(ROOT / "scripts" / "launch-x-omni.ps1").replace("'", "''")
    command = (
        "$tokens=$null; $errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-Command", command],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_setup_checks_native_exit_codes_and_verifies_built_interface() -> None:
    script = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    assert "function Assert-NativeSuccess" in script
    assert "UI dependency installation" in script
    assert "UI production build" in script
    assert "ui\\dist\\index.html" in script
    assert "UI build reported success" in script


def test_launcher_preserves_foreign_processes_and_gates_browser_on_health() -> None:
    script = (ROOT / "scripts" / "launch-x-omni.ps1").read_text(encoding="utf-8")
    assert "Test-XOmniCoreProcess" in script
    assert "Stop-VerifiedLegacyModel" in script
    assert "Stop-VerifiedLegacyComfyUI" in script
    assert "owned_process -eq $true" in script
    assert "state.process_started_at -is [datetime]" in script
    assert "[string]$state.managed_by -eq 'XV12'" in script
    assert "[math]::Abs(($actualStart - $recordedStart).TotalSeconds) -lt 4" in script
    assert "X Omni will not stop it" in script
    assert "StatusCode -eq 200" in script
    assert "Get-SourceRevision" in script
    assert "Payload.source_revision" in script
    assert "stale source revision" in script
    assert script.index("StatusCode -eq 200") < script.rindex("Open-XOmni -Port $corePort")
    assert '-ExecutionPolicy Bypass -File `"$startScript`"' in script


def test_x_omni_launcher_owns_no_recorder_at_all() -> None:
    # Surveillance recording moved to a Frigate NVR on its own machine. The
    # launcher starts and stops X Omni Core and nothing else: there is no
    # recorder, no media server, and no camera process on this machine for it
    # to start, stop, supervise, or accidentally kill.
    script = (ROOT / "scripts" / "launch-x-omni.ps1").read_text(encoding="utf-8")
    for forbidden in (
        "function Test-XOmniDvrRecorderArguments",
        "function Test-XOmniDvrRecorderProcess",
        "function Stop-VerifiedDvrRecorders",
        "Stop-VerifiedDvrRecorders",
        "dvrRecordingsRoot",
        "launch-mediamtx",
        "mediamtx.exe",
        "dvr_service",
    ):
        assert forbidden not in script, f"launch-x-omni.ps1 must not reference {forbidden!r}"


def test_the_retired_recorder_scripts_are_gone() -> None:
    # Every launcher, watchdog, and startup installer for the retired
    # MediaMTX/X DVR stack is deleted rather than left dormant: a script that
    # still exists is a script someone can still run.
    for retired in (
        "Launch-X-DVR.cmd",
        "scripts/launch-x-dvr.ps1",
        "scripts/launch-mediamtx.ps1",
        "scripts/sync-mediamtx-config.py",
        "scripts/watchdog-mediamtx-dvr.ps1",
        "scripts/install-watchdog-task.ps1",
        "scripts/install-mediamtx-startup.ps1",
        "scripts/install-mediamtx-launcher.ps1",
        "scripts/install-x-dvr-startup.ps1",
    ):
        assert not (ROOT / retired).exists(), f"{retired} should have been removed"


def test_the_cleanup_script_removes_only_the_named_startup_artifacts() -> None:
    script = (ROOT / "scripts" / "remove-mediamtx-dvr-startup.ps1").read_text(encoding="utf-8")
    # It must name exactly the three artifacts the old stack installed.
    assert "X Omni MediaMTX+DVR Watchdog" in script
    assert "MediaMTX.lnk" in script
    assert "X DVR.lnk" in script
    assert "Unregister-ScheduledTask" in script
    # Both Startup folders, because either installer could have written there.
    assert "GetFolderPath('Startup')" in script
    assert "GetFolderPath('CommonStartup')" in script
    # External data is never destroyed by a migration cleanup.
    assert "SupportsShouldProcess" in script
    assert "Remove-Item -LiteralPath $path -Force" in script
    assert "-Recurse" not in script, "the cleanup script must never delete a tree"
    recordings_guard = script.split("legacyMediaMtxRoot")[1:]
    assert recordings_guard, "the script should mention the legacy MediaMTX root"
    assert "Remove-Item -LiteralPath $legacyMediaMtxRoot" not in script


def test_installer_creates_a_real_desktop_shortcut_with_app_icon() -> None:
    installer = (ROOT / "scripts" / "install-windows-launcher.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in installer
    assert "X Omni.lnk" in installer
    assert "x-omni.ico" in installer


def test_a_launch_that_cannot_run_says_so_instead_of_reporting_success():
    """Two ways the launcher failed to start the app while looking fine.

    2026-09-12: a launcher whose error dialog was still open held the
    single-instance mutex, and the next launch hit `if (-not $hasMutex) {
    return }` -- exit 0, no window, no log line, no restart. Three deploys in
    a row appeared to succeed and changed nothing. Separately, git's routine
    "CRLF will be replaced by LF" notice aborted a launch outright, because
    ErrorActionPreference Stop makes any native stderr terminating even when
    it is redirected to $null.
    """

    script = (ROOT / "scripts" / "launch-x-omni.ps1").read_text(encoding="utf-8")

    # The mutex guard must report, not return quietly.
    assert "if (-not $hasMutex) { return }" not in script
    assert "Another X Omni launch is still open" in script

    # The merge check reads git's exit code, not its stderr.
    merge_check = script.split("function Assert-NoMergeConflicts", 1)[1].split("\nfunction ", 1)[0]
    assert "$ErrorActionPreference = 'Continue'" in merge_check
    assert "diff-filter=U" in merge_check
