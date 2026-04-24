# -*- coding: utf-8 -*-
"""
QMT 每日 16:00 增量同步引擎 (智能水库调度版)
架构: 自动探测数据湖水位 -> 智能切换 [大爆炸全量/5天增量] -> 批量并发下载 -> 物理缝合落盘
"""
from xtquant import xtdata
import pandas as pd
import os
import time
from datetime import datetime, timedelta
import re

# ================= 核心配置区 =================
NVME_PATH = r"Z:\QuantpC_Workspace\Data\Market_Daily"
MASTER_CSV = r"Z:\QuantpC_Workspace\Data\instrument_master.csv"
BATCH_SIZE = 500
# ==============================================

def get_clean_universe():
    print("🔍 正在从 QMT 获取纯净的 A 股与 ETF 白名单...")
    sector_list = xtdata.get_sector_list()
    all_codes = []
    for sector in sector_list:
        all_codes.extend(xtdata.get_stock_list_in_sector(sector))
        
    clean_codes = list(set(all_codes))
    valid_pattern = re.compile(r'^(00\d|30\d|60\d|688|51\d|56\d|588|159|16\d|501)\d{3}\.(SH|SZ)$')
    pre_filtered_codes = [c for c in clean_codes if valid_pattern.match(c)]
    
    junk_pattern = re.compile(r'ST|退|货币|理财|添益|日利|收益|纯债|政金债|信用债|国债|企债')
    
    final_instruments = []
    print(f"⏳ 正在核对 {len(pre_filtered_codes)} 只标的，执行联合排雷...")
    for code in pre_filtered_codes:
        info = xtdata.get_instrument_detail(code)
        if info:
            name = info.get('InstrumentName', '未知')
            if not junk_pattern.search(name):
                final_instruments.append({'code': code, 'name': name, 'type': info.get('InstrumentType', '未知')})
                
    print(f"🎯 数据清洗完毕。纯血 A 股 + 高波动 ETF: {len(final_instruments)} 只")
    return final_instruments

def update_master_csv():
    valid_instruments = get_clean_universe()
    df = pd.DataFrame(valid_instruments)
    os.makedirs(os.path.dirname(MASTER_CSV), exist_ok=True)
    df.to_csv(MASTER_CSV, index=False, encoding='utf-8-sig')
    return [item['code'] for item in valid_instruments]

def daily_incremental_sync():
    end_date = datetime.now().strftime('%Y%m%d')
    start_time_log = time.time()
    
    if not os.path.exists(MASTER_CSV):
        return

    df_master = pd.read_csv(MASTER_CSV)
    target_list = df_master['code'].tolist()
    
    os.makedirs(NVME_PATH, exist_ok=True)
    # 🧠 智能探测水库当前水位：如果文件少于100个，判定为“干涸”，触发全量
    is_empty_lake = len(os.listdir(NVME_PATH)) < 100
    
    if is_empty_lake:
        start_date = '20180101'
        print(f"🌊 [智能调度] 检测到数据湖干涸，触发【大爆炸全量模式】(自 20180101)...")
    else:
        start_date = (datetime.now() - timedelta(days=5)).strftime('%Y%m%d')
        print(f"🔄 [智能调度] 数据湖正常，触发【常规增量模式】(近 5 天)...")

    # ── 1. 高效批量下载 (绝不单点 DDOS) ──
    print(f"📥 正在向服务器批量请求 K 线 ({start_date} - {end_date})...")
    for i in range(0, len(target_list), BATCH_SIZE):
        batch = target_list[i:i + BATCH_SIZE]
        xtdata.download_history_data2(stock_list=batch, period='1d', start_time=start_date, end_time=end_date)
        time.sleep(1) # 给 QMT 底层 C++ 留出极其重要的呼吸时间
        
    if is_empty_lake:
        print("⏳ 全量数据包极其庞大，强行休眠 3 分钟，等待 QMT 本地 C++ 彻底落盘...")
        time.sleep(180) 
    else:
        time.sleep(5)

    # ── 2. 纯粹的数据提取与融合 (不再触发下载) ──
    print("🧮 正在执行【无缝融合与全局重算】...")
    success_count = 0
    
    for index, row in df_master.iterrows():
        code = row['code']
        asset_type = row['type']
        
        try:
            data_dict = xtdata.get_market_data_ex(
                field_list=['open', 'high', 'low', 'close', 'volume', 'amount'], 
                stock_list=[code], period='1d', start_time=start_date, end_time=end_date, dividend_type='front'
            )
            df_new = data_dict.get(code)
            
            if df_new is None or df_new.empty:
                continue
                
            df_new.index = pd.to_datetime(df_new.index.astype(str), format='%Y%m%d', errors='coerce').date
            df_new.index.name = 'date'
            df_new = df_new.dropna(subset=['close'])
            if df_new.empty: continue
            
            save_path = os.path.join(NVME_PATH, f"{code.replace('.', '_')}.parquet")
            
            if os.path.exists(save_path):
                df_old = pd.read_parquet(save_path)
                df_combined = pd.concat([df_old, df_new]).reset_index().drop_duplicates(subset=['date'], keep='last').set_index('date')
            else:
                df_combined = df_new.copy()

            df_combined['pct_change'] = df_combined['close'].pct_change() * 100
            df_combined['pct_change'] = df_combined['pct_change'].fillna(0)
            
            if asset_type == '股票':
                info = xtdata.get_instrument_detail(code)
                float_shares = info.get('FloatVolume', 0) if info else 0
                if float_shares > 0:
                    df_combined['turnover_rate'] = (df_combined['volume'] / float_shares) * 100
                    df_combined['float_market_cap'] = (df_combined['close'] * float_shares) / 1e8
                else:
                    df_combined['turnover_rate'] = 0.0
                    df_combined['float_market_cap'] = 0.0
            
            numeric_cols = df_combined.select_dtypes(include='number').columns
            df_combined[numeric_cols] = df_combined[numeric_cols].round(4)
            df_combined.to_parquet(save_path, engine='pyarrow')
            
            success_count += 1
            
        except Exception as e:
            continue
            
    print(f"✅ 融合落地完成！耗时: {time.time() - start_time_log:.2f} 秒。成功写入 {success_count} 个标的。")

if __name__ == "__main__":
    target_list = update_master_csv()  
    daily_incremental_sync()