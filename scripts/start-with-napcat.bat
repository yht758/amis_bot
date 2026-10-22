@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
title LLM IM Bridge - wait for OneBot

echo ========================================
echo   LLM IM Bridge - OneBot boot helper
echo ========================================
echo.
echo Please start NapCat / go-cqhttp and log in first.
echo Waiting for OneBot WebSocket at ws://127.0.0.1:3001 ...
echo.

set /a tries=0
:wait_loop
powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 3001 -InformationLevel Quiet) { exit 0 } else { exit 1 }" >nul 2>&1
if %errorlevel% equ 0 goto ready
set /a tries+=1
if %tries% geq 30 goto timeout
timeout /t 2 >nul
goto wait_loop

:ready
echo.
echo [OK] OneBot port 3001 is reachable. Starting the bridge...
echo.
call "%~dp0start.bat"
endlocal
exit /b 0

:timeout
echo.
echo [ERROR] OneBot WebSocket is not reachable after 60 seconds.
echo Start NapCat / go-cqhttp, confirm the WebSocket port is 3001, then retry.
pause
endlocal
exit /b 1
