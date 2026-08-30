from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def test_double_click_launcher_uses_bounded_windows_launcher() -> None:
    command = (ROOT / "Launch-X-Omni.cmd").read_text(encoding="utf-8")
    assert "scripts\\launch-x-omni.ps1" in command
    assert "-WindowStyle Hidden" in command


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


def test_mediamtx_startup_installer_creates_a_hidden_logon_shortcut() -> None:
    startup = (ROOT / "scripts" / "install-mediamtx-startup.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in startup
    assert "MediaMTX.lnk" in startup
    assert "GetFolderPath('Startup')" in startup
    assert "-NoOpen" in startup


def test_mediamtx_desktop_installer_creates_a_real_shortcut_with_app_icon() -> None:
    installer = (ROOT / "scripts" / "install-mediamtx-launcher.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in installer
    assert "MediaMTX.lnk" in installer
    assert "x-omni.ico" in installer
    assert "GetFolderPath('Desktop')" in installer
    # Unlike the Startup entry, a double-clicked desktop icon should open
    # MediaMTX's own status page -- no -NoOpen here.
    assert "-NoOpen" not in installer


def test_double_click_x_dvr_launcher_uses_bounded_windows_launcher() -> None:
    command = (ROOT / "Launch-X-DVR.cmd").read_text(encoding="utf-8")
    assert "scripts\\launch-x-dvr.ps1" in command
    assert "-WindowStyle Hidden" in command


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
