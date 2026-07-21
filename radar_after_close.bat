@echo off
setlocal
cd /d "%~dp0"
python stock_radar_scan.py --cadence auto --source yf --due-only
