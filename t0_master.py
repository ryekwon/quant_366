#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = [
#   "pandas",
#   "numpy",
#   "pyarrow",
#   "pyyaml",
# ]
# ///

import os
import glob
import re
import time
import numpy as np
import pandas as pd
import yaml
import warnings
warnings.filterwarnings('ignore')

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from xtquant import xtdata
    _HAS_XTDATA = True
except ImportError:
    _HAS_XTDATA = False

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# ================= 物理路径与全局参数 =================
_DATA_ROOT      = os.getenv("DATA_DIR",    r"Z:\QuantpC_Workspace\Data\Market_Daily")
MINUTE_DATA_DIR = os.path.join(os.path.dirname(_DATA_ROOT), "Market_Minute")
MASTER_CSV      = os.getenv("MASTER_CSV",  r"Z:\QuantpC_Workspace\Data\instrument_master.csv")
OUTPUT_CSV      = os.path.join(os.path.dirname(_DATA_ROOT), "t0_optimal_parameters.csv")
_STATE_DIR      = os.getenv("STATE_DIR",   os.path.join(_PROJECT_ROOT, ".state"))
FINAL_YAML      = os.path.join(_STATE_DIR, "grid_targets.yaml")

COMMISSION_RATE = 0.00005
MIN_COMMISSION = 0.5

# 资金管理
TOTAL_CAPITAL = 300000          # 【任务1】总本金跃迁至 30 万
TARGET_ETF_COUNT = 5
MAX_LOTS_PER_ETF = 5

# 🛡️ 补丁1：时间护城河 —— 最少 12000 根 1分钟 K 线（约 50 个交易日）
MIN_BARS = 12000

# 【任务2】ATR 乘数网格参数池（替代原来的绝对 spread）
ATR_MULTIPLIERS = [0.2, 0.3, 0.5, 0.8, 1.2, 1.5]

# ================= 模块 0：固定 T0 标的池（从 fixed_t0_target.yaml 读取）=================
FIXED_T0_FILE = os.path.join(_STATE_DIR, "fixed_t0_target.yaml")
SELECTED_ETF_COUNT = TARGET_ETF_COUNT

def load_fixed_t0_pool() -> list:
    """从 fixed_t0_target.yaml 读取固定 T0 标的池。
    文件格式: 每行 `code,name`，如 `159985.SZ,华夏饲料豆粕期货etf`
    返回: [(code, name), ...]
    """
    if not os.path.exists(FIXED_T0_FILE):
        print(f"❌ [固定池] 找不到 {FIXED_T0_FILE}")
        return []
    try:
        pool = []
        with open(FIXED_T0_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(',', 1)
                code = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else code
                pool.append((code, name))
        print(f"✅ [固定池] 从 fixed_t0_target.yaml 加载了 {len(pool)} 只固定 T0 标的")
        return pool
    except Exception as e:
        print(f"❌ [固定池] 读取失败: {e}")
        return []




# ================= 模块 1：T0 状态机 (带趋势护城河 + ATR 自适应网格) =================
def run_t0_grid_v2(df, atr_multiplier, grid_volume=1000, initial_cash=100000.0, ma_window=14400):
    """
    【任务2】波动率自适应网格：
      - 利用 1 分钟数据合成日线 OHLC，计算 5 日 ATR 并除以收盘价得 ATR_5d_pct
      - ffill 到 1 分钟级别
      - 状态机内每根 K 线：dynamic_spread = ATR_5d_pct * atr_multiplier

    【任务3】额外返回 daily_returns，供 Kelly 分配器使用。
    """
    df = df.copy()

    # ──────────── 【任务2】计算 5 日 ATR（基于 1m 合成日线） ────────────
    df_daily = df['close'].resample('D').ohlc().dropna()
    df_daily.columns = ['open', 'high', 'low', 'close']

    # 真实波幅 True Range
    df_daily['prev_close'] = df_daily['close'].shift(1)
    df_daily['tr'] = df_daily[['high', 'prev_close']].max(axis=1) - \
                     df_daily[['low',  'prev_close']].min(axis=1)
    df_daily['atr5'] = df_daily['tr'].rolling(5).mean()
    df_daily['ATR_5d_pct'] = df_daily['atr5'] / df_daily['close']   # 归一化百分比

    # 将 ATR_5d_pct 前向填充到 1 分钟数据中
    df['date'] = df.index.normalize()
    atr_map = df_daily['ATR_5d_pct'].to_dict()
    df['ATR_5d_pct'] = df['date'].map(atr_map)
    df['ATR_5d_pct'] = df['ATR_5d_pct'].ffill()
    df.drop(columns=['date'], inplace=True)

    # ──────────── 初始化状态机 ────────────
    cash, holdings, total_trades = initial_cash, 0, 0
    daily_equity = {}
    
    current_date = None
    daily_open = 0.0
    day_buy_frozen = False

    df_valid = df.dropna(subset=['ATR_5d_pct'])
    if len(df_valid) < 1000:
        return None, None

    initial_price = df_valid['close'].iloc[0]
    initial_shares = int((initial_cash * 0.5) / initial_price / 100) * 100

    cash -= initial_shares * initial_price * (1 + COMMISSION_RATE)
    holdings += initial_shares
    base_price = initial_price

    for row in df_valid.itertuples():
        current_price = row.close
        current_time = row.Index
        
        if current_date != current_time.date():
            current_date = current_time.date()
            daily_open = current_price
            day_buy_frozen = False
            
        if current_price < daily_open * 0.97:
            day_buy_frozen = True

        # 【任务2】动态网格间距
        dynamic_spread = row.ATR_5d_pct * atr_multiplier
        if dynamic_spread <= 0:
            continue  # 防御：ATR 尚未就绪时跳过

        # 卖出逻辑 (无视牛熊)
        if current_price >= base_price * (1 + dynamic_spread):
            if holdings >= grid_volume:
                sell_value = grid_volume * current_price
                fee = max(sell_value * COMMISSION_RATE, MIN_COMMISSION)
                cash += sell_value - fee
                holdings -= grid_volume
                base_price = current_price
                total_trades += 1

        # 买入逻辑 (纯均值回归 + 防绞杀熔断)
        elif current_price <= base_price * (1 - dynamic_spread):
            if day_buy_frozen:
                pass  # 当日已触发防绞杀熔断，冻结买入权限
            else:
                buy_value = grid_volume * current_price
                fee = max(buy_value * COMMISSION_RATE, MIN_COMMISSION)
                if cash >= buy_value + fee:
                    cash -= buy_value + fee
                    holdings += grid_volume
                    base_price = current_price
                    total_trades += 1

        if current_time.hour == 15 and current_time.minute == 0:
            daily_equity[current_time.date()] = cash + holdings * current_price

    final_equity = cash + holdings * df_valid['close'].iloc[-1]
    equity_s = pd.Series(daily_equity)
    if len(equity_s) < 2:
        return None, None

    daily_returns = equity_s.pct_change().dropna()   # 【任务3】额外返回
    sharpe_ratio = np.sqrt(242) * daily_returns.mean() / (daily_returns.std() + 1e-9)
    total_return = (final_equity / initial_cash) - 1

    result = {
        "ATR_Multiplier": atr_multiplier,
        "Sharpe_Ratio": sharpe_ratio,
        "Total_Trades": total_trades,
        "Total_Return": total_return,
        "Final_Equity": final_equity,
    }
    return result, daily_returns


# ================= 模块 2：动态标签生成器 =================
def get_etf_tag(name):
    """通过正则清洗名字，分配资产族群标签，用于后期排重"""
    pure_name = re.sub(r'ETF|华夏|易方达|博时|南方|广发|富国|汇添富|国泰|华宝|天弘|联接|发起式|回报|增强|收益|嘉实|鹏华|工银|建信|交银|银华|LOF|（.*?）|\(.*\)', '', str(name)).strip()

    core_taxonomy = {
        '原油油气': ['油气', '原油', '能源', '石油'],
        '黄金': ['黄金'],
        '医药医疗': ['医药', '医疗', '生物', '创新药', '药'],
        '中概互联': ['中概', '互联', '恒生科技', '恒科', '新经济'],
        '纳斯达克': ['纳指', '纳斯达克'],
        '标普': ['标普'],
        '日经': ['日经'],
        '红利': ['红利', '高股息', '央企'],
        '欧洲': ['德国', '法国', '英国', '欧洲', '欧盟'],
        '亚太及其他': ['亚太', '韩国', '印度', '沙特', '东南亚', '道指', '道琼斯', '日元', '新兴']
    }

    for main_tag, keywords in core_taxonomy.items():
        if any(kw in pure_name for kw in keywords):
            return main_tag
    return pure_name



def kelly_allocate(top_df, daily_returns_map, total_capital):
    """
    【任务3/重构】半凯利公式动态资金分配 + 活跃度阻尼器 + 25%硬顶钳制
    """
    kelly_scores = {}
    for _, row in top_df.iterrows():
        code = row['Code']
        total_trades = row.get('Total_Trades', 0)
        dr = daily_returns_map.get(code)
        
        if dr is None or len(dr) < 5:
            kelly_scores[code] = 0.0
            continue
            
        var = dr.var()
        mean = dr.mean()
        
        # 1. 活跃度阻尼器 (Activity Damper)
        base_k = mean / var if var > 1e-12 else 0.0
        if base_k > 0:
            k_score = base_k * np.log10(total_trades + 1)
        else:
            k_score = 0.0
            
        kelly_scores[code] = k_score

    # 剔除 K <= 0
    positive_codes = [c for c, k in kelly_scores.items() if k > 0]
    
    # 异常回退：等权
    if not positive_codes:
        n = len(top_df)
        result = {}
        for _, row in top_df.iterrows():
            code = row['Code']
            result[code] = {
                'kelly_score': kelly_scores.get(code, 0.0),
                'weight': 1.0 / n,
                'allocated_capital': total_capital / n,
            }
        return result

    # 2. 初步归一化
    total_k = sum(kelly_scores[c] for c in positive_codes)
    raw_weights = {c: kelly_scores[c] / total_k for c in positive_codes}

    # 3. 绝对硬顶压制 (Hard Cap Constraint) - MAX 25%
    MAX_WEIGHT = 0.25
    final_weights = raw_weights.copy()
    
    while True:
        overflow_sum = 0.0
        capped_codes = []
        uncapped_codes = []
        
        for pcode, w in final_weights.items():
            if w > MAX_WEIGHT:
                overflow_sum += (w - MAX_WEIGHT)
                final_weights[pcode] = MAX_WEIGHT
                capped_codes.append(pcode)
            else:
                if w < MAX_WEIGHT:
                    uncapped_codes.append(pcode)
                else:
                    capped_codes.append(pcode) # Exactly 0.25

        # If there's no overflow OR everyone is somehow capped, we are done
        if overflow_sum <= 1e-9 or not uncapped_codes:
            break
            
        # Re-distribute overflow proportionally to uncapped codes
        uncapped_weight_sum = sum(final_weights[c] for c in uncapped_codes)
        
        if uncapped_weight_sum <= 1e-9:
            # 防御：剩下的人权重都是0，只能均分
            share = overflow_sum / len(uncapped_codes)
            for uc in uncapped_codes:
                final_weights[uc] += share
        else:
            for uc in uncapped_codes:
                final_weights[uc] += overflow_sum * (final_weights[uc] / uncapped_weight_sum)

    # 构建返回结果结构
    result = {}
    for _, row in top_df.iterrows():
        code = row['Code']
        w = final_weights.get(code, 0.0)
        result[code] = {
            'kelly_score': kelly_scores.get(code, 0.0),
            'weight': w,
            'allocated_capital': total_capital * w,
        }
    return result


# ================= 辅助函数：14 日 ATR 百分比 =================
def _calc_atr14_pct(df: pd.DataFrame, window: int = 14) -> float:
    """
    从 1m 分钟数据 DataFrame 合成日线，计算标准 14 日 ATR 百分比。
    ATR_14_pct = 14日均 TrueRange / 最近收盘价

    容错设计：任何异常均返回 0.0，由上层钳制器回退至安全默认值。
    """
    try:
        df_d = df['close'].resample('D').ohlc().dropna()
        df_d.columns = ['open', 'high', 'low', 'close']

        if len(df_d) < window + 1:
            return 0.0

        df_d['prev_close'] = df_d['close'].shift(1)
        df_d['tr'] = df_d.apply(lambda r: max(
            r['high'] - r['low'],
            abs(r['high'] - r['prev_close']) if r['prev_close'] > 0 else 0.0,
            abs(r['low']  - r['prev_close']) if r['prev_close'] > 0 else 0.0,
        ), axis=1)

        recent = df_d.tail(window)
        last_close = float(recent['close'].iloc[-1])
        if last_close <= 0:
            return 0.0

        atr_abs = float(recent['tr'].mean())
        atr_pct = atr_abs / last_close

        # 防御：ATR > 15% 通常是数据异常
        if atr_pct > 0.15:
            return 0.0
        return round(atr_pct, 6)

    except Exception:
        return 0.0


# ================= 模块 5：固定标的暴破 + MA60 宏观锁 =================
def build_master_pipeline():
    from datetime import datetime

    # 0. 加载固定标的池
    print("🔬 [固定选股] 从 fixed_t0_target.yaml 读取静态标的池...")
    print("=" * 90)
    fixed_pool = load_fixed_t0_pool()
    if not fixed_pool:
        print("🚨 [选拔失败] 固定池为空，T0 引擎休眠，不更新配置。")
        return

    # 1. 宏观锁已切除：全部放行
    print("\n🔓 [网格引擎] 固定池加载完毕，全员放行...")
    ma60_passed = fixed_pool

    # 2. 对放行标的进行暴力回测参数优化
    total_targets = len(ma60_passed)
    print(f"\n🌍 {total_targets} 个标的通过 MA60 阀门，启动参数暴破...")
    print(f"🛡️  时间护城河：跳过不足 {MIN_BARS:,} 根 K 线的新券")
    print(f"🌊 波动率自适应网格，ATR 乘数候选集: {ATR_MULTIPLIERS}\n")
    print("=" * 90)

    all_results = []
    daily_returns_store = {}

    for i, (code, name) in enumerate(ma60_passed, 1):
        fname = code.replace('.', '_') + '_1m.parquet'
        file_path = os.path.join(MINUTE_DATA_DIR, fname)
        t_start = time.time()

        if not os.path.exists(file_path):
            print(f"  [{i:>4}/{total_targets}] ⚠️  {code} {name[:12]:<12} | 无分钟数据，跳过")
            continue

        try:
            df = pd.read_parquet(file_path)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
        except Exception as e:
            print(f"  [{i:>4}/{total_targets}] ❌ {code} 读取失败: {e}")
            continue

        bar_count = len(df)

        if bar_count < MIN_BARS:
            print(f"  [{i:>4}/{total_targets}] ⏭  {code} {name[:10]:<10} | {bar_count:>6,} 根 < {MIN_BARS:,} 护城河，物理丢弃")
            continue

        target_results = []
        for atr_mult in ATR_MULTIPLIERS:
            res, dr = run_t0_grid_v2(df, atr_multiplier=atr_mult)
            if res:
                res['daily_returns'] = dr
                target_results.append(res)

        elapsed = time.time() - t_start

        if target_results:
            best_res = sorted(target_results, key=lambda x: x['Sharpe_Ratio'], reverse=True)[0]
            best_daily_returns = best_res.pop('daily_returns')
            tag = get_etf_tag(name)

            sharpe_icon = "📈" if best_res['Sharpe_Ratio'] > 0 else "📉"
            ret_str = f"{best_res['Total_Return']*100:+.2f}%"
            print(
                f"  [{i:>4}/{total_targets}] {sharpe_icon} {code} {name[:12]:<12} | "
                f"K线 {bar_count:>6,} | 耗时 {elapsed:.1f}s | "
                f"交易 {best_res['Total_Trades']:>4} 次 | "
                f"收益 {ret_str:>8} | "
                f"夏普 {best_res['Sharpe_Ratio']:>6.3f} | "
                f"最优ATR乘数 {best_res['ATR_Multiplier']:.1f}×"
            )

            all_results.append({
                "Code": code, "Name": name, "Tag": tag,
                "Best_ATR_Mult": best_res['ATR_Multiplier'],
                "Sharpe_Ratio": best_res['Sharpe_Ratio'],
                "Total_Trades": best_res['Total_Trades'],
                "Total_Return": best_res['Total_Return'],
                "Data_Bars": bar_count,
                "Elapsed_s": elapsed,
                "ATR_14_pct": _calc_atr14_pct(df),   # ← 修复：真实 ATR14% 入账本
            })
            if best_daily_returns is not None:
                daily_returns_store[code] = best_daily_returns
        else:
            print(f"  [{i:>4}/{total_targets}] ⚠️  {code} {name[:12]:<12} | {bar_count:>6,} 根 | 耗时 {elapsed:.1f}s | 计算失败/数据不足")

    print("=" * 90)

    if not all_results:
        print("❌ 全部运算失败或数据量不足。")
        return

    df_res = pd.DataFrame(all_results)
    df_res.to_csv(OUTPUT_CSV, index=False)
    print(f"\n💾 全量结果已保存: {OUTPUT_CSV}")

    # 3. 严格席位制：仅保留所有通过回测且夏普比率为正的前 TARGET_ETF_COUNT 名！
    top_targets = df_res[df_res['Sharpe_Ratio'] > 0].sort_values(by='Sharpe_Ratio', ascending=False)
    if not top_targets.empty:
        top_targets = top_targets.head(TARGET_ETF_COUNT)
    if top_targets.empty:
        print("🚨 全部标的回测夏普为负！生成空配置，实盘强制休眠。")
        with open(FINAL_YAML, 'w', encoding='utf-8') as f:
            yaml.dump({}, f)
        return

    # 4. 半凯利动态资金分配
    print("\n💚 【半凯利资金分配器】计算中...")
    kelly_info = kelly_allocate(top_targets, daily_returns_store, TOTAL_CAPITAL)

    final_config = {}
    print("\n🏆 【终极 T0 黄金阵列】(固定标的 + 纯均值回归 + 半凯利动态分配):")
    print("=" * 80)
    for _, row in top_targets.iterrows():
        code    = str(row['Code'])
        ki      = kelly_info.get(code, {'kelly_score': 0.0, 'weight': 0.0, 'allocated_capital': 0.0})

        name_str    = str(row['Name'])
        trade_amt   = int(ki['allocated_capital'] / MAX_LOTS_PER_ETF)
        
        # 1. 剔除凯利分配极度边缘化的垃圾权重
        if trade_amt < 1500:
            continue  # 虚拟资金放大后单格仍不足 1500 的，证明凯利得分极低，物理抛弃，不上实盘

        # 2. 摩擦成本安全线强制托底
        if trade_amt < 3000:
            trade_amt = 3000  # 强制提额至 3000，摊薄券商 0.5 元最低手续费

        # 3. 取整为 100 的整数倍 (A 股股数规则向资金兼容)
        trade_amt = int(round(trade_amt / 100.0)) * 100

        atr_mult    = float(row['Best_ATR_Mult'])
        max_lots_v  = int(MAX_LOTS_PER_ETF)
        kelly_score = float(ki['kelly_score'])
        weight_pct  = float(ki['weight']) * 100


        # ============================================================
        # 🛡️ T0 专属日内网格宽度钳制器 v2（波动率嗅觉 + 摩擦成本墙）
        # ============================================================
        # 物理摩擦成本底线：双边万一佣金 + 单边 1-2 Tick 滑点 ≈ 0.2%-0.3%
        # MIN 必须能击穿摩擦成本墙，0.4% 是纯负期望，强制升至 1.0%
        MIN_T0_SPREAD = 0.010  # 绝对底线：1.0% — 确保利润能穿透摩擦成本墙
        MAX_T0_SPREAD = 0.030  # 物理上限：3.0% — 适配港股/商品ETF极端高波动
        _FALLBACK_SPREAD = 0.012  # ATR 数据缺失时的安全静态回退值

        # 从 df_res 中取出盘前计算好的 ATR_14_pct
        atr14_raw = float(row.get('ATR_14_pct', 0.0))
        if atr14_raw <= 0 or atr14_raw > 0.15:
            # 数据异常或缺失 → 安全回退
            spread_pct_raw = _FALLBACK_SPREAD
        else:
            # 动态锚定：日均振幅的 30% 作为基准切片
            spread_pct_raw = atr14_raw * 0.3

        spread_pct_fin = max(MIN_T0_SPREAD, min(spread_pct_raw, MAX_T0_SPREAD))

        # 特殊资产压制（518680 黄金ETF 波动大但回归慢，使用最低档）
        if code == '518680.SH':
            spread_pct_fin = MIN_T0_SPREAD  # 1.0%，不再使用崩溃级 0.4%

        final_config[code] = {
            'name':           name_str,
            'tag':            str(row.get('Tag', 'Other')),
            'trade_amount':   trade_amt,
            'atr_multiplier': atr_mult,
            'spread_pct':     round(spread_pct_fin, 4),
            'max_lots':       max_lots_v,
        }

        print(f"🎯 {code}  {name_str:<12} | ATR乘数: {atr_mult:.1f}x | 网格宽度: {spread_pct_fin*100:.2f}% | 网格金额: ¥{trade_amt:,} | Kelly: {kelly_score:.4f} | 权重: {weight_pct:.1f}%")
    print("=" * 80)

    with open(FINAL_YAML, 'w', encoding='utf-8') as f:
        yaml.dump(final_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"\n📁 实盘配置已生成: {FINAL_YAML}")
    print(f"🏁 T0 参数优化完成 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    build_master_pipeline()