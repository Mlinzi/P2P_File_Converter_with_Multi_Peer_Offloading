@echo off
:: P2P File Conversion Network — Web UI
:: Usage: start.bat [--name YourName] [--port 9001] [--ui-port 8080]

cd /d "%~dp0UI"

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause & exit /b 1
)

pip show zeroconf >nul 2>&1
if errorlevel 1 (
    echo Installing requirements...
    pip install -r requirements.txt
)

python peer.py %*
