@echo off
title Stock Monitor - Web + Tunnel (close window to stop)
cd /d "%~dp0"
echo ============================================================
echo   Stock Monitor starting...
echo   Web dashboard + Cloudflare tunnel (mobile access)
echo ============================================================
rem Kill old instances on port 8000 and stale tunnels (safe if none)
taskkill /F /IM cloudflared.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
timeout /t 2 /nobreak >nul
rem Open dashboard in browser after 10s (skip if ping blocked by firewall)
start "" /b cmd /c "ping -n 11 127.0.0.1 >nul & start "" http://127.0.0.1:8000"
py312\python.exe tools\tunnel_web.py
echo.
echo Service stopped. If unexpected, check messages above.
pause
