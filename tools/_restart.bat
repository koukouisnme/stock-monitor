@echo off
rem Agent helper: restart web service (kill port 8000 + stale tunnel, relaunch hidden)
cd /d "%~dp0.."
taskkill /F /IM cloudflared.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
ping -n 3 127.0.0.1 >nul
start "Stock Monitor" /min cmd /c py312\python.exe tools\tunnel_web.py
