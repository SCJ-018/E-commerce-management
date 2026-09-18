@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo.
echo  ============================================================
echo   [抖店滑块助手]  常驻后台，等后台页面下发任务
echo  ============================================================
echo   保持这个窗口开着就行（可最小化）。
echo.
echo   之后在后台「店铺账号管理 -^> 抖店 -^> 抖店登录邮箱」
echo   点「手动拖滑块」，这台电脑会自动弹出 Chrome，
echo   你拖完拼图滑块、登录成功，登录态会自动传到服务器。
echo.
echo   想开机自动待命：Win+R 输入 shell:startup，
echo   把本文件的快捷方式丢进弹出的文件夹即可。
echo.
echo   关掉此窗口 = 停止助手（后台按钮将无人响应）。
echo  ------------------------------------------------------------
echo.

"%PYEXE%" "tools\doudian_crawler\slider_agent.py"

echo.
echo  ------------------------------------------------------------
echo   助手已退出。按任意键关闭此窗口。
pause >nul
