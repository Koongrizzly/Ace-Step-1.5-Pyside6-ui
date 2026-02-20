@echo off
setlocal ENABLEEXTENSIONS

REM Run Ace Step 1.5 installer
cd /d "%~dp0"
set "APP_ROOT=%CD%"

REM Prefer the bundled/created venv python if it exists
set "PY="
if exist "%APP_ROOT%\presets\extra_env\.ace_env\Scripts\python.exe" set "PY=%APP_ROOT%\presets\extra_env\.ace_env\Scripts\python.exe"
if not defined PY set "PY=python"

echo.
echo [Ace Step 1.5] Installer
echo App root: "%APP_ROOT%"
echo Python  : "%PY%"
echo.

"%PY%" "%APP_ROOT%\presets\extra_env\ace_step_15_installer.py"
echo.
pause
