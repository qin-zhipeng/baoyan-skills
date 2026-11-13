@echo off
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk"
if not exist "%~dp0runtime\python.exe" (
  echo 缺少 runtime 文件夹，请先双击「一键安装.bat」
  pause
  exit /b 1
)
echo 带控制台启动（出错信息会留在窗口里，方便排查）
echo.
"%~dp0runtime\python.exe" -B "%~dp0app.py" %*
echo.
echo 程序已退出，错误码 %errorlevel%
pause
exit /b 0
