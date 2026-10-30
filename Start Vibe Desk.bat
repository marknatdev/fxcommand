@echo off
title Vibe Desk
cd /d "%~dp0"

rem Already running? Just open it.
curl -s -o nul http://127.0.0.1:8765/api/status && (start "" http://127.0.0.1:8765 & exit /b)

choice /C YN /T 15 /D Y /M "Start the OmniRoute tunnel?"
if errorlevel 2 goto skip_tunnel

rem OmniRoute listens only on the Ubuntu server; this tunnel brings it to port 20128 here.
rem If a tunnel is already up, the new one exits and the old one keeps serving.
start "OmniRoute tunnel" /min C:\Windows\System32\OpenSSH\ssh.exe -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o BatchMode=yes -i "D:\millmaker\ssh-key.pem" -L 20128:127.0.0.1:20128 ubuntu@54.254.218.196
goto start_server

:skip_tunnel
echo AI chat (OmniRoute) won't work until the tunnel is started.

:start_server
echo Starting Vibe Desk on http://127.0.0.1:8765 (close this window to stop it)
uv run --extra mt5 vibedesk-web --open
pause
