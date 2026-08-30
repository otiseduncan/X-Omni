@echo off
setlocal
cd /d "%~dp0"
start "X DVR" /b powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -File "%~dp0scripts\launch-x-dvr.ps1"
exit /b 0
