@echo off
setlocal
cd /d "%~dp0"
start "X DVR" /b "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\launch-x-dvr.ps1"
exit /b 0
