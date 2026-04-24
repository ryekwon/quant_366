import os
import sys
import time
from xtquant import xtdata
from xtquant.xttrader import XtQuantTrader, StockAccount
from dotenv import load_dotenv

load_dotenv()
QMT_PATH = os.getenv("QMT_PATH")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

def check_account():
    session_id = int(time.time())
    xt_trader = XtQuantTrader(QMT_PATH, session_id)
    xt_trader.start()
    res = xt_trader.connect()
    if res != 0:
        print(f"Connect failed: {res}")
        return
    
    acc = StockAccount(ACCOUNT_ID)
    positions = xt_trader.query_stock_positions(acc)
    print(f"\n--- Account Positions ({ACCOUNT_ID}) ---")
    if not positions:
        print("No positions found.")
    else:
        for p in positions:
            if p.volume > 0:
                print(f"Code: {p.stock_code}, Volume: {p.volume}, CanSell: {p.can_use_volume}, OpenPrice: {p.open_price}")
    
    xt_trader.stop()

if __name__ == "__main__":
    check_account()
