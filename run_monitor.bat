@echo off
cd /d "%~dp0"
title A股监控 · 常驻调度
echo ==============================================
echo   A股监控系统 启动中...
echo   收盘扫描 15:35 / 盘中扫描 10:30 14:00 / 心跳 16:00
echo   错过的当日任务会自动补跑（Ctrl+C 退出）
echo ==============================================
py312\python.exe -u main.py schedule
echo.
echo 调度已退出。
pause
