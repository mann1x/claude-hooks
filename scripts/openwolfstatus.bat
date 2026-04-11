@echo off
REM OpenWolf status — shows all daemons, dashboards, and port assignments
python "%~dp0openwolfstatus.py"
echo.
echo === PM2 Process List ===
pm2 list 2>nul | findstr openwolf
