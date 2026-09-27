@echo off
rem Double-click to start the ITAM Portal (offline). Close this window to stop it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_portal.ps1" %*
pause
