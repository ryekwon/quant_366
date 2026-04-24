# -*- coding: utf-8 -*-
"""
Sniper V5.1 (精准游资靶向版)
功能: 仅限 300 创业板 -> MA均线计算 -> 按流动性(成交额)降序排名 -> JSON 落盘
"""
import os
import pandas as pd
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")


# 路径从 .env 读取，方便不同机器移植
DATA_DIR = os.getenv("DATA_DIR",  r"Z:\QuantpC_Workspace\Data\Market_Daily")
CSV_PATH = os.getenv("MASTER_CSV", r"Z:\QuantpC_Workspace\Data\instrument_master.csv")
JSON_OUT = os.getenv("SNIPER_OUTPUT_FILE", r"Z:\QuantpC_Workspace\Quant_Pilot\.state\sniper_targets.json")

MIN_PCT_CHG = 0.15 
MIN_AMOUNT = 300000000 

def send_n8n_alert(title, message):
    if not N8N_WEBHOOK_URL: return
    try:
        payload = {"title": title, "message": message, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
        # 使用 POST 隐式传递
        requests.post(N8N_WEBHOOK_URL, json=payload, timeout=5)
    except Exception:
        pass

def run_sniper():
    from xtquant import xtdata
    print("🔫 启动 20CM 狙击雷达 (热切片内存版，仅限创业板)...")
    
    # 1. 物理隔离：只保留创业板 (300开头)
    if not os.path.exists(CSV_PATH):
        print(f"❌ 找不到 {CSV_PATH}")
        return
        
    df_master = pd.read_csv(CSV_PATH)
    chi_next_df = df_master[df_master['code'].str.startswith('300')]
    code_list = chi_next_df['code'].tolist()
    name_dict = dict(zip(chi_next_df['code'], chi_next_df['name']))
    
    # 2. 内存热切片：直接向 QMT 服务器索要这 1300 只股票近 15 天的日线数据
    print(f"⏳ 正在向 QMT 内存抽取 {len(code_list)} 只标的的热切片...")
    # 拿近两个月足够算 MA10 并包含实时行情
    xtdata.download_history_data2(code_list, period='1d', start_time='20251201') 
    
    # 获取特征矩阵
    market_data = xtdata.get_market_data_ex(
        field_list=['close', 'amount', 'preClose'], 
        stock_list=code_list, 
        period='1d'
    )
    
    final_targets = []
    
    # 2b. 一次性拉取全量 Tick（实时价格，盘中不会返回昨日数据）
    print(f"⏳ 正在获取 {len(code_list)} 只标的实时 Tick...")
    realtime_ticks = xtdata.get_full_tick(code_list)

    # 3. 内存极速计算
    for code in code_list:
        if code not in market_data or market_data[code].empty:
            continue
            
        df = market_data[code]
        if len(df) < 10:  # 新股数据不足，剔除
            continue

        # ── 关键修复：不信任 last_bar，改用实时 Tick ────────────────
        tick = realtime_ticks.get(code, {})
        real_close  = tick.get('lastPrice', 0)
        real_amount = tick.get('amount', 0)

        # 物理阻断：实时数据为空（停牌/无行情）直接跳过
        if real_close <= 0 or real_amount <= 0:
            continue

        # 昨收：用 last_bar 的 preClose（历史数据，准确）
        last_bar  = df.iloc[-1]
        pre_close = last_bar.get('preClose', 0)
        if pre_close <= 0:
            continue

        pct_chg = (real_close - pre_close) / pre_close

        # 游资门槛：+15% 以上，且成交额大于 3 亿（使用实时数据）
        if pct_chg >= MIN_PCT_CHG and real_amount >= MIN_AMOUNT:
            # MA 计算：历史收盘价序列 + 实时价格拼接到末尾
            history_closes = df['close'].tolist()
            history_closes.append(real_close)
            ma5  = sum(history_closes[-5:])  / min(5,  len(history_closes))
            ma10 = sum(history_closes[-10:]) / min(10, len(history_closes))
            
            final_targets.append({
                "code":       code,
                "name":       name_dict.get(code, "未知"),
                "close":      round(real_close, 2),
                "pct_chg":    round(pct_chg * 100, 2),
                "amount_raw": float(real_amount),
                "amount_str": f"{real_amount / 100000000:.2f}亿",
                "ma5":        round(ma5, 2),
                "ma10":       round(ma10, 2)
            })


    # 4. 龙虎榜排序与落盘
    final_targets = sorted(final_targets, key=lambda x: x['amount_raw'], reverse=True)

    os.makedirs(os.path.dirname(JSON_OUT), exist_ok=True)
    with open(JSON_OUT, 'w', encoding='utf-8') as f:
        json.dump(final_targets, f, ensure_ascii=False, indent=4)
        print(f"✅ 龙虎榜更新完成，共捕捉 {len(final_targets)} 只标的。")
        
    if final_targets:
        top_1 = final_targets[0]
        msg = f"👑 今日首推(龙一): {top_1['name']} | 成交: {top_1['amount_str']}\n"
        msg += "\n".join([f"🎯 {t['name']} | 涨幅: {t['pct_chg']}%" for t in final_targets[1:4]]) 
        send_n8n_alert("🔫 Sniper 龙虎榜更新", msg)

if __name__ == "__main__":
    run_sniper()