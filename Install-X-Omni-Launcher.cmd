@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -File "%~dp0scripts\install-windows-launcher.ps1"
if errorlevel 1 pause
exit /b %errorlevel%
