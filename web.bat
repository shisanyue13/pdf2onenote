@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title pdf2onenote - 图形界面

rem ============================================================
rem  双击本文件即可启动图形界面（Windows）
rem  第一次运行会自动建 .venv 并安装依赖；之后秒开
rem  额外参数会原样传给服务，例如：web.bat --port 9000 --no-browser
rem ============================================================

if exist ".venv\Scripts\python.exe" goto run

echo [pdf2onenote] 第一次运行：正在创建虚拟环境并安装依赖（需要联网，1-3 分钟）...
echo.

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD goto nopython

%PYCMD% -m venv .venv
if errorlevel 1 goto venvfail

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pipfail

echo.
echo [pdf2onenote] 核心依赖装好了。
echo [pdf2onenote] 如果你要处理的是扫描件（没有文本层），再装一次 OCR 依赖：
echo       .venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
echo.
pause

:run
echo [pdf2onenote] 正在启动本地服务，浏览器会自动打开 http://127.0.0.1:8765
echo [pdf2onenote] 用完后关掉这个窗口（或按 Ctrl+C）就会停止服务。
echo.
".venv\Scripts\python.exe" -m pdf2onenote web %*
echo.
echo [pdf2onenote] 服务已停止。
pause
exit /b 0

:nopython
echo.
echo [pdf2onenote] 没找到 Python。
echo       请先安装 Python 3.10 或更高版本，安装时务必勾选 "Add Python to PATH"：
echo       https://www.python.org/downloads/
echo.
pause
exit /b 1

:venvfail
echo.
echo [pdf2onenote] 创建虚拟环境失败。请确认 Python 版本在 3.10 以上（命令行执行 python -V 可查看）。
echo.
pause
exit /b 1

:pipfail
echo.
echo [pdf2onenote] 依赖安装失败。请检查网络（或换镜像源）后重新双击本文件。
echo.
pause
exit /b 1
