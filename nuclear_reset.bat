@echo off
chcp 65001 >nul
echo [Disaster Recovery] 正在执行核重置... %date% %time%

:: ======================================================
:: 1. 精准击杀：Python 量化进程（按命令行关键字匹配）
::    使用 taskkill /FI 过滤，不依赖已废弃的 wmic
::    /F = 强制 /T = 包含子进程树
:: ======================================================
echo [Step 1] 清场量化 Python 进程...

for %%K in (
    autopilot_master.py
    fat_fish_master.py
    fat_fish_guard.py
    fat_fish_executor.py
    t0_multigrid_executor.py
    t0_master.py
    sniper_entry_executor.py
    sniper_exit_guard.py
    etf_rotation_executor.py
    stat_arb_executor.py
    quant_debrief.py
    trade_settlement.py
    accounting_audit.py
    knowledge_manager.py
    intraday_reconcile.py
) do (
    taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq %%K" >nul 2>&1
    powershell -NoProfile -Command "Get-WmiObject Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*%%K*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
)

echo [Step 1] Done.

:: ======================================================
:: 2. 斩首 QMT 客户端（解决 API 假死）
::    真实进程名：xtminiqmt.exe / xtitclient.exe
:: ======================================================
echo [Step 2] 击杀 QMT 客户端进程...
taskkill /F /IM xtminiqmt.exe /T >nul 2>&1
taskkill /F /IM xtitclient.exe /T >nul 2>&1
echo [Step 2] Done.

:: ======================================================
:: 3. 内存排空（等待资源和端口释放）
:: ======================================================
echo [Step 3] 等待 8 秒资源释放...
timeout /t 8 /nobreak >nul

:: ======================================================
:: 4. 重新点火中枢（必须用 venv Python）
:: ======================================================
echo [Step 4] 重新点火 autopilot_master.py ...
cd /d Z:\QuantpC_Workspace\Quant_Pilot
start "Autopilot_Master" Z:\QuantpC_Workspace\Quant_Pilot\.venv\Scripts\python.exe autopilot_master.py --loop

echo [Disaster Recovery] 脉冲重置完成！%date% %time%