@echo off
REM ============================================================
REM  launch_autopilot.bat — AutoPilot 独立启动器
REM  由 Windows 任务计划程序在用户登录后自动调用
REM  窗口最小化运行，不依赖 IDE，可在任务栏找到
REM ============================================================
cd /d Z:\QuantpC_Workspace\Quant_Pilot

REM 使用虚拟环境 Python，最小化窗口启动（/MIN），保留窗口以便查看日志（/K 改为正常退出）
START "AutoPilot" /MIN Z:\QuantpC_Workspace\Quant_Pilot\.venv\Scripts\python.exe ^
    Z:\QuantpC_Workspace\Quant_Pilot\autopilot_master.py --task loop
