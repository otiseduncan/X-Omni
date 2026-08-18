from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def test_installer_creates_a_real_desktop_shortcut_with_app_icon() -> None:
    installer = (ROOT / "scripts" / "install-windows-launcher.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in installer
    assert "X Omni.lnk" in installer
    assert "x-omni.ico" in installer
