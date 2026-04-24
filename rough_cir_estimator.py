# ==============================================================================
# 🎯 [部署节点] : vm200 (AI-Core, Linux) / Quant-PC
# 📦 [环境依赖] : numpy, pandas, statsmodels
# 🔗 [职责]     : 解算 OU/CIR 偏微分方程，输出资金周转率的核心参数 (半衰期 & 波动率)
# ==============================================================================
import numpy as np
import pandas as pd
import statsmodels.api as sm

def calibrate_rough_cir(spread_series: pd.Series, hurst: float = None) -> dict:
    """
    [OU 方程 OLS 求解器]
    传入：资产偏离度序列 (Spread)
    输出：包含波动率(sigma) 和 半衰期(halflife) 的物理参数字典
    """
    y = spread_series.dropna().values
    
    if len(y) < 30:
        raise ValueError("时间序列过短，无法解算微分方程。")

    # 构建 OLS 回归方程: dY_t = a + b * Y_{t-1} + error
    # 对应连续时间 OU 过程: dX_t = theta * (mu - X_t)dt + sigma * dW_t
    Y_t = y[1:]
    Y_t_1 = y[:-1]

    # 添加常数项
    X = sm.add_constant(Y_t_1)
    
    try:
        model = sm.OLS(Y_t - Y_t_1, X).fit()
    except Exception as e:
        raise RuntimeError(f"OLS 回归矩阵崩塌: {e}")

    a = model.params[0] # 常数项
    b = model.params[1] # 回归系数

    # 物理防线：如果 b >= 0，说明序列在发散（单边主升/主跌），根本不具备均值回归特性
    if b >= 0:
        theta = 0.0001 # 强行赋予极小回归速度
        half_life = 999.0 # 无限长的半衰期 (死刑)
        mu = 0.0
    else:
        # 回归速度 theta
        theta = -b
        # 理论半衰期 (ln(2) / theta)
        half_life = np.log(2) / theta
        # 理论均值
        mu = a / (-b)

    # 提取残差的标准差作为动态波动率 (每日步长依据)
    sigma = np.std(model.resid)

    # ⚠️ 【The Rough Penalty / 粗糙度惩罚】
    # 如果外部传入了 Hurst 指数，我们对半衰期进行物理修正
    # H 越小(反转越暴躁)，实际收敛速度比理论值更快
    rough_halflife = half_life
    if hurst is not None and hurst < 0.5:
        # 经验惩罚公式：将半衰期进行非线性压缩
        penalty_factor = (hurst / 0.5) ** 0.5 
        rough_halflife = half_life * penalty_factor

    return {
        "theta": round(theta, 6),
        "mu": round(mu, 6),
        "sigma": round(sigma, 6),
        "theoretical_halflife_days": round(half_life, 2),
        "rough_halflife_days": round(rough_halflife, 2)
    }

if __name__ == "__main__":
    # 本地靶场测试
    np.random.seed(42)
    # 构造一个强均值反转序列
    ou_series = [0]
    for _ in range(500):
        ou_series.append(ou_series[-1] * 0.8 + np.random.normal(0, 0.02))
    
    params = calibrate_rough_cir(pd.Series(ou_series), hurst=0.3)
    print(f"📉 OU 解析结果: 波动率 {params['sigma']}, 粗糙半衰期 {params['rough_halflife_days']} 天")