import os
import time
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount
from xtquant import xtconstant, xtdata
from dotenv import load_dotenv

load_dotenv()

def check_pos():
    qmt_path = os.getenv('QMT_PATH')
    acc_id = os.getenv('ACCOUNT_ID')
    
    trader = XtQuantTrader(qmt_path, int(time.time()))
    trader.start()
    res = trader.connect()
    if res != 0:
        print("Connect Failed")
        return

    time.sleep(1)
    acc = StockAccount(acc_id)
    
    pos = trader.query_stock_positions(acc)
    data = [{'code': p.stock_code, 'volume': p.volume, 'can_sell': p.can_use_volume, 'avg_price': p.open_price} for p in pos if p.volume > 0]
    import json
    print(json.dumps(data, indent=4))
    trader.stop()

if __name__ == "__main__":
    check_pos()
