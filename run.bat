@echo off
setlocal
cd /d "%~dp0"
title BloodConnect
echo Stopping any old BloodConnect servers...
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM py.exe >nul 2>&1
if not exist ".env" (
  echo.
  echo First time setup: .env file not found, creating it now.
  call "%~dp0setup_env.bat"
)
echo.
echo Installing needed packages...
python -m pip install -r requirements.txt
if errorlevel 1 py -m pip install -r requirements.txt
echo.
echo Starting BloodConnect... open http://127.0.0.1:5000 in your browser.
python data.py
if errorlevel 1 py data.py
echo.
pause
