@echo off
:: P2P File Converter — Basic UI
:: Run with optional args:  run.bat --name YourName --port 9001

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause & exit /b 1
)

:: Install deps if needed
pip show zeroconf >nul 2>&1
if errorlevel 1 (
    echo Installing requirements...
    pip install -r requirements.txt
)

:: Launch
python peer.py %*
