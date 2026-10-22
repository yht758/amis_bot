@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

set "PY=python\python.exe"
if not exist "%PY%" set "PY=python"

echo Building BotManager.exe ...
"%PY%" launcher\build_exe.py
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)
echo.
echo Done. Run BotManager.exe in the project root.
pause
