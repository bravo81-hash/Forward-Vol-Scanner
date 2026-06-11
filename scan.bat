@echo off
setlocal
set "PYPATH=C:\Users\bhavi\AppData\Local\Programs\Python\Python312"
set "PATH=%PYPATH%;%PYPATH%\Scripts;%PATH%"
cd /d "%~dp0"

if "%~1"=="" (
    start "" http://127.0.0.1:8765
    python webapp.py
) else (
    python fwdvol_scanner.py %*
)
