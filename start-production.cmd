@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-production.ps1" -Open
if errorlevel 1 (
  echo.
  echo Startup failed. Read the message above before closing this window.
)
pause
