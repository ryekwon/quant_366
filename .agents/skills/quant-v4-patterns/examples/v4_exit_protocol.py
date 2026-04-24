import math
import time
from xtquant import xtconstant

def execute_v4_absolute_exit(xt_trader, acc, code, price, strategy_name, reason):
    """
    V4.0 物理全清标准范例
    """
    # 1. 强制查询实盘，获取绝对真实数量 (Truth on Chain)
    pos_list = xt_trader.query_stock_positions(acc) or []
    target_pos = next((p for p in pos_list if p.stock_code == code), None)
    
    if not target_pos or target_pos.volume <= 0:
        return False

    # 2. 物理歼灭协议 (Physical Sweep)
    # 绝对不使用内部记录的数量，直接清空物理持仓
    total_qty = int(target_pos.volume)
    
    # 3. 标识对齐 (Tagging)
    # 使用规范化的 Remark 和 StrategyName
    xt_trader.order_stock_async(
        acc, code, xtconstant.STOCK_SELL, 
        total_qty, xtconstant.FIX_PRICE, price, 
        strategy_name, 'Absolute' # 备注统一为用途
    )
    
    print(f"🔥 [V4-Exit] {code} 执行物理全清: {total_qty} 股 | 原因: {reason}")
    return True
