@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose_windows_recipient.ps1"
echo.
pause
