@echo off
echo Stopping old backend processes...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000.*LISTENING"') do taskkill /F /PID %%a 2>nul
timeout /t 1 /nobreak >nul
cd /d "%~dp0backend"
start "OCR-Backend-Paddle" py -3.12 app.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:5000
