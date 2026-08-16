@echo off
setlocal
cd /d "%~dp0"

rem Dedicated port for Fwd Vol Scanner (see scan.bat).
if not defined FVS_WEB_PORT set "FVS_WEB_PORT=8799"

start "" http://127.0.0.1:%FVS_WEB_PORT%/stocks
python webapp.py
