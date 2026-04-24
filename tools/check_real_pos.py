
import os
import time
import random
import json
from xtquant.xttrader import XtQuantTrader
from xtquant import xtconstant
from dotenv import load_dotenv

load_dotenv()

def check_positions():
    qmt_path = os.getenv("QMT_PATH")
    acc_id = os.getenv("ACCOUNT_ID")
    session_id = int(time.time() + random.randint(1000, 9999))
    
    xt_trader = XtQuantTrader(qmt_path, session_id)
    xt_trader.start()
    
    connect_result = xt_trader.connect()
    if connect_result != 0:
        print(f"❌ Connection failed: {connect_result}")
        return

    from xtquant.xttype import StockAccount
    acc = StockAccount(acc_id)
    
    # Give some time for data to sync
    print("⏳ Waiting for data synchronization...")
    time.sleep(3)
    
    print(f"📡 Querying positions for Account: {acc_id}...")
    positions = xt_trader.query_stock_positions(acc)
    
    result = []
    print(f"📊 Raw positions count: {len(positions)}")
    for i, p in enumerate(positions):
        print(f"   [{i}] Code: {p.stock_code} | Vol: {p.volume} | CanUse: {p.can_use_volume} | Open: {p.open_price} | Type: {type(p)}")
        if p.volume > 0:
            result.append({
                "code": p.stock_code,
                "volume": p.volume,
                "can_use": p.can_use_volume,
                "price": p.open_price
            })

    xt_trader.stop()
    
    # Also write to a small json for reference
    with open(".state/real_holdings.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    check_positions()
