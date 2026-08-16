@echo off
setlocal
set "PYPATH=C:\Users\bhavi\AppData\Local\Programs\Python\Python312"
set "PATH=%PYPATH%;%PYPATH%\Scripts;%PATH%"
cd /d "%~dp0"

rem Dedicated port for Fwd Vol Scanner. Kept clear of Price Action (8765-8775),
rem TWS Matcher (8787), Bedrock (8791) and Live Market Console (8794) so all the
rem apps can run at once without the browser opening the wrong one.
if not defined FVS_WEB_PORT set "FVS_WEB_PORT=8799"

if "%~1"=="" (
    start "" http://127.0.0.1:%FVS_WEB_PORT%
    python webapp.py
) else (
    python fwdvol_scanner.py %*
)
