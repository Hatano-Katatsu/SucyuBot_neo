@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SucyuBot.ps1"
if errorlevel 1 (
  echo.
  echo SucyuBot failed to start. Check logs\service.err.log for details.
  pause
)
