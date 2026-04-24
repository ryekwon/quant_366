# -*- coding: utf-8 -*-
"""
统计套利协整性检验与参数生成器 (大脑 / 选品器)
功能: 读取 YAML -> 提取历史数据 -> OLS 回归 -> ADF 检验 -> OU 过程计算半衰期 -> 导出 CSV
"""
import os
import time
import yaml
import itertools
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller
from datetime import datetime, timedelta
from xtquant import xtdata

# ================= 核心配置 =================
_DIR = os.path.dirname(os.path.abspath(__file__))
# 标的池文件位于 Data 根目录
YAML_PATH = r"Z:\QuantpC_Workspace\Data\refined_etf_list.yaml"
STATE_DIR = os.path.join(_DIR, ".state")
OUTPUT_CSV = os.path.join(STATE_DIR, "tradable_pairs_halflife.csv")

LOOKBACK_DAYS = 500  # 约 2 年交易日，用于协整检验。过长会导致 Regime Shift 失真
MAX_P_VALUE = 0.05   # ADF检验显著性阈值 (95%置信度)
MIN_HALF_LIFE = 5.0  # 提升下限，强制过滤 5 天内极速回归的伪信号（极大概率为系统性 β 同涨同跌，而非真正套利）
MAX_HALF_LIFE = 30.0 # 最大半衰期(天)，过滤死水标的，提高资金周转率

# 新增：绝对动量防线
TREND_MA_WINDOW = 60 # 剔除跌破 60 日均线的下降通道标的
# ============================================

def load_and_cluster_pairs(yaml_path):
    """解析聚类 YAML，严格限制仅在同板块内生成 ETF 配对组合"""
    with open(yaml_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    all_codes = []
    name_map = {}
    valid_pairs = []
    
    # 遍历各类目，构建同源护城河
    for category, etf_list in data.get('categories', {}).items():
        category_codes = [item['code'] for item in etf_list]
        all_codes.extend(category_codes)
        for item in etf_list:
            name_map[item['code']] = item['name']
        
        # 仅在同类目内进行两两组合 (N选2)
        if len(category_codes) >= 2:
            valid_pairs.extend(list(itertools.combinations(category_codes, 2)))
            
    # 去重后返回全局代码池（用于下载数据）、代码名称映射和合法的同源配对池
    return list(set(all_codes)), name_map, valid_pairs

def get_historical_closes(codes, days=LOOKBACK_DAYS):
    """从本地 QMT 数据源获取齐整的收盘价矩阵"""
    end_time = datetime.now().strftime('%Y%m%d')
    start_time = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

    print(f"📥 正在加载 {len(codes)} 只 ETF 的历史日线数据 ({start_time} - {end_time})...")
    
    # 触发本地数据补充（针对 Z690 上的 miniQMT）
    xtdata.download_history_data2(codes, '1d', start_time, end_time)
    data = xtdata.get_market_data_ex(['close'], codes, '1d')

    df_list = []
    for code in codes:
        if code in data and not data[code].empty:
            s = data[code]['close']
            s.name = code
            df_list.append(s)

    if not df_list:
        raise RuntimeError("❌ 所有标的均无有效历史数据，请检查 QMT 连接与数据下载。")

    # 【修改点】仅合并，不进行全局 dropna()
    df_closes = pd.concat(df_list, axis=1)
    print(f"✅ 数据加载完成，矩阵维度: {df_closes.shape} (包含 NaN)")
    return df_closes

def filter_downtrend_assets(df_closes, window=TREND_MA_WINDOW):
    """
    绝对动量过滤器：清洗单边做多的致命风险
    逻辑：剔除当前最新收盘价跌破 MA60 的标的，拒绝在雪崩中寻找协整。
    """
    print(f"\n📉 启动绝对动量拦截网 (MA{window})...")
    uptrend_codes = set()
    
    for code in df_closes.columns:
        s_clean = df_closes[code].dropna()
        if len(s_clean) < window:
            continue # 次新股数据不足，出于风控直接舍弃
            
        latest_price = s_clean.iloc[-1]
        ma_value = s_clean.tail(window).mean()
        
        if latest_price >= ma_value:
            uptrend_codes.add(code)
        else:
            print(f"  🚫 [趋势否决] {code} 跌破均线 (最新: {latest_price:.3f} < MA{window}: {ma_value:.3f})")
            
    print(f"✅ 趋势清洗完毕：{len(df_closes.columns)} 只标的中，仅保留 {len(uptrend_codes)} 只多头排列标的。")
    return uptrend_codes

def calculate_halflife(spread):
    """利用 Ornstein-Uhlenbeck 过程逼近计算均值回归半衰期"""
    spread_lag = spread.shift(1).dropna()
    spread_diff = spread.diff().dropna()
    spread_lag = spread_lag.loc[spread_diff.index]

    X = sm.add_constant(spread_lag.values)
    Y = spread_diff.values
    res = sm.OLS(Y, X).fit()
    
    theta = res.params[1]
    if theta >= 0:  # theta=0 时半衰期无穷大（随机游走），也应排除
        return np.inf # 序列发散，不具备均值回归特性
    if abs(theta) < 1e-10:  # 防止除以接近零的值导致 inf 溢出
        return np.inf
        
    half_life = -np.log(2) / theta
    return half_life

def find_cointegrated_pairs(df_closes, valid_pairs, name_map):
    """接收同源配对池，而非全量无差别组合"""
    total_pairs = len(valid_pairs)
    print(f"🔬 启动同源聚类 ADF 检验，过滤跨界伪相关，共计 {total_pairs} 个组合...")

    results = []
    count = 0

    for code_A, code_B in valid_pairs:
        count += 1
        if count % 200 == 0:
            print(f"  ...运算进度: {count}/{total_pairs}")

        # 如果数据列缺失（可能是某标的历史数据全为空）
        if code_A not in df_closes.columns or code_B not in df_closes.columns:
            continue

        # 提取出这对其，进行成对清洗
        pair_data = df_closes[[code_A, code_B]].dropna()
        
        # 样本量底线防线：如果这对组合的共同历史数据少于 250 天（约一年），直接放弃
        if len(pair_data) < 250:
            continue
            
        Y = pair_data[code_A]
        X = pair_data[code_B]

        # 1. OLS 回归计算 Hedge Ratio (β)
        # 【Bug修复】add_constant 在 X 为 Series 时，返回 DataFrame 的列名为 ["const", code_B]。
        # 但当 X 的 dtype 与 code_B 命名不一致时，params 下标会 KeyError。
        # 改为按位置取 params[1] 以兼容所有情形，并额外保护 hedge_ratio 为正数约束。
        X_with_const = sm.add_constant(X.values)  # .values 避免 Series 命名污染
        ols_result = sm.OLS(Y.values, X_with_const).fit()
        hedge_ratio = ols_result.params[1]  # params[0]=截距, params[1]=β
        if hedge_ratio <= 0:
            continue  # 对冲比率须为正数，否则两标的方向相同无套利意义

        # 2. 计算残差 Spread
        spread = Y - hedge_ratio * X

        # 3. ADF 检验 (检查残差序列是否平稳)
        try:
            adf_result = adfuller(spread, maxlag=1, autolag=None)
            p_value = adf_result[1]
        except Exception:
            continue

        # 4. 阈值拦截与半衰期过滤
        if p_value <= MAX_P_VALUE:
            half_life = calculate_halflife(spread)

            if MIN_HALF_LIFE <= half_life <= MAX_HALF_LIFE:
                results.append({
                    'Code_A': code_A,
                    'Name_A': name_map.get(code_A, '未知'),
                    'Code_B': code_B,
                    'Name_B': name_map.get(code_B, '未知'),
                    'Hedge_Ratio': round(hedge_ratio, 4),
                    'ADF_P_Value': p_value,  # 【Bug修复】保存浮点数而非字符串，确保后续 sort_values 数字排序正确
                    'Half_Life_Days': round(half_life, 2)
                })

    df_results = pd.DataFrame(results)
    return df_results

def main():
    start_time = time.time()
    print("====== 大脑：统计套利研究引擎启动 ======")
    os.makedirs(STATE_DIR, exist_ok=True)

    if not os.path.exists(YAML_PATH):
        print(f"❌ 严重错误：未找到标的池文件 {YAML_PATH}")
        return

    codes, name_map, valid_pairs = load_and_cluster_pairs(YAML_PATH)
    df_closes = get_historical_closes(codes)

    # 3. 执行绝对动量过滤
    uptrend_codes = filter_downtrend_assets(df_closes)
    
    # 4. 净化配对池：强行拆散任何包含空头标的的组合
    purified_pairs = [
        (A, B) for A, B in valid_pairs 
        if A in uptrend_codes and B in uptrend_codes
    ]
    
    print(f"\n🧱 护城护构建完毕：初始同源配对 {len(valid_pairs)} 个 -> 趋势过滤后剩余 {len(purified_pairs)} 个可交易多头组合。")

    if not purified_pairs:
        print("⚠️ 结论：系统性熊市触发！当前无任何同源且处于多头的组合，停止输出参数。")
        return

    # 5. 送入核心数理引擎检验
    df_pairs = find_cointegrated_pairs(df_closes, purified_pairs, name_map)

    if df_pairs.empty:
        print("⚠️ 结论：当前周期内，未发现任何符合显著性阈值与半衰期要求的协整配对。")
    else:
        # 按照 P-value 排序，P值越小，协整关系越强（优先级越高）
        df_pairs = df_pairs.sort_values(by='ADF_P_Value').reset_index(drop=True)
        df_pairs.to_csv(OUTPUT_CSV, index=False, encoding='utf-8')
        print(f"\n🎯 寻参完毕！共挖掘出 {len(df_pairs)} 对符合标准的标的组合。")
        print(f"📁 核心参数矩阵已物理持久化至: {OUTPUT_CSV}")
        print("\n--- 最佳配对 Top 5 ---")
        print(df_pairs.head(5).to_string(index=False))

    print(f"\n====== 引擎静默 (耗时: {time.time() - start_time:.2f}秒) ======")

if __name__ == "__main__":
    main()