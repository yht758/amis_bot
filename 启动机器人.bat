@echo off
chcp 936 >nul
title 爱弥斯 - 机器人
cd /d "%~dp0"

set "PYTHONPATH=%~dp0libs;%PYTHONPATH%"

if not exist "%~dp0python\python.exe" (
    echo 未找到 python 运行环境。
    echo 请下载完整 Windows 便携版，或自行安装 Python 3.14 并安装依赖。
    pause
    exit /b 1
)

start "爱弥斯" "%~dp0python\python.exe" run.py
pause