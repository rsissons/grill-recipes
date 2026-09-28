@echo off
rem Double-click me. Opens a pop-up to set the code that unlocks editing and deleting recipes.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set-admin-code.ps1"
echo.
pause
