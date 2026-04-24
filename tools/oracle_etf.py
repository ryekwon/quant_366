from xtquant import xtdata
import pandas as pd

def refine_core_universe():
    # 初始种子池（涵盖宽基、行业、跨境、商品）
    raw_pool = [
        "510300.SH", "510500.SH", "159915.SZ", "588000.SH", # 宽基
        "513100.SH", "513500.SH", "513050.SH", "159941.SZ", # 跨境
        "518880.SH", "511260.SH", "159981.SZ", "159985.SZ", # 商品/债
        "512800.SH", "512000.SH", "512760.SH", "512660.SH", # 金融/芯片/军工
        "515050.SH", "512480.SH", "512170.SH", "512880.SH", # 5G/半导体/医疗/证券
        # ... 继续补全至40只
    ]
    
    # 获取最近 5 天的成交数据
    xtdata.download_history_data2(raw_pool, period='1d', count=5)
    df_vols = xtdata.get_market_data_ex(field_list=['amount'], stock_list=raw_pool, period='1d', count=5)
    
    refined_pool = []
    for code in raw_pool:
        avg_amount = df_vols[code]['amount'].mean()
        if avg_amount > 100_000_000: # 物理阈值：日均成交额 > 1亿
            refined_pool.append(code)
        else:
            print(f"🚫 剔除流动性不足标的: {code} (日均成交额: {avg_amount/1e8:.2f}亿)")
            
    print(f"✅ 最终精选宇宙共 {len(refined_pool)} 只。")
    return refined_pool