# -*- coding: utf-8 -*-
"""
ETF List Retrieval Script (Final Version)
Retrieves the full list of ETFs from MiniQMT, matches with names from instrument_master.csv,
and falls back to API for missing names (like money market funds).
"""
import os
import yaml
import pandas as pd
from xtquant import xtdata
from datetime import datetime
import time

# ================= 配置区 =================
DATA_DIR = r"Z:\QuantpC_Workspace\Data"
OUTPUT_FILE = os.path.join(DATA_DIR, "ETF_list.yaml")
MASTER_CSV = os.path.join(DATA_DIR, "instrument_master.csv")
# ==========================================

def get_etf_list():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 开始获取并丰富 ETF 列表...")
    
    # 1. 确保输出目录存在
    if not os.path.exists(DATA_DIR):
        print(f"📁 创建目录: {DATA_DIR}")
        os.makedirs(DATA_DIR, exist_ok=True)
    
    # 2. 连接检测
    print("🔌 正在检测 XtData 连接...")
    sectors = xtdata.get_sector_list()
    if not sectors:
        print("❌ 无法获取板块列表，请确保 MiniQMT 客户端已登录并处于运行状态。")
        return
    print(f"✅ 连接成功，找到 {len(sectors)} 个板块。")

    # 3. 寻找 ETF 相关板块
    print("🔎 正在寻找 ETF 相关代码...")
    etf_keywords = ["ETF", "基金", "分级基金", "货币"]
    target_sectors = [s for s in sectors if any(k in s for k in etf_keywords)]
    
    if "ETF" not in target_sectors:
        target_sectors.append("ETF")

    all_etfs = set()
    for sector in target_sectors:
        try:
            codes = xtdata.get_stock_list_in_sector(sector)
            if codes:
                for c in codes:
                    # 51xxxx, 56xxxx, 58xxxx (SH) or 15xxxx, 16xxxx (SZ)
                    # 增加 15, 16, 501 常用基金前缀
                    if c.startswith(('51', '56', '58', '15', '16', '501')):
                        all_etfs.add(c)
        except Exception:
            pass

    etf_codes = sorted(list(all_etfs))
    if not etf_codes:
        print("⚠️ 未发现符合特征的 ETF 代码。")
        return

    print(f"🎯 提取到 {len(etf_codes)} 只 ETF，正在匹配名称...")

    # 4. 加载名称映射 (instrument_master.csv)
    name_map = {}
    if os.path.exists(MASTER_CSV):
        try:
            df_master = pd.read_csv(MASTER_CSV)
            if 'code' in df_master.columns and 'name' in df_master.columns:
                name_map = dict(zip(df_master['code'], df_master['name']))
                print(f"📖 已加载 {len(name_map)} 条本地名称映射。")
        except Exception as e:
            print(f"⚠️ 读取 {MASTER_CSV} 失败: {e}")
    
    # 5. 构建丰富后的列表 (带 API 回退)
    enriched_list = []
    fallback_count = 0
    
    for code in etf_codes:
        name = name_map.get(code)
        
        # 如果 CSV 里没有，尝试从 API 获取 (Fallback)
        if not name:
            try:
                detail = xtdata.get_instrument_detail(code)
                if detail:
                    name = detail.get('InstrumentName')
                    fallback_count += 1
            except Exception:
                pass
        
        if not name:
            name = "未知名称"
            
        enriched_list.append({
            'code': code,
            'name': name
        })

    if fallback_count > 0:
        print(f"💡 通过 API 补充获取了 {fallback_count} 个名称。")

    # 6. 保存到 YAML
    result = {
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(enriched_list),
        'etf_list': enriched_list
    }
    
    try:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(result, f, allow_unicode=True, sort_keys=False)
        print(f"💾 列表已保存至: {OUTPUT_FILE}")
    except Exception as e:
        print(f"❌ 保存文件失败: {e}")

if __name__ == "__main__":
    get_etf_list()
