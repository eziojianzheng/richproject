@echo off
chcp 65001 >nul
title 淘股吧数据服务
echo ========================================
echo   淘股吧数据服务
echo ========================================
echo.
echo 启动中...
echo 服务地址: http://127.0.0.1:5000
echo 热门股追踪: http://127.0.0.1:5000/hot
echo.
python api_server.py
pause
