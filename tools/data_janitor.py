# -*- coding: utf-8 -*-
"""
数据清洗机器人 (Data Janitor)
功能: 扫描 Market_Daily 文件夹，物理删除不在 CSV 白名单中的污染文件
"""
import os
import pandas as pd

# ================= 配置区 =================
DATA_DIR = r"Z:\QuantpC_Workspace\Data\Market_Daily"
CSV_PATH = r"Z:\QuantpC_Workspace\Data\instrument_master.csv"
# ==========================================

def run_janitor():
    print("🧹 启动数据清洗机器人 (Data Janitor)...")
    
    if not os.path.exists(CSV_PATH):
        print("❌ 找不到 CSV 白名单，请先运行数据同步引擎。")
        return
        
    if not os.path.exists(DATA_DIR):
        print("❌ 找不到 Market_Daily 文件夹。")
        return

    # 1. 提取 5191 个绝对干净的底层资产代码
    df = pd.read_csv(CSV_PATH)
    
    # 构建合法文件名集合 (兼顾带点和带下划线的两种命名可能)
    valid_filenames = set()
    for code in df['code']:
        valid_filenames.add(f"{code}.parquet")
        valid_filenames.add(f"{code.replace('.', '_')}.parquet")

    # 2. 遍历数据仓库，执行物理斩杀
    deleted_count = 0
    retained_count = 0
    
    print(f"🗑️ 正在扫描 {DATA_DIR} 中的冗余数据，准备执行物理抹除...")
    
    for filename in os.listdir(DATA_DIR):
        if not filename.endswith('.parquet'):
            continue
            
        if filename not in valid_filenames:
            file_path = os.path.join(DATA_DIR, filename)
            try:
                os.remove(file_path)
                deleted_count += 1
            except Exception as e:
                print(f"⚠️ 无法删除 {filename}: {e}")
        else:
            retained_count += 1

    print("-" * 40)
    print(f"✅ 物理超度完成！")
    print(f"🛡️ 成功保留有效标的: {retained_count} 只")
    print(f"🔥 无情销毁衍生品垃圾: {deleted_count} 个")
    print("-" * 40)
    print("🎯 数据仓库已被彻底净化，您可以安全启动 Sniper 雷达了。")

if __name__ == "__main__":
    run_janitor()