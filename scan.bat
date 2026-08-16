@echo off
setlocal
set "PYPATH=C:\Users\bhavi\AppData\Local\Programs\Python\Python312"
set "PATH=%PYPATH%;%PYPATH%\Scripts;%PATH%"
cd /d "%~dp0"

if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0launch_te_app.ps1" -Page equity
) else (
    python fwdvol_scanner.py %*
)
