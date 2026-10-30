@echo off
rem Double-click to start Outpost (needs Docker Desktop). See start.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
pause
