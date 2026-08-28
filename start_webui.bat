@echo off
chcp 65001 > nul
title WeBan WebUI
echo ========================================
echo    WeBan WebUI Launcher
echo ========================================
echo.

REM WeBan currently targets Python 3.12
py -3.12 --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12 not found. Please install Python 3.12.
    pause
    exit /b 1
)

REM Check dependencies
echo [INFO] Checking dependencies...
py -3.12 -c "import fastapi, uvicorn, tomli_w, requests, pyaes, nodriver, cv2" 2>nul
if errorlevel 1 (
    echo [WARN] Missing dependencies. Installing...
    py -3.12 -m pip install -r requirements-webui.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies
        pause
        exit /b 1
    )
)

echo [INFO] Starting WeBan WebUI with the local WeBan source...
echo [INFO] Access at: http://127.0.0.1:8080
echo [INFO] Press Ctrl+C to stop
echo.

cd /d "%~dp0"
py -3.12 webui.py

if errorlevel 1 (
    echo.
    echo [ERROR] WebUI exited with error
    pause
)
