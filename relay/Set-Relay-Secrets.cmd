@echo off
rem Double-click me. Opens pop-up boxes for the two keys and stores them in Cloudflare.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set-secrets.ps1"
echo.
pause
