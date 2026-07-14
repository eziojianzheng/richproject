@echo off
chcp 65001 >nul 2>&1
title 淘股吧数据服务 (debug + 探针会话)
setlocal enabledelayedexpansion

cd /d "%~dp0"

REM ============================================================
REM  一键启动: 单实例服务 + debug模式 + 探针会话
REM  1. 杀掉所有占用 5000 端口的旧进程 (保证唯一实例)
REM  2. 以 debug 模式启动 api_server
REM  3. 服务起来后自动启动探针会话 (Playwright Chrome 常驻)
REM ============================================================

REM ---- 探测 Python ----
set PYCMD=
where py >nul 2>&1 && set PYCMD=py
if "%PYCMD%"=="" (
    where python >nul 2>&1 && set PYCMD=python
)
if "%PYCMD%"=="" (
    echo [错误] 未找到 Python! 下载: https://python.org
    pause
    goto :EOF
)
echo [环境] Python: %PYCMD%

REM ---- 检查 playwright ----
%PYCMD% -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo [警告] 未安装 playwright, 探针会话将不可用。
    set NO_PROBE=1
) else (
    echo [环境] playwright: OK
    set NO_PROBE=0
)

REM ============================================================
REM 1. 单实例检测: 杀掉所有占用 5000 端口的进程
REM ============================================================
echo [实例] 检查 5000 端口占用...
set KILLED=0
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":5000.*LISTENING"') do (
    echo [实例] 杀掉占用 5000 端口的进程 PID=%%P
    taskkill /PID %%P /F >nul 2>&1
    set KILLED=1
)
if "!KILLED!"=="1" (
    echo [实例] 已清理旧进程, 等待端口释放...
    ping -n 4 127.0.0.1 >nul
) else (
    echo [实例] 端口空闲
)

REM ============================================================
REM 2. 启动 api_server (debug 模式, 后台)
REM ============================================================
echo.
echo ========================================
echo   淘股吧数据服务 (debug 模式)
echo ========================================
echo   服务地址: http://127.0.0.1:5000
echo   热门股:   http://127.0.0.1:5000/hot
echo   盯盘:     http://127.0.0.1:5000/monitor
echo   debug:    已开启 (config.yml debug.enabled=true)
echo ========================================
echo.
echo [服务] 启动中...

REM 后台启动服务
start "api_server (debug)" /min %PYCMD% api_server.py

REM 等待服务就绪 (用 ping 延时, 最多 25 秒)
set READY=0
for /l %%i in (1,1,25) do (
    if "!READY!"=="0" (
        ping -n 2 127.0.0.1 >nul
        powershell -Command "try{=Invoke-WebRequest -Uri 'http://127.0.0.1:5000/' -UseBasicParsing -TimeoutSec 2; if(.StatusCode -eq 200){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
        if not errorlevel 1 (
            set READY=1
            echo [服务] 已就绪
        )
    )
)
if "!READY!"=="0" (
    echo [错误] 服务启动失败! 请检查 api_server.py 报错。
    pause
    goto :EOF
)

REM ============================================================
REM 3. 启动探针会话 (Playwright Chrome 常驻)
REM ============================================================
echo.
if "!NO_PROBE!"=="1" (
    echo [探针] 跳过 (未安装 playwright)
    echo.
    echo 服务已启动, 可在浏览器访问 http://127.0.0.1:5000
    echo 按 Ctrl+C 或关闭本窗口停止服务。
    echo.
    cmd /k
    goto :EOF
)

echo [探针] 启动 Playwright 会话 (Chrome 常驻)...
echo [探针] 你在 Chrome 里随意操作, 跟 agent 说"看现在"即可抓当前页。
echo [探针] 关闭本窗口会同时停止服务和探针。
echo.

REM 前台运行探针会话 (关闭本窗口=全部停止)
%PYCMD% debug_session.py serve // --wait 4

echo.
echo [探针] 会话已结束。服务仍在后台运行, 可手动关闭。
pause
