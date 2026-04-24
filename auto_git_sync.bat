@echo off
echo =========================================
echo [Auto Pilot] 启动每日代码资产云端封存...
echo =========================================

:: 1. 进入你的代码核心目录 (请确保路径正确)
cd /d Z:\QuantpC_Workspace\Quant_Pilot

:: 2. 检查是否有变动，如果有，一并提交
git add .

:: 3. 用当前的时间戳作为冷血的提交信息
set timestamp=%date:~0,10% %time:~0,8%
git commit -m "🤖 Auto-Pilot Daily Vault: %timestamp%"

:: 4. 强推至云端地堡
git push origin main

echo ✅ 资产已封存至云端。