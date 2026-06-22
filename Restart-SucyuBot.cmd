@echo off
setlocal
rem === SucyuBot 一键重启：停掉当前实例，再起一个新的（加载最新代码/配置）===

echo [SucyuBot] Stopping current instance on port 8787 ...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('  killing PID ' + $_); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"

rem 等端口释放
timeout /t 2 /nobreak >nul

echo [SucyuBot] Starting fresh instance ...
start "SucyuBot" cmd /c "%~dp0run.cmd"

echo [SucyuBot] Restart issued. A new console window has opened.
timeout /t 2 /nobreak >nul
