@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\install-x-dvr-launcher.ps1"
if errorlevel 1 pause
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\install-x-dvr-startup.ps1"
if errorlevel 1 pause
exit /b %errorlevel%
