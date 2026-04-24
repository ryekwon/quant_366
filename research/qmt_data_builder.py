#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 运行方式: python qmt_data_builder.py
# (需使用安装了 xtquant、pandas、pyarrow 的系统 Python 环境)
"""
QMT 历史日频数据构建脚本
用法: python qmt_data_builder.py
"""
from xtquant import xtdata
import pandas as pd
import os
import time

# ================= 核心配置区 =================
# 强制写入你的 NVMe 固态硬盘路径
NVME_PATH = r"Z:\QuantpC_Workspace\Data\Market_Daily"
START_DATE = "20120101"
END_DATE = "20260222"
BATCH_SIZE = 200  # 每次向服务器请求的股票池大小，防止限流
# ==============================================


def build_core_database():
    os.makedirs(NVME_PATH, exist_ok=True)

    print("⏳ 正在连接 miniQMT 客户端...")
    # 唤醒并订阅全市场 A 股
    stock_list = xtdata.get_stock_list_in_sector('沪深A股')
    # 过滤北交所(8开头/4开头)，保留沪深主板和创业板 (00, 30, 60开头)
    stock_list = [s for s in stock_list if s.split('.')[0].startswith(('00', '30', '60'))]
    print(f"🎯 获取到有效 A 股标的: {len(stock_list)} 只")

    print(f"📥 开始分批拉取历史数据 ({START_DATE} 至 {END_DATE})...")
    # 1. 物理下载阶段 (存入 QMT 安装目录的本地缓存)
    for i in range(0, len(stock_list), BATCH_SIZE):
        batch = stock_list[i:i + BATCH_SIZE]
        print(f"   正在下载进度: {i}/{len(stock_list)} ...")
        # FIX: 第一个参数为下载目标代码（空串表示批量），第二个为列表
        # period='1d' 为日频。必须逐批下载，不可省略此步骤
        xtdata.download_history_data2(
            stock_list=batch,
            period='1d',
            start_time=START_DATE,
            end_time=END_DATE,
        )
        time.sleep(1)  # 尊重服务器，防止被强踢

    print("🧮 物理下载完毕，开始提取数据并重构为 Parquet 写入 NVMe...")
    # 2. 数据提取与重组阶段
    success_count = 0
    for code in stock_list:
        try:
            # get_market_data_ex 提取缓存数据，dividend_type='front' 表示使用前复权
            # 必须使用前复权，这样利用当前的流通股本计算历史换手率才在数学上成立
            data_dict = xtdata.get_market_data_ex(
                field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
                stock_list=[code],
                period='1d',
                start_time=START_DATE,
                end_time=END_DATE,
                dividend_type='front'
            )

            df = data_dict.get(code)
            if df is None or df.empty:
                continue

            # FIX: 日频索引格式为 '%Y%m%d'（8位），而非14位时间戳
            df.index = pd.to_datetime(df.index.astype(str), format='%Y%m%d', errors='coerce').date
            df.index.name = 'date'

            # 提取基本面静态数据 (获取当前的流通股本)
            # QMT 中 FloatVolume 单位通常为股
            info = xtdata.get_instrument_detail(code)
            float_shares = info.get('FloatVolume', None) if info else None

            # 计算核心衍生因子
            df['pct_change'] = df['close'].pct_change() * 100

            if float_shares and float_shares > 0:
                # 换手率 = 成交量 / 流通股本
                df['turnover_rate'] = (df['volume'] / float_shares) * 100
                # 流通市值 (亿元) = 收盘价 * 流通股本 / 1亿
                df['float_market_cap'] = (df['close'] * float_shares) / 1e8
            else:
                df['turnover_rate'] = 0.0
                df['float_market_cap'] = 0.0

            # 清理无用数据
            df = df.dropna(subset=['close'])

            # FIX: 只对数值列做 round，避免混合类型报错
            numeric_cols = df.select_dtypes(include='number').columns
            df[numeric_cols] = df[numeric_cols].round(4)

            # 直接写入 NVMe 硬盘
            save_path = os.path.join(NVME_PATH, f"{code.replace('.', '_')}.parquet")
            df.to_parquet(save_path, engine='pyarrow')
            success_count += 1

        except Exception as e:
            print(f"❌ 处理 {code} 时出错: {e}")

    print(f"✅ NVMe 底层数据构建彻底完成！共成功落盘 {success_count} 只股票。")


if __name__ == "__main__":
    build_core_database()
