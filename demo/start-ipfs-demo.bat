@echo off
rem IPFS demo, run on EACH of two computers: IPFS node + attestor + client GUI + peer inbox.
rem Downloads IPFS on first use. Allow the firewall prompts (ipfs, Python), then peer the two
rem computers via the P2P button (see demo\README.md, section 3).
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
"%VENV_PY%" demo\demo.py up
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo The demo failed to start. Make sure Python 3.10+ and git are installed and on PATH,
echo and that ports 8080, 8765, 8790, 5001 and 4001 are free.
pause
exit /b 1
