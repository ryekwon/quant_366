# -*- coding: utf-8 -*-
"""
QMT 物理清算引擎 (Standalone)
运行位置: Quant-PC (Windows)
功能: 抓取流水，计算今日真实 PnL，生成结帐单
"""
import os
import time
import json
import pandas as pd
from datetime import datetime
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from dotenv import load_dotenv

def settle_daily_trades():
    """
    【硬核清算引擎】
    1. 连接 QMT 交易网关
    2. 拉取今日全量成交单 (query_trades)
    3. 分标的进行买卖对冲计算 (Net PnL)
    4. 落盘至 .state/settlement_YYYYMMDD.json
    """
    print(f"\n🪙  启动物理清算引擎 ({datetime.now().strftime('%H:%M')})...")
    
    load_dotenv()
    qmt_path = os.getenv("QMT_PATH")
    acc_id = os.getenv("ACCOUNT_ID")
    
    if not qmt_path or not acc_id:
        print("❌ 未在 .env 中发现 QMT_PATH 或 ACCOUNT_ID，清算中止。")
        return

    # 连接 QMT
    session_id = int(time.time())
    xt_trader = XtQuantTrader(qmt_path, session_id)
    xt_trader.start()
    
    if xt_trader.connect() != 0:
        print("❌ 交易网关连接失败，无法清算今日账本。")
        return
        
    acc = StockAccount(acc_id)
    trades = xt_trader.query_trades(acc)
    
    if not trades:
        print("📅 今日无成交记录，生成空账本。")
        results = {"total_profit": 0, "details": {}}
    else:
        # 转为 DataFrame 处理
        data = []
        today_str = datetime.now().strftime('%Y%m%d')
        for t in trades:
            # 仅处理今日交易
            trade_time = datetime.fromtimestamp(t.order_time).strftime('%Y%m%d')
            if trade_time == today_str:
                data.append({
                    'code': t.stock_code,
                    'action': t.order_type, # 23:买, 24:卖
                    'price': t.traded_price,
                    'volume': t.traded_volume,
                    'amount': t.traded_amount
                })
        
        if not data:
            print("📅 今日无成交记录 (过滤日期后)，生成空账本。")
            results = {"total_profit": 0, "details": {}}
        else:
            df = pd.DataFrame(data)
            # 计算每笔估算手续费 (万一计，最低 5 元)
            df['comm'] = df['amount'] * 0.0001
            df.loc[df['comm'] < 5, 'comm'] = 5
            
            summary = {}
            total_profit = 0.0
            
            for code, group in df.groupby('code'):
                # 买入额 (Action 23)
                buy_amt = group[group['action'] == 23]['amount'].sum()
                buy_vol = group[group['action'] == 23]['volume'].sum()
                # 卖出额 (Action 24)
                sell_amt = group[group['action'] == 24]['amount'].sum()
                sell_vol = group[group['action'] == 24]['volume'].sum()
                # 总手续费
                comm_sum = group['comm'].sum()
                
                # 净利润 = 卖出额 - 买入额 - 手续费
                net_pnl = sell_amt - buy_amt - comm_sum
                total_profit += net_pnl
                
                summary[code] = {
                    "net_pnl": round(net_pnl, 2),
                    "buy_vol": int(buy_vol),
                    "sell_vol": int(sell_vol),
                    "comm": round(comm_sum, 2)
                }
            
            results = {
                "total_profit": round(total_profit, 2),
                "details": summary,
                "settle_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

    # 落盘
    script_dir = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.join(script_dir, ".state")
    os.makedirs(state_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    save_file = os.path.join(state_dir, f"settlement_{today_str}.json")
    
    with open(save_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=4, ensure_ascii=False)
    
    print(f"📊 今日清算完毕！总净利: ￥{results['total_profit']} | 细节已写入 {save_file}")
    xt_trader.stop()

if __name__ == "__main__":
    settle_daily_trades()
