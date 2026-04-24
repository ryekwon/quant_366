import pandas as pd
import numpy as np
import yaml
import json
import os
import sys
from datetime import datetime
from xtquant import xtdata

# =============================================================================
# 1. 策略配置
# =============================================================================
SYMBOL_POOL = [
    '510300.SH', # 沪深300
    '513500.SH', # 标普500
    '513100.SH', # 纳斯达克100
    '159915.SZ', # 创业板
    '512890.SH', # 红利低波ETF
    '518880.SH', # 黄金ETF
    '511260.SH'  # 国债ETF (避险标的)
]

SAFE_ASSET      = '511260.SH'
MOMENTUM_WINDOW = 20
KAMA_PERIOD     = 10
MA120_PERIOD    = 120
HIST_LOOKBACK   = 135  # 略大于 120 以对齐指标
_DIR       = os.path.dirname(os.path.abspath(__file__))
STATE_DIR  = os.path.join(_DIR, ".state")
OUTPUT_FILE = os.path.join(STATE_DIR, "rotation_targets.yaml")

# =============================================================================
# 2. 核心数学函数
# =============================================================================
def calculate_kama(series, n=10, fast=2, slow=30):
    """Kaufman's Adaptive Moving Average (KAMA)"""
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    change     = series.diff(n).abs()
    volatility = series.diff(1).abs().rolling(window=n).sum()
    er  = (change / volatility).fillna(0)
    sc  = (er * (fast_sc - slow_sc) + slow_sc) ** 2
    kama = series.copy()
    for i in range(n, len(series)):
        kama.iloc[i] = kama.iloc[i-1] + sc.iloc[i] * (series.iloc[i] - kama.iloc[i-1])
    return kama

# =============================================================================
# 3. 信号引擎
# =============================================================================
def run_rotation_master():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 ETF 轮动信号机：全量扫描 + 绝对动量政审启动...")

    # A. 获取历史数据
    xtdata.download_history_data2(SYMBOL_POOL, period='1d', start_time='20240101')
    raw_hist = xtdata.get_market_data_ex(
        field_list=['close'], stock_list=SYMBOL_POOL,
        period='1d', count=HIST_LOOKBACK
    )

    # ── 🛡️ [动态防火墙] 联合封锁 T0 领地 ─────────────────────────────
    T0_TARGETS_FILE = os.path.join(STATE_DIR, "grid_targets.yaml")
    T0_STATE_FILE   = os.path.join(STATE_DIR, "grid_state.json")
    t0_exclusive_set = set()

    if os.path.exists(T0_TARGETS_FILE):
        try:
            with open(T0_TARGETS_FILE, 'r', encoding='utf-8') as f:
                t0_targets = yaml.safe_load(f) or {}
                t0_exclusive_set.update(t0_targets.keys())
        except Exception as e:
            print(f"⚠️ 读取 T0 目标池失败: {e}")

    if os.path.exists(T0_STATE_FILE):
        try:
            with open(T0_STATE_FILE, 'r', encoding='utf-8') as f:
                t0_state = json.load(f) or {}
                t0_exclusive_set.update(t0_state.keys())
        except Exception as e:
            print(f"⚠️ 读取 T0 物理状态失败: {e}")

    print(f"🛡️ [动态防火墙] 已联合锁定 T0 领地: {list(t0_exclusive_set)}")

    # B. 批量拉取实时 Tick
    live_ticks = xtdata.get_full_tick(SYMBOL_POOL)

    # ── 阶段 1：构建融合数据映射（全量评分，不做逐标的剔除）──────────
    # 改造说明：原逻辑用 is_valid 逐条跑 MA120/KAMA/Momentum 三层关卡过滤。
    # 新逻辑：全量计算 score，放入 latest_data_map，只做日志诊断，不做剔除。
    # 最终由"绝对动量政审"在 Top1 层面一刀判断：若最强者亦跌破 MA120，全量避险。
    latest_data_map = {}   # code → {last_price, ma120, kama, score, ret_20d}
    stats_list      = []   # 全候选列表，用于排序

    for code in SYMBOL_POOL:
        if code in t0_exclusive_set:
            print(f"🚧 {code} | 拦截：处于 T0 活跃领地/孤儿列表，动态避让。")
            continue

        try:
            if code not in raw_hist:
                print(f"⚠️ {code} | 历史数据缺失，跳过。")
                continue
            hist_series = raw_hist[code]['close']

            # 注入实时价格（Tick 无效时回退历史末值）
            last_price = 0
            if code in live_ticks:
                last_price = live_ticks[code].get('lastPrice', 0)
            if last_price <= 0:
                last_price = float(hist_series.iloc[-1])
                print(f"⚠️ {code} 无实时 Tick，回退使用历史收盘价: {last_price}")

            # 混合序列（历史 + 今日实时末尾追加）
            full_series = pd.concat(
                [hist_series, pd.Series([last_price])]
            ).reset_index(drop=True)

            # 指标计算
            ma120       = float(full_series.rolling(window=MA120_PERIOD).mean().iloc[-1])
            kama_series = calculate_kama(full_series, n=KAMA_PERIOD)
            latest_kama = float(kama_series.iloc[-1])
            ret_20d     = float((last_price / full_series.iloc[-21]) - 1)
            daily_rets  = full_series.pct_change().tail(MOMENTUM_WINDOW)
            vol_20d     = float(daily_rets.std() * np.sqrt(252))
            score       = ret_20d / vol_20d if vol_20d > 0 else -1.0

            # 落盘融合映射
            latest_data_map[code] = {
                'last_price': float(last_price),
                'ma120':      ma120,
                'kama':       latest_kama,
                'score':      float(score),
                'ret_20d':    ret_20d,
            }
            stats_list.append({'code': code, 'score': float(score)})

            # 诊断日志（仅告知，不做剔除）
            trend_tag = "✅ 趋势健康" if last_price >= ma120 else "⚠️ 跌破半年线"
            print(
                f"{trend_tag} {code} "
                f"| Score: {score:.4f} "
                f"| Price/MA120: {last_price/ma120:.2%} "
                f"| Ret20d: {ret_20d:.2%}"
            )

        except Exception as e:
            print(f"🔥 处理 {code} 异常: {e}")

    # ── 阶段 2：绝对动量政审 — 取 Top 1，一刀判断 ────────────────────
    if not stats_list:
        print("⚠️ [轮动] 全池数据不可用，强制切入避险资产。")
        selected = [SAFE_ASSET]
    else:
        df_rank  = pd.DataFrame(stats_list).sort_values(by='score', ascending=False)
        top_code = df_rank.iloc[0]['code']
        top_data = latest_data_map[top_code]

        if top_data['last_price'] < top_data['ma120']:
            # 动量最强的标的尚且跌破半年线 → 全球风险资产均在衰退通道
            print(
                f"\n🚨 [绝对动量熔断] 最强标的 {top_code} 亦跌破半年线"
                f"（当前 {top_data['last_price']:.3f} < MA120 {top_data['ma120']:.3f}）"
            )
            print("🛡️ 全球风险资产均陷入衰退通道，启动全量避险。")
            selected = [SAFE_ASSET]
        else:
            print(
                f"\n✅ [轮动击发] 最强标的 {top_code} 趋势健康"
                f"（{top_data['last_price']:.3f} / MA120 {top_data['ma120']:.3f}），准备推入。"
            )
            selected = [top_code]

    # 兜底：selected 为空时强制国债
    if not selected:
        selected = [SAFE_ASSET]

    # ── 保存结果 ──────────────────────────────────────────────────────
    result = {
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'strategy':    'Sharpe Momentum + 绝对动量政审 (Top1)',
        'targets':     selected,
    }

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(result, f, allow_unicode=True)

    print(f"\n🎯 信号生成完毕！最终目标: {selected}")
    print(f"💾 状态文件已更新: {OUTPUT_FILE}")


if __name__ == "__main__":
    run_rotation_master()
