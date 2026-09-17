@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo.
echo  ============================================================
echo   [抖音扫码登录]  本机扫码 - 扫完自动上传服务器
echo  ============================================================
echo   稍后会弹出一个 Chrome 窗口，请用手机抖音 App 扫窗口里的二维码
echo   扫完在手机上点「确认登录」，脚本会自动把登录态传到服务器
echo.
echo  ------------------------------------------------------------
"%PYEXE%" "tools\douyin_login.py"

echo.
echo  ------------------------------------------------------------
echo   结束。按任意键关闭此窗口。
pause >nul
