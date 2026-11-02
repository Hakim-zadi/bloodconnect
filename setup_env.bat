@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo ===== BloodConnect setup =====
set /p E=Gmail address: 
set /p P=Gmail App Password (16 letters): 
set /p D=MySQL root password (press Enter if none): 
set "P=!P: =!"
if not exist "C:\BloodConnect_uploads\golden_blood" mkdir "C:\BloodConnect_uploads\golden_blood"
(
  echo SMTP_EMAIL=!E!
  echo SMTP_PASSWORD=!P!
  echo DB_PASSWORD=!D!
  echo UPLOAD_DIR=C:\BloodConnect_uploads
)> .env
echo.
echo .env created.
