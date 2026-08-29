import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def _windows_command_line(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments)


def _evaluate_dvr_recorder_processes(cases: list[dict[str, object]]) -> dict[str, bool]:
    payload = base64.b64encode(json.dumps(cases).encode("utf-8")).decode("ascii")
    launcher_path = str(ROOT / "scripts" / "launch-x-omni.ps1").replace("'", "''")
    powershell = rf"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{launcher_path}',
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) {{
    throw ($errors | ForEach-Object {{ $_.ToString() }} | Out-String)
}}
foreach ($name in @(
    'ConvertFrom-WindowsCommandLine',
    'Test-XOmniDvrRecorderArguments',
    'Test-XOmniDvrRecorderProcess'
)) {{
    $node = $ast.Find({{
        param($candidate)
        $candidate -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $candidate.Name -eq $name
    }}, $true)
    if (-not $node) {{ throw "Missing launcher function $name" }}
    Invoke-Expression $node.Extent.Text
}}
$script:dvrRecordingsRoot = [IO.Path]::GetFullPath('E:\XOmni-DVR\recordings')
$json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
$cases = ConvertFrom-Json -InputObject $json
$results = @{{}}
foreach ($case in $cases) {{
    $process = [pscustomobject]@{{
        Name = [string]$case.process_name
        ProcessId = 1234
        ExecutablePath = [string]$case.executable_path
        CommandLine = [string]$case.command_line
    }}
    $results[[string]$case.name] = [bool](
        Test-XOmniDvrRecorderProcess -Process $process
    )
}}
$results | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(powershell.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


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


def test_launcher_reconciles_only_verified_dvr_recorders_after_core_stop() -> None:
    script = (ROOT / "scripts" / "launch-x-omni.ps1").read_text(encoding="utf-8")
    assert "function Test-XOmniDvrRecorderArguments" in script
    assert "function Test-XOmniDvrRecorderProcess" in script
    assert "function Stop-VerifiedDvrRecorders" in script
    assert "CommandLineToArgvW" in script

    main_flow = script[script.index("$owner = Get-PortOwner -Port $corePort") :]
    core_stop = main_flow.index("Stop-VerifiedCore -Port $corePort")
    dvr_stop = main_flow.index("Stop-VerifiedDvrRecorders")
    rebuild = main_flow.index("Invoke-UiRebuild")
    assert core_stop < dvr_stop < rebuild

    stop_function = script[
        script.index("function Stop-VerifiedDvrRecorders") : script.index(
            "function Get-CoreHealth"
        )
    ]
    assert "Stop-Process -Id $processId" in stop_function
    assert "Stop-Process -Name" not in stop_function
    assert 'Get-CimInstance Win32_Process -Filter "ProcessId=$processId"' in stop_function
    assert "$Process.ExecutablePath" in script
    assert "$Process.CommandLine" in script
    assert "$commandLine" not in "\n".join(
        line for line in stop_function.splitlines() if "Write-LauncherLog" in line
    )


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="The Windows command-line parser is only available on Windows",
)
def test_dvr_recorder_predicate_rejects_near_miss_ffmpeg_processes() -> None:
    executable = r"C:\Tools\ffmpeg.exe"
    recorder = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-protocol_whitelist",
        "file,pipe,tcp,rtsp",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "copy",
        "-f",
        "segment",
        "-segment_format",
        "matroska",
        "-segment_time",
        "300",
        "-reset_timestamps",
        "1",
        r"E:\XOmni-DVR\recordings\20260829T120000123456Z-%06d.mkv",
    ]
    preview = recorder[:12] + [
        "-an",
        "-frames:v",
        "1",
        "-c:v",
        "mjpeg",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    playback = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        r"E:\XOmni-DVR\recordings\20260829T120000123456Z-000001.mkv",
        "-c:v",
        "copy",
        r"E:\XOmni-DVR\playback-cache\clip.mp4",
    ]
    foreign_target = recorder[:-1] + [
        r"E:\Foreign-DVR\recordings\20260829T120000123456Z-%06d.mkv"
    ]
    extra_argument = recorder[:1] + ["-nostdin"] + recorder[1:]
    invalid_name = recorder[:-1] + [r"E:\XOmni-DVR\recordings\clip-%06d.mkv"]

    def case(
        name: str,
        arguments: list[str],
        *,
        executable_path: str = executable,
        process_name: str = "ffmpeg.exe",
    ) -> dict[str, object]:
        return {
            "name": name,
            "process_name": process_name,
            "executable_path": executable_path,
            "command_line": _windows_command_line(arguments),
        }

    results = _evaluate_dvr_recorder_processes(
        [
            case("exact_recorder", recorder),
            case("preview", preview),
            case("playback", playback),
            case("foreign_target", foreign_target),
            case("extra_argument", extra_argument),
            case("invalid_name", invalid_name),
            case(
                "executable_mismatch",
                recorder,
                executable_path=r"C:\Other\ffmpeg.exe",
            ),
            case("foreign_process_name", recorder, process_name="foreign.exe"),
        ]
    )
    assert results == {
        "exact_recorder": True,
        "preview": False,
        "playback": False,
        "foreign_target": False,
        "extra_argument": False,
        "invalid_name": False,
        "executable_mismatch": False,
        "foreign_process_name": False,
    }


def test_installer_creates_a_real_desktop_shortcut_with_app_icon() -> None:
    installer = (ROOT / "scripts" / "install-windows-launcher.ps1").read_text(encoding="utf-8")
    assert "WScript.Shell" in installer
    assert "X Omni.lnk" in installer
    assert "x-omni.ico" in installer
