"""bat 必须存为 GBK(ANSI)，cmd 才能正确解析中文；UTF-8 会被拆成乱码命令。"""
bat = r"""@echo off
title A股监控台 - 运行中（关闭窗口即停止）
cd /d "%~dp0"
echo ══════════════════════════════════════════
echo   A股监控台 启动中...
echo   Web看板 + 内网穿透（手机可访问）
echo   窗口保持打开=运行中  关闭窗口=停止
echo ══════════════════════════════════════════
rem 10秒后自动打开看板（等Web就绪）ping延时无需交互输入
start "" /b cmd /c "ping -n 11 127.0.0.1 >nul & start "" http://127.0.0.1:8000"
py312\python.exe tools\tunnel_web.py
echo.
echo 服务已停止。若为异常退出，请查看上方报错。
pause
"""
open(r"c:\Users\Administrator\Desktop\stock_monitor\启动监控台.bat", "w", encoding="gbk").write(bat)
print("bat written in gbk")
