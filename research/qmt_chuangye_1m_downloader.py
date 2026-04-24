# -*- coding: utf-8 -*-
"""
QMT 创业板 (300) 1m K线 增量下载器
架构: CSV读取 300 标的 -> 断点检测 -> 增量下载 -> 合并去重 -> 覆写 Parquet
保存路径: Z:\QuantpC_Workspace\Data\Market300_Minute
"""
import os
import sys
import pandas as pd
from xtquant import xtdata
from datetime import datetime

# Windows GBK 终端兼容：强制 stdout 输出为 UTF-8
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# =================== 路径配置 ===================
MASTER_CSV      = r"Z:\QuantpC_Workspace\Data\instrument_master.csv"
SAVE_DIR        = r"Z:\QuantpC_Workspace\Data\Market300_Minute"
FULL_START_DATE = '20240101'   # 首次全量下载起始日
# ================================================

os.makedirs(SAVE_DIR, exist_ok=True)


def get_chuangye_targets():
    """从 instrument_master.csv 中筛选 300 开头的创业板标的"""
    if not os.path.exists(MASTER_CSV):
        print(f"❌ 找不到 {MASTER_CSV}")
        return []

    df = pd.read_csv(MASTER_CSV, usecols=['code'])
    targets = [
        code for code in df['code'].astype(str)
        if code.startswith('300')
    ]
    print(f"📋 从 instrument_master.csv 提取创业板 (300): {len(targets)} 只")
    return targets


def download_and_save_1m_data():
    # ── 环境预检 ──────────────────────────────────────────────────
    try:
        test_tick = xtdata.get_full_tick(['000001.SH'])
        if not test_tick:
            print("⚠️  Warning: 无法连接行情快照，请检查 QMT 登录状态。")
    except Exception as e:
        print(f"❌ 环境预检失败: {e}")

    targets = get_chuangye_targets()
    if not targets:
        print("⚠️ 目标池为空，退出。")
        return

    end_date = datetime.now().strftime('%Y%m%d')
    print(f"🚀 启动创业板 1m 增强同步引擎... 起始设定: {FULL_START_DATE} | 目标: {len(targets)} 只\n")

    for idx, code in enumerate(targets, 1):
        file_name = f"{code.replace('.', '_')}_1m.parquet"
        file_path = os.path.join(SAVE_DIR, file_name)

        # ── 智能断点/补全逻辑 ──────────────────────────────────────────
        df_old    = None
        old_count = 0
        start_time = FULL_START_DATE
        op_type    = "全量/补历史"

        if os.path.exists(file_path):
            try:
                df_old    = pd.read_parquet(file_path)
                old_count = len(df_old)
                if not df_old.empty:
                    last_ts    = pd.to_datetime(df_old.index).max()
                    first_ts   = pd.to_datetime(df_old.index).min()
                    
                    # 如果设定的起始日期比文件里最老的数据还要早，则需要从设定日期开始下载进行补全
                    if FULL_START_DATE < first_ts.strftime('%Y%m%d'):
                        start_time = FULL_START_DATE
                        op_type    = "追加历史"
                    else:
                        # 正常的增量更新：从最后一条往前倒 1 天
                        start_time = (last_ts - pd.Timedelta(days=1)).strftime('%Y%m%d')
                        op_type    = "增量更新"
            except Exception:
                df_old = None

        # ── 下载逻辑 ──────────────────────────────────────────────────
        try:
            print(f"[{idx}/{len(targets)}] {code} ({op_type}) 从 {start_time}: ", end='', flush=True)
            
            # 使用同步下载，确保数据落到客户端本地缓存
            xtdata.download_history_data2([code], period='1m', start_time=start_time, end_time=end_date)
            
            # 从客户端提取数据到本地脚本内存
            data_dict = xtdata.get_market_data_ex(
                field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
                stock_list=[code],
                period='1m',
                start_time=start_time,
                end_time=end_date
            )
            
            if code not in data_dict or data_dict[code].empty:
                print("⚠️  无响应数据")
                continue

            df_new = data_dict[code]
            df_new.index.name = 'time'

            # ── 合并与去重 ────────────────────────────────────────────────
            if df_old is not None and not df_old.empty:
                df_old.index.name = 'time'
                df = pd.concat([df_old, df_new])
            else:
                df = df_new

            df = df[~df.index.duplicated(keep='last')]
            df = df.sort_index()

            # ── 存盘 ────────────────────────────────────────────────────
            df.to_parquet(file_path, compression='snappy')
            add_c = len(df) - old_count
            print(f"✅ +{add_c} 根 (Total: {len(df)})")

        except Exception as e:
            print(f"🔥 出错: {e}")
            continue

    print("\n🏁 创业板同步任务顺利结束。")


if __name__ == "__main__":
    download_and_save_1m_data()
