@echo off
setlocal
cd /d "%~dp0"
start "X Omni" /b powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -File "%~dp0scripts\launch-x-omni.ps1"
exit /b 0
