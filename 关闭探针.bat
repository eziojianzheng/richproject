@echo off
chcp 65001 >nul 2>&1
title 关闭前端探针会话
cd /d "%~dp0"

set PYCMD=
where py >nul 2>&1 && set PYCMD=py
if "%PYCMD%"=="" where python >nul 2>&1 && set PYCMD=python

if "%PYCMD%"=="" (
    if exist "_session_alive.flag" del /q "_session_alive.flag"
    echo [提示] 未找到 Python, 仅清理了标记文件。
    pause
    goto :EOF
)

echo 正在关闭探针会话...
%PYCMD% debug_session.py stop
echo.
echo 会话已停止, 可关闭本窗口。
pause
