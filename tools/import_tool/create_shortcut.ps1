# 创建桌面快捷方式
$exePath = "$PSScriptRoot\dist\数据导入工具.exe"
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "数据导入工具.lnk"

if (-not (Test-Path $exePath)) {
    Write-Host "错误: 找不到 $exePath" -ForegroundColor Red
    Write-Host "请先运行 build_exe.py 打包生成 EXE" -ForegroundColor Yellow
    pause
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($shortcutPath)
$Shortcut.TargetPath = $exePath
$Shortcut.WorkingDirectory = (Split-Path $exePath -Parent)
$Shortcut.Description = "MySQL 数据导入工具"
$Shortcut.IconLocation = $exePath
$Shortcut.Save()

Write-Host "桌面快捷方式已创建: $shortcutPath" -ForegroundColor Green
pause
