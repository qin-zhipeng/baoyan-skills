@echo off
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk"
if not exist "%~dp0runtime\pythonw.exe" (
  echo 缺少 runtime 文件夹，请先双击「一键安装.bat」
  pause
  exit /b 1
)
start "" "%~dp0runtime\pythonw.exe" "%~dp0app.py"
exit /b 0
