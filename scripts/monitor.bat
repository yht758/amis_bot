@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
title LLM IM Bridge - Monitor

:loop
cls
echo ========================================
echo   LLM IM Bridge Monitor   %date% %time%
echo ========================================
echo.

echo [Process]
tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /i "python" >nul
if !errorlevel! equ 0 (echo   python: RUNNING) else (echo   python: NOT RUNNING)
echo.

echo [status.log]
powershell -NoProfile -Command "if (Test-Path '.\logs\status.log') { Get-Content '.\logs\status.log' -Tail 10 -Encoding UTF8 }" 2>nul
echo.

echo [questions.log]
powershell -NoProfile -Command "if (Test-Path '.\logs\questions.log') { Get-Content '.\logs\questions.log' -Tail 6 -Encoding UTF8 }" 2>nul
echo.

echo [answers.log]
powershell -NoProfile -Command "if (Test-Path '.\logs\answers.log') { Get-Content '.\logs\answers.log' -Tail 6 -Encoding UTF8 }" 2>nul
echo.
echo Refresh every 5 seconds. Press Ctrl+C to exit.
timeout /t 5 >nul
goto loop
