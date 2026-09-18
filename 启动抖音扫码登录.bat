@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

title Douyin Login - QR code
"%PYEXE%" "tools\douyin_login.py"

echo.
echo  ------------------------------------------------------------
echo   Finished. Press any key to close this window.
echo  ------------------------------------------------------------
pause >nul
