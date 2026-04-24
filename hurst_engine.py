# ==============================================================================
# 🎯 [部署节点] :  Quant-PC
# 📦 [环境依赖] : numpy, pandas
# 🔗 [职责]     : 纯物理状态提取器 —— 计算分数布朗运动的粗糙度 (Hurst)
# ==============================================================================
import numpy as np
import pandas as pd

def calculate_hurst_variance_scaling(price_series: pd.Series, max_lag: int = 20) -> float:
    """
    [方差标度律估算器]
    物理意义：
      - H = 0.5 (标准布朗运动 / 随机游走)
      - H < 0.5 (均值反转 / 粗糙 / 适合网格收割)
      - H > 0.5 (趋势动量 / 平滑 / 容易让网格爆仓)
    """
    # 清洗数据
    series = price_series.dropna().values
    
    # 样本极度不足时，退化为随机游走假定
    if len(series) < max_lag * 2:
        return 0.5 
        
    lags = range(2, max_lag + 1)
    tau = []
    
    # 计算不同时间滞后阶数下的对数方差
    for lag in lags:
        diff = np.subtract(series[lag:], series[:-lag])
        # 🛡️ [防线：NaN 幽灵装甲] A 股一字跌停/停牌时 diff 全 0 → var=0 → log(0)=-inf → NaN
        # NaN > MAX_HURST 在 Python 中为 False，会让死水标的穿透粗糙度防御网！
        variance = np.var(diff)
        if variance == 0:
            variance = 1e-8   # 赋予最小正扰动，在对数坐标上映射为一个极小但有限的点
        tau.append(np.sqrt(variance))
        
    # 对数线性回归: log(tau) = H * log(lag) + C
    # 斜率即为 Hurst 指数
    poly = np.polyfit(np.log(lags), np.log(tau), 1)
    hurst_exponent = poly[0]
    
    # 物理极值截断 (防止异常极端数据导致 H 越界)
    return float(np.clip(hurst_exponent, 0.01, 0.99))

if __name__ == "__main__":
    # 本地靶场测试
    np.random.seed(42)
    # 构造一个强均值反转序列 (OU 过程)
    ou_series = [0]
    for _ in range(500):
        ou_series.append(ou_series[-1] * 0.8 + np.random.normal(0, 1))
    
    h_value = calculate_hurst_variance_scaling(pd.Series(ou_series))
    print(f"🔬 模拟均值反转序列的 Hurst 指数: {h_value:.4f} (应显著小于 0.5)")