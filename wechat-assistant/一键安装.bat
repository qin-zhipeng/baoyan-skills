@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title 微信辅助器 - 一键安装
set "PYTHONIOENCODING=gbk"

echo.
echo   ============================================================
echo      微信辅助器  .  一键安装
echo   ============================================================
echo.
echo   不用装 Python、不用装依赖、不用配路径。
echo   这一步只做三件事：检查文件、跑自检、建桌面快捷方式。
echo.

if not exist "%~dp0runtime\pythonw.exe" (
  echo   [X] 缺少 runtime 文件夹，安装包不完整。
  echo       请重新完整解压压缩包后再运行本文件。
  echo.
  pause
  exit /b 1
)
if not exist "%~dp0bin\wx_key.dll" (
  echo   [X] 缺少 bin 目录里的密钥捕获组件，安装包不完整。
  echo.
  pause
  exit /b 1
)
echo   [1/3] 自带运行环境 ... 完成

echo   [2/3] 正在自检（约 10 秒）...
"%~dp0runtime\python.exe" -B "%~dp0app.py" --selftest > "%TEMP%\wxassist_selftest.log" 2>&1
if errorlevel 1 (
  echo.
  echo   [X] 自检没过，详情如下：
  type "%TEMP%\wxassist_selftest.log"
  echo.
  echo   多数情况是杀毒软件拦截或文件没解压完整。
  pause
  exit /b 1
)
echo         自检全部通过

echo   [3/3] 创建桌面快捷方式...
rem VBS 是纯 ASCII 源码、安装目录走命令行参数传入，中文路径不会乱码
cscript //nologo "%~dp0创建快捷方式.vbs" "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo         快捷方式没建成（不影响使用），双击「启动.bat」即可
) else (
  echo         桌面快捷方式已建好
)

echo.
echo   ============================================================
echo      安装完成！正在启动...
echo   ============================================================
echo.
echo   首次启动会自动读本机微信、解密聊天记录、出客户名单和话术归因。
echo   微信会要求重启一次并在手机上点「确认登录」，之后全自动。
echo   数据只留在这台电脑，不联网、不上传。
echo.
start "" "%~dp0runtime\pythonw.exe" "%~dp0app.py"
exit /b 0
