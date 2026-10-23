@echo off
rem IPFS demo on ONE computer: two clients (Laptop A + Laptop B), each with its own workspace,
rem attestor and IPFS node, already peered, one browser tab each. Downloads IPFS on first use.
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
"%VENV_PY%" demo\demo.py pair
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo The demo failed to start. Make sure Python 3.10+ and git are installed and on PATH,
echo and that ports 8080-8081, 8765-8766, 8790-8791, 5001-5002 and 4001-4002 are free.
pause
exit /b 1
