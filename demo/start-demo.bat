@echo off
rem Local DOSR demo: one client on this computer (no IPFS). Sets up demo\.venv on first run,
rem then starts the attestor + client GUI and opens the browser.
rem Close this window to stop.
setlocal
cd /d "%~dp0.."

set "VENV_PY=demo\.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :run

echo First run: setting up demo\.venv (this takes a minute)...
set "SYS_PY=python"
where py >nul 2>nul
if not errorlevel 1 set "SYS_PY=py -3"
%SYS_PY% demo\setup_env.py
if errorlevel 1 goto :fail

:run
"%VENV_PY%" demo\demo.py up --no-ipfs --p2p-port 0
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo The demo failed to start. Make sure Python 3.10+ and git are installed and on PATH,
echo and that ports 8080 and 8765 are free.
pause
exit /b 1
