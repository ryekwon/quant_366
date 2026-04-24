# -*- coding: utf-8 -*-
"""
QMT 1m K线 增量下载器 (极简定向狙击版)
架构: 仅读取 YAML 核心目标池 -> 断点检测 -> 并发批处理 -> 落盘
已切除: 彻底删除了全市场 CSV 兜底逻辑，严防无用数据污染和内存撑爆。
"""
import os
import sys
import yaml
import time
import pandas as pd
from xtquant import xtdata
from datetime import datetime, timedelta

# Windows GBK 终端兼容：强制 stdout 输出为 UTF-8
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# =================== 路径配置 ===================
_DIR               = os.path.dirname(os.path.abspath(__file__))
FIXED_TARGETS_PATH = os.path.join(_DIR, ".state", "fixed_t0_target.yaml")
ORACLE_UNIVERSE    = os.path.join(_DIR, ".state", "oracle_v2_universe.json")
SAVE_DIR           = r"Z:\QuantpC_Workspace\Data\Market_Minute"
FULL_START_DATE    = '20230101'   # QMT 服务端最多返回近1年
# ================================================

os.makedirs(SAVE_DIR, exist_ok=True)

def get_targets_from_yaml():
    """
    定向读取目标池：支持多种格式以防格式对齐失败
    1. 优先尝试解析为 YAML (结构化字典或列表)
    2. 如果 YAML 解析出的不是预期结构，则回退到逐行文本解析 (适配 t0_master.py 的格式)
    """
    if not os.path.exists(FIXED_TARGETS_PATH):
        print(f"❌ 找不到核心目标池文件: {FIXED_TARGETS_PATH}，直接退出。")
        return []

    targets = set()
    try:
        with open(FIXED_TARGETS_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # --- 策略 A: 尝试标准 YAML 解析 ---
        try:
            data = yaml.safe_load(content)
            if isinstance(data, dict) and 'targets' in data:
                raw_list = data['targets']
            elif isinstance(data, list) and len(data) > 0 and not isinstance(data[0], str):
                # 如果列表项不是字符串，可能是复杂的 YAML 结构
                raw_list = data
            elif isinstance(data, list):
                raw_list = data
            else:
                raw_list = []
                
            for item in raw_list:
                code = str(item).split(',')[0].strip()
                if code and ('.' in code): # 简单校验是否像代码
                    targets.add(code)
        except Exception:
            pass # YAML 解析失败则进入策略 B

        # --- 策略 B: 物理兜底解析 (逐行扫描，适配 CSV/txt 风格) ---
        if not targets:
            lines = content.splitlines()
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # 处理 159985.SZ,名字 格式
                code = line.split(',')[0].strip()
                if code and ('.' in code):
                    targets.add(code)

        target_list = list(targets)
        if target_list:
            print(f"🎯 [精确制导] 从配置锁定核心交易标的: {len(target_list)} 只 -> {target_list}")
            return target_list
        else:
            print("📭 目标池文件中未提取到有效标的，请检查格式。")
            return []
            
    except Exception as e:
        print(f"🚨 读取目标池失败: {e}")
        return []


def get_targets_from_oracle_universe() -> list:
    """
    读取 .state/oracle_v2_universe.json（达尔文机制输出），
    提取全量宏观 ETF 宇宙标的代码列表。
    """
    if not os.path.exists(ORACLE_UNIVERSE):
        print(f"ℹ️  [OracleUniverse] 文件不存在，跳过: {ORACLE_UNIVERSE}")
        return []
    try:
        import json
        with open(ORACLE_UNIVERSE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        codes = [item['code'] for item in data.get('universe', []) if item.get('code')]
        print(f"🌌 [OracleUniverse] 从宏观 ETF 宇宙锁定 {len(codes)} 只标的")
        return codes
    except Exception as e:
        print(f"⚠️  [OracleUniverse] 读取失败: {e}")
        return []


def download_and_save_1m_data():
    # ── 合并两个来源（取并集）──────────────────────────────────────────
    yaml_codes   = get_targets_from_yaml()
    oracle_codes = get_targets_from_oracle_universe()
    targets = list(set(yaml_codes) | set(oracle_codes))

    if not targets:
        print("🛑 无核心目标，1m 下载引擎物理静默。")
        return

    print(f"📋 合并后总目标: {len(targets)} 只"
          f"（T0={len(yaml_codes)}, ETF宇宙={len(oracle_codes)}, 去重后={len(targets)}）")

    end_date = datetime.now().strftime('%Y%m%d')
    start_time_log = time.time()
    print(f"\n🚀 启动 1m 高频数据同步引擎 (极简定向版)...")

    # ── 1. 智能水库水位探测 ──
    full_targets = []
    inc_targets = []
    
    for code in targets:
        file_path = os.path.join(SAVE_DIR, f"{code.replace('.', '_')}_1m.parquet")
        if os.path.exists(file_path):
            inc_targets.append(code)
        else:
            full_targets.append(code)

    # ── 2. 物理网络层：定向请求与休眠 ──
    if full_targets:
        print(f"🌊 检测到 {len(full_targets)} 只标的缺失 1m 底座，触发【全量下载】...")
        xtdata.download_history_data2(stock_list=full_targets, period='1m', start_time=FULL_START_DATE, end_time=end_date)
        print("⏳ 强制休眠 30 秒，等待 QMT 本地 C++ 彻底落盘...")
        time.sleep(30)

    if inc_targets:
        print(f"🔄 检测到 {len(inc_targets)} 只标的已有底座，触发【增量下载】...")
        sync_start = (datetime.now() - timedelta(days=5)).strftime('%Y%m%d')
        xtdata.download_history_data2(stock_list=inc_targets, period='1m', start_time=sync_start, end_time=end_date)
        print("⏳ 增量指令下发完毕，休眠 5 秒等待落盘...")
        time.sleep(5)

    # ── 3. 内存计算层：纯粹的数据提取与缝合 ──
    print("\n🧮 正在执行【无缝融合与落地】...")
    success_count = 0
    
    for code in targets:
        file_name = f"{code.replace('.', '_')}_1m.parquet"
        file_path = os.path.join(SAVE_DIR, file_name)

        query_start = FULL_START_DATE if code in full_targets else (datetime.now() - timedelta(days=5)).strftime('%Y%m%d')

        try:
            data_dict = xtdata.get_market_data_ex(
                field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
                stock_list=[code],
                period='1m',
                start_time=query_start,
                end_time=end_date
            )
        except Exception:
            continue

        if code not in data_dict or data_dict[code].empty:
            continue

        df_new = data_dict[code]
        
        # 强制格式化时间戳
        df_new.index = pd.to_datetime(df_new.index.astype(str), format='%Y%m%d%H%M%S', errors='coerce')
        df_new.index.name = 'datetime'
        df_new = df_new[df_new.index.notnull()] 

        # 时空缝合
        if code in inc_targets and os.path.exists(file_path):
            try:
                df_old = pd.read_parquet(file_path)
                df_old.index.name = 'datetime'
                df = pd.concat([df_old, df_new])
            except Exception:
                df = df_new
        else:
            df = df_new

        # 去重排序
        df = df[~df.index.duplicated(keep='last')]
        df = df.sort_index()

        # 落地
        df.to_parquet(file_path, compression='snappy')
        success_count += 1

    print(f"🏁 核心标的 1m 物理同步合龙！耗时: {time.time() - start_time_log:.2f} 秒。成功落地 {success_count} 个文件。")

if __name__ == "__main__":
    download_and_save_1m_data()