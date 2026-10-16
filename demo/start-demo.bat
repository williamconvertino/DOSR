@echo off
rem One-click DOSR demo (Windows): sets up demo\.venv on first run, then starts
rem the attestor + client GUI and opens the browser. Close this window to stop.
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
echo and that ports 8080 and 8765 are free.
pause
exit /b 1
