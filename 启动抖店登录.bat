@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

title Doudian Login - manual slider
"%PYEXE%" "tools\doudian_crawler\_local_login_all.py"

echo.
echo  ------------------------------------------------------------
echo   Finished. Press any key to close this window.
echo  ------------------------------------------------------------
pause >nul
