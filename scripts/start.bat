@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
title LLM IM Bridge

if not exist "src\im_bridge" (
    echo [ERROR] src\im_bridge not found. Please run this script from the project root.
    pause
    exit /b 1
)

set "PY="
if exist "%~dp0..\python\python.exe" set "PY=%~dp0..\python\python.exe"
if not defined PY set "PY=python"

echo ========================================
echo   LLM IM Bridge
echo ========================================
echo.
echo Starting (Python: %PY%) ...
echo.

"%PY%" run.py
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [ERROR] Process exited with code %EXITCODE%.
    pause
)
endlocal
exit /b %EXITCODE%
