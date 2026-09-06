import base64
import os
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
    assert len(script) < 18_000
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


def test_x_omni_launcher_no_longer_manages_dvr_recorder() -> None:
    # X DVR (core/dvr_service.py) owns continuous recording as its own
    # independent process now; X Omni Core's launcher must never start,
    # stop, or verify the DVR recorder -- restarting Core must never
    # interrupt recording. This is the regression guard for that boundary.
    script = (ROOT / "scripts" / "launch-x-omni.ps1").read_text(encoding="utf-8")
    for forbidden in (
        "function Test-XOmniDvrRecorderArguments",
        "function Test-XOmniDvrRecorderProcess",
        "function Stop-VerifiedDvrRecorders",
        "Stop-VerifiedDvrRecorders",
        "CommandLineToArgvW",
        "dvrRecordingsRoot",
    ):
        assert forbidden not in script, f"launch-x-omni.ps1 must not reference {forbidden!r}"


def test_mediamtx_launcher_verifies_before_replacing_and_regenerates_paths() -> None:
    # MediaMTX now owns continuous recording as its own independent process
    # (not X DVR's ffmpeg recorder, retired with the custom DVR pipeline);
    # this launcher must still verify before stopping/replacing it, and must
    # always resync camera paths from current credentials before starting.
    script = (ROOT / "scripts" / "launch-mediamtx.ps1").read_text(encoding="utf-8")
    assert "function Test-MediaMTXProcess" in script
    assert "sync-mediamtx-config.py" in script
    assert "already running from an unexpected location" in script
    assert "-m core.main" not in script

    sync_index = script.index("sync-mediamtx-config.py")
    start_index = script.index("Start-Process -FilePath $mediamtxExe")
    assert sync_index < start_index

    # Regression guard for a real bug: Get-Process's Name (no ".exe") and
    # missing ExecutablePath property silently made Test-MediaMTXProcess
    # reject the launcher's own already-running, exactly-correct process
    # every time -- Get-CimInstance Win32_Process must be used consistently
    # wherever a process gets checked against that function.
    for forbidden in ("Get-Process mediamtx", "$existing.Id", "$remaining.Id"):
        assert forbidden not in script, f"launch-mediamtx.ps1 must not reference {forbidden!r}"
    assert script.count("Get-CimInstance Win32_Process -Filter \"Name='mediamtx.exe'\"") >= 2


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="Test-MediaMTXProcess is only invokable on Windows",
)
def test_mediamtx_process_predicate_accepts_the_real_win32_process_shape() -> None:
    # The concrete regression: verify Test-MediaMTXProcess against an object
    # shaped exactly like what Get-CimInstance Win32_Process actually
    # returns (Name carries ".exe", ExecutablePath is a real property) --
    # not a hand-picked shape that happens to satisfy the function.
    import subprocess

    script_path = str(ROOT / "scripts" / "launch-mediamtx.ps1").replace("'", "''")
    powershell = rf"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | ForEach-Object {{ $_.ToString() }} | Out-String) }}
$node = $ast.Find({{
    param($candidate)
    $candidate -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $candidate.Name -eq 'Test-MediaMTXProcess'
}}, $true)
if (-not $node) {{ throw 'Missing launcher function Test-MediaMTXProcess' }}
Invoke-Expression $node.Extent.Text
$mediamtxExe = 'X:\MediaMTX\mediamtx.exe'

# Shaped exactly like a real Get-CimInstance Win32_Process result (Name
# carries ".exe", ExecutablePath is a real property) -- the shape the
# launcher actually feeds this function with today.
$matching = [pscustomobject]@{{
    Name = 'mediamtx.exe'
    ProcessId = 1234
    ExecutablePath = $mediamtxExe
    CommandLine = "`"$mediamtxExe`" `"X:\MediaMTX\mediamtx.yml`""
}}
$mismatchedPath = [pscustomobject]@{{
    Name = 'mediamtx.exe'
    ProcessId = 5678
    ExecutablePath = 'C:\Other\mediamtx.exe'
    CommandLine = 'C:\Other\mediamtx.exe'
}}
# Shaped like a bare Get-Process result: Name has no extension and there is
# no ExecutablePath property at all -- this must never be mistaken for a
# match, since Get-Process's Name/ExecutablePath never look like this.
$bareGetProcessShape = [pscustomobject]@{{
    Name = 'mediamtx'
    Id = 1234
}}
[pscustomobject]@{{
    accepts_the_verified_shape = [bool](Test-MediaMTXProcess -Process $matching)
    rejects_an_unverified_path = -not [bool](Test-MediaMTXProcess -Process $mismatchedPath)
    rejects_the_bare_get_process_shape = -not [bool](Test-MediaMTXProcess -Process $bareGetProcessShape)
}} | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(powershell.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    result = json.loads(completed.stdout)
    assert result == {
        "accepts_the_verified_shape": True,
        "rejects_an_unverified_path": True,
        "rejects_the_bare_get_process_shape": True,
    }


def test_mediamtx_startup_installer_creates_a_hidden_logon_shortcut() -> None:
    startup = (ROOT / "scripts" / "install-mediamtx-startup.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in startup
    assert "MediaMTX.lnk" in startup
    assert "GetFolderPath('Startup')" in startup
    assert "-ExecutionPolicy Bypass" in startup
    assert "-NoOpen" in startup


def test_mediamtx_desktop_installer_creates_a_real_shortcut_with_app_icon() -> None:
    installer = (ROOT / "scripts" / "install-mediamtx-launcher.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in installer
    assert "MediaMTX.lnk" in installer
    assert "x-omni.ico" in installer
    assert "-ExecutionPolicy Bypass" in installer
    assert "Parser]::ParseFile" in installer
    assert "Shortcut was not installed" in installer
    assert "GetFolderPath('Desktop')" in installer
    # Unlike the Startup entry, a double-clicked desktop icon should open
    # MediaMTX's own status page -- no -NoOpen here.
    assert "-NoOpen" not in installer


def test_double_click_x_dvr_launcher_uses_bounded_windows_launcher() -> None:
    command = (ROOT / "Launch-X-DVR.cmd").read_text(encoding="utf-8")
    assert "scripts\\launch-x-dvr.ps1" in command
    assert "-WindowStyle Hidden" in command
    assert "-ExecutionPolicy Bypass" in command


def test_x_dvr_gui_launcher_owns_no_recorder_and_verifies_before_replacing() -> None:
    # The DVR GUI (core/dvr_service.py) is a thin browser front end now --
    # it must never start, stop, or verify a recorder process (that is
    # MediaMTX's job, entirely outside this script), and must still verify
    # any existing GUI process by its exact command line before replacing it.
    script = (ROOT / "scripts" / "launch-x-dvr.ps1").read_text(encoding="utf-8")
    assert "function Test-XDvrGuiProcess" in script
    assert "core.dvr_service" in script
    for forbidden in ("ffmpeg", "Test-XOmniDvrRecorderProcess", "dvrRecordingsRoot", "-m core.main"):
        assert forbidden not in script, f"launch-x-dvr.ps1 must not reference {forbidden!r}"


def test_x_dvr_startup_installer_creates_a_hidden_logon_shortcut() -> None:
    startup = (ROOT / "scripts" / "install-x-dvr-startup.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in startup
    assert "X DVR.lnk" in startup
    assert "GetFolderPath('Startup')" in startup
    assert "-NoOpen" in startup


def test_installer_creates_a_real_desktop_shortcut_with_app_icon() -> None:
    installer = (ROOT / "scripts" / "install-windows-launcher.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in installer
    assert "X Omni.lnk" in installer
    assert "x-omni.ico" in installer
