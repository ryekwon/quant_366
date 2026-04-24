# -*- coding: utf-8 -*-
"""
sniper_t2_dip_backtest.py
===================================================
T+2 低吸反弹回测 — 创业板全量 3 年历史
===================================================
策略逻辑：
  T 日：涨幅 > 15%（强势突破）
  T+1：跌幅 > 3%（游资离场/获利了结）
  T+2：收盘价 < T+1 收盘价（继续下行锁定恐慌底）
  信号：T+2 尾盘买入（以 T+2 收盘价模拟）
  持有：3 个交易日，以 T+5 收盘价卖出

输出：
  - 胜率 (Win Rate)
  - 数学期望 (Expected Value)
  - 盈亏比、平均收益等统计
"""

import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

# ─── 参数 ─────────────────────────────────────────────
T_GAIN_THRESH   =  0.15   # T 日涨幅阈值（>15%）
T1_DROP_THRESH  = -0.03   # T+1 跌幅阈值（<-3%）
HOLD_DAYS       =  3      # 持有天数
YEARS_BACK      =  3      # 回测年数
COST_RATE       =  0.001  # 单边万一印花税+佣金（约合 0.1%）
# ──────────────────────────────────────────────────────

def get_gem_codes():
    """从 xtdata 获取创业板（300/301开头）全量标的"""
    try:
        from xtquant import xtdata
        sector_list = xtdata.get_sector_list()
        # 寻找创业板板块
        gem_sectors = [s for s in sector_list if '创业板' in s and '指数' not in s]
        print(f"   找到创业板板块: {gem_sectors}")

        all_codes = set()
        if gem_sectors:
            for s in gem_sectors:
                codes = xtdata.get_stock_list_in_sector(s)
                all_codes.update(codes)

        # 补充：直接按代码前缀过滤，防止板块接口遗漏
        all_instruments = xtdata.get_stock_list_in_sector('全部A股') or []
        gem_by_prefix = [c for c in all_instruments
                         if c.startswith('300') or c.startswith('301')]
        all_codes.update(gem_by_prefix)

        result = sorted(all_codes)
        print(f"   创业板标的总计: {len(result)} 只")
        return result
    except Exception as e:
        print(f"❌ xtdata 获取标的失败: {e}")
        return []


def fetch_daily_data(codes: list, start: str, end: str) -> dict:
    """批量拉取日线数据，返回 {code: DataFrame(close, pct_chg)}"""
    try:
        from xtquant import xtdata
        # 分批拉取，避免一次性请求太大导致超时
        BATCH = 200
        all_close = []

        for i in range(0, len(codes), BATCH):
            batch = codes[i:i+BATCH]
            print(f"   拉取中 {i+1}~{min(i+BATCH,len(codes))}/{len(codes)}...", end='\r')
            raw = xtdata.get_market_data(
                field_list=['close'],
                stock_list=batch,
                period='1d',
                start_time=start,
                end_time=end,
                count=-1,
                dividend_type='front'
            )
            c = raw.get('close')        # shape: (n_codes, n_dates)
            if c is not None and not c.empty:
                all_close.append(c)

        if not all_close:
            print("❌ 未获取到日线数据")
            return {}

        # 合并：行=代码，列=日期字符串 → 转置 → 行=日期，列=代码
        close_wide = pd.concat(all_close, axis=0)   # (all_codes, dates)
        close_t    = close_wide.T                    # (dates, all_codes)
        close_t.index = pd.to_datetime(close_t.index, format='%Y%m%d')
        close_t = close_t.sort_index()

        result = {}
        for code in close_t.columns:
            series = close_t[code].dropna()
            if len(series) < 20:
                continue
            df = pd.DataFrame({'close': series})
            df['pct_chg'] = df['close'].pct_change()
            result[code] = df

        print(f"\n   有效数据标的: {len(result)} 只")
        return result

    except Exception as e:
        import traceback
        print(f"❌ 数据拉取失败: {e}")
        traceback.print_exc()
        return {}



def run_backtest(data_dict: dict) -> pd.DataFrame:
    """核心回测逻辑"""
    trades = []

    for code, df in data_dict.items():
        df = df.copy().reset_index(drop=False)
        # index 列就是日期
        date_col = df.columns[0]
        df = df.rename(columns={date_col: 'date'})

        n = len(df)
        if n < 6:
            continue

        for i in range(n - 5):
            # T 日
            t_pct = df.loc[i, 'pct_chg']
            if pd.isna(t_pct) or t_pct <= T_GAIN_THRESH:
                continue

            # T+1
            t1_pct = df.loc[i+1, 'pct_chg']
            if pd.isna(t1_pct) or t1_pct >= T1_DROP_THRESH:
                continue

            # T+2: 收盘价 < T+1 收盘价
            t1_close = df.loc[i+1, 'close']
            t2_close = df.loc[i+2, 'close']
            if pd.isna(t1_close) or pd.isna(t2_close):
                continue
            if t2_close >= t1_close:
                continue

            # 入场：T+2 收盘价买入，T+5 收盘价卖出
            if i + 2 + HOLD_DAYS >= n:
                continue
            exit_close = df.loc[i + 2 + HOLD_DAYS, 'close']
            if pd.isna(exit_close) or t2_close <= 0:
                continue

            raw_return = (exit_close / t2_close - 1)
            net_return = raw_return - COST_RATE * 2   # 双边成本

            trades.append({
                'code':         code,
                'T_date':       df.loc[i, 'date'],
                'T_pct':        round(t_pct * 100, 2),
                'T1_pct':       round(t1_pct * 100, 2),
                'entry_price':  round(t2_close, 3),
                'exit_price':   round(exit_close, 3),
                'raw_return':   round(raw_return * 100, 2),
                'net_return':   round(net_return * 100, 2),
                'win':          net_return > 0
            })

    return pd.DataFrame(trades)


def print_report(df: pd.DataFrame):
    """打印回测统计报告"""
    if df.empty:
        print("⚠️ 无有效交易记录")
        return

    total    = len(df)
    wins     = df['win'].sum()
    losses   = total - wins
    win_rate = wins / total

    avg_win  = df.loc[df['win'],  'net_return'].mean()
    avg_loss = df.loc[~df['win'], 'net_return'].mean()

    # 数学期望 = win_rate × avg_win + (1-win_rate) × avg_loss
    ev = win_rate * avg_win + (1 - win_rate) * avg_loss

    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
    avg_return    = df['net_return'].mean()
    median_return = df['net_return'].median()
    std_return    = df['net_return'].std()

    print("\n" + "=" * 60)
    print("  T+2 低吸反弹策略 — 创业板 3 年回测报告")
    print("=" * 60)
    print(f"  策略条件：T涨幅>{T_GAIN_THRESH*100:.0f}% | T+1跌幅>{abs(T1_DROP_THRESH)*100:.0f}% | T+2收更低")
    print(f"  持有天数：{HOLD_DAYS} 个交易日   成本假设：单边 {COST_RATE*100:.1f}%")
    print("-" * 60)
    print(f"  总触发次数：{total:,} 次")
    print(f"  盈利次数：  {wins:,} 次  | 亏损次数：{losses:,} 次")
    print(f"\n  ★ 胜率 (Win Rate)：         {win_rate*100:.2f}%")
    print(f"  ★ 数学期望 (EV)：           {ev:+.3f}%  每次交易")
    print("-" * 60)
    print(f"  平均盈利：   {avg_win:+.2f}%")
    print(f"  平均亏损：   {avg_loss:+.2f}%")
    print(f"  盈亏比：     {profit_factor:.2f}x")
    print(f"  平均净收益： {avg_return:+.2f}%")
    print(f"  中位数收益： {median_return:+.2f}%")
    print(f"  收益标准差： {std_return:.2f}%")
    print("=" * 60)

    # 按收益分布
    bins  = [-999, -10, -5, -3, 0, 3, 5, 10, 999]
    label = ['<-10%', '-10~-5%', '-5~-3%', '-3~0%', '0~3%', '3~5%', '5~10%', '>10%']
    df['bucket'] = pd.cut(df['net_return'], bins=bins, labels=label)
    dist = df['bucket'].value_counts().reindex(label)
    print("\n  收益分布：")
    for lbl, cnt in dist.items():
        bar = '█' * int(cnt / max(dist) * 30)
        print(f"  {lbl:>10}: {cnt:>5} 次  {bar}")
    print("=" * 60)

    # 保存明细
    out_path = r"Z:\QuantpC_Workspace\Quant_Pilot\logs\t2_dip_backtest_detail.csv"
    df.to_csv(out_path, index=False, encoding='utf-8-sig')
    print(f"\n  明细已保存 → {out_path}")


if __name__ == '__main__':
    print("=" * 60)
    print("  T+2 低吸反弹策略 — 创业板 3 年回测")
    print("=" * 60)

    end_date   = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=365 * YEARS_BACK + 30)).strftime('%Y%m%d')
    print(f"  回测区间: {start_date} ~ {end_date}")

    print("\n[Step 1] 获取创业板标的清单...")
    codes = get_gem_codes()
    if not codes:
        sys.exit(1)

    print("\n[Step 2] 拉取日线数据...")
    data = fetch_daily_data(codes, start_date, end_date)
    if not data:
        sys.exit(1)

    print(f"\n[Step 3] 运行回测逻辑（{YEARS_BACK} 年 × {len(data)} 只标的）...")
    result_df = run_backtest(data)

    print_report(result_df)
